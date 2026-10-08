"""
POST /api/v1/booking/<id>/reschedule for a SINGLE booking (SINGLE_BOOKING_ACTIONS_V1):

    the caller's own Upcoming shelf, page 1        (is it SINGLE?)
    the salon's hours, as the routine move checks  (before booking-api is asked)
    POST /v1/holds                                 (the new slot, on booking-api's clock)
    POST /v1/bookings/<id>/reschedule              (the move; on failure the hold is released)

Through the view, with the routine move's seams: no database, no network.
The salon is in Dubai (+04:00) and booking-api reads every branch at +06:00,
10:00 to 22:00 there (08:00 to 20:00 in Dubai). The salon opens 09:00 to
21:00 unless a test says otherwise. Now is 1 October 2026, 09:00.
"""

import json
import types
import uuid
from contextlib import contextmanager
from unittest import mock

from django.test import SimpleTestCase, override_settings
from django.urls import resolve
from rest_framework.test import APIRequestFactory, force_authenticate

from apps.salons import booking_api
from apps.salons.booking_api import BookingApiUnavailable
from apps.salons.single_views import SingleBookingRescheduleView
from apps.salons.test_group_booking import (
    ANYA,
    BRANCH,
    CLOCK,
    COVERAGE,
    CUT,
    GROUP_ID,
    HOLD_ID,
    MAYA,
    NAILS,
    SALON,
    STRANGER,
    STYLISTS,
    TENANT,
    TIMING,
    Customer,
)
from apps.salons.test_series_booking import CARD, NOW, opens
from apps.salons.test_single_cancel import ROUTINE_ID, SESSION_ID, SINGLE_ID, SOMEONE_ELSES, shelf

PATH = f"/api/v1/booking/{SINGLE_ID}/reschedule"
SENTENCE = "Rescheduled by the customer in the app."

# Thursday 22 October 2026, 19:00 in Dubai: 21:00 (minute 1260) at booking-api.
MOVE = {"date": "2026-10-22", "time": "19:00"}


def single_row(booking_id=SINGLE_ID, *, booking_type="SINGLE", stylists=(MAYA,), services=(CUT,)):
    """One row of the caller's Upcoming shelf, as booking-api lists it."""
    return {
        "id": booking_id, "salon_id": BRANCH, "status": "CONFIRMED_BY_SALON",
        "booking_type": booking_type, "date": "2026-10-20",
        "start_time": "2026-10-20T20:00:00+04:00", "end_time": "2026-10-20T20:45:00+04:00",
        "services": [{"id": s, "name": "Cut"} for s in services],
        "stylists": [{"id": s, "name": None, "avatar_url": None} for s in stylists],
    }


UPCOMING = shelf([
    single_row(),
    single_row(GROUP_ID, booking_type="GROUP"),
    single_row(SESSION_ID, booking_type="ROUTINE"),
])

# booking-api's HoldView and RescheduleView, as they answer.
HELD = {"holdId": HOLD_ID, "expiresAt": "2026-10-01T05:15:00.000Z", "expiresInSeconds": 900,
        "countdown": "15:00", "staff": {"id": MAYA, "name": "Maya E."}, "startMin": 1260}
MOVED = {
    "code": "GS-1042", "from": "2026-10-20T16:00:00.000Z", "to": "2026-10-22T15:00:00.000Z",
    "moveCount": 1, "lateMove": False, "deposit": "AED 0.00", "depositOutcome": "CARRIED",
    "explanation": "Moved more than 24 hours ahead: the deposit carries.",
}
NOT_FOUND = {"statusCode": 404, "code": "BOOKING_NOT_FOUND", "message": "No such booking",
             "error": "Not Found"}


class Seams:
    @contextmanager
    def seams(self, *, single=None, group=None, hours=None):
        group_fakes = {
            "salon_profile": mock.Mock(return_value=SALON),
            "booking_route": mock.Mock(return_value={"tenant_id": TENANT, "branch_id": BRANCH}),
            "service_timing_rows": mock.Mock(side_effect=lambda salon, ids: [
                row for row in TIMING if str(row["id"]) in ids]),
            "salon_stylists": mock.Mock(return_value=STYLISTS),
            "stylist_service_coverage": mock.Mock(return_value=COVERAGE),
            "engine_clock": mock.Mock(return_value=CLOCK),
        }
        group_fakes.update(group or {})
        single_fakes = {
            "list_bookings": mock.Mock(return_value=(200, UPCOMING)),
            "salon_cards_for_refs": mock.Mock(return_value={BRANCH: CARD}),
            "place_hold": mock.Mock(return_value=(201, dict(HELD))),
            "reschedule_booking": mock.Mock(return_value=(201, dict(MOVED))),
            "release_hold": mock.Mock(return_value=(200, {"released": True})),
        }
        single_fakes.update(single or {})
        patches = [mock.patch(f"apps.salons.group_views.{n}", f) for n, f in group_fakes.items()]
        patches += [mock.patch(f"apps.salons.single_views.{n}", f) for n, f in single_fakes.items()]
        patches.append(mock.patch(
            "apps.salons.routine_views._now", mock.Mock(side_effect=lambda tz: NOW.astimezone(tz))))
        patches.append(mock.patch("apps.salons.series_views._open_span", hours or opens()))
        for p in patches:
            p.start()
        try:
            yield types.SimpleNamespace(**group_fakes, **single_fakes)
        finally:
            for p in patches:
                p.stop()

    def post(self, body, booking_id=SINGLE_ID, *, content_type="application/json", **headers):
        request = APIRequestFactory().post(
            f"/api/v1/booking/{booking_id}/reschedule",
            data=json.dumps(body) if isinstance(body, dict) else body,
            content_type=content_type, HTTP_AUTHORIZATION="Bearer customer-token", **headers,
        )
        force_authenticate(request, user=Customer())
        return SingleBookingRescheduleView.as_view()(request, booking_id=uuid.UUID(booking_id))

    def held(self, s):
        """What booking-api's hold got: (body, kwargs)."""
        (body,), kw = s.place_hold.call_args
        return body, kw

    def moved(self, s):
        """What booking-api's reschedule got: (booking id, body, kwargs)."""
        (booking_id, body), kw = s.reschedule_booking.call_args
        return str(booking_id), body, kw

    def assert_refused(self, response, code, field, status=422):
        self.assertEqual(response.status_code, status, response.data)
        self.assertEqual(response.data["code"], "validation_error")
        self.assertEqual(response.data["errors"][0]["code"], code)
        self.assertEqual(response.data["errors"][0]["field"], field)

    def assert_nothing_held(self, s):
        s.place_hold.assert_not_called()
        s.reschedule_booking.assert_not_called()
        s.release_hold.assert_not_called()


# ------------------------------------------------------------ the move


@override_settings(SINGLE_BOOKING_ACTIONS_V1=True)
class MoveTests(Seams, SimpleTestCase):

    def test_hold_then_move_on_booking_apis_clock_and_its_answer_comes_back(self):
        with self.seams() as s:
            response = self.post(MOVE)
        self.assertEqual(response.status_code, 201, response.data)
        self.assertEqual(response.data, MOVED)

        body, kw = self.held(s)
        self.assertEqual(body, {
            "branch": BRANCH, "day": "2026-10-22", "services": [CUT],
            "startMin": 1260, "staffId": MAYA, "channel": "online",
        })
        self.assertEqual(kw, {"authorization": "Bearer customer-token", "tenant_id": TENANT})

        booking_id, body, kw = self.moved(s)
        self.assertEqual(booking_id, SINGLE_ID)
        self.assertEqual(body, {"holdId": HOLD_ID, "day": "2026-10-22", "reason": SENTENCE})
        # No Idempotency-Key: each try holds afresh (see the view).
        self.assertEqual(kw, {"authorization": "Bearer customer-token", "tenant_id": TENANT})
        s.release_hold.assert_not_called()

    def test_the_hold_and_the_move_are_on_booking_apis_clock(self):
        # 09:00 in Dubai is 11:00 at booking-api: minute 660.
        with self.seams() as s:
            self.post({"date": "2026-10-22", "time": "09:00"})
        body, _ = self.held(s)
        self.assertEqual((body["day"], body["startMin"]), ("2026-10-22", 660))
        self.assertEqual(self.moved(s)[1]["day"], "2026-10-22")

    def test_only_the_body_built_here_goes_to_booking_api(self):
        sent = {**MOVE, "reason": "MOVING", "holdId": str(uuid.uuid4()), "nowMs": 1,
                "actor": "staff", "staffId": STRANGER}
        with self.seams() as s:
            response = self.post(sent, HTTP_IDEMPOTENCY_KEY="tap-1")
        self.assertEqual(response.status_code, 201, response.data)
        self.assertEqual(self.held(s)[0]["staffId"], MAYA)
        _, body, kw = self.moved(s)
        self.assertEqual(body, {"holdId": HOLD_ID, "day": "2026-10-22", "reason": SENTENCE})
        self.assertNotIn("idempotency_key", kw)

    def test_the_lookup_is_the_callers_own_upcoming_shelf(self):
        with self.seams() as s:
            self.post(MOVE, HTTP_X_TENANT_ID="tenant-from-app")
        call = s.list_bookings.call_args
        self.assertEqual(call.args[0], "filter=upcoming&page=1&pageSize=50")
        self.assertEqual(call.kwargs, {"authorization": "Bearer customer-token",
                                       "tenant_id": "tenant-from-app", "timeout": 3})

    def test_the_route_is_wired(self):
        match = resolve(PATH)
        self.assertIs(match.func.view_class, SingleBookingRescheduleView)
        self.assertEqual(match.kwargs, {"booking_id": uuid.UUID(SINGLE_ID)})


# ------------------------------------------------------------ the stylist


@override_settings(SINGLE_BOOKING_ACTIONS_V1=True)
class StylistTests(Seams, SimpleTestCase):

    def test_left_out_or_null_the_booking_keeps_its_own_stylist(self):
        for body in (MOVE, {**MOVE, "stylist_id": None}):
            with self.subTest(body=body), self.seams() as s:
                response = self.post(body)
                self.assertEqual(response.status_code, 201, response.data)
                self.assertEqual(self.held(s)[0]["staffId"], MAYA)

    def test_another_stylist_on_the_roster_who_does_the_services_is_held(self):
        with self.seams() as s:
            response = self.post({**MOVE, "stylist_id": ANYA})
        self.assertEqual(response.status_code, 201, response.data)
        self.assertEqual(self.held(s)[0]["staffId"], ANYA)

    def test_a_stylist_off_the_roster_is_foreign_id_and_nothing_is_held(self):
        with self.seams() as s:
            response = self.post({**MOVE, "stylist_id": STRANGER})
        self.assert_refused(response, "foreign_id", "stylist_id")
        self.assert_nothing_held(s)

    def test_a_stylist_who_cannot_do_the_services_is_stylist_mismatch(self):
        upcoming = shelf([single_row(services=(CUT, NAILS))])
        with self.seams(single={"list_bookings": mock.Mock(return_value=(200, upcoming))}) as s:
            response = self.post({**MOVE, "stylist_id": ANYA})
        self.assert_refused(response, "stylist_mismatch", "stylist_id")
        self.assert_nothing_held(s)

    def test_two_stylists_on_one_booking_is_422_before_any_other_call(self):
        upcoming = shelf([single_row(stylists=(MAYA, ANYA), services=(CUT, NAILS))])
        for body in (MOVE, {**MOVE, "stylist_id": MAYA}, {"time": "not a time"}):
            with self.subTest(body=body), self.seams(
                    single={"list_bookings": mock.Mock(return_value=(200, upcoming))}) as s:
                response = self.post(body)
                self.assertEqual(response.status_code, 422, response.data)
                self.assertEqual(response.data["errors"], [{
                    "field": None, "code": "multiple_stylists",
                    "message": "This booking has more than one stylist, so it cannot be "
                               "moved in the app. Please contact the salon.",
                }])
                # Only the lookup was asked: no salon, no clock, no hold.
                s.list_bookings.assert_called_once()
                s.salon_cards_for_refs.assert_not_called()
                s.engine_clock.assert_not_called()
                self.assert_nothing_held(s)

    def test_the_same_stylist_twice_on_the_row_is_one_stylist(self):
        upcoming = shelf([single_row(stylists=(MAYA, MAYA))])
        with self.seams(single={"list_bookings": mock.Mock(return_value=(200, upcoming))}) as s:
            response = self.post(MOVE)
        self.assertEqual(response.status_code, 201, response.data)
        self.assertEqual(self.held(s)[0]["staffId"], MAYA)

    def test_no_stylist_on_the_booking_and_none_sent_is_never_anyone_free(self):
        upcoming = shelf([single_row(stylists=())])
        with self.seams(single={"list_bookings": mock.Mock(return_value=(200, upcoming))}) as s:
            response = self.post(MOVE)
            self.assert_refused(response, "stylist_required", "stylist_id")
            self.assert_nothing_held(s)
            # A stylist sent is enough.
            response = self.post({**MOVE, "stylist_id": MAYA})
        self.assertEqual(response.status_code, 201, response.data)
        self.assertEqual(self.held(s)[0]["staffId"], MAYA)


# ------------------------------------------------------------ the services


@override_settings(SINGLE_BOOKING_ACTIONS_V1=True)
class ServiceTests(Seams, SimpleTestCase):

    def test_a_service_no_longer_offered_is_foreign_id_and_nothing_is_held(self):
        with self.seams(group={"service_timing_rows": mock.Mock(return_value=[])}) as s:
            response = self.post(MOVE)
        self.assert_refused(response, "foreign_id", "services")
        self.assert_nothing_held(s)

    def test_a_service_id_that_is_not_ours_or_none_at_all_is_foreign_id(self):
        for services in (("svc_fade",), (CUT, "svc_fade"), ()):
            upcoming = shelf([single_row(services=services)])
            with self.subTest(services=services), self.seams(
                    single={"list_bookings": mock.Mock(return_value=(200, upcoming))}) as s:
                response = self.post(MOVE)
                self.assert_refused(response, "foreign_id", "services")
                self.assert_nothing_held(s)

    def test_the_hold_names_the_services_as_booking_api_listed_them(self):
        shouted = CUT.upper()
        upcoming = shelf([single_row(services=(shouted,))])
        with self.seams(single={"list_bookings": mock.Mock(return_value=(200, upcoming))}) as s:
            response = self.post(MOVE)
        self.assertEqual(response.status_code, 201, response.data)
        self.assertEqual(self.held(s)[0]["services"], [shouted])


# ------------------------------------------------------------ the body


@override_settings(SINGLE_BOOKING_ACTIONS_V1=True)
class BodyTests(Seams, SimpleTestCase):

    def test_a_bad_date_or_time_is_422_and_nothing_is_held(self):
        cases = [
            ({"time": "19:00"}, "date"),
            ({"date": "2026-10-22"}, "time"),
            ({"date": "22/10/2026", "time": "19:00"}, "date"),
            ({"date": "2026-10-22", "time": "7pm"}, "time"),
            ({"date": "2026-10-22", "time": "24:00"}, "time"),
            ({**MOVE, "stylist_id": "maya"}, "stylist_id"),
        ]
        for body, field in cases:
            with self.subTest(body=body), self.seams() as s:
                response = self.post(body)
                self.assertEqual(response.status_code, 422, response.data)
                self.assertEqual(response.data["errors"][0]["field"], field)
                s.engine_clock.assert_not_called()
                self.assert_nothing_held(s)

    def test_a_body_that_is_not_json_is_415_for_a_single_booking(self):
        with self.seams() as s:
            response = self.post("date=2026-10-22", content_type="application/x-www-form-urlencoded")
        self.assertEqual(response.status_code, 415)
        self.assert_nothing_held(s)


# ------------------------------------------------------------ the hours


@override_settings(SINGLE_BOOKING_ACTIONS_V1=True)
class HoursTests(Seams, SimpleTestCase):
    """The new time held to the salon's hours, before booking-api is asked to hold or move."""

    def test_outside_the_hours_is_refused_before_booking_api_is_asked(self):
        for when in ("20:15", "08:30"):
            with self.subTest(when=when), self.seams() as s:
                response = self.post({"date": "2026-10-22", "time": when})
                self.assert_refused(response, "outside_hours", "time")
                self.assertEqual(
                    response.data["errors"][0]["message"],
                    f"A visit on 2026-10-22 at {when} would not start and finish within the salon's hours.",
                )
                self.assert_nothing_held(s)

    def test_a_closed_day(self):
        with self.seams(hours=opens(closed=("2026-10-22",))) as s:
            response = self.post(MOVE)
        self.assert_refused(response, "salon_closed", "time")
        self.assert_nothing_held(s)

    def test_a_time_already_past(self):
        with self.seams() as s:
            response = self.post({"date": "2026-09-30", "time": "16:00"})
        self.assert_refused(response, "too_soon", "time")
        self.assert_nothing_held(s)

    def test_the_services_notice_counts(self):
        # Nails asks 30 minutes' notice: 09:15 today is too soon, 09:30 is not.
        upcoming = shelf([single_row(services=(NAILS,))])
        lookup = {"list_bookings": mock.Mock(return_value=(200, upcoming))}
        with self.seams(single=lookup) as s:
            response = self.post({"date": "2026-10-01", "time": "09:15"})
            self.assert_refused(response, "too_soon", "time")
            self.assert_nothing_held(s)
        with self.seams(single=lookup) as s:
            response = self.post({"date": "2026-10-01", "time": "09:30"})
        self.assertEqual(response.status_code, 201, response.data)

    def test_booking_api_down_for_its_clock_is_503_and_nothing_is_held(self):
        with self.seams(group={"engine_clock": mock.Mock(side_effect=BookingApiUnavailable("x"))}) as s:
            response = self.post(MOVE)
        self.assertEqual(response.status_code, 503)
        self.assert_nothing_held(s)


# ------------------------------------------------------------ the hold


@override_settings(SINGLE_BOOKING_ACTIONS_V1=True)
class HoldTests(Seams, SimpleTestCase):

    def test_booking_apis_400_for_a_start_it_will_not_take_is_our_422_on_time(self):
        refused = {"statusCode": 400, "message": ["startMin must not be greater than 1320"],
                   "error": "Bad Request", "code": "BOOKING_VALIDATION_FAILED"}
        with self.seams(single={"place_hold": mock.Mock(return_value=(400, refused))}) as s:
            with self.assertLogs("apps.salons.single_views", level="WARNING"):
                response = self.post(MOVE)
        self.assertEqual(response.status_code, 422, response.data)
        self.assertEqual(response.data, {
            "detail": "Please correct the highlighted fields.",
            "code": "validation_error",
            "errors": [{
                "field": "time", "code": "outside_hours",
                "message": "A visit on 2026-10-22 at 19:00 would not start and finish within the salon's hours.",
            }],
        })
        s.reschedule_booking.assert_not_called()
        s.release_hold.assert_not_called()

    def test_any_other_400_on_the_hold_is_our_422_too(self):
        others = [
            {"statusCode": 400, "message": ["pick at least one service"], "error": "Bad Request",
             "code": "BOOKING_VALIDATION_FAILED"},
            {"statusCode": 400, "message": "Bad Request"},
            None,
        ]
        for refused in others:
            with self.subTest(refused=refused), self.seams(
                    single={"place_hold": mock.Mock(return_value=(400, refused))}) as s:
                with self.assertLogs("apps.salons.single_views", level="WARNING"):
                    response = self.post(MOVE)
                self.assertEqual(response.status_code, 422, response.data)
                message = "This booking could not be moved to that time. Please pick another time."
                self.assertEqual(response.data, {
                    "detail": message, "code": "validation_error",
                    "errors": [{"field": None, "code": "reschedule_refused", "message": message}],
                })
                s.reschedule_booking.assert_not_called()

    def test_a_hold_refused_otherwise_comes_back_as_it_came_and_nothing_moves(self):
        answers = [
            (409, {"statusCode": 409, "code": "BOOKING_SLOT_TAKEN",
                   "message": "Someone took this while you were deciding.", "details": {"offers": []}}),
            (409, {"statusCode": 409, "code": "BOOKING_STAFF_UNAVAILABLE",
                   "message": "19:00 is no longer available. Offers have refreshed."}),
            (422, {"statusCode": 422, "code": "BOOKING_STAFF_UNKNOWN",
                   "message": "That stylist does not work at this salon."}),
            (404, {"statusCode": 404, "message": "One or more services do not exist"}),
        ]
        for code, body in answers:
            with self.subTest(code=code, body=body), self.seams(
                    single={"place_hold": mock.Mock(return_value=(code, body))}) as s:
                response = self.post(MOVE)
                self.assertEqual((response.status_code, response.data), (code, body))
                s.reschedule_booking.assert_not_called()
                s.release_hold.assert_not_called()

    def test_booking_api_down_on_the_hold_is_503_and_nothing_moves(self):
        with self.seams(single={"place_hold": mock.Mock(side_effect=BookingApiUnavailable("x"))}) as s:
            response = self.post(MOVE)
        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.data["errors"][0]["code"], "booking_api_unavailable")
        s.reschedule_booking.assert_not_called()

    def test_a_hold_answer_without_its_id_is_503_and_nothing_moves(self):
        for body in ({"expiresInSeconds": 900}, {"holdId": None}, None):
            with self.subTest(body=body), self.seams(
                    single={"place_hold": mock.Mock(return_value=(201, body))}) as s:
                with self.assertLogs("apps.salons.single_views", level="WARNING"):
                    response = self.post(MOVE)
                self.assertEqual(response.status_code, 503)
                s.reschedule_booking.assert_not_called()


# ------------------------------------------------------------ the move failing


@override_settings(SINGLE_BOOKING_ACTIONS_V1=True)
class MoveFailsTests(Seams, SimpleTestCase):
    """The hold is placed, the move fails: the slot goes back at once, and booking-api's error comes back."""

    def test_the_hold_is_released_and_booking_apis_error_comes_back(self):
        answers = [
            (409, {"statusCode": 409, "code": "BOOKING_STATE_INVALID",
                   "message": "A checked_in booking cannot be moved."}),
            (410, {"statusCode": 410, "code": "BOOKING_HOLD_EXPIRED",
                   "message": "That slot went while you were deciding. The original booking is untouched."}),
            (404, NOT_FOUND),
            (422, {"statusCode": 422, "code": "BOOKING_SERIAL_RESCHEDULE", "message": "Too many moves."}),
            (500, {"statusCode": 500, "code": "INTERNAL", "message": "Something went wrong."}),
        ]
        for code, body in answers:
            with self.subTest(code=code), self.seams(
                    single={"reschedule_booking": mock.Mock(return_value=(code, body))}) as s:
                response = self.post(MOVE)
                self.assertEqual((response.status_code, response.data), (code, body))
                s.release_hold.assert_called_once_with(
                    HOLD_ID, authorization="Bearer customer-token", tenant_id=TENANT)
                # Released after the move was tried, never before.
                self.assertEqual(s.reschedule_booking.call_count, 1)

    def test_booking_api_down_on_the_move_releases_the_hold_and_is_503(self):
        with self.seams(single={"reschedule_booking": mock.Mock(side_effect=BookingApiUnavailable("x"))}) as s:
            response = self.post(MOVE)
        self.assertEqual(response.status_code, 503)
        s.release_hold.assert_called_once_with(
            HOLD_ID, authorization="Bearer customer-token", tenant_id=TENANT)

    def test_a_release_that_fails_still_answers_the_moves_error(self):
        failures = {
            "unreachable": mock.Mock(side_effect=BookingApiUnavailable("x")),
            "refused": mock.Mock(return_value=(500, None)),
        }
        refused = (409, {"statusCode": 409, "code": "BOOKING_STATE_INVALID", "message": "No."})
        for name, release in failures.items():
            with self.subTest(name), self.seams(single={
                    "reschedule_booking": mock.Mock(return_value=refused), "release_hold": release}):
                with self.assertLogs("apps.salons.single_views", level="WARNING") as logs:
                    response = self.post(MOVE)
                self.assertEqual((response.status_code, response.data), refused)
                self.assertIn(HOLD_ID, logs.output[0])

    def test_a_move_that_lands_releases_nothing(self):
        for code in (200, 201):
            with self.subTest(code=code), self.seams(
                    single={"reschedule_booking": mock.Mock(return_value=(code, dict(MOVED)))}) as s:
                response = self.post(MOVE)
                self.assertEqual((response.status_code, response.data), (code, MOVED))
                s.release_hold.assert_not_called()


# ------------------------------------------------------------ not a single booking


@override_settings(SINGLE_BOOKING_ACTIONS_V1=True)
class NotSingleTests(Seams, SimpleTestCase):
    """Every other id answers as this path did before the route: 404, nothing asked of booking-api."""

    def assert_untouched(self, response, s):
        self.assertEqual(response.status_code, 404, response.data)
        self.assertEqual(response.data["code"], "not_found")
        s.salon_cards_for_refs.assert_not_called()
        s.engine_clock.assert_not_called()
        self.assert_nothing_held(s)

    def test_someone_elses_booking_is_404(self):
        # Not on the caller's own shelf, whatever booking-api would say of it.
        with self.seams() as s:
            response = self.post(MOVE, SOMEONE_ELSES)
        self.assert_untouched(response, s)

    def test_a_party_and_a_routine_are_untouched(self):
        for booking_id in (GROUP_ID, SESSION_ID, ROUTINE_ID):
            with self.subTest(booking_id=booking_id), self.seams() as s:
                response = self.post(MOVE, booking_id)
                self.assert_untouched(response, s)

    def test_a_party_with_a_body_that_is_not_json_is_still_404(self):
        with self.seams() as s:
            response = self.post("x", GROUP_ID, content_type="text/plain")
        self.assert_untouched(response, s)

    def test_a_lookup_that_fails_is_404(self):
        failures = {
            "refused": mock.Mock(return_value=(401, {"statusCode": 401, "message": "Invalid token"})),
            "5xx": mock.Mock(return_value=(500, None)),
            "unreachable": mock.Mock(side_effect=BookingApiUnavailable("x")),
        }
        for name, lookup in failures.items():
            with self.subTest(name), self.seams(single={"list_bookings": lookup}) as s:
                response = self.post(MOVE)
                self.assert_untouched(response, s)

    def test_a_salon_that_cannot_be_resolved_is_404(self):
        with self.seams(single={"salon_cards_for_refs": mock.Mock(return_value={})}) as s:
            response = self.post(MOVE)
        self.assertEqual(response.status_code, 404)
        self.assert_nothing_held(s)


@override_settings(SINGLE_BOOKING_ACTIONS_V1=False)
class FlagOffTests(Seams, SimpleTestCase):

    def test_off_every_id_is_404_and_nothing_is_asked(self):
        for booking_id in (SINGLE_ID, GROUP_ID, SESSION_ID):
            with self.subTest(booking_id=booking_id), self.seams() as s:
                response = self.post(MOVE, booking_id)
                self.assertEqual(response.status_code, 404)
                s.list_bookings.assert_not_called()
                s.engine_clock.assert_not_called()
                self.assert_nothing_held(s)


# ------------------------------------------------------------ the client


class ClientTests(SimpleTestCase):
    """The three booking-api requests, through the real client."""

    def sent(self, call, *args, **kwargs):
        response = mock.MagicMock(status=201)
        response.read.return_value = b'{"ok": true}'
        response.__enter__.return_value = response
        with mock.patch("apps.salons.booking_api.urllib.request.urlopen", return_value=response) as urlopen:
            answer = call(*args, **kwargs)
        self.assertEqual(answer, (201, {"ok": True}))
        return urlopen.call_args.args[0]

    def test_the_hold(self):
        request = self.sent(booking_api.place_hold, {"day": "2026-10-22"},
                            authorization="Bearer t", tenant_id=TENANT)
        self.assertEqual((request.get_method(), request.full_url.rsplit("/v1", 1)[1]), ("POST", "/holds"))
        self.assertEqual(json.loads(request.data), {"day": "2026-10-22"})
        self.assertEqual(request.get_header("X-tenant-id"), TENANT)
        self.assertEqual(request.get_header("Authorization"), "Bearer t")

    def test_the_release(self):
        request = self.sent(booking_api.release_hold, HOLD_ID, authorization="Bearer t", tenant_id=TENANT)
        self.assertEqual((request.get_method(), request.full_url.rsplit("/v1", 1)[1]),
                         ("DELETE", f"/holds/{HOLD_ID}"))
        self.assertIsNone(request.data)
        self.assertEqual(request.get_header("X-tenant-id"), TENANT)

    def test_the_move(self):
        request = self.sent(booking_api.reschedule_booking, uuid.UUID(SINGLE_ID),
                            {"holdId": HOLD_ID, "day": "2026-10-22", "reason": SENTENCE},
                            authorization="Bearer t", tenant_id=TENANT)
        self.assertEqual((request.get_method(), request.full_url.rsplit("/v1", 1)[1]),
                         ("POST", f"/bookings/{SINGLE_ID}/reschedule"))
        self.assertEqual(json.loads(request.data),
                         {"holdId": HOLD_ID, "day": "2026-10-22", "reason": SENTENCE})
        self.assertEqual(request.get_header("Content-type"), "application/json")
