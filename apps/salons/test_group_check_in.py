"""
POST and GET /api/v1/booking/group/<group_id>/check-in (GROUP_CHECK_IN_V1):
the booker says "we are here" for an app party, and reads each lane's answer.

booking-api decides everything (the booker only, every lane's rules); this
route checks the two switches, refuses a chair (a party has none) and a
malformed booking_ids before calling anyone, forwards the caller's own token
(and booking_ids, when sent) and passes booking-api's answer back as it came.
No network: booking-api's two calls are the seams, and ClientTests fakes
only `_send`.
"""

import json
import uuid
from unittest import mock

from django.test import SimpleTestCase, override_settings
from django.urls import resolve
from rest_framework.test import APIRequestFactory, force_authenticate

from apps.salons import booking_api
from apps.salons.booking_api import BookingApiUnavailable
from apps.salons.group_check_in_views import GroupCheckInView
from apps.salons.test_group_booking import Customer

GROUP_ID = "5b1c2d3e-4f50-4a61-8b72-9c83d4e5f601"
L1 = "dddddddd-0001-4ddd-8ddd-dddddddddddd"
L3 = "dddddddd-0003-4ddd-8ddd-dddddddddddd"
PATH = f"/api/v1/booking/group/{GROUP_ID}/check-in"
TOKEN = "Bearer customer-token"


def _lane(booking_id, code, kind, outcome, state):
    return {
        "bookingId": booking_id,
        "bookingCode": code,
        "memberKind": kind,
        "outcome": outcome,
        "request": None if state is None else {"bookingId": booking_id, "state": state},
        "error": None,
    }


# booking-api's answers, as it gives them.
RAISED = {"groupId": GROUP_ID, "lanes": [
    _lane(L1, "GS-1", "self", "RAISED", "WAITING"),
    _lane(L3, "GS-3", "guest", "RAISED", "WAITING"),
]}
WAITING = {"groupId": GROUP_ID, "lanes": [
    _lane(L1, "GS-1", "self", "WAITING", "WAITING"),
    _lane(L3, "GS-3", "guest", "WAITING", "WAITING"),
]}
NOTHING_TO_DO = {
    "statusCode": 409,
    "code": "BOOKING_GROUP_CHECKIN_NONE",
    "message": "Nothing could be checked in for this party.",
    "details": {"groupId": GROUP_ID, "lanes": [_lane(L1, "GS-1", "self", "CHECKED_IN", None)]},
    "error": "Conflict",
}
NOT_FOUND = {"statusCode": 404, "code": "not_found", "message": "No such booking"}
READ = {"groupId": GROUP_ID, "lanes": [
    {**_lane(L1, "GS-1", "self", "RAISED", "APPROVED")},
]}


class Seams:
    def call(self, method, *, raised=(201, RAISED), read=(200, READ), body=None):
        factory = APIRequestFactory()
        if body is None:
            request = getattr(factory, method)(PATH, HTTP_AUTHORIZATION=TOKEN)
        else:
            request = getattr(factory, method)(
                PATH, json.dumps(body), content_type="application/json",
                HTTP_AUTHORIZATION=TOKEN)
        force_authenticate(request, user=Customer())
        fakes = {
            "raise": mock.Mock(**_answer(raised)),
            "read": mock.Mock(**_answer(read)),
        }
        with mock.patch("apps.salons.group_check_in_views.raise_group_check_in", fakes["raise"]), \
                mock.patch("apps.salons.group_check_in_views.read_group_check_in", fakes["read"]):
            response = GroupCheckInView.as_view()(request, group_id=uuid.UUID(GROUP_ID))
        return response, fakes


def _answer(value):
    if isinstance(value, Exception):
        return {"side_effect": value}
    return {"return_value": value}


class SwitchTests(Seams, SimpleTestCase):
    """Off unless BOTH switches are on: the 404 this path was before, no call."""

    def test_off_by_default(self):
        for method in ("post", "get"):
            response, fakes = self.call(method)
            self.assertEqual(response.status_code, 404, method)
            fakes["raise"].assert_not_called()
            fakes["read"].assert_not_called()

    @override_settings(SELF_CHECK_IN_V1=True, GROUP_CHECK_IN_V1=False)
    def test_off_when_ours_is_off(self):
        response, fakes = self.call("post")
        self.assertEqual(response.status_code, 404)
        fakes["raise"].assert_not_called()

    @override_settings(SELF_CHECK_IN_V1=False, GROUP_CHECK_IN_V1=True)
    def test_off_when_self_check_in_is_off(self):
        response, fakes = self.call("post")
        self.assertEqual(response.status_code, 404)
        fakes["raise"].assert_not_called()


@override_settings(SELF_CHECK_IN_V1=True, GROUP_CHECK_IN_V1=True)
class RaiseTests(Seams, SimpleTestCase):
    def test_no_body_every_lane_and_the_answer_as_it_came(self):
        response, fakes = self.call("post")
        self.assertEqual(response.status_code, 201)
        self.assertEqual(response.data, RAISED)
        fakes["raise"].assert_called_once_with(
            uuid.UUID(GROUP_ID), authorization=TOKEN, booking_ids=None)

    def test_sent_again_200_as_it_came(self):
        response, _ = self.call("post", raised=(200, WAITING))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data, WAITING)

    def test_booking_api_refusals_pass_through(self):
        for answer in ((409, NOTHING_TO_DO), (404, NOT_FOUND)):
            response, _ = self.call("post", raised=answer)
            self.assertEqual(response.status_code, answer[0])
            self.assertEqual(response.data, answer[1])

    def test_booking_ids_are_forwarded(self):
        response, fakes = self.call("post", body={"booking_ids": [L3]})
        self.assertEqual(response.status_code, 201)
        fakes["raise"].assert_called_once_with(
            uuid.UUID(GROUP_ID), authorization=TOKEN, booking_ids=[L3])

    def test_bad_booking_ids_422_and_nothing_called(self):
        for bad in ("abc", [1, 2], {"a": L1}):
            response, fakes = self.call("post", body={"booking_ids": bad})
            self.assertEqual(response.status_code, 422, repr(bad))
            fakes["raise"].assert_not_called()

    def test_a_chair_token_is_refused_never_dropped(self):
        for token in ("q7Xk2mP9rT4vW8yZ1aB3cD", ""):
            response, fakes = self.call("post", body={"chair_token": token})
            self.assertEqual(response.status_code, 422, repr(token))
            fakes["raise"].assert_not_called()

    def test_a_null_chair_token_is_no_chair(self):
        response, fakes = self.call("post", body={"chair_token": None})
        self.assertEqual(response.status_code, 201)
        fakes["raise"].assert_called_once()

    def test_booking_api_down_503(self):
        response, _ = self.call("post", raised=BookingApiUnavailable("down"))
        self.assertEqual(response.status_code, 503)


@override_settings(SELF_CHECK_IN_V1=True, GROUP_CHECK_IN_V1=True)
class ReadTests(Seams, SimpleTestCase):
    def test_read_as_it_came_token_only(self):
        response, fakes = self.call("get")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data, READ)
        fakes["read"].assert_called_once_with(uuid.UUID(GROUP_ID), authorization=TOKEN)
        fakes["raise"].assert_not_called()

    def test_read_down_503(self):
        response, _ = self.call("get", read=BookingApiUnavailable("down"))
        self.assertEqual(response.status_code, 503)


class ClientTests(SimpleTestCase):
    """booking_api.py's two calls: the path, the token, and nothing else."""

    PATH = f"/v1/mobile-booking/group/{GROUP_ID}/check-in"

    def test_raise_with_no_list_posts_the_token_only(self):
        for ids in (None, []):
            with mock.patch("apps.salons.booking_api._send", return_value=(201, RAISED)) as send:
                self.assertEqual(
                    booking_api.raise_group_check_in(
                        uuid.UUID(GROUP_ID), authorization=TOKEN, booking_ids=ids),
                    (201, RAISED),
                )
            send.assert_called_once_with("POST", self.PATH, headers={"Authorization": TOKEN})

    def test_raise_with_a_list_sends_a_body_we_built(self):
        with mock.patch("apps.salons.booking_api._send", return_value=(201, RAISED)) as send:
            booking_api.raise_group_check_in(
                uuid.UUID(GROUP_ID), authorization=TOKEN, booking_ids=[L3])
        send.assert_called_once_with(
            "POST", self.PATH,
            headers={"Content-Type": "application/json", "Authorization": TOKEN},
            body=json.dumps({"bookingIds": [L3]}, separators=(",", ":")).encode(),
        )

    def test_read_gets_with_the_token_only(self):
        with mock.patch("apps.salons.booking_api._send", return_value=(200, READ)) as send:
            booking_api.read_group_check_in(uuid.UUID(GROUP_ID), authorization=TOKEN)
        send.assert_called_once_with("GET", self.PATH, headers={"Authorization": TOKEN})


class RouteTests(SimpleTestCase):
    def test_the_path_is_ours_not_a_booking_id(self):
        match = resolve(PATH)
        self.assertIs(match.func.view_class, GroupCheckInView)
        self.assertEqual(match.kwargs, {"group_id": uuid.UUID(GROUP_ID)})
