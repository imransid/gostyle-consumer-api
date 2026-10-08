"""
POST /api/v1/booking/<id>/cancel for a SINGLE booking (SINGLE_BOOKING_ACTIONS_V1).

The type comes from the caller's own Upcoming shelf; only a SINGLE row is
cancelled here, through booking-api's POST /v1/bookings/<id>/cancel. Every
other id goes on to the group cancel exactly as before the flag. No database,
no network: booking-api's calls are the seams.
"""

import json
import uuid
from pathlib import Path
from unittest import mock

from django.test import SimpleTestCase, override_settings
from rest_framework.test import APIRequestFactory, force_authenticate

from apps.salons import booking_api, single_views
from apps.salons.booking_api import BookingApiUnavailable
from apps.salons.group_views import GroupBookingCancelView
from apps.salons.test_group_booking import BRANCH, GROUP_ID, RANA, USER_ID, Customer
from apps.salons.test_group_booking_v2 import ANSWER, CARD

SINGLE_ID = "9f1c0f4e-3a2b-4d55-9a71-2c8e5b0d7a11"
SESSION_ID = "5e551011-0000-4000-8000-000000000001"   # one routine session's booking
ROUTINE_ID = "70071e00-0000-4000-8000-000000000001"   # the routine itself
SOMEONE_ELSES = "0e15e000-0000-4000-8000-000000000001"


def row(booking_id, booking_type):
    return {"id": booking_id, "salon_id": "marina-walk", "status": "CONFIRMED_BY_SALON",
            "booking_type": booking_type, "start_time": "2026-10-20T20:00:00+04:00"}


def shelf(rows, *, count=None):
    return {"count": len(rows) if count is None else count, "page": 1, "page_size": 50,
            "counts": {"upcoming": len(rows), "recurring": 0, "archive": 0}, "results": rows}


UPCOMING = shelf([row(SINGLE_ID, "SINGLE"), row(GROUP_ID, "GROUP"), row(SESSION_ID, "ROUTINE")])

# booking-api's LifecycleView for a cancel, as it answers it.
CANCELLED = {
    "code": "GS-1042", "bookingId": SINGLE_ID, "from": "CONFIRMED", "to": "CANCELLED",
    "paymentStatus": "REFUNDED", "refund": "AED 54.07", "kept": "AED 0.00",
    "lateCancel": False, "explanation": "Cancelled more than 24 hours ahead: full refund.",
}
NOT_FOUND = {"statusCode": 404, "message": "No such booking"}
SENTENCE = "Cancelled by the customer in the app."


class Seams:
    """booking-api's three calls, and what the party's answer needs."""

    def cancel(self, booking_id=SINGLE_ID, body=None, *, content_type="application/json", headers=None,
            **over):
        seams = {
            "apps.salons.single_views.list_bookings": mock.Mock(return_value=(200, UPCOMING)),
            "apps.salons.single_views.cancel_booking": mock.Mock(return_value=(201, dict(CANCELLED))),
            "apps.salons.group_views.cancel_group_booking": mock.Mock(return_value=(200, dict(ANSWER))),
            "apps.salons.group_views.salon_cards_for_refs": mock.Mock(return_value={BRANCH: CARD}),
            "apps.salons.group_views._account_names": mock.Mock(
                return_value={str(USER_ID): "Dana", RANA: "Rana Hassan"}),
        }
        seams.update(over)
        factory = APIRequestFactory()
        url = f"/api/v1/booking/{booking_id}/cancel"
        extra = {"HTTP_AUTHORIZATION": "Bearer customer-token", **(headers or {})}
        if body is None:
            request = factory.post(url, **extra)
        elif content_type == "application/json":
            request = factory.post(url, json.dumps(body), content_type=content_type, **extra)
        else:
            request = factory.post(url, body, content_type=content_type, **extra)
        force_authenticate(request, user=Customer())
        patches = [mock.patch(target, fake) for target, fake in seams.items()]
        for p in patches:
            p.start()
        try:
            return GroupBookingCancelView.as_view()(request, booking_id=uuid.UUID(booking_id)), seams
        finally:
            for p in patches:
                p.stop()

    @staticmethod
    def sent(seams):
        """What went to booking-api's single cancel: (id, body, keywords)."""
        call = seams["apps.salons.single_views.cancel_booking"].call_args
        return str(call.args[0]), call.args[1], call.kwargs


@override_settings(SINGLE_BOOKING_ACTIONS_V1=True, GROUP_BOOKING_V2=True)
class SingleCancelTests(Seams, SimpleTestCase):

    # ------------------------------------------------------------ the cancel

    def test_a_single_booking_is_cancelled_with_an_empty_body(self):
        response, s = self.cancel()
        self.assertEqual(response.status_code, 201, response.data)
        self.assertEqual(response.data, CANCELLED)
        booking_id, body, kw = self.sent(s)
        self.assertEqual((booking_id, body), (SINGLE_ID, {"reason": SENTENCE}))
        self.assertEqual(kw["authorization"], "Bearer customer-token")
        s["apps.salons.group_views.cancel_group_booking"].assert_not_called()

    def test_each_reason_goes_after_the_sentence(self):
        for reason in ("NOT_SATISFIED", "TOO_EXPENSIVE", "MOVING", "OTHER"):
            with self.subTest(reason):
                response, s = self.cancel(body={"reason": reason})
                self.assertEqual(response.status_code, 201, response.data)
                self.assertEqual(self.sent(s)[1], {"reason": f"{SENTENCE[:-1]}. Reason: {reason}."})

    def test_a_reason_left_null_or_blank_is_no_reason(self):
        for body in ({}, {"reason": None}, {"reason": ""}, {"reason": "   "}):
            with self.subTest(body):
                response, s = self.cancel(body=body)
                self.assertEqual(response.status_code, 201, response.data)
                self.assertEqual(self.sent(s)[1], {"reason": SENTENCE})

    def test_a_bad_reason_is_422_and_nothing_is_cancelled(self):
        for reason in ("moving", "SCHEDULE_CONFLICT", "I changed my mind", 5, True, ["MOVING"], {"x": 1}):
            with self.subTest(reason=reason):
                response, s = self.cancel(body={"reason": reason})
                self.assertEqual(response.status_code, 422, response.data)
                error = response.data["errors"][0]
                self.assertEqual((error["field"], error["code"]), ("reason", "invalid_cancel_reason"))
                s["apps.salons.single_views.cancel_booking"].assert_not_called()
                s["apps.salons.group_views.cancel_group_booking"].assert_not_called()

    def test_only_the_body_built_here_goes_to_booking_api(self):
        # booking-api answers 400 for a key it does not know, and `actor` or
        # `nowMs` from the app must never reach it.
        response, s = self.cancel(body={"reason": "OTHER", "actor": "SALON", "nowMs": 1, "initiatedBy": "SALON"})
        self.assertEqual(response.status_code, 201, response.data)
        self.assertEqual(self.sent(s)[1], {"reason": f"{SENTENCE[:-1]}. Reason: OTHER."})

    def test_the_apps_idempotency_key_goes_along_and_none_is_made_up(self):
        _, s = self.cancel(headers={"HTTP_IDEMPOTENCY_KEY": "tap-1"})
        self.assertEqual(self.sent(s)[2]["idempotency_key"], "tap-1")
        _, s = self.cancel()
        self.assertIsNone(self.sent(s)[2]["idempotency_key"])

    def test_booking_api_answers_come_back_as_they_came(self):
        answers = [
            (404, NOT_FOUND),
            (409, {"statusCode": 409, "message": "A cancelled booking cannot become cancelled."}),
            (403, {"statusCode": 403, "message": "A customer may not cancel."}),
            (422, {"statusCode": 422, "message": "Choose a reason"}),
        ]
        for code, body in answers:
            with self.subTest(code):
                response, _ = self.cancel(**{
                    "apps.salons.single_views.cancel_booking": mock.Mock(return_value=(code, body))})
                self.assertEqual((response.status_code, response.data), (code, body))

    def test_a_body_that_is_not_json_is_415(self):
        response, s = self.cancel(body="reason=MOVING", content_type="application/x-www-form-urlencoded")
        self.assertEqual(response.status_code, 415)
        s["apps.salons.single_views.cancel_booking"].assert_not_called()

    def test_booking_api_down_on_the_cancel_is_503(self):
        response, _ = self.cancel(**{
            "apps.salons.single_views.cancel_booking": mock.Mock(side_effect=BookingApiUnavailable("x"))})
        self.assertEqual(response.status_code, 503)

    # ------------------------------------------------------------ not yours

    def test_someone_elses_booking_is_404_and_never_cancelled_as_single(self):
        # Not on the caller's own shelf, so not a single cancel: it goes on to
        # the group cancel, whose 404 comes back as it came.
        missing = mock.Mock(return_value=(404, NOT_FOUND))
        response, s = self.cancel(SOMEONE_ELSES, **{"apps.salons.group_views.cancel_group_booking": missing})
        self.assertEqual((response.status_code, response.data), (404, NOT_FOUND))
        s["apps.salons.single_views.cancel_booking"].assert_not_called()

    # ------------------------------------------------------------ the lookup

    def test_the_lookup_is_the_callers_own_upcoming_shelf(self):
        _, s = self.cancel(headers={"HTTP_X_TENANT_ID": "tenant-1"})
        call = s["apps.salons.single_views.list_bookings"].call_args
        self.assertEqual(call.args[0], "filter=upcoming&page=1&pageSize=50")
        self.assertEqual(call.kwargs, {"authorization": "Bearer customer-token", "tenant_id": "tenant-1",
                                       "timeout": 3})

    def test_a_lookup_that_times_out_goes_on_to_the_group_cancel(self):
        # Through the real client: the lookup waits SINGLE_LOOKUP_TIMEOUT,
        # not BOOKING_API_TIMEOUT, and a timeout is "not single".
        urlopen = mock.Mock(side_effect=TimeoutError("timed out"))
        cancelled = mock.Mock(return_value=(200, dict(ANSWER, status="CANCELLED")))
        response, s = self.cancel(GROUP_ID, **{
            "apps.salons.single_views.list_bookings": booking_api.list_bookings,
            "apps.salons.booking_api.urllib.request.urlopen": urlopen,
            "apps.salons.group_views.cancel_group_booking": cancelled,
        })
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(response.data["status"], "CANCELLED")
        urlopen.assert_called_once()
        self.assertEqual(urlopen.call_args.kwargs["timeout"], 3)
        cancelled.assert_called_once()
        s["apps.salons.single_views.cancel_booking"].assert_not_called()

        with override_settings(SINGLE_LOOKUP_TIMEOUT=1):
            self.cancel(GROUP_ID, **{
                "apps.salons.single_views.list_bookings": booking_api.list_bookings,
                "apps.salons.booking_api.urllib.request.urlopen": urlopen,
            })
        self.assertEqual(urlopen.call_args.kwargs["timeout"], 1)

    def test_only_the_first_page_is_read(self):
        # The id is on page 2: not looked for, so not a single cancel. It
        # goes on to the group cancel, whose 404 comes back.
        filler = [row(str(uuid.uuid4()), "SINGLE") for _ in range(50)]
        pages = mock.Mock(return_value=(200, shelf(filler, count=51)))
        missing = mock.Mock(return_value=(404, NOT_FOUND))
        response, s = self.cancel(**{"apps.salons.single_views.list_bookings": pages,
                                     "apps.salons.group_views.cancel_group_booking": missing})
        self.assertEqual(response.status_code, 404)
        pages.assert_called_once()
        s["apps.salons.single_views.cancel_booking"].assert_not_called()

    def test_a_lookup_that_fails_is_not_a_single_cancel(self):
        # Refused, broken or unreachable: the id goes on to the group cancel
        # and its answer comes back. Never the lookup's own error.
        failures = {
            "refused": mock.Mock(return_value=(401, {"statusCode": 401, "message": "Invalid token"})),
            "5xx": mock.Mock(return_value=(500, {"statusCode": 500})),
            "not an object": mock.Mock(return_value=(200, None)),
            "unreachable": mock.Mock(side_effect=BookingApiUnavailable("x")),
        }
        for name, lookup in failures.items():
            with self.subTest(name):
                missing = mock.Mock(return_value=(404, NOT_FOUND))
                response, s = self.cancel(**{"apps.salons.single_views.list_bookings": lookup,
                                             "apps.salons.group_views.cancel_group_booking": missing})
                self.assertEqual((response.status_code, response.data), (404, NOT_FOUND))
                missing.assert_called_once()
                s["apps.salons.single_views.cancel_booking"].assert_not_called()

    # ------------------------------------------------------------ routines

    def test_a_routine_sessions_id_is_not_cancelled_as_single(self):
        # A ROUTINE row: one session's booking. It goes on to the group
        # cancel, as it did before the flag (booking-api: not a party, 404).
        missing = mock.Mock(return_value=(404, NOT_FOUND))
        response, s = self.cancel(SESSION_ID, **{"apps.salons.group_views.cancel_group_booking": missing})
        self.assertEqual(response.status_code, 404)
        s["apps.salons.single_views.cancel_booking"].assert_not_called()
        missing.assert_called_once()

    def test_a_routines_own_id_is_not_cancelled_as_single(self):
        missing = mock.Mock(return_value=(404, NOT_FOUND))
        response, s = self.cancel(ROUTINE_ID, **{"apps.salons.group_views.cancel_group_booking": missing})
        self.assertEqual(response.status_code, 404)
        s["apps.salons.single_views.cancel_booking"].assert_not_called()


class GroupCancelUnchangedTests(Seams, SimpleTestCase):
    """A party's id answers exactly the same with the flag on as with it off."""

    def both(self, body=None, **over):
        with override_settings(SINGLE_BOOKING_ACTIONS_V1=False, GROUP_BOOKING_V2=True):
            off, s_off = self.cancel(GROUP_ID, body, **over)
        with override_settings(SINGLE_BOOKING_ACTIONS_V1=True, GROUP_BOOKING_V2=True):
            on, s_on = self.cancel(GROUP_ID, body, **over)
        return (off, s_off), (on, s_on)

    def test_the_booker_cancels_the_party_exactly_as_before(self):
        cancelled = mock.Mock(return_value=(200, dict(ANSWER, status="CANCELLED")))
        (off, s_off), (on, s_on) = self.both(**{"apps.salons.group_views.cancel_group_booking": cancelled})
        self.assertEqual(on.status_code, 200, on.data)
        self.assertEqual((on.status_code, on.data), (off.status_code, off.data))
        self.assertEqual(on.data["status"], "CANCELLED")
        calls = cancelled.call_args_list
        self.assertEqual(len(calls), 2)
        self.assertEqual(calls[0], calls[1])
        s_on["apps.salons.single_views.cancel_booking"].assert_not_called()

    def test_a_party_cancel_still_reads_no_body(self):
        # A reason that a single cancel would refuse means nothing to a party.
        (off, _), (on, s_on) = self.both(body={"reason": "not one of the four"})
        self.assertEqual((on.status_code, on.data), (off.status_code, off.data))
        self.assertEqual(on.status_code, 200)
        s_on["apps.salons.single_views.cancel_booking"].assert_not_called()

    def test_a_failing_lookup_never_breaks_a_party_cancel(self):
        # Before the flag a party's cancel never called the list, so with the
        # flag on a list that fails must not change its answer.
        failures = {
            "unreachable": mock.Mock(side_effect=BookingApiUnavailable("x")),
            "refused": mock.Mock(return_value=(401, {"statusCode": 401})),
            "5xx": mock.Mock(return_value=(502, None)),
        }
        for name, lookup in failures.items():
            with self.subTest(name):
                cancelled = mock.Mock(return_value=(200, dict(ANSWER, status="CANCELLED")))
                (off, _), (on, s_on) = self.both(**{
                    "apps.salons.group_views.cancel_group_booking": cancelled,
                    "apps.salons.single_views.list_bookings": lookup})
                self.assertEqual(on.status_code, 200, on.data)
                self.assertEqual((on.status_code, on.data), (off.status_code, off.data))
                self.assertEqual(on.data["status"], "CANCELLED")
                s_on["apps.salons.single_views.cancel_booking"].assert_not_called()

    def test_a_refusal_is_forwarded_exactly_as_before(self):
        refusal = {"detail": "GS-1281 is checked in", "code": "cannot_cancel", "errors": []}
        (off, _), (on, _) = self.both(**{
            "apps.salons.group_views.cancel_group_booking": mock.Mock(return_value=(409, refusal))})
        self.assertEqual((on.status_code, on.data), (off.status_code, off.data))
        self.assertEqual((on.status_code, on.data), (409, refusal))


class FlagOffTests(Seams, SimpleTestCase):
    """SINGLE_BOOKING_ACTIONS_V1 off: the route is exactly what it was."""

    @override_settings(SINGLE_BOOKING_ACTIONS_V1=False, GROUP_BOOKING_V2=True)
    def test_a_single_id_goes_to_the_group_cancel_as_today(self):
        missing = mock.Mock(return_value=(404, NOT_FOUND))
        response, s = self.cancel(body={"reason": "MOVING"},
                               **{"apps.salons.group_views.cancel_group_booking": missing})
        self.assertEqual((response.status_code, response.data), (404, NOT_FOUND))
        s["apps.salons.single_views.list_bookings"].assert_not_called()
        s["apps.salons.single_views.cancel_booking"].assert_not_called()
        missing.assert_called_once()

    @override_settings(SINGLE_BOOKING_ACTIONS_V1=False, GROUP_BOOKING_V2=False)
    def test_with_both_flags_off_it_is_404_and_calls_nothing(self):
        response, s = self.cancel()
        self.assertEqual(response.status_code, 404)
        for seam in ("apps.salons.single_views.list_bookings",
                     "apps.salons.single_views.cancel_booking",
                     "apps.salons.group_views.cancel_group_booking"):
            s[seam].assert_not_called()

    @override_settings(SINGLE_BOOKING_ACTIONS_V1=True, GROUP_BOOKING_V2=False)
    def test_single_works_without_the_group_flag_and_a_party_stays_404(self):
        response, _ = self.cancel()
        self.assertEqual(response.status_code, 201, response.data)
        response, s = self.cancel(GROUP_ID)
        self.assertEqual(response.status_code, 404)
        s["apps.salons.group_views.cancel_group_booking"].assert_not_called()

    def test_the_flag_is_off_by_default(self):
        self.assertIn("SINGLE_BOOKING_ACTIONS_V1=false", Path(".env.example").read_text())
        self.assertIn("SINGLE_BOOKING_ACTIONS_V1: ${SINGLE_BOOKING_ACTIONS_V1:-False}",
                      Path("docker-compose.yml").read_text())
        source = Path("config/settings/base.py").read_text()
        self.assertIn(
            'SINGLE_BOOKING_ACTIONS_V1 = env.bool("SINGLE_BOOKING_ACTIONS_V1", default=False)', source)


class LookupTimeoutDefaultTests(SimpleTestCase):

    def test_the_lookup_timeout_defaults_to_3_seconds(self):
        self.assertIn("SINGLE_LOOKUP_TIMEOUT=3", Path(".env.example").read_text())
        self.assertIn("SINGLE_LOOKUP_TIMEOUT: ${SINGLE_LOOKUP_TIMEOUT:-3}",
                      Path("docker-compose.yml").read_text())
        source = Path("config/settings/base.py").read_text()
        self.assertIn('SINGLE_LOOKUP_TIMEOUT = env.int("SINGLE_LOOKUP_TIMEOUT", default=3)', source)


class CancelReasonTests(SimpleTestCase):
    """The pure half: the reason the app sent, and the sentence booking-api gets."""

    def test_the_sentence_is_never_blank(self):
        self.assertEqual(single_views.cancel_text(None), SENTENCE)
        self.assertEqual(single_views.cancel_text("MOVING"), f"{SENTENCE[:-1]}. Reason: MOVING.")

    def test_the_four_reasons_are_the_routines(self):
        self.assertEqual(single_views.CANCEL_REASONS, ("NOT_SATISFIED", "TOO_EXPENSIVE", "MOVING", "OTHER"))

    def test_padding_around_a_reason_is_dropped(self):
        self.assertEqual(single_views.cancel_reason({"reason": " MOVING "}), "MOVING")
