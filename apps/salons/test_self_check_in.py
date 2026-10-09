"""
POST and GET /api/v1/booking/<id>/check-in (SELF_CHECK_IN_V1): the customer
says "I am here", and reads the desk's answer.

booking-api decides everything; this route checks the flag, forwards the
caller's own token (and, at a chair, the scanned token and the app's User-Agent)
and passes booking-api's answer back as it came. No network: booking-api's two
calls are the seams, and WireTests fakes only urlopen, so `_send`, the view and
the middleware all run.
"""

import contextlib
import io
import json
import urllib.error
import uuid
from unittest import mock

from django.test import SimpleTestCase, TestCase, override_settings
from django.urls import resolve
from rest_framework.response import Response
from rest_framework.test import APIClient, APIRequestFactory, force_authenticate

from apps.accounts.models import ConsumerAccount
from apps.accounts.services import tokens_for
from apps.salons import booking_api
from apps.salons.booking_api import BookingApiUnavailable
from apps.salons.check_in_views import SelfCheckInView, SelfCheckInWithdrawView
from apps.salons.test_group_booking import Customer
from apps.salons.views import BookingDetailView

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

# At a chair (booking-api docs/chair-check-in.md, check-in-request.handler.ts).
CHAIR_TOKEN = "q7Xk2mP9rT4vW8yZ1aB3cD"
APP_UA = "GoStyle/1.4 (iPhone; iOS 18.1)"
AT_CHAIR = {"request": {**WAITING["request"], "chair": {"number": "7", "zoneName": "Window section"}}}

# The read's welcome (booking-api check-in-attribution.handler.ts), in the
# shapes booking-api really sends. `via` is never absent: it is null only on
# a check-in written before booking-api recorded it. The whole `checkIn` is
# null while the request waits, and when no check-in stands (none yet, the
# desk undid it, or a status set by hand with no check-in behind it).
APPROVED = {**WAITING["request"], "state": "APPROVED", "decidedAt": "2026-10-11T03:55:00.000Z"}
APPROVED_WELCOME = {
    "request": APPROVED,
    "checkIn": {"at": "2026-10-11T03:55:00.000Z", "via": "SELF", "byName": "Layla R."},
}
WELCOME_FROM_BEFORE_VIA = {
    "request": APPROVED,
    "checkIn": {"at": "2026-10-11T03:55:00.000Z", "via": None, "byName": "Layla R."},
}
WELCOME_WITHOUT_A_NAME = {
    "request": APPROVED,
    "checkIn": {"at": "2026-10-11T03:55:00.000Z", "via": "STAFF", "byName": None},
}
WAITING_NO_WELCOME = {**WAITING, "checkIn": None}
CHECKED_IN_BY_HAND = {"request": APPROVED, "checkIn": None}


def chair_refused(reason, message):
    return {"statusCode": 409, "code": "BOOKING_CHAIR_REFUSED", "message": message,
            "details": {"reason": reason}, "error": "Conflict"}


CHAIR_REFUSALS = (
    chair_refused("CARD_OUT_OF_DATE", "This card is out of date. Please see the desk."),
    chair_refused("OTHER_SALON",
                  "This chair is not at the salon of your booking. Please see the desk."),
    chair_refused("CHAIR_NOT_AVAILABLE",
                  "That chair is not available. Please take another or see the desk."),
    chair_refused("UNKNOWN_CARD", "This is not a chair card we know. Please scan the card "
                                  "on your chair, or see the desk."),
)
# booking-api could not check the chair: scanning is off, the desk still works.
WAIT_FOR_STAFF = {
    "statusCode": 503,
    "code": "DEPENDENCY_UNAVAILABLE",
    "message": "We could not check this chair just now. Please use Wait for Staff and "
               "the desk will check you in.",
    "details": {"reason": "CHAIR_CHECK_UNAVAILABLE", "fallback": "WAIT_FOR_STAFF"},
    "error": "Service Unavailable",
}
# booking-api could not check the customer's own token: Wait for Staff fails too.
AUTH_DOWN = {
    "statusCode": 503,
    "code": "DEPENDENCY_UNAVAILABLE",
    "message": "Customer authentication is unavailable",
    "details": {"dependency": "consumer-auth", "address": "consumer_grpc:50051",
                "grpcStatus": "UNAVAILABLE"},
    "error": "Service Unavailable",
}


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

    @override_settings(SELF_CHECK_IN_V1=False)
    def test_off_is_404_at_a_chair_too_even_a_broken_scan(self):
        for token in (CHAIR_TOKEN, ""):
            response, fakes = self.call("post", body={"chair_token": token})
            self.assertEqual(response.status_code, 404, repr(token))
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


@override_settings(SELF_CHECK_IN_V1=True, CHAIR_SCAN_V1=True)
class ChairTests(Seams, SimpleTestCase):
    """At a chair, scanning on: the scanned token and the app's User-Agent go on; nothing else changes."""

    def test_a_scanned_chair_goes_on_with_the_apps_user_agent(self):
        response, fakes = self.call("post", raised=(201, AT_CHAIR),
                                    body={"chair_token": CHAIR_TOKEN}, HTTP_USER_AGENT=APP_UA)
        self.assertEqual(response.status_code, 201)
        self.assertEqual(response.data, AT_CHAIR)
        fakes["raise_check_in"].assert_called_once_with(
            uuid.UUID(BOOKING_ID), authorization=TOKEN, chair_token=CHAIR_TOKEN,
            user_agent=APP_UA)

    def test_no_user_agent_header_is_an_empty_one(self):
        _, fakes = self.call("post", body={"chair_token": CHAIR_TOKEN})
        fakes["raise_check_in"].assert_called_once_with(
            uuid.UUID(BOOKING_ID), authorization=TOKEN, chair_token=CHAIR_TOKEN, user_agent="")

    def test_the_token_goes_on_exactly_as_scanned(self):
        # Never trimmed: the token is platform's, and a changed one is another card.
        _, fakes = self.call("post", body={"chair_token": f" {CHAIR_TOKEN}\n"})
        self.assertEqual(fakes["raise_check_in"].call_args.kwargs["chair_token"],
                         f" {CHAIR_TOKEN}\n")

    def test_wait_for_staff_is_the_call_from_before_chairs(self):
        # No chair, no user agent, nothing of the app's body: the very call
        # it was before chairs, which is what makes it a safe fallback.
        for body in (None, {}, {"chair_token": None}, {"tenant": "x"}, ["not", "an", "object"]):
            _, fakes = self.call("post", body=body, HTTP_USER_AGENT=APP_UA)
            fakes["raise_check_in"].assert_called_once_with(
                uuid.UUID(BOOKING_ID), authorization=TOKEN)

    def test_a_broken_scan_is_refused_here_and_booking_api_is_not_called(self):
        # The project's field-level 422 (apps/accounts/exceptions.py).
        for token, code in (("", "blank"), ("   ", "blank"), ("\n", "blank"), (42, "invalid"),
                            (True, "invalid"), (["x"], "invalid"), ({"t": "x"}, "invalid")):
            response, fakes = self.call("post", body={"chair_token": token})
            self.assertEqual(response.status_code, 422, repr(token))
            self.assertEqual(response.data["errors"],
                             [{"field": "chair_token", "code": code,
                               "message": mock.ANY}], repr(token))
            fakes["raise_check_in"].assert_not_called()

    def test_chair_refusals_come_back_as_they_came(self):
        for refusal in CHAIR_REFUSALS:
            response, _ = self.call("post", raised=(409, refusal),
                                    body={"chair_token": CHAIR_TOKEN})
            self.assertEqual(response.status_code, 409)
            self.assertEqual(response.data, refusal)

    def test_booking_apis_503s_come_back_as_they_came(self):
        # WAIT_FOR_STAFF: the desk still works. AUTH_DOWN: it does not. The
        # app tells them apart by details.fallback, so neither is rewritten.
        for answer in (WAIT_FOR_STAFF, AUTH_DOWN):
            response, _ = self.call("post", raised=(503, answer),
                                    body={"chair_token": CHAIR_TOKEN})
            self.assertEqual(response.status_code, 503)
            self.assertEqual(response.data, answer)

    def test_booking_api_down_at_a_chair_is_our_503_with_no_fallback(self):
        response, _ = self.call("post", raised=BookingApiUnavailable("refused"),
                                body={"chair_token": CHAIR_TOKEN})
        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.data["errors"][0]["code"], "booking_api_unavailable")
        self.assertNotIn("WAIT_FOR_STAFF", json.dumps(response.data))


# Written out, not imported: this is the contract the app reads.
SCAN_OFF = {
    "statusCode": 409,
    "code": "BOOKING_CHAIR_SCAN_OFF",
    "message": "Scanning the chair is not available right now. Please use Wait for "
               "Staff and the desk will check you in.",
    "details": {"fallback": "WAIT_FOR_STAFF"},
    "error": "Conflict",
}


@override_settings(SELF_CHECK_IN_V1=True)
class ChairScanOffTests(Seams, SimpleTestCase):
    """
    CHAIR_SCAN_V1 off: a scan is refused HERE, 409 BOOKING_CHAIR_SCAN_OFF in
    booking-api's shape, and booking-api is not called, so nothing is raised
    without the chair. A backstop: every can_scan_chair is false while it is
    off. Wait for Staff is untouched.
    """

    def test_off_by_default_a_scan_is_refused_and_booking_api_is_not_called(self):
        response, fakes = self.call("post", body={"chair_token": CHAIR_TOKEN},
                                    HTTP_USER_AGENT=APP_UA)
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.data, SCAN_OFF)
        fakes["raise_check_in"].assert_not_called()

    @override_settings(CHAIR_SCAN_V1=False)
    def test_off_when_set_off(self):
        response, fakes = self.call("post", body={"chair_token": CHAIR_TOKEN})
        self.assertEqual((response.status_code, response.data), (409, SCAN_OFF))
        fakes["raise_check_in"].assert_not_called()

    @override_settings(CHAIR_SCAN_V1=False)
    def test_any_chair_token_even_a_broken_one_is_the_409_not_the_422(self):
        # "Scan again" would send the customer back to a scanner that is off.
        for token in ("", "   ", "\n", 42, True, ["x"], {"t": "x"}, f" {CHAIR_TOKEN}\n"):
            response, fakes = self.call("post", body={"chair_token": token})
            self.assertEqual((response.status_code, response.data), (409, SCAN_OFF), repr(token))
            fakes["raise_check_in"].assert_not_called()

    @override_settings(CHAIR_SCAN_V1=False)
    def test_wait_for_staff_goes_on_as_ever(self):
        for body in (None, {}, {"chair_token": None}, {"tenant": "x"}, ["not", "an", "object"]):
            response, fakes = self.call("post", body=body, HTTP_USER_AGENT=APP_UA)
            self.assertEqual((response.status_code, response.data), (201, WAITING), repr(body))
            fakes["raise_check_in"].assert_called_once_with(
                uuid.UUID(BOOKING_ID), authorization=TOKEN)

    def test_the_switch_is_the_only_difference(self):
        for chairs, status, called in ((False, 409, False), (True, 201, True)):
            with override_settings(CHAIR_SCAN_V1=chairs), self.subTest(chairs=chairs):
                response, fakes = self.call("post", raised=(201, AT_CHAIR),
                                            body={"chair_token": CHAIR_TOKEN})
                self.assertEqual(response.status_code, status)
                self.assertIs(fakes["raise_check_in"].called, called)

    def test_self_check_in_off_is_still_the_404_whatever_chairs_say(self):
        for chairs in (True, False):
            with override_settings(SELF_CHECK_IN_V1=False, CHAIR_SCAN_V1=chairs), \
                    self.subTest(chairs=chairs):
                response, fakes = self.call("post", body={"chair_token": CHAIR_TOKEN})
                self.assertEqual(response.status_code, 404)
                fakes["raise_check_in"].assert_not_called()

    @override_settings(CHAIR_SCAN_V1=False)
    def test_signed_out_is_still_the_401_first(self):
        response, fakes = self.call("post", body={"chair_token": CHAIR_TOKEN}, user=False)
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

    def test_the_welcome_comes_back_as_booking_api_answered(self):
        for answer in (APPROVED_WELCOME, WELCOME_FROM_BEFORE_VIA, WELCOME_WITHOUT_A_NAME,
                       WAITING_NO_WELCOME, CHECKED_IN_BY_HAND):
            response, _ = self.call("get", read=(200, answer))
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.data, answer)

    def test_a_null_via_and_a_null_name_keep_their_keys(self):
        # The keys are booking-api's contract: present, with null. Dropping
        # one would tell the app something booking-api did not say.
        response, _ = self.call("get", read=(200, WELCOME_FROM_BEFORE_VIA))
        self.assertIn("via", response.data["checkIn"])
        self.assertIsNone(response.data["checkIn"]["via"])
        response, _ = self.call("get", read=(200, WELCOME_WITHOUT_A_NAME))
        self.assertIn("byName", response.data["checkIn"])
        self.assertIsNone(response.data["checkIn"]["byName"])


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

    def raise_at_a_chair(self, **kwargs):
        with mock.patch("apps.salons.booking_api._send", return_value=(201, AT_CHAIR)) as send:
            booking_api.raise_check_in(uuid.UUID(BOOKING_ID), authorization=TOKEN, **kwargs)
        send.assert_called_once_with(
            "POST", f"/v1/bookings/{BOOKING_ID}/check-in-request",
            headers={"Content-Type": "application/json", "Authorization": TOKEN},
            body=mock.ANY,
        )
        return json.loads(send.call_args.kwargs["body"])

    def test_raise_at_a_chair_sends_the_body_this_service_built(self):
        self.assertEqual(self.raise_at_a_chair(chair_token=CHAIR_TOKEN, user_agent=APP_UA),
                         {"chairToken": CHAIR_TOKEN, "userAgent": APP_UA})

    def test_no_user_agent_key_without_a_user_agent(self):
        for user_agent in (None, ""):
            self.assertEqual(self.raise_at_a_chair(chair_token=CHAIR_TOKEN, user_agent=user_agent),
                             {"chairToken": CHAIR_TOKEN})

    def test_a_user_agent_alone_is_not_a_chair(self):
        with mock.patch("apps.salons.booking_api._send", return_value=(201, WAITING)) as send:
            booking_api.raise_check_in(uuid.UUID(BOOKING_ID), authorization=TOKEN,
                                       user_agent=APP_UA)
        send.assert_called_once_with(
            "POST", f"/v1/bookings/{BOOKING_ID}/check-in-request",
            headers={"Authorization": TOKEN},
        )


@override_settings(SELF_CHECK_IN_V1=True, CHAIR_SCAN_V1=True, BOOKING_API_URL="http://booking-api.test")
class WireTests(TestCase):
    """
    The whole stack but the network: a real customer token, the URL, the
    middleware, the view and `_send`, with only urlopen faked. What leaves
    for booking-api, and what reaches the app.
    """

    def setUp(self):
        self.client = APIClient()
        account = ConsumerAccount.objects.create(phone="+971500000071")
        self.auth = f"Bearer {tokens_for(account)['access']}"
        self.client.credentials(HTTP_AUTHORIZATION=self.auth)

    def get(self, answer):
        """GET the check-in with booking-api answering `answer`; returns (response, sent)."""
        return self.post(answer, method="get")

    def post(self, answer, body=None, path=PATH, method="post", **headers):
        """POST `path` with booking-api answering `answer`; returns (response, sent)."""
        sent = []

        def urlopen(request, timeout):
            sent.append(request)
            if isinstance(answer, Exception):
                raise answer
            status, payload = answer
            if status >= 400:
                raise urllib.error.HTTPError(request.full_url, status, "", {},
                                             io.BytesIO(json.dumps(payload).encode()))
            opened = mock.MagicMock()
            opened.__enter__.return_value.status = status
            opened.__enter__.return_value.read.return_value = json.dumps(payload).encode()
            return opened

        with mock.patch("urllib.request.urlopen", urlopen):
            if body is None:
                response = getattr(self.client, method)(path, **headers)
            else:
                response = self.client.post(path, body, format="json", **headers)
        self.assertEqual(len(sent), 1)
        return response, sent[0]

    def test_wait_for_staff_503_reaches_the_app_unchanged(self):
        response, sent = self.post((503, WAIT_FOR_STAFF), {"chair_token": CHAIR_TOKEN},
                                   HTTP_USER_AGENT=APP_UA, HTTP_X_TENANT_ID="tenant-from-app")
        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.json(), WAIT_FOR_STAFF)
        # What left for booking-api: the built body, and no tenant header.
        self.assertEqual(sent.full_url,
                         f"http://booking-api.test/v1/bookings/{BOOKING_ID}/check-in-request")
        self.assertEqual(sent.get_method(), "POST")
        self.assertEqual(json.loads(sent.data), {"chairToken": CHAIR_TOKEN, "userAgent": APP_UA})
        self.assertEqual(dict(sent.header_items()),
                         {"Content-type": "application/json", "Authorization": self.auth})

    def test_a_chair_reaches_the_app_unchanged(self):
        response, _ = self.post((201, AT_CHAIR), {"chair_token": CHAIR_TOKEN})
        self.assertEqual(response.status_code, 201)
        self.assertEqual(response.json(), AT_CHAIR)

    @override_settings(CHAIR_SCAN_V1=False)
    def test_scanning_off_the_409_reaches_the_app_and_nothing_leaves(self):
        with mock.patch("urllib.request.urlopen") as urlopen:
            response = self.client.post(PATH, {"chair_token": CHAIR_TOKEN}, format="json",
                                        HTTP_USER_AGENT=APP_UA)
        urlopen.assert_not_called()
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response["Content-Type"], "application/json")
        self.assertEqual(response.json(), SCAN_OFF)

    @override_settings(CHAIR_SCAN_V1=False)
    def test_scanning_off_wait_for_staff_still_leaves_as_before(self):
        response, sent = self.post((201, WAITING), HTTP_USER_AGENT=APP_UA)
        self.assertEqual((response.status_code, response.json()), (201, WAITING))
        self.assertIsNone(sent.data)

    def test_wait_for_staff_sends_no_body_and_only_the_token(self):
        response, sent = self.post((201, WAITING), HTTP_USER_AGENT=APP_UA,
                                   HTTP_X_TENANT_ID="tenant-from-app")
        self.assertEqual(response.status_code, 201)
        self.assertEqual(response.json(), WAITING)
        self.assertIsNone(sent.data)
        self.assertEqual(dict(sent.header_items()), {"Authorization": self.auth})

    def test_booking_api_unreachable_is_our_503_with_no_fallback(self):
        response, _ = self.post(urllib.error.URLError("connection refused"),
                                {"chair_token": CHAIR_TOKEN})
        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.json()["errors"][0]["code"], "booking_api_unavailable")
        self.assertNotIn("WAIT_FOR_STAFF", response.content.decode())

    def test_the_read_and_its_welcome_reach_the_app_byte_for_byte(self):
        # Through the renderer too: a null via and a null name keep their keys.
        for answer in (APPROVED_WELCOME, WELCOME_FROM_BEFORE_VIA, WELCOME_WITHOUT_A_NAME,
                       WAITING_NO_WELCOME):
            response, sent = self.get((200, answer))
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.json(), answer)
            self.assertEqual(sent.get_method(), "GET")
            self.assertEqual(sent.full_url,
                             f"http://booking-api.test/v1/bookings/{BOOKING_ID}/check-in-request")
            self.assertEqual(dict(sent.header_items()), {"Authorization": self.auth})

    def test_withdraw_sends_no_body_and_only_the_token_and_its_409_reaches_the_app_unchanged(self):
        response, sent = self.post((409, NOTHING_TO_CANCEL), {"tenant": "x"},
                                   path=WITHDRAW_PATH, HTTP_X_TENANT_ID="tenant-from-app")
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json(), NOTHING_TO_CANCEL)
        self.assertEqual(
            sent.full_url,
            f"http://booking-api.test/v1/bookings/{BOOKING_ID}/check-in-request/withdraw",
        )
        self.assertEqual(sent.get_method(), "POST")
        self.assertIsNone(sent.data)
        self.assertEqual(dict(sent.header_items()), {"Authorization": self.auth})


# ------------------------------------------------------------ Cancel Request

WITHDRAW_PATH = f"{PATH}/withdraw"

# booking-api's answers to POST .../check-in-request/withdraw, as it gives them.
WITHDRAWN = {"request": {**WAITING["request"], "state": "WITHDRAWN",
                         "decidedAt": "2026-10-11T03:52:00.000Z"}}
# Nothing waiting: details.request is the request's state as it now is.
ALREADY_ENDED = {
    "statusCode": 409,
    "code": "BOOKING_STATE_INVALID",
    "message": "This request has already ended.",
    "details": {"request": "CLOSED"},
    "error": "Conflict",
}
NOTHING_TO_CANCEL = {
    "statusCode": 409,
    "code": "BOOKING_STATE_INVALID",
    "message": "There is no check-in request to cancel.",
    "details": {"request": None},
    "error": "Conflict",
}


class WithdrawSeams:
    def withdraw(self, *, answer=(200, WITHDRAWN), body=None, user=True, **headers):
        factory = APIRequestFactory()
        extra = {"HTTP_AUTHORIZATION": TOKEN, **headers}
        if body is None:
            request = factory.post(WITHDRAW_PATH, **extra)
        else:
            request = factory.post(WITHDRAW_PATH, json.dumps(body),
                                   content_type="application/json", **extra)
        if user:
            force_authenticate(request, user=Customer())
        fake = mock.Mock(**_answer(answer))
        with mock.patch("apps.salons.check_in_views.withdraw_check_in", fake):
            response = SelfCheckInWithdrawView.as_view()(request, booking_id=uuid.UUID(BOOKING_ID))
        return response, fake


class WithdrawFlagOffTests(WithdrawSeams, SimpleTestCase):
    """Off by default: the 404 this path was before, and booking-api is not called."""

    def test_off_by_default(self):
        response, fake = self.withdraw()
        self.assertEqual(response.status_code, 404)
        fake.assert_not_called()

    @override_settings(SELF_CHECK_IN_V1=False)
    def test_off_when_set_off(self):
        response, fake = self.withdraw()
        self.assertEqual(response.status_code, 404)
        fake.assert_not_called()


@override_settings(SELF_CHECK_IN_V1=True)
class WithdrawTests(WithdrawSeams, SimpleTestCase):

    def test_withdrawn_200_as_booking_api_answered(self):
        response, fake = self.withdraw()
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data, WITHDRAWN)
        fake.assert_called_once_with(uuid.UUID(BOOKING_ID), authorization=TOKEN)

    def test_a_second_tap_is_the_same_200(self):
        response, _ = self.withdraw()
        again, _ = self.withdraw()
        self.assertEqual((again.status_code, again.data), (response.status_code, response.data))

    def test_both_409s_come_back_as_they_came(self):
        # Already ended, with the state as it now is; and none ever raised.
        for answer in ((409, ALREADY_ENDED), (409, NOTHING_TO_CANCEL)):
            response, _ = self.withdraw(answer=answer)
            self.assertEqual(response.status_code, 409)
            self.assertEqual(response.data, answer[1])

    def test_someone_elses_booking_is_booking_apis_404(self):
        response, _ = self.withdraw(answer=(404, NOT_FOUND))
        self.assertEqual(response.status_code, 404)
        self.assertEqual(response.data, NOT_FOUND)

    def test_neither_the_apps_body_nor_its_tenant_header_is_forwarded(self):
        _, fake = self.withdraw(body={"reason": "changed my mind"},
                                HTTP_X_TENANT_ID="tenant-from-app")
        fake.assert_called_once_with(uuid.UUID(BOOKING_ID), authorization=TOKEN)

    def test_booking_api_down_is_503_in_our_envelope(self):
        response, _ = self.withdraw(answer=BookingApiUnavailable("refused"))
        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.data["errors"][0]["code"], "booking_api_unavailable")

    def test_signed_out_is_refused_before_booking_api(self):
        response, fake = self.withdraw(user=False)
        self.assertEqual(response.status_code, 401)
        fake.assert_not_called()


class WithdrawRouteTests(SimpleTestCase):

    def test_the_route_is_wired(self):
        match = resolve(WITHDRAW_PATH)
        self.assertIs(match.func.view_class, SelfCheckInWithdrawView)
        self.assertEqual(match.kwargs, {"booking_id": uuid.UUID(BOOKING_ID)})

    def test_it_is_not_the_visits_cancel(self):
        # One segment away, and a different thing: booking/<id>/cancel
        # cancels the whole visit.
        cancel = resolve(f"/api/v1/booking/{BOOKING_ID}/cancel")
        self.assertIsNot(cancel.func.view_class, SelfCheckInWithdrawView)
        self.assertNotEqual(cancel.url_name, resolve(WITHDRAW_PATH).url_name)


class WithdrawClientTests(SimpleTestCase):

    def test_withdraw_posts_with_the_token_only(self):
        with mock.patch("apps.salons.booking_api._send", return_value=(200, WITHDRAWN)) as send:
            self.assertEqual(
                booking_api.withdraw_check_in(uuid.UUID(BOOKING_ID), authorization=TOKEN),
                (200, WITHDRAWN),
            )
        send.assert_called_once_with(
            "POST", f"/v1/bookings/{BOOKING_ID}/check-in-request/withdraw",
            headers={"Authorization": TOKEN},
        )


# ------------------------------------------------------------ the booking read
#
# GET /api/v1/booking/<id> carries booking-api's `check_in` (snake_case, like
# the rest of that booking) only while THIS service's SELF_CHECK_IN_V1 is on.
# Off, the key is removed, never nulled: no key is "off here", null is "on,
# nobody checked in". booking-api's party and routine reads carry no
# `check_in`, so only the single booking's 200 is gated.

BOOKING_PATH = f"/api/v1/booking/{BOOKING_ID}"
SALON_ID = "b7e92439-8285-469a-bba4-dcaa3dd5842c"
# booking-api's single booking, cut to the keys these tests need.
SINGLE = {"id": BOOKING_ID, "status": "CHECKED_IN", "salon_id": SALON_ID}
STAFF_WELCOME = {"at": "2026-10-11T03:55:00.000Z", "via": "STAFF", "by_name": "Layla R."}


def single(**over):
    return {**SINGLE, **over}


class BookingReadWelcomeTests(SimpleTestCase):

    def read(self, upstream, **patches):
        request = APIRequestFactory().get(BOOKING_PATH, HTTP_AUTHORIZATION=TOKEN)
        force_authenticate(request, user=Customer())
        fakes = {
            "apps.salons.views.read_booking": mock.Mock(return_value=upstream),
            "apps.salons.views.salon_cards_for_refs": mock.Mock(return_value={}),
            **patches,
        }
        with contextlib.ExitStack() as stack:
            for target, fake in fakes.items():
                stack.enter_context(mock.patch(target, fake))
            return BookingDetailView.as_view()(request, booking_id=uuid.UUID(BOOKING_ID))

    @override_settings(SELF_CHECK_IN_V1=False)
    def test_off_here_the_key_is_removed_not_nulled(self):
        for welcome in (STAFF_WELCOME, None):
            response = self.read((200, single(check_in=welcome)))
            self.assertEqual(response.status_code, 200)
            self.assertNotIn("check_in", response.data)
            # Only that key: everything else as booking-api sent it, plus our salon.
            self.assertEqual(response.data, {**SINGLE, "salon": None})

    @override_settings(SELF_CHECK_IN_V1=False)
    def test_off_here_with_the_key_already_absent_is_no_error(self):
        # Off at booking-api too: it never sent the key.
        response = self.read((200, single()))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data, {**SINGLE, "salon": None})

    @override_settings(SELF_CHECK_IN_V1=True)
    def test_on_here_it_passes_as_it_came_and_is_never_looked_inside(self):
        for welcome in (STAFF_WELCOME, None, {"whatever": ["booking-api", "sends"]}):
            response = self.read((200, single(check_in=welcome)))
            self.assertIn("check_in", response.data)
            self.assertEqual(response.data["check_in"], welcome)

    @override_settings(SELF_CHECK_IN_V1=True)
    def test_on_here_but_off_at_booking_api_no_key_is_invented(self):
        response = self.read((200, single()))
        self.assertNotIn("check_in", response.data)

    @override_settings(SELF_CHECK_IN_V1=False, GROUP_BOOKING_V2=False, ROUTINE_CONTRACT_V1=False)
    def test_a_404_passes_as_booking_api_said(self):
        response = self.read((404, NOT_FOUND))
        self.assertEqual((response.status_code, response.data), (404, NOT_FOUND))

    @override_settings(SELF_CHECK_IN_V1=False, GROUP_BOOKING_V2=False, ROUTINE_CONTRACT_V1=True)
    def test_the_routine_fallback_is_its_own_answer_untouched(self):
        routine = Response({"id": BOOKING_ID, "booking_type": "ROUTINE", "sessions": []})
        response = self.read((404, NOT_FOUND), **{
            "apps.salons.routine_views.read_as_routine": mock.Mock(return_value=routine),
        })
        self.assertEqual(response.data, {"id": BOOKING_ID, "booking_type": "ROUTINE", "sessions": []})

    @override_settings(SELF_CHECK_IN_V1=False, GROUP_BOOKING_V2=True, ROUTINE_CONTRACT_V1=False)
    def test_the_party_fallback_is_its_own_answer_untouched(self):
        party = Response({"id": BOOKING_ID, "booking_type": "GROUP", "participants": []})
        response = self.read((404, NOT_FOUND), **{
            "apps.salons.group_views.read_group_response": mock.Mock(return_value=party),
        })
        self.assertEqual(response.data, {"id": BOOKING_ID, "booking_type": "GROUP", "participants": []})


@override_settings(BOOKING_API_URL="http://booking-api.test")
class BookingReadWelcomeWireTests(TestCase):
    """The booking read with only urlopen faked: what reaches the app."""

    def setUp(self):
        self.client = APIClient()
        account = ConsumerAccount.objects.create(phone="+971500000072")
        self.client.credentials(HTTP_AUTHORIZATION=f"Bearer {tokens_for(account)['access']}")

    def get(self, payload):
        def urlopen(request, timeout):
            opened = mock.MagicMock()
            opened.__enter__.return_value.status = 200
            opened.__enter__.return_value.read.return_value = json.dumps(payload).encode()
            return opened

        with mock.patch("urllib.request.urlopen", urlopen), \
                mock.patch("apps.salons.views.salon_cards_for_refs", return_value={}):
            return self.client.get(BOOKING_PATH)

    def test_off_here_the_app_gets_no_check_in_key(self):
        with override_settings(SELF_CHECK_IN_V1=False):
            body = self.get(single(check_in=STAFF_WELCOME)).json()
        self.assertNotIn("check_in", body)
        self.assertEqual(body["status"], "CHECKED_IN")

    def test_on_here_the_app_gets_it_byte_for_byte(self):
        with override_settings(SELF_CHECK_IN_V1=True):
            for welcome in (STAFF_WELCOME, None):
                body = self.get(single(check_in=welcome)).json()
                self.assertIn("check_in", body)
                self.assertEqual(body["check_in"], welcome)
