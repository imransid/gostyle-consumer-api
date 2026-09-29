"""
The routine contract's preview, step C3 (docs/ROUTINE_FE_CONTRACT_AUDIT.md):

    POST /api/v1/booking/routine-preview  ->  POST /v1/mobile-booking/series (dry run)

Through the view, with the same seams as the old routine tests: no
database, no network. The salon is in Dubai (+04:00) and booking-api reads
every branch at +06:00. The salon opens 09:00 to 21:00 unless a test says
otherwise; booking-api's day ends at 20:00 in Dubai.
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
from apps.salons.routine_views import RoutinePreviewView
from apps.salons.test_group_booking import (
    ANYA,
    BRANCH,
    CLOCK,
    COVERAGE,
    CUT,
    ELSEWHERE,
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
from apps.salons.test_routine_contract import engine_preview, engine_session
from apps.salons.test_series_booking import NOW, opens

PATH = "/api/v1/booking/routine-preview"


def request_body(**over):
    """The app's preview: monthly, 2 sessions from Tuesday 6 October at 16:30 in Dubai."""
    body = {
        "salon_id": SALON_ID, "service_ids": [CUT], "stylist_id": MAYA,
        "start_time": "2026-10-06T16:30:00+04:00", "cadence": "week", "sessions": 2,
    }
    body.update(over)
    return {k: v for k, v in body.items() if v is not ...}


# booking-api's dry run for request_body(): the second session is busy.
PLAN = engine_preview(
    [
        engine_session(0, "2026-10-06"),
        engine_session(1, "2026-10-13", free=False, reason="stylist_unavailable", alternatives=(
            "2026-10-13T19:00:00+06:00", "2026-10-14T18:30:00+06:00",
        )),
    ],
    frequency="WEEKLY",
)


def booking_api(plan=PLAN, status=200):
    return mock.Mock(return_value=(status, json.loads(json.dumps(plan))))


class Seams:
    """Every seam the preview reaches, replaced: the old routes' own seams."""

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
            "create_series_booking": booking_api(),
            "_now": mock.Mock(side_effect=lambda tz: NOW.astimezone(tz)),
        }
        routine_fakes.update(routine or {})
        patches = [mock.patch(f"apps.salons.group_views.{n}", f) for n, f in group_fakes.items()]
        patches += [mock.patch(f"apps.salons.routine_views.{n}", f) for n, f in routine_fakes.items()]
        open_span = hours or opens()
        patches.append(mock.patch("apps.salons.series_views._open_span", open_span))
        for p in patches:
            p.start()
        try:
            yield types.SimpleNamespace(**group_fakes, **routine_fakes, _open_span=open_span)
        finally:
            for p in patches:
                p.stop()

    def post(self, body, *, content_type="application/json", **headers):
        request = APIRequestFactory().post(
            PATH, data=json.dumps(body) if isinstance(body, dict) else body,
            content_type=content_type, HTTP_AUTHORIZATION="Bearer customer-token", **headers,
        )
        force_authenticate(request, user=Customer())
        return RoutinePreviewView.as_view()(request)

    def sent(self, s):
        (body,), kw = s.create_series_booking.call_args
        return body, kw

    def assert_refused(self, response, code, field, status=422):
        self.assertEqual(response.status_code, status, response.data)
        self.assertEqual(response.data["errors"][0]["code"], code)
        self.assertEqual(response.data["errors"][0]["field"], field)


@override_settings(ROUTINE_CONTRACT_V1=False)
class FlagOffTests(Seams, SimpleTestCase):
    def test_off_404_and_booking_api_is_never_called(self):
        with self.seams() as s:
            response = self.post(request_body())
        self.assertEqual(response.status_code, 404)
        s.create_series_booking.assert_not_called()
        s.engine_clock.assert_not_called()
        s.salon_profile.assert_not_called()


@override_settings(ROUTINE_CONTRACT_V1=True)
class PreviewTests(Seams, SimpleTestCase):
    def test_the_whole_answer_for_a_normal_plan(self):
        with self.seams() as s:
            response = self.post(request_body())
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(response.data, {
            "cadence": "week",
            "sessions": [
                {
                    "index": 0, "date": "2026-10-06",
                    "start_time": "2026-10-06T16:30:00+04:00",
                    "end_time": "2026-10-06T17:15:00+04:00",
                    "available": True,
                    "stylist": {"id": MAYA, "name": "Maya E."},
                    "alternatives": [],
                },
                {
                    "index": 1, "date": "2026-10-13",
                    "start_time": "2026-10-13T16:30:00+04:00",
                    "end_time": "2026-10-13T17:15:00+04:00",
                    "available": False,
                    "reason": "stylist_unavailable",
                    "stylist": None,
                    "alternatives": ["2026-10-13T17:00:00+04:00", "2026-10-14T16:30:00+04:00"],
                },
            ],
            "per_session_total": 105.0,
            "plan_total": 210.0,
        })

    def test_what_booking_api_is_asked_a_dry_run_on_its_clock_never_with_a_key(self):
        with self.seams() as s:
            self.post(request_body(), HTTP_IDEMPOTENCY_KEY="app-key-1")
        body, kw = self.sent(s)
        self.assertEqual(body, {
            "dry_run": True, "salon_id": BRANCH, "services": [{"id": CUT}],
            "stylist_id": MAYA, "frequency": "WEEKLY",
            "start_date": "2026-10-06", "sessions": 2, "dates": None, "time": "18:30",
            "payment_plan": "PAY_AT_SALON", "picks": [], **rc.CONTRACT_OPTIONS,
        })
        self.assertEqual(kw, {
            "authorization": "Bearer customer-token", "idempotency_key": None,
            "tenant_id": TENANT,
        })
        self.assertEqual(s.create_series_booking.call_count, 1)

    def test_a_start_time_in_plus_six_or_z_gives_the_same_preview(self):
        answers, bodies = [], []
        for start in ("2026-10-06T16:30:00+04:00", "2026-10-06T18:30:00+06:00", "2026-10-06T12:30:00Z"):
            with self.seams() as s:
                answers.append(self.post(request_body(start_time=start)).data)
                bodies.append(self.sent(s)[0])
        self.assertEqual(answers[1], answers[0])
        self.assertEqual(answers[2], answers[0])
        self.assertEqual(bodies[1], bodies[0])
        self.assertEqual(bodies[2], bodies[0])

    def test_a_session_on_a_closed_day_its_alternatives_on_the_next_open_days(self):
        # booking-api does not know the salon shuts on Tuesday 13 October: it
        # finds the day free and offers two more times on it, then the next days.
        plan = engine_preview([
            engine_session(0, "2026-10-06"),
            engine_session(1, "2026-10-13", alternatives=(
                "2026-10-13T19:00:00+06:00",   # the closed day
                "2026-10-13T19:30:00+06:00",   # the closed day
                "2026-10-14T18:30:00+06:00",
                "2026-10-14T18:00:00+06:00",
                "2026-10-15T18:30:00+06:00",
                "2026-10-16T18:30:00+06:00",   # the fourth open one: not shown
            )),
        ], frequency="WEEKLY")
        with self.seams(routine={"create_series_booking": booking_api(plan)},
                        hours=opens(closed=("2026-10-13",))):
            response = self.post(request_body())
        closed = response.data["sessions"][1]
        self.assertEqual(
            (closed["date"], closed["available"], closed["reason"], closed["stylist"]),
            ("2026-10-13", False, "salon_closed", None),
        )
        self.assertEqual(closed["alternatives"], [
            "2026-10-14T16:30:00+04:00", "2026-10-14T16:00:00+04:00", "2026-10-15T16:30:00+04:00",
        ])
        self.assertEqual(response.data["sessions"][0]["available"], True)

    def test_a_closed_day_wins_over_a_busy_stylist(self):
        plan = engine_preview([
            engine_session(0, "2026-10-06"),
            engine_session(1, "2026-10-13", free=False, reason="stylist_unavailable"),
        ], frequency="WEEKLY")
        with self.seams(routine={"create_series_booking": booking_api(plan)},
                        hours=opens(closed=("2026-10-13",))):
            response = self.post(request_body())
        self.assertEqual(response.data["sessions"][1]["reason"], "salon_closed")

    def test_sessions_more_than_90_days_away_are_available_or_not(self):
        # Monthly from 6 October: 6 January (97 days) is busy, 6 February free.
        plan = engine_preview([
            engine_session(0, "2026-10-06"),
            engine_session(1, "2026-11-06"),
            engine_session(2, "2026-12-06"),
            {**engine_session(3, "2027-01-06", free=False, reason="stylist_unavailable"), "later": True},
            {**engine_session(4, "2027-02-06"), "later": True},
        ])
        with self.seams(routine={"create_series_booking": booking_api(plan)}) as s:
            response = self.post(request_body(cadence="month", sessions=5))
        far = response.data["sessions"][3:]
        self.assertEqual([(x["date"], x["available"]) for x in far],
                         [("2027-01-06", False), ("2027-02-06", True)])
        self.assertEqual(far[0]["reason"], "stylist_unavailable")
        self.assertEqual(self.sent(s)[0]["check_later"], True)

    def test_any_available_expert_sends_who_does_all_the_services(self):
        chosen = json.loads(json.dumps(PLAN))
        for session in chosen["sessions"]:
            session["stylist_id"] = ANYA
        chosen["stylist_id"] = ANYA
        with self.seams(routine={"create_series_booking": booking_api(chosen)}) as s:
            response = self.post(request_body(stylist_id=None))
        body, _ = self.sent(s)
        self.assertIsNone(body["stylist_id"])
        self.assertEqual(body["stylist_candidates"], sorted([MAYA, ANYA]))
        # The roster the coverage was measured on: everyone at the salon.
        (salon, services, staff), _ = s.stylist_service_coverage.call_args
        self.assertEqual((services, sorted(map(str, staff))),
                         ([uuid.UUID(CUT)], sorted([MAYA, ANYA])))
        self.assertEqual(response.data["sessions"][0]["stylist"], {"id": ANYA, "name": "Anya"})

    def test_any_available_expert_leaving_the_field_out_works_the_same(self):
        with self.seams() as s:
            self.post(request_body(stylist_id=...))
        self.assertEqual(self.sent(s)[0]["stylist_candidates"], sorted([MAYA, ANYA]))

    def test_nobody_able_is_no_stylist_available_and_booking_api_is_not_asked(self):
        nobody_does_nails = {uuid.UUID(MAYA): [uuid.UUID(CUT)], uuid.UUID(ANYA): [uuid.UUID(CUT)]}
        with self.seams(group={"stylist_service_coverage": mock.Mock(return_value=nobody_does_nails)}) as s:
            response = self.post(request_body(stylist_id=None, service_ids=[CUT, NAILS]))
        self.assert_refused(response, "no_stylist_available", "stylist_id")
        s.create_series_booking.assert_not_called()

    def test_sessions_1_is_booking_apis_invalid_session_count_in_our_envelope(self):
        refusal = {
            "detail": "Please correct the highlighted fields.", "code": "validation_error",
            "errors": [{"field": "sessions", "code": "invalid_session_count",
                        "message": "A routine is 2 to 6 sessions."}],
        }
        with self.seams(routine={"create_series_booking": booking_api(refusal, status=422)}):
            response = self.post(request_body(sessions=1))
        self.assertEqual(response.status_code, 422)
        self.assertEqual(response.data, refusal)

    def test_booking_apis_other_400_is_our_422(self):
        other = {"statusCode": 400, "message": ["sessions must be a number"],
                 "error": "Bad Request", "code": "VALIDATION_FAILED"}
        with self.seams(routine={"create_series_booking": booking_api(other, status=400)}):
            response = self.post(request_body())
        self.assertEqual(response.status_code, 422)
        self.assertEqual(response.data["code"], "validation_error")
        self.assertEqual(response.data["errors"][0]["field"], "sessions")

    def test_booking_apis_words_are_renamed(self):
        refusal = {
            "detail": "Please correct the highlighted fields.", "code": "validation_error",
            "errors": [{"field": "services", "code": "unknown_service", "message": "Not sold here."}],
        }
        with self.seams(routine={"create_series_booking": booking_api(refusal, status=422)}):
            response = self.post(request_body())
        self.assert_refused(response, "unknown_service", "service_ids")

    def test_booking_api_down_is_our_503(self):
        down = mock.Mock(side_effect=BookingApiUnavailable("down"))
        with self.seams(routine={"create_series_booking": down}):
            response = self.post(request_body())
        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.data["errors"][0]["code"], "booking_api_unavailable")


@override_settings(ROUTINE_CONTRACT_V1=True)
class OldChecksTests(Seams, SimpleTestCase):
    """The old routine create's checks, in its order, before booking-api is asked."""

    def test_json_only(self):
        with self.seams() as s:
            response = self.post("salon_id=x", content_type="application/x-www-form-urlencoded")
        self.assertEqual(response.status_code, 415)
        s.create_series_booking.assert_not_called()

    def test_an_unknown_salon_is_404(self):
        with self.seams(group={"salon_profile": mock.Mock(return_value=None)}) as s:
            response = self.post(request_body())
        self.assertEqual(response.status_code, 404)
        s.create_series_booking.assert_not_called()

    def test_a_service_not_sold_here_is_foreign_id(self):
        with self.seams() as s:
            response = self.post(request_body(service_ids=[CUT, ELSEWHERE]))
        self.assert_refused(response, "foreign_id", "service_ids")
        s.create_series_booking.assert_not_called()

    def test_no_services_is_no_services(self):
        with self.seams() as s:
            response = self.post(request_body(service_ids=[]))
        self.assert_refused(response, "no_services", "service_ids")
        s.create_series_booking.assert_not_called()

    def test_a_stylist_off_the_roster_is_foreign_id(self):
        with self.seams() as s:
            response = self.post(request_body(stylist_id=STRANGER))
        self.assert_refused(response, "foreign_id", "stylist_id")
        s.create_series_booking.assert_not_called()

    def test_a_stylist_who_cannot_do_the_services_is_stylist_mismatch(self):
        with self.seams() as s:
            response = self.post(request_body(stylist_id=ANYA, service_ids=[CUT, NAILS]))
        self.assert_refused(response, "stylist_mismatch", "stylist_id")
        s.create_series_booking.assert_not_called()

    def test_a_start_time_without_an_offset_is_invalid_time_before_booking_api(self):
        with self.seams() as s:
            response = self.post(request_body(start_time="2026-10-06T16:30:00"))
        self.assert_refused(response, "invalid_time", "start_time")
        s.create_series_booking.assert_not_called()

    def test_a_cadence_the_contract_does_not_have_is_invalid_cadence(self):
        with self.seams() as s:
            response = self.post(request_body(cadence="daily"))
        self.assert_refused(response, "invalid_cadence", "cadence")
        s.create_series_booking.assert_not_called()

    def test_a_start_outside_the_salons_hours_says_so(self):
        # 20:15 in Dubai: booking-api's day ends at 20:00.
        plan = engine_preview([engine_session(0, "2026-10-06", "22:15"), engine_session(1, "2026-10-13", "22:15")],
                              frequency="WEEKLY", time="22:15")
        with self.seams(routine={"create_series_booking": booking_api(plan)}):
            response = self.post(request_body(start_time="2026-10-06T20:15:00+04:00"))
        self.assertEqual(
            [(x["available"], x["reason"]) for x in response.data["sessions"]],
            [(False, "outside_hours"), (False, "outside_hours")],
        )

    def test_the_day_views_hours_are_read_for_each_session_day(self):
        with self.seams() as s:
            self.post(request_body())
        days = sorted({c.args[2].isoformat() for c in s._open_span.call_args_list})
        self.assertIn("2026-10-06", days)
        self.assertIn("2026-10-13", days)
