"""
The routine contract's move, step C5 (docs/ROUTINE_FE_CONTRACT_AUDIT.md):

    PATCH /api/v1/booking/<id>/sessions/<session_id>
        ->  GET   /v1/mobile-booking/series/:id?view=booking   (the routine first)
        ->  PATCH /v1/mobile-booking/series/:id                (RESCHEDULE, on booking-api's clock)
        ->  GET   /v1/mobile-booking/series/:id?view=booking   (read back, a real move only)

Through the view, with the old routine tests' seams: no database, no
network. The salon is in Dubai (+04:00) and booking-api reads every branch
at +06:00. The salon opens 09:00 to 21:00 unless a test says otherwise;
booking-api's day ends at 20:00 in Dubai. Now is 1 October 2026, 09:00.
"""

import json
import types
import uuid
from contextlib import contextmanager
from unittest import mock

from django.test import SimpleTestCase, override_settings
from rest_framework.test import APIRequestFactory, force_authenticate

from apps.salons import routine_contract as rc
from apps.salons.booking_api import BookingApiUnavailable
from apps.salons.routine_views import RoutineSessionMoveView
from apps.salons.test_group_booking import (
    ANYA,
    BRANCH,
    CLOCK,
    COVERAGE,
    CUT,
    MAYA,
    NAILS,
    SALON,
    SALON_ID,
    STRANGER,
    STYLISTS,
    TENANT,
    TIMING,
    Customer,
)
from apps.salons.test_routine_contract import DUBAI, ROUTINE_ID, SESSION_ID, engine_routine
from apps.salons.test_series_booking import CARD, NOW, opens
from apps.salons.views import _salon_card

PATH = f"/api/v1/booking/{ROUTINE_ID}/sessions/{SESSION_ID}"
# Thursday 22 October 2026, 19:00 in Dubai: 21:00 at booking-api.
NEW_START = "2026-10-22T19:00:00+04:00"


def moved_routine():
    """The routine as booking-api reads it after the move."""
    routine = engine_routine()
    routine["sessions"][0] = {
        **routine["sessions"][0],
        "date": "2026-10-22", "start_time": "2026-10-22T21:00:00+06:00",
        "end_time": "2026-10-22T21:45:00+06:00",
    }
    return routine


def refused_by_booking_api(status, field, code, message="Because."):
    return (status, {
        "detail": message if status == 409 else "Please correct the highlighted fields.",
        "code": "validation_error" if status == 422 else code,
        "errors": [{"field": field, "code": code, "message": message}],
    })


class Seams:
    @contextmanager
    def seams(self, *, group=None, routine=None, hours=None):
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
        routine_fakes = {
            # The routine first, then (a real move) the routine read back.
            "read_routine_booking": mock.Mock(side_effect=[
                (200, engine_routine()), (200, moved_routine()),
            ]),
            "manage_series_booking": mock.Mock(return_value=(200, {"id": ROUTINE_ID})),
            "salon_cards_for_refs": mock.Mock(return_value={BRANCH: CARD}),
            "_now": mock.Mock(side_effect=lambda tz: NOW.astimezone(tz)),
        }
        routine_fakes.update(routine or {})
        patches = [mock.patch(f"apps.salons.group_views.{n}", f) for n, f in group_fakes.items()]
        patches += [mock.patch(f"apps.salons.routine_views.{n}", f) for n, f in routine_fakes.items()]
        patches.append(mock.patch("apps.salons.series_views._open_span", hours or opens()))
        for p in patches:
            p.start()
        try:
            yield types.SimpleNamespace(**group_fakes, **routine_fakes)
        finally:
            for p in patches:
                p.stop()

    def patch(self, body, *, content_type="application/json", **headers):
        request = APIRequestFactory().patch(
            PATH, data=json.dumps(body) if isinstance(body, dict) else body,
            content_type=content_type, HTTP_AUTHORIZATION="Bearer customer-token", **headers,
        )
        force_authenticate(request, user=Customer())
        return RoutineSessionMoveView.as_view()(
            request, booking_id=uuid.UUID(ROUTINE_ID), session_id=uuid.UUID(SESSION_ID),
        )

    def sent(self, s):
        """What booking-api's PATCH got: (routine id, body, kwargs)."""
        (routine_id, body), kw = s.manage_series_booking.call_args
        return routine_id, body, kw

    def assert_refused(self, response, code, field, status=422):
        self.assertEqual(response.status_code, status, response.data)
        self.assertEqual(response.data["errors"][0]["code"], code)
        self.assertEqual(response.data["errors"][0]["field"], field)


def presented(routine):
    out = rc.present_routine(
        routine, salon_id=SALON_ID, tz=DUBAI, avatars={MAYA: None, ANYA: "https://cdn/anya.png"},
    )
    out["salon"] = _salon_card(CARD, full=True)
    return out


@override_settings(ROUTINE_CONTRACT_V1=False)
class FlagOffTests(Seams, SimpleTestCase):
    def test_off_404_and_nothing_is_asked(self):
        with self.seams() as s:
            response = self.patch({"start_time": NEW_START})
        self.assertEqual(response.status_code, 404)
        s.read_routine_booking.assert_not_called()
        s.manage_series_booking.assert_not_called()


@override_settings(ROUTINE_CONTRACT_V1=True)
class MoveTests(Seams, SimpleTestCase):
    def test_a_real_move_the_old_reschedule_on_booking_apis_clock_then_the_routine(self):
        with self.seams() as s:
            response = self.patch({"start_time": NEW_START}, HTTP_IDEMPOTENCY_KEY="move-1")
        self.assertEqual(response.status_code, 200, response.data)
        routine_id, body, kw = self.sent(s)
        self.assertEqual(routine_id, ROUTINE_ID)
        self.assertEqual(body, {
            "action": "RESCHEDULE", "session_id": SESSION_ID,
            "date": "2026-10-22", "time": "21:00", "dry_run": False,
        })
        self.assertEqual(kw, {
            "authorization": "Bearer customer-token", "idempotency_key": "move-1",
            "tenant_id": TENANT,
        })
        # The whole routine, read back after the move, as GET /booking/{id} reads it.
        self.assertEqual(response.data, presented(moved_routine()))
        self.assertEqual(response.data["sessions"][0]["start_time"], NEW_START)
        self.assertEqual(s.read_routine_booking.call_count, 2)

    def test_the_same_move_in_plus_four_plus_six_and_z(self):
        answers, bodies = [], []
        for start in (NEW_START, "2026-10-22T21:00:00+06:00", "2026-10-22T15:00:00Z"):
            with self.seams() as s:
                answers.append(self.patch({"start_time": start}).data)
                bodies.append(self.sent(s)[1])
        self.assertEqual(bodies[1], bodies[0])
        self.assertEqual(bodies[2], bodies[0])
        self.assertEqual(answers[1], answers[0])
        self.assertEqual(answers[2], answers[0])

    def test_a_dry_run_sends_no_key_moves_nothing_and_answers_the_routine_as_it_is(self):
        with self.seams() as s:
            response = self.patch({"start_time": NEW_START, "dry_run": True},
                                  HTTP_IDEMPOTENCY_KEY="move-1")
        self.assertEqual(response.status_code, 200, response.data)
        _, body, kw = self.sent(s)
        self.assertEqual(body["dry_run"], True)
        self.assertIsNone(kw["idempotency_key"])
        # Read once: the routine as it is now, not read back.
        self.assertEqual(s.read_routine_booking.call_count, 1)
        self.assertEqual(response.data, presented(engine_routine()))

    def test_a_real_move_without_a_key_is_sent_without_one_never_made_up(self):
        with self.seams() as s:
            self.patch({"start_time": NEW_START})
        self.assertIsNone(self.sent(s)[2]["idempotency_key"])

    def test_dry_run_is_true_or_false_only(self):
        for value in ("true", 1, None, "yes"):
            with self.subTest(value=value), self.seams() as s:
                response = self.patch({"start_time": NEW_START, "dry_run": value})
                self.assert_refused(response, "invalid_dry_run", "dry_run")
                s.read_routine_booking.assert_not_called()
                s.manage_series_booking.assert_not_called()

    def test_no_offset_is_invalid_time_before_booking_api(self):
        with self.seams() as s:
            response = self.patch({"start_time": "2026-10-22T19:00:00"})
        self.assert_refused(response, "invalid_time", "start_time")
        s.read_routine_booking.assert_not_called()

    def test_json_only(self):
        with self.seams() as s:
            response = self.patch("start_time=x", content_type="application/x-www-form-urlencoded")
        self.assertEqual(response.status_code, 415)
        s.read_routine_booking.assert_not_called()


@override_settings(ROUTINE_CONTRACT_V1=True)
class HoursTests(Seams, SimpleTestCase):
    """The new time held to the salon's own hours, before booking-api is asked to move."""

    def test_outside_the_hours(self):
        with self.seams() as s:
            response = self.patch({"start_time": "2026-10-22T20:15:00+04:00"})
        self.assert_refused(response, "outside_hours", "start_time")
        self.assertEqual(response.data["errors"][0]["message"],
                         "A visit on 2026-10-22 at 20:15 would not start and finish within the salon's hours.")
        s.manage_series_booking.assert_not_called()

    def test_a_closed_day(self):
        with self.seams(hours=opens(closed=("2026-10-22",))) as s:
            response = self.patch({"start_time": NEW_START})
        self.assert_refused(response, "salon_closed", "start_time")
        s.manage_series_booking.assert_not_called()

    def test_a_time_already_past(self):
        with self.seams() as s:
            response = self.patch({"start_time": "2026-09-30T16:00:00+04:00"})
        self.assert_refused(response, "too_soon", "start_time")
        s.manage_series_booking.assert_not_called()

    def test_outside_the_hours_and_too_soon_says_outside_hours_first(self):
        with self.seams() as s:
            response = self.patch({"start_time": "2026-09-30T21:30:00+04:00"})
        self.assert_refused(response, "outside_hours", "start_time")
        s.manage_series_booking.assert_not_called()


@override_settings(ROUTINE_CONTRACT_V1=True)
class StylistTests(Seams, SimpleTestCase):
    def test_a_stylist_on_the_roster_who_does_the_services_goes_on(self):
        with self.seams() as s:
            self.patch({"start_time": NEW_START, "stylist_id": ANYA})
        self.assertEqual(self.sent(s)[1]["stylist_id"], ANYA)

    def test_none_the_session_keeps_its_stylist(self):
        with self.seams() as s:
            self.patch({"start_time": NEW_START, "stylist_id": None})
        self.assertNotIn("stylist_id", self.sent(s)[1])

    def test_a_stylist_off_the_roster_is_foreign_id(self):
        with self.seams() as s:
            response = self.patch({"start_time": NEW_START, "stylist_id": STRANGER})
        self.assert_refused(response, "foreign_id", "stylist_id")
        s.manage_series_booking.assert_not_called()

    def test_a_stylist_who_cannot_do_the_routines_services_is_stylist_mismatch(self):
        routine = engine_routine()
        routine["services"] = [{"id": CUT, "name": "Cut", "amount": 100},
                               {"id": NAILS, "name": "Nails", "amount": 150}]
        with self.seams(routine={"read_routine_booking": mock.Mock(return_value=(200, routine))}) as s:
            response = self.patch({"start_time": NEW_START, "stylist_id": ANYA})
        self.assert_refused(response, "stylist_mismatch", "stylist_id")
        s.manage_series_booking.assert_not_called()


@override_settings(ROUTINE_CONTRACT_V1=True)
class RefusalTests(Seams, SimpleTestCase):
    def refused(self, answer):
        with self.seams(routine={"manage_series_booking": mock.Mock(return_value=answer)}) as s:
            response = self.patch({"start_time": NEW_START})
        return response, s

    def test_not_free_is_slot_taken_409_on_start_time(self):
        response, s = self.refused(refused_by_booking_api(
            409, "time", "session_not_free", "That time is not free. Please pick another."))
        self.assert_refused(response, "slot_taken", "start_time", status=409)
        self.assertEqual(response.data["code"], "slot_taken")
        self.assertEqual(s.read_routine_booking.call_count, 1)   # nothing read back

    def test_a_session_not_in_this_routine_is_404(self):
        response, _ = self.refused(refused_by_booking_api(
            422, "session_id", "invalid_sessions", "That session is not part of this routine."))
        self.assertEqual(response.status_code, 404)
        self.assertEqual(response.data["code"], "not_found")

    def test_someone_elses_routine_is_404_and_nothing_is_asked_to_move(self):
        not_yours = refused_by_booking_api(404, "id", "not_found", "No such booking.")
        with self.seams(routine={"read_routine_booking": mock.Mock(return_value=not_yours)}) as s:
            response = self.patch({"start_time": NEW_START})
        self.assertEqual(response.status_code, 404)
        self.assertEqual(response.data["code"], "not_found")
        s.manage_series_booking.assert_not_called()

    def test_session_locked_in_our_envelope(self):
        response, _ = self.refused(refused_by_booking_api(
            422, "session_id", "session_locked",
            "A session cannot be skipped or moved in the 24 hours before it starts."))
        self.assertEqual(response.status_code, 422)
        self.assertEqual(response.data["code"], "validation_error")
        self.assertEqual(response.data["errors"][0]["code"], "session_locked")
        self.assertIsNone(response.data["errors"][0]["field"])

    def test_session_day_taken_on_start_time(self):
        response, _ = self.refused(refused_by_booking_api(
            422, "date", "session_day_taken", "Another session of this routine is already on that day."))
        self.assert_refused(response, "session_day_taken", "start_time")

    def test_reschedule_out_of_range_on_start_time(self):
        response, _ = self.refused(refused_by_booking_api(422, "date", "reschedule_out_of_range"))
        self.assert_refused(response, "reschedule_out_of_range", "start_time")


@override_settings(ROUTINE_CONTRACT_V1=True)
class AfterTheMoveTests(Seams, SimpleTestCase):
    def test_the_read_back_failing_after_a_real_move_still_answers_200(self):
        failures = {
            "an error answer": mock.Mock(side_effect=[(200, engine_routine()), (500, None)]),
            "booking-api down": mock.Mock(side_effect=[
                (200, engine_routine()), BookingApiUnavailable("down")]),
        }
        for name, read in failures.items():
            with self.subTest(name=name), self.seams(routine={"read_routine_booking": read}):
                with self.assertLogs("apps.salons.routine_views", level="WARNING") as logs:
                    response = self.patch({"start_time": NEW_START})
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.data, {"id": ROUTINE_ID, "booking_type": "ROUTINE"})
            self.assertIn(f"routine {ROUTINE_ID} is moved", logs.output[0])

    def test_booking_api_down_before_the_move_is_503(self):
        down = mock.Mock(side_effect=BookingApiUnavailable("down"))
        for fakes in ({"read_routine_booking": down}, {"manage_series_booking": down}):
            with self.subTest(fakes=list(fakes)), self.seams(routine=fakes):
                response = self.patch({"start_time": NEW_START})
            self.assertEqual(response.status_code, 503)
            self.assertEqual(response.data["errors"][0]["code"], "booking_api_unavailable")
