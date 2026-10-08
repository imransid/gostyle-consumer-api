"""
POST and GET /api/v1/booking/<id>/check-in (SELF_CHECK_IN_V1): the customer
says "I am here", and reads the desk's answer.

booking-api decides everything; this route checks the flag, forwards the
caller's own token and passes booking-api's answer back as it came. No
database, no network: booking-api's two calls are the seams.
"""

import json
import uuid
from unittest import mock

from django.test import SimpleTestCase, override_settings
from django.urls import resolve
from rest_framework.test import APIRequestFactory, force_authenticate

from apps.salons import booking_api
from apps.salons.booking_api import BookingApiUnavailable
from apps.salons.check_in_views import SelfCheckInView
from apps.salons.test_group_booking import Customer

BOOKING_ID = "9f1c0f4e-3a2b-4d55-9a71-2c8e5b0d7a11"
PATH = f"/api/v1/booking/{BOOKING_ID}/check-in"
TOKEN = "Bearer customer-token"

# booking-api's answers, as it gives them.
WAITING = {
    "request": {
        "requestId": "01a11b4c-aa95-75f5-8e65-b815eccc7204",
        "bookingId": BOOKING_ID,
        "state": "WAITING",
        "raisedAt": "2026-10-11T03:50:00.000Z",
        "decidedAt": None,
    }
}
TOO_EARLY = {
    "statusCode": 409,
    "code": "BOOKING_CHECKIN_WINDOW",
    "message": "Check-in opens at 2026-10-11T03:30:00.000Z.",
    "details": {"windowOpensAt": "2026-10-11T03:30:00.000Z"},
    "error": "Conflict",
}
REJECTED_BEFORE = {
    "statusCode": 409,
    "code": "BOOKING_CHECKIN_REJECTED",
    "message": "The desk could not confirm your arrival. Please speak to the desk.",
    "error": "Conflict",
}
NOT_FOUND = {"statusCode": 404, "code": "BOOKING_NOT_FOUND", "message": "No such booking",
             "error": "Not Found"}


class Seams:
    def call(self, method, *, raised=(201, WAITING), read=(200, {"request": None}),
             body=None, user=True, **headers):
        factory = APIRequestFactory()
        extra = {"HTTP_AUTHORIZATION": TOKEN, **headers}
        if body is None:
            request = getattr(factory, method)(PATH, **extra)
        else:
            request = getattr(factory, method)(
                PATH, json.dumps(body), content_type="application/json", **extra)
        if user:
            force_authenticate(request, user=Customer())
        fakes = {
            "raise_check_in": mock.Mock(**_answer(raised)),
            "read_check_in": mock.Mock(**_answer(read)),
        }
        with mock.patch("apps.salons.check_in_views.raise_check_in", fakes["raise_check_in"]), \
                mock.patch("apps.salons.check_in_views.read_check_in", fakes["read_check_in"]):
            response = SelfCheckInView.as_view()(request, booking_id=uuid.UUID(BOOKING_ID))
        return response, fakes


def _answer(value):
    if isinstance(value, Exception):
        return {"side_effect": value}
    return {"return_value": value}


class FlagOffTests(Seams, SimpleTestCase):
    """Off by default: the 404 this path was before, and booking-api is not called."""

    def test_off_by_default(self):
        for method in ("post", "get"):
            response, fakes = self.call(method)
            self.assertEqual(response.status_code, 404, method)
            fakes["raise_check_in"].assert_not_called()
            fakes["read_check_in"].assert_not_called()

    @override_settings(SELF_CHECK_IN_V1=False)
    def test_off_when_set_off(self):
        response, fakes = self.call("post")
        self.assertEqual(response.status_code, 404)
        fakes["raise_check_in"].assert_not_called()


@override_settings(SELF_CHECK_IN_V1=True)
class RaiseTests(Seams, SimpleTestCase):

    def test_raised_201_as_booking_api_answered(self):
        response, fakes = self.call("post")
        self.assertEqual(response.status_code, 201)
        self.assertEqual(response.data, WAITING)
        fakes["raise_check_in"].assert_called_once_with(
            uuid.UUID(BOOKING_ID), authorization=TOKEN)

    def test_a_second_tap_is_200_with_the_same_request(self):
        response, _ = self.call("post", raised=(200, WAITING))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data, WAITING)

    def test_neither_the_apps_body_nor_its_tenant_header_is_forwarded(self):
        # booking-api takes the tenant from the booking itself.
        _, fakes = self.call("post", body={"tenant": "x", "note": "hi"},
                             HTTP_X_TENANT_ID="tenant-from-app")
        fakes["raise_check_in"].assert_called_once_with(
            uuid.UUID(BOOKING_ID), authorization=TOKEN)

    def test_refusals_come_back_as_they_came(self):
        for answer in ((409, TOO_EARLY), (409, REJECTED_BEFORE), (404, NOT_FOUND)):
            response, _ = self.call("post", raised=answer)
            self.assertEqual(response.status_code, answer[0])
            self.assertEqual(response.data, answer[1])

    def test_booking_api_down_is_503_in_our_envelope(self):
        response, _ = self.call("post", raised=BookingApiUnavailable("refused"))
        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.data["errors"][0]["code"], "booking_api_unavailable")

    def test_signed_out_is_refused_before_booking_api(self):
        response, fakes = self.call("post", user=False)
        self.assertEqual(response.status_code, 401)
        fakes["raise_check_in"].assert_not_called()


@override_settings(SELF_CHECK_IN_V1=True)
class ReadTests(Seams, SimpleTestCase):

    def test_none_raised_yet(self):
        response, fakes = self.call("get")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data, {"request": None})
        fakes["read_check_in"].assert_called_once_with(
            uuid.UUID(BOOKING_ID), authorization=TOKEN)
        fakes["raise_check_in"].assert_not_called()

    def test_the_latest_request_as_booking_api_answered(self):
        approved = {"request": {**WAITING["request"], "state": "APPROVED",
                                "decidedAt": "2026-10-11T03:55:00.000Z"}}
        response, _ = self.call("get", read=(200, approved))
        self.assertEqual(response.data, approved)

    def test_someone_elses_booking_is_booking_apis_404(self):
        response, _ = self.call("get", read=(404, NOT_FOUND))
        self.assertEqual(response.status_code, 404)

    def test_booking_api_down_is_503(self):
        response, _ = self.call("get", read=BookingApiUnavailable("refused"))
        self.assertEqual(response.status_code, 503)


class RouteTests(SimpleTestCase):

    def test_the_route_is_wired(self):
        match = resolve(PATH)
        self.assertIs(match.func.view_class, SelfCheckInView)
        self.assertEqual(match.kwargs, {"booking_id": uuid.UUID(BOOKING_ID)})


class ClientTests(SimpleTestCase):
    """booking_api.py's two calls: the path, the token, and nothing else."""

    def test_raise_posts_with_the_token_only(self):
        with mock.patch("apps.salons.booking_api._send", return_value=(201, WAITING)) as send:
            self.assertEqual(
                booking_api.raise_check_in(uuid.UUID(BOOKING_ID), authorization=TOKEN),
                (201, WAITING),
            )
        send.assert_called_once_with(
            "POST", f"/v1/bookings/{BOOKING_ID}/check-in-request",
            headers={"Authorization": TOKEN},
        )

    def test_read_gets_with_the_token_only(self):
        with mock.patch("apps.salons.booking_api._send", return_value=(200, {"request": None})) as send:
            booking_api.read_check_in(uuid.UUID(BOOKING_ID), authorization=TOKEN)
        send.assert_called_once_with(
            "GET", f"/v1/bookings/{BOOKING_ID}/check-in-request",
            headers={"Authorization": TOKEN},
        )
