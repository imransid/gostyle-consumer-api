"""
The app team's routine contract, steps C1 and C2
(docs/ROUTINE_FE_CONTRACT_AUDIT.md, 4.2).

C1: the switch ROUTINE_CONTRACT_V1 and the three routes. Off, 404 and
booking-api never called; the old routes resolve exactly as before.

C2: routine_contract.py, the pure translator, both ways. No database, no
network. The salon is in Dubai (+04:00) and booking-api reads every branch
at +06:00, so every time here crosses two hours, both ways.
"""

import json
import uuid
import zoneinfo
from datetime import date, datetime, timedelta
from pathlib import Path
from unittest import mock

from django.conf import settings
from django.test import SimpleTestCase, override_settings
from django.urls import resolve
from rest_framework.test import APIRequestFactory, force_authenticate

from apps.salons import routine_contract as rc
from apps.salons import series_translate as st
from apps.salons.group_translate import engine_zone
from apps.salons.routine_views import (
    RoutineCreateView,
    RoutinePreviewView,
    RoutineSessionMoveView,
)
from apps.salons.series_views import (
    SeriesBookingCancelView,
    SeriesBookingCreateView,
    SeriesBookingDetailView,
)
from apps.salons.test_group_booking import (
    ANYA,
    BRANCH,
    CLOCK,
    COVERAGE,
    CUT,
    ELSEWHERE,
    MAYA,
    NAILS,
    SALON_ID,
    Customer,
)
from apps.salons.views import (
    BookingCreateView,
    BookingDetailView,
    BookingListView,
    NearestAvailableView,
)

DUBAI = zoneinfo.ZoneInfo("Asia/Dubai")
ENGINE = engine_zone(CLOCK)
ROUTINE_ID = "1b5a88e8-a672-4681-8e9e-c91492af7b2d"
SESSION_ID = "5e551011-0000-4000-8000-000000000001"


# ------------------------------------------------------------ C1: the switch and the routes


class Routes:
    def call(self, view, method, path, *, authenticated=True, **kwargs):
        factory = getattr(APIRequestFactory(), method)
        request = factory(path, data=json.dumps({}), content_type="application/json",
                          HTTP_AUTHORIZATION="Bearer customer-token")
        if authenticated:
            force_authenticate(request, user=Customer())
        return view.as_view()(request, **kwargs)

    def every_route(self, **kw):
        return [
            self.call(RoutinePreviewView, "post", "/api/v1/booking/routine-preview", **kw),
            self.call(RoutineCreateView, "post", "/api/v1/booking/routine", **kw),
            self.call(
                RoutineSessionMoveView, "patch",
                f"/api/v1/booking/{ROUTINE_ID}/sessions/{SESSION_ID}",
                booking_id=uuid.UUID(ROUTINE_ID), session_id=uuid.UUID(SESSION_ID), **kw,
            ),
        ]


class SwitchTests(Routes, SimpleTestCase):
    def test_the_switch_is_off_by_default(self):
        self.assertIn("ROUTINE_CONTRACT_V1=false", Path(".env.example").read_text())
        source = Path("config/settings/base.py").read_text()
        self.assertIn('ROUTINE_CONTRACT_V1 = env.bool("ROUTINE_CONTRACT_V1", default=False)', source)

    @override_settings(ROUTINE_CONTRACT_V1=False)
    def test_off_every_route_is_404_and_booking_api_is_never_called(self):
        with mock.patch("apps.salons.booking_api.urllib.request.urlopen") as urlopen:
            answers = self.every_route()
        self.assertEqual([a.status_code for a in answers], [404, 404, 404])
        for a in answers:
            self.assertEqual(a.data["code"], "not_found")
        urlopen.assert_not_called()

    @override_settings(ROUTINE_CONTRACT_V1=True)
    def test_on_the_routes_not_built_yet_say_so_and_call_nothing(self):
        # The preview (C3) and the create (C4) are built, in their own test
        # files; the move answers 501 until its step.
        with mock.patch("apps.salons.booking_api.urllib.request.urlopen") as urlopen:
            answers = self.every_route()[2:]
        self.assertEqual([a.status_code for a in answers], [501])
        self.assertEqual(answers[0].data["errors"][0]["code"], "not_built")
        urlopen.assert_not_called()

    @override_settings(ROUTINE_CONTRACT_V1=False)
    def test_a_caller_with_no_token_is_401_first(self):
        self.assertEqual(
            [a.status_code for a in self.every_route(authenticated=False)], [401, 401, 401],
        )


class RouteTests(SimpleTestCase):
    def test_the_three_new_routes(self):
        self.assertIs(resolve("/api/v1/booking/routine-preview").func.view_class, RoutinePreviewView)
        self.assertIs(resolve("/api/v1/booking/routine").func.view_class, RoutineCreateView)
        match = resolve(f"/api/v1/booking/{ROUTINE_ID}/sessions/{SESSION_ID}")
        self.assertIs(match.func.view_class, RoutineSessionMoveView)
        self.assertEqual(match.kwargs, {
            "booking_id": uuid.UUID(ROUTINE_ID), "session_id": uuid.UUID(SESSION_ID),
        })

    def test_every_old_booking_route_resolves_exactly_as_before(self):
        cases = {
            "/api/v1/booking": BookingCreateView,
            "/api/v1/bookings": BookingListView,
            f"/api/v1/booking/{ROUTINE_ID}": BookingDetailView,
            f"/api/v1/booking/nearest-available/{SALON_ID}": NearestAvailableView,
            "/api/v1/booking/series": SeriesBookingCreateView,
            f"/api/v1/booking/series/{ROUTINE_ID}": SeriesBookingDetailView,
            f"/api/v1/booking/series/{ROUTINE_ID}/cancel": SeriesBookingCancelView,
        }
        for path, view in cases.items():
            with self.subTest(path=path):
                self.assertIs(resolve(path).func.view_class, view)
        for path, name in {
            f"/api/v1/booking/{ROUTINE_ID}/cancel": "GroupBookingCancelView",
            "/api/v1/booking/group": "GroupBookingCreateView",
            "/api/v1/booking/group-availability": "GroupAvailabilityView",
        }.items():
            with self.subTest(path=path):
                self.assertEqual(resolve(path).func.view_class.__name__, name)

    def test_urls_py_keeps_its_crlf_line_endings(self):
        raw = Path("apps/salons/urls.py").read_bytes()
        self.assertGreater(raw.count(b"\r\n"), 0)
        self.assertEqual(raw.count(b"\n"), raw.count(b"\r\n"))


# ------------------------------------------------------------ C2: fixtures


def engine_session(index, day, hhmm="18:30", *, minutes=45, free=True, reason=None,
                   stylist=MAYA, alternatives=()):
    """One session as booking-api's dry run answers it, on ITS clock (+06:00)."""
    start = datetime.fromisoformat(f"{day}T{hhmm}:00+06:00")
    session = {
        "index": index, "date": day,
        "start_time": start.isoformat(),
        "end_time": (start + timedelta(minutes=minutes)).isoformat(),
        "stylist_id": stylist, "free": free, "later": False, "picked": False,
        "moved_from_day_of_month": None,
        "alternatives": [
            {"date": a[:10], "time": a[11:16], "start_time": a, "stylist_id": stylist}
            for a in alternatives
        ],
    }
    if reason is not None:
        session["reason"] = reason
    return session


def engine_preview(sessions, *, frequency="MONTHLY", time="18:30", each=105.0):
    return {
        "dry_run": True, "frequency": frequency, "time": time, "stylist_id": MAYA,
        "payment_plan": "PAY_AT_SALON", "sessions": sessions, "available_times": None,
        "all_free": all(s["free"] for s in sessions),
        "money": {"plans": {"PAY_AT_SALON": {
            "available": True, "amount_without_tax": 100.0 * len(sessions), "discount": 0,
            "tax_amount": 5.0 * len(sessions), "total": each * len(sessions), "pay_now": 0,
            "percent": None,
            "sessions": [{"total": each, "pay_now": 0, "at_visit": each} for _ in sessions],
        }}},
        "rules": {"min_sessions": 2},
    }


# A MONTHLY routine from Sunday 31 January 2027 at 18:00 in Dubai (20:00 at
# booking-api): the plan clamps February to the 28th and goes back to the 31st.
JAN_31 = engine_preview(
    [
        engine_session(0, "2027-01-31", "20:00"),
        engine_session(1, "2027-02-28", "20:00"),
        engine_session(2, "2027-03-31", "20:00"),
    ],
    time="20:00",
)


def salon_session(index, day, hhmm="18:00", minutes=45):
    """One session of the create, as the app sends it: Dubai ISO times."""
    start = datetime.fromisoformat(f"{day}T{hhmm}:00+04:00")
    return {
        "index": index, "date": day, "start_time": start.isoformat(),
        "end_time": (start + timedelta(minutes=minutes)).isoformat(),
    }


def create(**over):
    body = {
        "salon_id": SALON_ID,
        "cadence": "month",
        # services[].amount is NOT checked (Q5): a wrong one changes nothing.
        "services": [{"id": CUT, "amount": 1}],
        "products": [],
        "stylist_id": MAYA,
        "start_time": "2027-01-31T18:00:00+04:00",
        "sessions": [
            salon_session(0, "2027-01-31"),
            salon_session(1, "2027-02-28"),
            salon_session(2, "2027-03-31"),
        ],
        "amount_without_tax": 300, "tax_amount": 15, "discount": 0, "promo_code": None,
        "total": 315, "advance_paid_amount": 0, "due_amount": 315,
        "payment_status": "DRAFT", "status": "BOOKED", "booking_type": "ROUTINE",
    }
    body.update(over)
    return body


def preview_request(**over):
    body = {
        "salon_id": SALON_ID, "service_ids": [CUT], "stylist_id": MAYA,
        "start_time": "2026-10-06T16:30:00+04:00", "cadence": "month", "sessions": 5,
    }
    body.update(over)
    return body


def refusal(fn, *args, **kwargs):
    try:
        fn(*args, **kwargs)
    except rc.Refusal as exc:
        return exc.as_error()
    raise AssertionError("expected a refusal")


def preview_body(request, candidates=()):
    return rc.preview_body(request, branch_id=BRANCH, tz=DUBAI, clock=CLOCK, candidates=candidates)


def jan_31_plan():
    """booking-api's plan for create(), on the salon's clock, as the view will hold it."""
    salon = st.present_preview(JAN_31, tz=DUBAI, engine_tz=ENGINE)
    return rc.plan_slots(salon), rc.plan_minutes(salon)


def create_body(body, candidates=()):
    slots, minutes = jan_31_plan()
    return rc.create_body(
        body, branch_id=BRANCH, tz=DUBAI, clock=CLOCK, candidates=candidates,
        slots=slots, minutes=minutes,
    )


# ------------------------------------------------------------ C2: the request


class StartTimeTests(SimpleTestCase):
    """One ISO time with any offset, on the salon's clock, then on booking-api's."""

    def test_the_same_instant_in_every_offset_is_the_same_request(self):
        expected = {
            "dry_run": True, "salon_id": BRANCH, "services": [{"id": CUT}],
            "stylist_id": MAYA, "frequency": "MONTHLY",
            # 16:30 in Dubai is 18:30 at booking-api.
            "start_date": "2026-10-06", "sessions": 5, "dates": None, "time": "18:30",
            "payment_plan": "PAY_AT_SALON", "picks": [],
            **rc.CONTRACT_OPTIONS,
        }
        for start in (
            "2026-10-06T16:30:00+04:00",   # the salon's own offset
            "2026-10-06T18:30:00+06:00",   # booking-api's
            "2026-10-06T12:30:00Z",        # UTC
            "2026-10-06T12:30:00+00:00",
        ):
            with self.subTest(start=start):
                self.assertEqual(preview_body(preview_request(start_time=start)), expected)

    def test_no_offset_is_invalid_time(self):
        self.assertEqual(
            refusal(preview_body, preview_request(start_time="2026-10-06T16:30:00")),
            {"field": "start_time", "code": "invalid_time",
             "message": "The time needs its offset, for example 2026-09-20T18:00:00+04:00."},
        )

    def test_not_a_time_or_with_seconds_is_invalid_time(self):
        for start in ("tomorrow", "", None, 1700000000, "2026-10-06T16:30:15+04:00"):
            with self.subTest(start=start):
                self.assertEqual(
                    refusal(preview_body, preview_request(start_time=start))["code"],
                    "invalid_time",
                )

    def test_near_midnight_the_day_moves_with_the_time(self):
        body = preview_body(preview_request(start_time="2026-10-06T23:30:00+04:00"))
        self.assertEqual((body["start_date"], body["time"]), ("2026-10-07", "01:30"))

    def test_a_monthly_routine_from_31_january(self):
        body = preview_body(preview_request(start_time="2027-01-31T18:00:00+04:00"))
        self.assertEqual((body["start_date"], body["time"]), ("2027-01-31", "20:00"))

    def test_salon_day_and_time_reads_any_offset_on_the_salon_clock(self):
        day, hhmm, moment = rc.salon_day_and_time("2026-10-06T12:30:00Z", DUBAI, "start_time")
        self.assertEqual((day, hhmm), (date(2026, 10, 6), "16:30"))
        self.assertEqual(moment.utcoffset(), timedelta(hours=4))


class WordsTests(SimpleTestCase):
    def test_cadence_words(self):
        for word, frequency in (("week", "WEEKLY"), ("fortnight", "EVERY_2_WEEKS"), ("month", "MONTHLY")):
            with self.subTest(word=word):
                self.assertEqual(preview_body(preview_request(cadence=word))["frequency"], frequency)
                self.assertEqual(rc.CADENCE_OF[frequency], word)

    def test_anything_else_is_invalid_cadence(self):
        for cadence in ("MONTHLY", "daily", "custom", "", None, 7):
            with self.subTest(cadence=cadence):
                self.assertEqual(
                    refusal(preview_body, preview_request(cadence=cadence)),
                    {"field": "cadence", "code": "invalid_cadence",
                     "message": "cadence is week, fortnight or month."},
                )

    def test_service_ids_become_services_and_the_plan_is_always_pay_at_salon(self):
        body = preview_body(preview_request(service_ids=[CUT, NAILS]))
        self.assertEqual(body["services"], [{"id": CUT}, {"id": NAILS}])
        self.assertEqual(body["payment_plan"], "PAY_AT_SALON")

    def test_every_request_asks_for_the_contract_options(self):
        body = preview_body(preview_request())
        self.assertEqual(
            {k: body[k] for k in rc.CONTRACT_OPTIONS},
            {"check_later": True, "with_reasons": True,
             "alternative_rule": "SAME_STYLIST_FORWARD", "alternatives_max": 12,
             "strict_picks": True},
        )


class AnyExpertTests(SimpleTestCase):
    def test_who_does_all_the_services(self):
        self.assertEqual(rc.stylists_doing_all(COVERAGE, [CUT]), sorted([MAYA, ANYA]))
        self.assertEqual(rc.stylists_doing_all(COVERAGE, [CUT, NAILS]), [MAYA])
        self.assertEqual(rc.stylists_doing_all(COVERAGE, [NAILS, ELSEWHERE]), [])

    def test_with_a_stylist_no_candidates_are_sent(self):
        body = preview_body(preview_request(), candidates=[MAYA, ANYA])
        self.assertEqual(body["stylist_id"], MAYA)
        self.assertNotIn("stylist_candidates", body)

    def test_without_one_the_candidates_are_sent(self):
        body = preview_body(preview_request(stylist_id=None), candidates=[ANYA, MAYA])
        self.assertIsNone(body["stylist_id"])
        self.assertEqual(body["stylist_candidates"], [ANYA, MAYA])

    def test_without_one_and_nobody_able_it_is_no_stylist_available(self):
        for request in (preview_request(stylist_id=None), {**preview_request(), "stylist_id": ""}):
            with self.subTest(request=request["stylist_id"]):
                self.assertEqual(
                    refusal(preview_body, request, candidates=[])["code"], "no_stylist_available",
                )


class PaymentTests(SimpleTestCase):
    """Draft §4: pay at the salon only, for now."""

    def test_the_contract_payload_passes(self):
        rc.check_payment(create())
        rc.check_payment(create(advance_paid_amount=0.0, due_amount=315.0, total=315))
        rc.check_payment({k: v for k, v in create().items() if k != "products"})

    def test_each_field_refused_with_its_own_code(self):
        cases = [
            ({"payment_status": "PAY_AFTER_CHECK_IN"}, "payment_status", "invalid_payment_status", None),
            ({"payment_status": None}, "payment_status", "invalid_payment_status", None),
            ({"advance_paid_amount": 50}, "advance_paid_amount", "amount_mismatch", 0),
            ({"advance_paid_amount": "x"}, "advance_paid_amount", "amount_mismatch", 0),
            ({"due_amount": 314.99}, "due_amount", "amount_mismatch", 315),
            ({"due_amount": None}, "due_amount", "amount_mismatch", 315),
            ({"products": [{"id": "pomade", "amount": 20}]}, "products", "products_not_supported", None),
            ({"products": "none"}, "products", "products_not_supported", None),
            ({"promo_code": "GOSTYLE20"}, "promo_code", "invalid_promo", None),
            ({"status": "CONFIRMED_BY_SALON"}, "status", "invalid_status", None),
            ({"booking_type": "SINGLE"}, "booking_type", "invalid_booking_type", None),
        ]
        for over, field, code, expected in cases:
            with self.subTest(over=over):
                error = refusal(rc.check_payment, create(**over))
                self.assertEqual((error["field"], error["code"]), (field, code))
                self.assertEqual(error.get("expected"), expected)

    def test_a_service_line_amount_is_never_checked(self):
        rc.check_payment(create(services=[{"id": CUT, "amount": 999}]))
        body = create_body(create(services=[{"id": CUT, "amount": 999}]))
        self.assertEqual(body["services"], [{"id": CUT}])


class CreateTests(SimpleTestCase):
    """start_time is the anchor (Q1); sessions[] on their slots, or picks."""

    def test_on_the_cadence_nothing_is_picked_and_every_figure_goes_on(self):
        body = create_body(create())
        self.assertEqual(body["dry_run"], False)
        self.assertEqual(body["picks"], [])
        self.assertEqual((body["frequency"], body["start_date"], body["time"], body["sessions"]),
                         ("MONTHLY", "2027-01-31", "20:00", 3))
        self.assertEqual(
            {k: body[k] for k in ("amount_without_tax", "tax_amount", "discount", "total")},
            {"amount_without_tax": 300, "tax_amount": 15, "discount": 0, "total": 315},
        )
        self.assertNotIn("products", body)
        self.assertEqual(body["strict_picks"], True)

    def test_the_monthly_plan_from_31_january_is_28_february_then_31_march(self):
        slots, minutes = jan_31_plan()
        self.assertEqual([s.isoformat() for s in slots], [
            "2027-01-31T18:00:00+04:00", "2027-02-28T18:00:00+04:00", "2027-03-31T18:00:00+04:00",
        ])
        self.assertEqual(minutes, 45)

    def test_the_same_sessions_in_other_offsets_are_still_on_the_cadence(self):
        for offset, hhmm in (("+06:00", "20:00"), ("Z", "14:00")):
            sessions = []
            for i, day in enumerate(("2027-01-31", "2027-02-28", "2027-03-31")):
                start = datetime.fromisoformat(f"{day}T{hhmm}:00{offset if offset != 'Z' else '+00:00'}")
                text = start.isoformat() if offset != "Z" else f"{day}T{hhmm}:00Z"
                end = (start + timedelta(minutes=45)).isoformat()
                sessions.append({"index": i, "date": day, "start_time": text, "end_time": end})
            with self.subTest(offset=offset):
                self.assertEqual(create_body(create(sessions=sessions))["picks"], [])

    def test_a_session_off_its_slot_becomes_a_pick_on_booking_apis_clock(self):
        sessions = create()["sessions"]
        sessions[1] = salon_session(1, "2027-02-28", "17:00")   # same day, an hour earlier
        sessions[2] = salon_session(2, "2027-04-01")            # the next day
        self.assertEqual(create_body(create(sessions=sessions))["picks"], [
            {"index": 1, "date": "2027-02-28", "time": "19:00", "stylist_id": None},
            {"index": 2, "date": "2027-04-01", "time": "20:00", "stylist_id": None},
        ])

    def test_a_pick_near_midnight_moves_to_booking_apis_next_day(self):
        sessions = create()["sessions"]
        sessions[1] = salon_session(1, "2027-02-28", "23:30")
        self.assertEqual(create_body(create(sessions=sessions))["picks"], [
            {"index": 1, "date": "2027-03-01", "time": "01:30", "stylist_id": None},
        ])

    def test_the_session_numbers_must_be_0_1_2_in_order(self):
        good = create()["sessions"]
        for sessions in (
            [good[0], good[2], good[1]],
            [good[0], {**good[1], "index": 5}, good[2]],
            [good[0], {k: v for k, v in good[1].items() if k != "index"}, good[2]],
            good[:2],                       # the plan has 3
            "three",
        ):
            with self.subTest(sessions=str(sessions)[:40]):
                self.assertEqual(refusal(create_body, create(sessions=sessions))["code"],
                                 "invalid_sessions")

    def test_date_must_be_the_salon_date_of_start_time(self):
        sessions = create()["sessions"]
        sessions[1] = {**sessions[1], "date": "2027-02-27"}
        self.assertEqual(refusal(create_body, create(sessions=sessions)), {
            "field": "sessions[1].date", "code": "date_mismatch",
            "message": "date says 2027-02-27 and start_time is 2027-02-28 at the salon.",
        })

    def test_end_time_must_be_the_start_plus_the_services_length(self):
        sessions = create()["sessions"]
        sessions[2] = salon_session(2, "2027-03-31", minutes=50)
        self.assertEqual(refusal(create_body, create(sessions=sessions)), {
            "field": "sessions[2].end_time", "code": "invalid_window",
            "message": "These services run 45 minutes, so the visit ends at 2027-03-31T18:45:00+04:00.",
        })

    def test_a_session_time_without_an_offset_is_invalid_time(self):
        sessions = create()["sessions"]
        sessions[1] = {**sessions[1], "start_time": "2027-02-28T18:00:00"}
        self.assertEqual(refusal(create_body, create(sessions=sessions))["field"],
                         "sessions[1].start_time")

    def test_the_payment_fields_are_checked_before_the_sessions(self):
        self.assertEqual(
            refusal(create_body, create(status="BOOKED", booking_type="SINGLE", sessions="x"))["code"],
            "invalid_booking_type",
        )

    def test_plan_request_is_the_preview_the_create_stands_for(self):
        self.assertEqual(rc.plan_request(create()), {
            "salon_id": SALON_ID, "service_ids": [CUT], "stylist_id": MAYA,
            "start_time": "2027-01-31T18:00:00+04:00", "cadence": "month", "sessions": 3,
        })


class MoveTests(SimpleTestCase):
    def test_start_time_in_any_offset_is_the_salons_day_and_time(self):
        for start in ("2026-10-22T19:00:00+04:00", "2026-10-22T21:00:00+06:00", "2026-10-22T15:00:00Z"):
            with self.subTest(start=start):
                self.assertEqual(rc.move_request({"start_time": start}, SESSION_ID, DUBAI), {
                    "action": "RESCHEDULE", "session_id": SESSION_ID,
                    "date": "2026-10-22", "time": "19:00", "dry_run": False,
                })

    def test_a_stylist_and_a_dry_run_go_on_as_sent(self):
        body = rc.move_request(
            {"start_time": "2026-10-22T19:00:00+04:00", "stylist_id": ANYA, "dry_run": True},
            SESSION_ID, DUBAI,
        )
        self.assertEqual((body["stylist_id"], body["dry_run"]), (ANYA, True))

    def test_no_offset_is_invalid_time(self):
        self.assertEqual(
            refusal(rc.move_request, {"start_time": "2026-10-22T19:00:00"}, SESSION_ID, DUBAI)["code"],
            "invalid_time",
        )


# ------------------------------------------------------------ C2: the salon's hours and the reason


class ReasonTests(SimpleTestCase):
    OPEN = (datetime(2026, 11, 6, 9, 0, tzinfo=DUBAI), datetime(2026, 11, 6, 20, 0, tzinfo=DUBAI))
    ENGINE_DAY = (datetime(2026, 11, 6, 8, 0, tzinfo=DUBAI), datetime(2026, 11, 6, 20, 0, tzinfo=DUBAI))
    EARLIEST = datetime(2026, 11, 6, 12, 0, tzinfo=DUBAI)

    def codes(self, hour, minute=0, open_span=OPEN, longest=45):
        start = datetime(2026, 11, 6, hour, minute, tzinfo=DUBAI)
        return rc.hours_codes(start, open_span, self.ENGINE_DAY, self.EARLIEST, longest)

    def test_every_hours_reason_that_applies_in_the_contracts_order(self):
        self.assertEqual(self.codes(15), [])
        self.assertEqual(self.codes(19, 30), ["outside_hours"])        # ends 20:15
        self.assertEqual(self.codes(11), ["too_soon"])
        self.assertEqual(self.codes(8, 30), ["outside_hours", "too_soon"])
        self.assertEqual(self.codes(15, open_span=None), ["salon_closed"])
        self.assertEqual(self.codes(11, open_span=None), ["salon_closed", "too_soon"])

    def test_one_reason_the_first_in_order(self):
        cases = [
            (["salon_closed", "too_soon"], "stylist_unavailable", "salon_closed"),
            (["outside_hours", "too_soon"], "stylist_unavailable", "outside_hours"),
            (["too_soon"], "stylist_unavailable", "too_soon"),
            ([], "stylist_unavailable", "stylist_unavailable"),
            ([], None, None),
        ]
        for codes, engine, expected in cases:
            with self.subTest(codes=codes, engine=engine):
                self.assertEqual(rc.final_reason(codes, engine), expected)
        self.assertEqual(rc.REASON_ORDER,
                         ("salon_closed", "outside_hours", "too_soon", "stylist_unavailable"))


# ------------------------------------------------------------ C2: the preview answer


def after_20_is_outside(start):
    """The salon's hours, as the view will hand them in: shut at 20:00."""
    return ["outside_hours"] if start.astimezone(DUBAI).hour >= 20 else []


def open_always(_start):
    return []


NAMES = {MAYA: "Maya E.", ANYA: "Anya"}


def present(answer, hours=open_always):
    return rc.present_preview(answer, tz=DUBAI, engine_tz=ENGINE, hours=hours, names=NAMES)


class PreviewAnswerTests(SimpleTestCase):
    def test_the_contract_shape_on_the_salons_clock(self):
        out = present(JAN_31)
        self.assertEqual(list(out), ["cadence", "sessions", "per_session_total", "plan_total"])
        self.assertEqual(out["cadence"], "month")
        self.assertEqual(out["sessions"][0], {
            "index": 0, "date": "2027-01-31",
            "start_time": "2027-01-31T18:00:00+04:00", "end_time": "2027-01-31T18:45:00+04:00",
            "available": True, "stylist": {"id": MAYA, "name": "Maya E."}, "alternatives": [],
        })
        self.assertEqual([s["date"] for s in out["sessions"]], ["2027-01-31", "2027-02-28", "2027-03-31"])
        self.assertEqual((out["per_session_total"], out["plan_total"]), (105.0, 315.0))

    def test_a_busy_session_says_why_shows_no_stylist_and_offers_iso_times(self):
        busy = engine_preview([
            engine_session(0, "2026-11-06", free=False, reason="stylist_unavailable",
                           alternatives=("2026-11-06T18:00:00+06:00", "2026-11-07T18:30:00+06:00")),
        ])
        session = present(busy)["sessions"][0]
        self.assertEqual(session["available"], False)
        self.assertEqual(session["reason"], "stylist_unavailable")
        self.assertIsNone(session["stylist"])
        # On the salon's clock: booking-api's 18:00 and 18:30 are Dubai's 16:00 and 16:30.
        self.assertEqual(session["alternatives"], [
            "2026-11-06T16:00:00+04:00", "2026-11-07T16:30:00+04:00",
        ])
        self.assertEqual(list(session), [
            "index", "date", "start_time", "end_time", "available", "reason", "stylist", "alternatives",
        ])

    def test_alternatives_outside_the_hours_are_dropped_first_then_three_kept(self):
        alternatives = (
            "2026-11-06T22:00:00+06:00",   # 20:00 in Dubai: outside
            "2026-11-06T17:30:00+06:00",   # 15:30
            "2026-11-07T22:30:00+06:00",   # 20:30: outside
            "2026-11-07T18:30:00+06:00",   # 16:30
            "2026-11-07T18:00:00+06:00",   # 16:00
            "2026-11-08T18:30:00+06:00",   # 16:30, the fourth that fits: not shown
        )
        busy = engine_preview([engine_session(
            0, "2026-11-06", free=False, reason="stylist_unavailable", alternatives=alternatives,
        )])
        self.assertEqual(present(busy, after_20_is_outside)["sessions"][0]["alternatives"], [
            "2026-11-06T15:30:00+04:00", "2026-11-07T16:30:00+04:00", "2026-11-07T16:00:00+04:00",
        ])

    def test_free_for_booking_api_but_outside_the_hours_is_not_available(self):
        late = engine_preview([engine_session(0, "2026-11-06", "22:15")])   # 20:15 in Dubai
        session = present(late, after_20_is_outside)["sessions"][0]
        self.assertEqual((session["available"], session["reason"], session["stylist"]),
                         (False, "outside_hours", None))

    def test_the_hours_reason_wins_over_booking_apis(self):
        def closed(_start):
            return ["salon_closed"]
        busy = engine_preview([engine_session(0, "2026-11-06", free=False, reason="stylist_unavailable")])
        self.assertEqual(present(busy, closed)["sessions"][0]["reason"], "salon_closed")

    def test_a_not_free_session_with_no_reason_from_booking_api_is_stylist_unavailable(self):
        busy = engine_preview([engine_session(0, "2026-11-06", free=False)])
        self.assertEqual(present(busy)["sessions"][0]["reason"], "stylist_unavailable")

    def test_a_stylist_with_no_name_on_our_list(self):
        out = rc.present_preview(JAN_31, tz=DUBAI, engine_tz=ENGINE, hours=open_always, names={})
        self.assertEqual(out["sessions"][0]["stylist"], {"id": MAYA, "name": None})


# ------------------------------------------------------------ C2: the routine answer (view=booking)


def engine_routine():
    """booking-api's routine as one booking (B7), on ITS words and clock."""
    def session(index, day, hhmm, state, booking=True):
        start = datetime.fromisoformat(f"{day}T{hhmm}:00+06:00")
        return {
            "id": f"occ-{index}", "index": index, "date": day,
            "start_time": start.isoformat(), "end_time": (start + timedelta(minutes=45)).isoformat(),
            "state": state, "stylist": {"id": MAYA, "name": "Maya"},
            "booking_id": f"b-{index}" if booking else None,
            "pass_qr_code": f"GS-{index}" if booking else None, "total": 105,
            "locked": False, "can_skip": True, "can_reschedule": True,
        }
    return {
        "id": ROUTINE_ID, "booking_type": "ROUTINE", "salon_id": BRANCH, "status": "ACTIVE",
        "frequency": "WEEKLY",
        "date": "2026-10-27", "start_time": "2026-10-27T18:30:00+06:00",
        "end_time": "2026-10-27T19:15:00+06:00",
        "services": [{"id": CUT, "name": "Cut", "amount": 100}], "products": [],
        "stylists": [{"id": MAYA, "name": "Maya", "avatar_url": None},
                     {"id": ANYA, "name": "Anya", "avatar_url": None}],
        "amount_without_tax": 200, "tax_amount": 10, "discount": 0, "total": 210,
        "promo_code": None, "advance_paid_amount": 0, "due_amount": 210,
        "payment_status": "PAY_AFTER_CHECK_IN", "payment_method": None, "pass_qr_code": "GS-1",
        "counts": {"total": 2, "done": 0, "remaining": 2, "skipped": 0, "cancelled": 0},
        "pause": None, "can": {"skip": True},
        "sessions": [
            session(0, "2026-10-27", "18:30", "SCHEDULED"),
            # 01:00 at booking-api on the 4th is 23:00 in Dubai on the 3rd.
            session(1, "2026-11-04", "01:00", "PLANNED", booking=False),
        ],
        "created_at": "2026-10-01T11:00:00+06:00",
    }


class RoutineAnswerTests(SimpleTestCase):
    def present(self, avatars=None):
        return rc.present_routine(
            engine_routine(), salon_id=SALON_ID, tz=DUBAI,
            avatars=avatars if avatars is not None else {MAYA: "https://cdn/maya.png"},
        )

    def test_cadence_words_in_frequencys_place(self):
        out = self.present()
        self.assertEqual(out["cadence"], "week")
        self.assertNotIn("frequency", out)
        keys = list(out)
        self.assertEqual(keys[keys.index("cadence") - 1], "status")

    def test_every_time_on_the_salons_clock_and_each_date_the_salons(self):
        out = self.present()
        self.assertEqual((out["date"], out["start_time"], out["end_time"]),
                         ("2026-10-27", "2026-10-27T16:30:00+04:00", "2026-10-27T17:15:00+04:00"))
        self.assertEqual(out["created_at"], "2026-10-01T09:00:00+04:00")
        self.assertEqual(
            [(s["date"], s["start_time"], s["end_time"]) for s in out["sessions"]],
            [("2026-10-27", "2026-10-27T16:30:00+04:00", "2026-10-27T17:15:00+04:00"),
             ("2026-11-03", "2026-11-03T23:00:00+04:00", "2026-11-03T23:45:00+04:00")],
        )

    def test_avatar_url_from_our_own_stylist_list(self):
        out = self.present()
        self.assertEqual(out["stylists"], [
            {"id": MAYA, "name": "Maya", "avatar_url": "https://cdn/maya.png"},
            {"id": ANYA, "name": "Anya", "avatar_url": None},
        ])

    def test_the_salon_the_app_knows_and_everything_else_as_it_came(self):
        out = self.present()
        self.assertEqual(out["salon_id"], SALON_ID)
        source = engine_routine()
        for key in ("id", "booking_type", "status", "services", "products", "total",
                    "due_amount", "payment_status", "pass_qr_code", "counts", "pause", "can"):
            with self.subTest(key=key):
                self.assertEqual(out[key], source[key])
        self.assertEqual(out["sessions"][1]["booking_id"], None)
        self.assertEqual(out["sessions"][0]["pass_qr_code"], "GS-0")

    def test_the_answer_booking_api_gave_is_not_changed_in_place(self):
        source = engine_routine()
        rc.present_routine(source, salon_id=SALON_ID, tz=DUBAI, avatars={})
        self.assertEqual(source, engine_routine())


# ------------------------------------------------------------ C2: refusals


def mobile(status, *errors):
    """booking-api's mobile envelope."""
    first = errors[0]
    return {
        "detail": first["message"] if status == 409 else "Please correct the highlighted fields.",
        "code": "validation_error" if status == 422 else first["code"],
        "errors": list(errors),
    }


def error(field, code, message="Because."):
    return {"field": field, "code": code, "message": message}


class RefusalTests(SimpleTestCase):
    def test_session_not_free_is_slot_taken(self):
        status, body = rc.translate_refusal(
            409, mobile(409, error("sessions[1]", "session_not_free", "Session 2 was taken.")),
            route="create",
        )
        self.assertEqual(status, 409)
        self.assertEqual(body, {
            "detail": "Session 2 was taken.", "code": "slot_taken",
            "errors": [error("sessions[1]", "slot_taken", "Session 2 was taken.")],
        })

    def test_invalid_frequency_is_invalid_cadence_on_cadence(self):
        status, body = rc.translate_refusal(
            422, mobile(422, error("frequency", "invalid_frequency")), route="preview",
        )
        self.assertEqual((status, body["code"]), (422, "validation_error"))
        self.assertEqual(body["errors"], [error("cadence", "invalid_cadence")])

    def test_a_pick_is_named_as_the_session_it_stood_for(self):
        picks = [{"index": 2}, {"index": 4}]
        for field in ("picks[1]", "picks[1].date", "picks[1].stylist_id"):
            with self.subTest(field=field):
                _, body = rc.translate_refusal(
                    422, mobile(422, error(field, "session_not_offered")), route="create", picks=picks,
                )
                self.assertEqual(body["errors"][0]["field"], "sessions[4]")
                self.assertEqual(body["errors"][0]["code"], "session_not_offered")

    def test_field_names_in_the_contracts_words(self):
        cases = [
            ("preview", "services", "service_ids"),
            ("create", "services", "services"),
            ("preview", "start_date", "start_time"),
            ("create", "time", "start_time"),
            ("create", "dates", "sessions"),
            ("preview", "stylist_id", "stylist_id"),
            ("create", "total", "total"),
        ]
        for route, field, expected in cases:
            with self.subTest(route=route, field=field):
                _, body = rc.translate_refusal(422, mobile(422, error(field, "x")), route=route)
                self.assertEqual(body["errors"][0]["field"], expected)

    def test_a_move_of_a_session_not_in_the_routine_is_404(self):
        status, body = rc.translate_refusal(
            422, mobile(422, error("session_id", "invalid_sessions",
                                   "That session is not part of this routine.")),
            route="move",
        )
        self.assertEqual(status, 404)
        self.assertEqual(body, {
            "detail": "No such session in this routine.", "code": "not_found",
            "errors": [{"field": None, "code": "not_found",
                        "message": "No such session in this routine."}],
        })

    def test_a_move_speaks_of_start_time_and_the_path(self):
        status, body = rc.translate_refusal(
            409, mobile(409, error("time", "session_not_free", "That time is not free.")), route="move",
        )
        self.assertEqual((status, body["code"], body["errors"][0]["field"]), (409, "slot_taken", "start_time"))
        _, body = rc.translate_refusal(
            422, mobile(422, error("session_id", "session_locked")), route="move",
        )
        self.assertEqual(body["errors"], [error(None, "session_locked")])

    def test_booking_apis_other_400_is_our_422(self):
        other = {
            "statusCode": 400,
            "message": ["property check_later should not exist",
                        "frequency must be a string", "sessions must be a number"],
            "error": "Bad Request", "code": "VALIDATION_FAILED",
        }
        status, body = rc.translate_refusal(400, other, route="preview")
        self.assertEqual(status, 422)
        self.assertEqual(body, {
            "detail": "Please correct the highlighted fields.", "code": "validation_error",
            "errors": [
                {"field": "check_later", "code": "invalid",
                 "message": "property check_later should not exist"},
                {"field": "cadence", "code": "invalid", "message": "frequency must be a string"},
                {"field": "sessions", "code": "invalid", "message": "sessions must be a number"},
            ],
        })

    def test_idempotency_key_reused_is_our_409(self):
        other = {
            "statusCode": 409, "code": "IDEMPOTENCY_KEY_REUSED",
            "message": "That Idempotency-Key was already used for a different request.",
            "details": {"key": "k-1", "operation": "POST /v1/mobile-booking/series"},
            "error": "Conflict",
        }
        status, body = rc.translate_refusal(409, other, route="create")
        self.assertEqual(status, 409)
        self.assertEqual(body, {
            "detail": "That Idempotency-Key was already used for a different request.",
            "code": "idempotency_key_reused",
            "errors": [{"field": None, "code": "idempotency_key_reused",
                        "message": "That Idempotency-Key was already used for a different request."}],
        })

    def test_anything_else_goes_back_as_it_came(self):
        not_found = mobile(404, error("id", "not_found", "No such booking."))
        self.assertEqual(rc.translate_refusal(404, not_found, route="create")[1]["code"], "not_found")
        for status, body in ((502, None), (500, "oops"),
                             (503, {"statusCode": 503, "message": "down", "error": "x"})):
            with self.subTest(status=status):
                self.assertEqual(rc.translate_refusal(status, body, route="create"), (status, body))

    def test_the_body_booking_api_sent_is_not_changed_in_place(self):
        body = mobile(422, error("frequency", "invalid_frequency"))
        rc.translate_refusal(422, body, route="preview")
        self.assertEqual(body["errors"][0]["code"], "invalid_frequency")


class OnlyTheNewRoutesUseItTests(SimpleTestCase):
    def test_no_old_view_imports_the_translator(self):
        for name in ("views.py", "series_views.py", "group_views.py"):
            with self.subTest(name=name):
                imports = [
                    line for line in (Path("apps/salons") / name).read_text().splitlines()
                    if line.startswith(("import ", "from ")) and "routine_contract" in line
                ]
                self.assertEqual(imports, [])

    def test_settings_default(self):
        self.assertTrue(hasattr(settings, "ROUTINE_CONTRACT_V1"))
