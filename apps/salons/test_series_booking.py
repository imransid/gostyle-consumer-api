"""
Routine (series) bookings for the app (SERIES_BOOKING_V1).

    POST /api/v1/booking/series        -> POST /v1/mobile-booking/series
    GET  /api/v1/booking/series/<id>   -> GET  /v1/mobile-booking/series/:id

Driven through the same seams as the group booking tests: no database, no
network. The salon is in Dubai (+04:00) and booking-api reads every branch
at +06:00, so every time here crosses two hours, both ways.
"""

import json
import types
import uuid
import zoneinfo
from contextlib import contextmanager
from datetime import date, datetime, timedelta
from unittest import mock

from django.test import SimpleTestCase, override_settings
from django.urls import resolve
from rest_framework.test import APIRequestFactory, force_authenticate

from apps.salons import series_translate as st
from apps.salons.booking_api import BookingApiUnavailable
from apps.salons.group_translate import EngineClock
from apps.salons.series_views import SeriesBookingCreateView, SeriesBookingDetailView
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
from apps.salons.views import BookingDetailView

DUBAI = zoneinfo.ZoneInfo("Asia/Dubai")
SERIES_ID = "1b5a88e8-a672-4681-8e9e-c91492af7b2d"

CARD = {
    "id": SALON_ID, "name": "Marina Walk", "logo_url": None, "city": "Dubai",
    "slug": "marina-walk", "cover_url": None, "address": "Marina", "region": None,
    "country_code": "AE", "lat": None, "lng": None, "timezone": "Asia/Dubai",
}

RULES = {"min_sessions": 2, "max_sessions": 6, "lock_hours": 24}


def session(index, day, start=None, end=None, **over):
    """One session as booking-api answers it, on ITS clock (+06:00)."""
    return {
        "index": index, "date": day,
        "start_time": start, "end_time": end,
        "stylist_id": MAYA, "free": None if start is None else True,
        "later": False, "picked": False, "moved_from_day_of_month": None,
        "alternatives": [], **over,
    }


# booking-api's dry run without a time: the days, and the free times at +06:00.
PREVIEW_NO_TIME = {
    "dry_run": True, "frequency": "WEEKLY", "time": None, "stylist_id": MAYA,
    "payment_plan": "PAY_AT_SALON",
    "sessions": [session(0, "2026-10-06"), session(1, "2026-10-13")],
    "available_times": ["10:00", "18:30", "21:55"],
    "all_free": None, "money": None, "rules": RULES,
}

# booking-api's dry run at 18:30 (+06:00): the second session is busy.
PREVIEW = {
    "dry_run": True, "frequency": "WEEKLY", "time": "18:30", "stylist_id": MAYA,
    "payment_plan": "PAY_AT_SALON",
    "sessions": [
        session(0, "2026-10-06", "2026-10-06T18:30:00+06:00", "2026-10-06T19:15:00+06:00"),
        session(1, "2026-10-13", "2026-10-13T18:30:00+06:00", "2026-10-13T19:15:00+06:00",
                free=False, alternatives=[
                    {"date": "2026-10-13", "time": "19:00",
                     "start_time": "2026-10-13T19:00:00+06:00", "stylist_id": MAYA},
                ]),
    ],
    "available_times": None, "all_free": False,
    "money": {"plans": {"PAY_AT_SALON": {"available": True, "total": 336}}},
    "rules": RULES,
}

# booking-api's hub, as the create and the read answer it.
HUB = {
    "id": SERIES_ID, "booking_type": "ROUTINE", "salon_id": BRANCH,
    "status": "ACTIVE", "frequency": "WEEKLY", "time": "18:30",
    "stylist": {"id": MAYA, "name": "Maya E."},
    "services": [{"id": CUT, "name": "Cut"}],
    "payment_plan": "PAY_AT_SALON", "pause": None,
    "counts": {"total": 2, "done": 0, "remaining": 2, "skipped": 0, "cancelled": 0},
    "next_session": {
        "id": "occ-0", "index": 0, "date": "2026-10-06",
        "start_time": "2026-10-06T18:30:00+06:00", "end_time": "2026-10-06T19:15:00+06:00",
        "state": "SCHEDULED",
    },
    "sessions": [
        {"id": "occ-0", "index": 0, "date": "2026-10-06",
         "start_time": "2026-10-06T18:30:00+06:00", "end_time": "2026-10-06T19:15:00+06:00",
         "state": "SCHEDULED", "booking_id": "b-0"},
        {"id": "occ-1", "index": 1, "date": "2026-10-13",
         "start_time": "2026-10-13T18:30:00+06:00", "end_time": "2026-10-13T19:15:00+06:00",
         "state": "SCHEDULED", "booking_id": "b-1"},
    ],
    "money": {"total": 336, "pay_now": 0},
    "created_at": "2026-09-26T09:00:00+06:00",
}


def preview_for(days, engine_time="18:30", minutes=45, **session_over):
    """
    booking-api's dry run for a routine on these days (ITS dates), at this
    time on ITS clock, every session free. `session_over` maps an index to
    what differs for that session.
    """
    h, m = map(int, engine_time.split(":"))
    sessions = []
    for i, day in enumerate(days):
        start = datetime.fromisoformat(f"{day}T{h:02d}:{m:02d}:00+06:00")
        s = session(i, day, start.isoformat(), (start + timedelta(minutes=minutes)).isoformat())
        s.update(session_over.get(f"s{i}", {}))
        sessions.append(s)
    return {**PREVIEW, "time": engine_time, "sessions": sessions, "all_free": True}


def opens(hours_by_weekday=None, closed=(), default=(9, 21)):
    """
    The salon's hours as _open_span answers them: (opens, closes) in hours,
    by weekday (0 is Monday), with some days shut.
    """
    hours_by_weekday = hours_by_weekday or {}

    def span(salon, tz, day, day_start):
        if day.isoformat() in closed:
            return None
        o, c = hours_by_weekday.get(day.weekday(), default)
        return day_start + timedelta(hours=o), day_start + timedelta(hours=c)
    return mock.Mock(side_effect=span)


def booking_api(preview=None, created=(201, None)):
    """POST /v1/mobile-booking/series: the plan for a dry run, the hub for a create."""
    def answer(body, **kw):
        if body["dry_run"]:
            return 200, json.loads(json.dumps(preview or PREVIEW))
        code, hub = created
        return code, json.loads(json.dumps(hub if hub is not None else HUB))
    return mock.Mock(side_effect=answer)


# 2026-10-01 09:00 in Dubai: every routine here is in the salon's future.
NOW = datetime(2026, 10, 1, 9, 0, tzinfo=DUBAI)


def routine(**over):
    """The app's request: a weekly routine of 2 at 16:30, the salon's clock."""
    body = {
        "salon_id": SALON_ID,
        "services": [{"id": CUT}],
        "stylist_id": MAYA,
        "frequency": "WEEKLY",
        "start_date": "2026-10-06",
        "sessions": 2,
        "time": "16:30",
        "payment_plan": "PAY_AT_SALON",
        "amount_without_tax": 320, "tax_amount": 16, "discount": 0, "total": 336,
    }
    body.update(over)
    return {k: v for k, v in body.items() if v is not ...}


class Seams:
    """Every seam the routine views reach, replaced."""

    @contextmanager
    def seams(self, *, group=None, series=None):
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
        series_fakes = {
            "create_series_booking": booking_api(),
            "read_series_booking": mock.Mock(return_value=(200, json.loads(json.dumps(HUB)))),
            "salon_cards_for_refs": mock.Mock(return_value={BRANCH: CARD}),
            "_open_span": opens(),
            "_now": mock.Mock(side_effect=lambda tz: NOW.astimezone(tz)),
        }
        series_fakes.update(series or {})
        patches = [mock.patch(f"apps.salons.group_views.{n}", f) for n, f in group_fakes.items()]
        patches += [mock.patch(f"apps.salons.series_views.{n}", f) for n, f in series_fakes.items()]
        for p in patches:
            p.start()
        try:
            yield types.SimpleNamespace(**group_fakes, **series_fakes)
        finally:
            for p in patches:
                p.stop()

    def post(self, body, *, authenticated=True, content_type="application/json", **headers):
        request = APIRequestFactory().post(
            "/api/v1/booking/series",
            data=json.dumps(body) if isinstance(body, dict) else body,
            content_type=content_type, HTTP_AUTHORIZATION="Bearer customer-token", **headers,
        )
        if authenticated:
            force_authenticate(request, user=Customer())
        return SeriesBookingCreateView.as_view()(request)

    def get(self, series_id=SERIES_ID, *, authenticated=True):
        request = APIRequestFactory().get(
            f"/api/v1/booking/series/{series_id}", HTTP_AUTHORIZATION="Bearer customer-token",
        )
        if authenticated:
            force_authenticate(request, user=Customer())
        return SeriesBookingDetailView.as_view()(request, series_id=series_id)

    def sent(self, s):
        """What booking-api was last sent: (body, keyword arguments)."""
        (body,), kw = s.create_series_booking.call_args
        return body, kw

    def creates(self, s):
        """The calls that would book: not the plan asked for first."""
        return [c for c in s.create_series_booking.call_args_list if not c.args[0]["dry_run"]]

    def assert_refused(self, response, code, field):
        self.assertEqual(response.status_code, 422, response.data)
        self.assertEqual(response.data["errors"][0]["code"], code)
        self.assertEqual(response.data["errors"][0]["field"], field)


# ------------------------------------------------------------ the flag


class FlagOffTests(Seams, SimpleTestCase):
    @override_settings(SERIES_BOOKING_V1=False)
    def test_off_both_routes_are_404_and_booking_api_is_not_asked(self):
        with self.seams() as s:
            self.assertEqual(self.post(routine()).status_code, 404)
            self.assertEqual(self.get().status_code, 404)
        s.create_series_booking.assert_not_called()
        s.read_series_booking.assert_not_called()

    def test_the_routes_and_the_untouched_ones(self):
        self.assertIs(resolve("/api/v1/booking/series").func.view_class, SeriesBookingCreateView)
        self.assertIs(
            resolve(f"/api/v1/booking/series/{SERIES_ID}").func.view_class,
            SeriesBookingDetailView,
        )
        # A single booking and a party still reach the views they always did.
        self.assertIs(resolve(f"/api/v1/booking/{SERIES_ID}").func.view_class, BookingDetailView)
        self.assertEqual(
            resolve(f"/api/v1/booking/{SERIES_ID}/cancel").func.view_class.__name__,
            "GroupBookingCancelView",
        )


# ------------------------------------------------------------ create and preview


@override_settings(SERIES_BOOKING_V1=True, SERIES_BOOKING_CREATE_TIMEOUT=45)
class CreateTests(Seams, SimpleTestCase):

    # ---- auth and shape, as for a group booking

    def test_needs_a_signed_in_customer(self):
        with self.seams() as s:
            response = self.post(routine(), authenticated=False)
        self.assertEqual(response.status_code, 401)
        s.create_series_booking.assert_not_called()

    def test_json_only(self):
        with self.seams():
            response = self.post("salon_id=x", content_type="application/x-www-form-urlencoded")
        self.assertEqual(response.status_code, 415)

    def test_an_unknown_salon_is_404(self):
        with self.seams(group={"salon_profile": mock.Mock(return_value=None)}) as s:
            response = self.post(routine())
        self.assertEqual(response.status_code, 404)
        s.create_series_booking.assert_not_called()

    def test_a_service_from_another_salon_is_foreign(self):
        with self.seams() as s:
            response = self.post(routine(services=[{"id": ELSEWHERE}]))
        self.assert_refused(response, "foreign_id", "services")
        s.engine_clock.assert_not_called()

    def test_a_stylist_off_the_roster_is_foreign(self):
        with self.seams() as s:
            response = self.post(routine(stylist_id=STRANGER))
        self.assert_refused(response, "foreign_id", "stylist_id")
        s.create_series_booking.assert_not_called()

    def test_a_stylist_who_cannot_do_the_work(self):
        with self.seams() as s:
            response = self.post(routine(stylist_id=ANYA, services=[{"id": NAILS}]))
        self.assert_refused(response, "stylist_mismatch", "stylist_id")
        s.create_series_booking.assert_not_called()

    def test_a_time_that_is_not_hh_mm(self):
        with self.seams() as s:
            response = self.post(routine(time="4:30pm"))
        self.assertEqual(response.status_code, 422, response.data)
        self.assertEqual(response.data["errors"][0]["field"], "time")
        s.create_series_booking.assert_not_called()

    # ---- TIMES: the salon's clock one way, booking-api's the other

    def test_the_apps_1630_reaches_booking_api_as_1830(self):
        with self.seams() as s:
            self.post(routine())
        body, kw = self.sent(s)
        self.assertEqual((body["start_date"], body["time"]), ("2026-10-06", "18:30"))
        self.assertEqual(body["salon_id"], BRANCH)
        self.assertEqual(kw["tenant_id"], TENANT)

    def test_booking_api_1830_comes_back_as_1630(self):
        with self.seams():
            data = self.post(routine()).data
        self.assertEqual(data["time"], "16:30")
        self.assertEqual(data["sessions"][0]["start_time"], "2026-10-06T16:30:00+04:00")
        self.assertEqual(data["sessions"][0]["end_time"], "2026-10-06T17:15:00+04:00")
        self.assertEqual(data["sessions"][0]["date"], "2026-10-06")
        self.assertEqual(data["next_session"]["start_time"], "2026-10-06T16:30:00+04:00")
        self.assertEqual(data["created_at"], "2026-09-26T07:00:00+04:00")

    def test_picks_cross_over_with_their_dates(self):
        with self.seams() as s:
            self.post(routine(picks=[
                {"index": 1, "date": "2026-10-13", "time": "17:00"},
                {"index": 0, "date": "2026-10-07", "time": "09:15", "stylist_id": ANYA},
            ]))
        body, _ = self.sent(s)
        self.assertEqual(body["picks"], [
            {"index": 1, "date": "2026-10-13", "time": "19:00", "stylist_id": None},
            {"index": 0, "date": "2026-10-07", "time": "11:15", "stylist_id": ANYA},
        ])

    def test_custom_days_cross_over_with_the_time(self):
        with self.seams() as s:
            self.post(routine(
                frequency="CUSTOM", start_date=..., sessions=...,
                dates=["2026-10-06", "2026-10-09"],
            ))
        body, _ = self.sent(s)
        self.assertEqual(body["dates"], ["2026-10-06", "2026-10-09"])
        self.assertEqual(body["time"], "18:30")
        self.assertIsNone(body["start_date"])

    def test_near_midnight_the_day_crosses_over_too(self):
        # 23:30 in Dubai on the 5th is 01:30 on the 6th at +06:00.
        with self.seams() as s:
            self.post(routine(start_date="2026-10-05", time="23:30"))
        body, _ = self.sent(s)
        self.assertEqual((body["start_date"], body["time"]), ("2026-10-06", "01:30"))

    def test_a_dry_run_without_a_time_sends_the_days_and_converts_the_free_times(self):
        answer = {**PREVIEW_NO_TIME, "available_times": ["10:00", "18:30", "21:00"]}
        with self.seams(series={
            "create_series_booking": booking_api(answer),
            "_open_span": opens(default=(0, 24)),
        }) as s:
            response = self.post(routine(dry_run=True, time=...))
        self.assertEqual(response.status_code, 200, response.data)
        body, _ = self.sent(s)
        self.assertEqual((body["start_date"], body["time"], body["dry_run"]), ("2026-10-06", None, True))
        # booking-api's 10:00, 18:30 and 21:00 are the salon's 08:00, 16:30, 19:00.
        self.assertEqual(response.data["available_times"], ["08:00", "16:30", "19:00"])

    def test_a_dry_run_with_a_time_converts_sessions_and_alternatives(self):
        with self.seams():
            response = self.post(routine(dry_run=True))
        self.assertEqual(response.status_code, 200, response.data)
        data = response.data
        self.assertEqual(data["time"], "16:30")
        self.assertEqual(data["sessions"][1]["start_time"], "2026-10-13T16:30:00+04:00")
        self.assertFalse(data["sessions"][1]["free"])
        self.assertEqual(data["sessions"][1]["alternatives"], [{
            "date": "2026-10-13", "time": "17:00",
            "start_time": "2026-10-13T17:00:00+04:00", "stylist_id": MAYA,
        }])
        self.assertEqual(data["money"], PREVIEW["money"])

    # ---- the key, the timeout, the money

    def test_a_dry_run_never_sends_an_idempotency_key(self):
        with self.seams() as s:
            self.post(routine(dry_run=True), HTTP_IDEMPOTENCY_KEY="the-apps-key")
        s.create_series_booking.assert_called_once()
        _, kw = self.sent(s)
        self.assertIsNone(kw["idempotency_key"])
        self.assertIsNone(kw.get("timeout"))

    def test_the_create_passes_the_apps_key_through_and_waits_longer(self):
        with self.seams() as s:
            response = self.post(routine(), HTTP_IDEMPOTENCY_KEY="the-apps-key")
        self.assertEqual(response.status_code, 201, response.data)
        body, kw = self.sent(s)
        self.assertEqual(kw["idempotency_key"], "the-apps-key")
        self.assertEqual(kw["timeout"], 45)
        self.assertFalse(body["dry_run"])

    def test_without_a_key_the_same_create_carries_the_same_derived_key(self):
        with self.seams() as s:
            self.post(routine())
            self.post(routine())
        keys = [c.kwargs["idempotency_key"] for c in self.creates(s)]
        self.assertTrue(keys[0])
        self.assertEqual(keys[0], keys[1])

    def test_the_apps_figures_and_products_ride_along(self):
        products = [{"id": "5a1e0000-0000-4000-8000-000000000001", "amount": 20, "quantity": 1}]
        with self.seams() as s:
            self.post(routine(products=products))
        body, _ = self.sent(s)
        for key in ("amount_without_tax", "tax_amount", "discount", "total"):
            self.assertEqual(body[key], routine()[key], key)
        self.assertEqual(body["products"], products)
        self.assertEqual(body["services"], [{"id": CUT}])
        self.assertEqual(body["stylist_id"], MAYA)

    # ---- the answer

    def test_the_hub_names_the_salon_and_carries_its_card(self):
        with self.seams():
            data = self.post(routine()).data
        self.assertEqual(data["salon_id"], SALON_ID)
        self.assertEqual(data["salon"]["id"], SALON_ID)
        self.assertEqual(data["salon"]["name"], "Marina Walk")

    def test_a_refusal_comes_back_as_booking_api_sent_it(self):
        refusal = {"detail": "Please correct the highlighted fields.", "code": "validation_error",
                   "errors": [{"field": "total", "code": "amount_mismatch",
                               "message": "Prices changed since this routine was started.",
                               "expected": 336}]}
        with self.seams(series={"create_series_booking": booking_api(created=(422, refusal))}):
            response = self.post(routine())
        self.assertEqual(response.status_code, 422)
        self.assertEqual(response.data, refusal)

    def test_a_refusal_of_the_plan_comes_back_as_it_is_and_nothing_is_booked(self):
        refusal = {"detail": "Please correct the highlighted fields.", "code": "validation_error",
                   "errors": [{"field": "sessions", "code": "invalid_session_count",
                               "message": "A routine is 2 to 6 sessions."}]}
        with self.seams(series={"create_series_booking": mock.Mock(return_value=(422, refusal))}) as s:
            response = self.post(routine(sessions=9))
        self.assertEqual(response.status_code, 422)
        self.assertEqual(response.data, refusal)
        self.assertEqual(self.creates(s), [])

    def test_a_busy_session_409_comes_back_as_it_is(self):
        refusal = {"detail": "Session 2 on 2026-10-13 is not free.", "code": "session_not_free",
                   "errors": [{"field": "sessions[1]", "code": "session_not_free",
                               "message": "Session 2 on 2026-10-13 is not free."}]}
        with self.seams(series={"create_series_booking": booking_api(created=(409, refusal))}):
            response = self.post(routine())
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.data, refusal)

    def test_booking_api_unreachable_is_503(self):
        down = mock.Mock(side_effect=BookingApiUnavailable("timed out"))
        with self.seams(series={"create_series_booking": down}):
            response = self.post(routine())
        self.assertEqual(response.status_code, 503)

    def test_booking_api_clock_unreachable_is_503(self):
        down = mock.Mock(side_effect=BookingApiUnavailable("no settings"))
        with self.seams(group={"engine_clock": down}) as s:
            response = self.post(routine())
        self.assertEqual(response.status_code, 503)
        s.create_series_booking.assert_not_called()


# ------------------------------------------------------------ the salon's hours


@override_settings(SERIES_BOOKING_V1=True, SERIES_BOOKING_CREATE_TIMEOUT=45)
class HoursTests(Seams, SimpleTestCase):
    """
    Every session, and every pick, held to the salon's own hours on its own
    day and to the services' notice: the rule a group start is held to
    (group_translate.start_refusal), with its codes.
    """

    # ---- dry run: shown, never booked

    def test_a_session_on_a_day_the_salon_is_shut_is_not_free(self):
        with self.seams(series={"_open_span": opens(closed={"2026-10-13"})}):
            data = self.post(routine(dry_run=True)).data
        self.assertTrue(data["sessions"][0]["free"])
        self.assertFalse(data["sessions"][1]["free"])
        self.assertEqual(data["sessions"][1]["refusal"]["code"], "salon_closed")
        self.assertFalse(data["all_free"])

    def test_hours_are_the_days_own_a_friday_that_shuts_early(self):
        # Tuesday the 6th and Friday the 9th at 16:30; Fridays close at 16:00.
        preview = preview_for(["2026-10-06", "2026-10-09"])
        with self.seams(series={
            "create_series_booking": booking_api(preview),
            "_open_span": opens({4: (9, 16)}),
        }):
            data = self.post(routine(
                dry_run=True, frequency="CUSTOM", start_date=..., sessions=...,
                dates=["2026-10-06", "2026-10-09"],
            )).data
        self.assertTrue(data["sessions"][0]["free"])
        self.assertEqual(data["sessions"][1]["refusal"]["code"], "outside_hours")
        self.assertFalse(data["sessions"][1]["free"])

    def test_a_session_that_would_run_past_closing_is_not_free(self):
        # Tuesdays close at 17:00; 16:30 plus 45 minutes is 17:15.
        with self.seams(series={"_open_span": opens({1: (9, 17)})}):
            data = self.post(routine(dry_run=True)).data
        self.assertEqual(
            [s["refusal"]["code"] for s in data["sessions"]], ["outside_hours", "outside_hours"],
        )

    def test_a_session_inside_the_services_notice_is_too_soon(self):
        # Nails needs 30 minutes' notice. It is 09:00; the first visit is at 09:20.
        preview = preview_for(["2026-10-01", "2026-10-08"], engine_time="11:20", minutes=60)
        with self.seams(series={"create_series_booking": booking_api(preview)}):
            data = self.post(routine(
                dry_run=True, services=[{"id": NAILS}], start_date="2026-10-01", time="09:20",
            )).data
        self.assertEqual(data["sessions"][0]["refusal"]["code"], "too_soon")
        self.assertNotIn("refusal", data["sessions"][1])

    def test_only_alternatives_inside_the_hours_are_offered(self):
        # The 13th closes at 17:30: 17:00 (to 17:45) is out, 15:00 is in.
        preview = preview_for(
            ["2026-10-06", "2026-10-13"],
            s1={"free": False, "alternatives": [
                {"date": "2026-10-13", "time": "19:00",
                 "start_time": "2026-10-13T19:00:00+06:00", "stylist_id": MAYA},
                {"date": "2026-10-13", "time": "17:00",
                 "start_time": "2026-10-13T17:00:00+06:00", "stylist_id": MAYA},
            ]},
        )
        hours = mock.Mock(side_effect=lambda salon, tz, day, day_start: (
            day_start + timedelta(hours=9),
            day_start + timedelta(hours=17, minutes=30 if day == date(2026, 10, 13) else 210),
        ))
        with self.seams(series={"create_series_booking": booking_api(preview), "_open_span": hours}):
            data = self.post(routine(dry_run=True)).data
        self.assertEqual(
            [(a["time"]) for a in data["sessions"][1]["alternatives"]], ["15:00"],
        )

    def test_free_times_outside_the_hours_on_any_day_are_dropped(self):
        # Tuesday the 6th opens 09:00 to 21:00, Friday the 9th closes at 16:00.
        # booking-api's 12:00, 17:30, 18:30 are the salon's 10:00, 15:30, 16:30:
        # 15:30 ends at 16:15, after Friday's close.
        answer = {
            **PREVIEW_NO_TIME,
            "sessions": [session(0, "2026-10-06"), session(1, "2026-10-09")],
            "available_times": ["10:00", "12:00", "17:30", "18:30"],
        }
        with self.seams(series={
            "create_series_booking": booking_api(answer),
            "_open_span": opens({4: (9, 16)}),
        }):
            data = self.post(routine(
                dry_run=True, time=..., frequency="CUSTOM", start_date=..., sessions=...,
                dates=["2026-10-06", "2026-10-09"],
            )).data
        # 08:00 is before opening on both days.
        self.assertEqual(data["available_times"], ["10:00"])

    def test_a_busy_session_stays_busy_inside_the_hours(self):
        with self.seams():
            data = self.post(routine(dry_run=True)).data
        # PREVIEW's second session is busy at booking-api; the hours give
        # nothing back.
        self.assertFalse(data["sessions"][1]["free"])
        self.assertNotIn("refusal", data["sessions"][1])

    # ---- create: refused before booking-api is asked to book

    def test_create_refuses_a_session_outside_the_hours_with_groups_code(self):
        with self.seams(series={"_open_span": opens({1: (9, 17)})}) as s:
            response = self.post(routine())
        self.assert_refused(response, "outside_hours", "sessions[0]")
        self.assertEqual(self.creates(s), [])

    def test_create_refuses_a_day_the_salon_is_shut(self):
        with self.seams(series={"_open_span": opens(closed={"2026-10-13"})}) as s:
            response = self.post(routine())
        self.assert_refused(response, "salon_closed", "sessions[1]")
        self.assertIn("2026-10-13", response.data["errors"][0]["message"])
        self.assertEqual(self.creates(s), [])

    def test_create_refuses_inside_the_notice(self):
        preview = preview_for(["2026-10-01", "2026-10-08"], engine_time="11:20", minutes=60)
        with self.seams(series={"create_series_booking": booking_api(preview)}) as s:
            response = self.post(routine(
                services=[{"id": NAILS}], start_date="2026-10-01", time="09:20",
            ))
        self.assert_refused(response, "too_soon", "sessions[0]")
        self.assertEqual(self.creates(s), [])

    def test_create_names_the_pick_when_the_picked_time_is_outside_the_hours(self):
        # Session 2 was picked at 17:00 (19:00 at booking-api); the 13th
        # closes at 17:30, and the visit would end at 17:45.
        preview = preview_for(
            ["2026-10-06", "2026-10-13"],
            s1={"start_time": "2026-10-13T19:00:00+06:00",
                "end_time": "2026-10-13T19:45:00+06:00", "picked": True},
        )
        hours = mock.Mock(side_effect=lambda salon, tz, day, day_start: (
            day_start + timedelta(hours=9),
            day_start + timedelta(hours=17, minutes=30 if day == date(2026, 10, 13) else 210),
        ))
        with self.seams(series={"create_series_booking": booking_api(preview), "_open_span": hours}) as s:
            response = self.post(routine(picks=[{"index": 1, "date": "2026-10-13", "time": "17:00"}]))
        self.assert_refused(response, "outside_hours", "picks[0]")
        self.assertEqual(self.creates(s), [])

    def test_inside_the_hours_the_plan_is_asked_first_then_the_routine_is_booked(self):
        with self.seams() as s:
            response = self.post(routine(), HTTP_IDEMPOTENCY_KEY="the-apps-key")
        self.assertEqual(response.status_code, 201, response.data)
        (plan_body,), plan_kw = s.create_series_booking.call_args_list[0]
        (book_body,), book_kw = s.create_series_booking.call_args_list[1]
        self.assertEqual((plan_body["dry_run"], plan_kw["idempotency_key"]), (True, None))
        self.assertIsNone(plan_kw.get("timeout"))
        self.assertEqual((book_body["dry_run"], book_kw["idempotency_key"]), (False, "the-apps-key"))
        self.assertEqual(book_kw["timeout"], 45)
        self.assertEqual({**plan_body, "dry_run": False}, book_body)


# ------------------------------------------------------------ the hub


@override_settings(SERIES_BOOKING_V1=True)
class DetailTests(Seams, SimpleTestCase):

    def test_the_hub_on_the_salons_clock_with_its_card(self):
        with self.seams() as s:
            response = self.get()
        self.assertEqual(response.status_code, 200, response.data)
        data = response.data
        self.assertEqual(data["salon_id"], SALON_ID)
        self.assertEqual(data["salon"]["name"], "Marina Walk")
        self.assertEqual(data["time"], "16:30")
        self.assertEqual(data["sessions"][1]["start_time"], "2026-10-13T16:30:00+04:00")
        self.assertEqual(data["created_at"], "2026-09-26T07:00:00+04:00")
        # The read needs no clock call: booking-api's offset is on its answer.
        s.engine_clock.assert_not_called()
        (series_id,), kw = s.read_series_booking.call_args
        self.assertEqual((series_id, kw["authorization"]), (SERIES_ID, "Bearer customer-token"))

    def test_not_found_comes_back_as_it_is(self):
        missing = {"detail": "Please correct the highlighted fields.", "code": "not_found",
                   "errors": [{"field": "id", "code": "not_found", "message": "No such booking."}]}
        with self.seams(series={"read_series_booking": mock.Mock(return_value=(404, missing))}):
            response = self.get()
        self.assertEqual(response.status_code, 404)
        self.assertEqual(response.data, missing)

    def test_booking_api_unreachable_is_503(self):
        down = mock.Mock(side_effect=BookingApiUnavailable("refused"))
        with self.seams(series={"read_series_booking": down}):
            self.assertEqual(self.get().status_code, 503)

    def test_needs_a_signed_in_customer(self):
        with self.seams():
            self.assertEqual(self.get(authenticated=False).status_code, 401)


# ------------------------------------------------------------ the pure part


class TranslateTests(SimpleTestCase):
    ENGINE = st.engine_zone_of(HUB)

    def test_the_example(self):
        self.assertEqual(st.to_engine_time(date(2026, 10, 6), "16:30", DUBAI, CLOCK),
                         ("2026-10-06", "18:30"))
        self.assertEqual(st.from_engine_time("2026-10-06", "18:30", DUBAI, self.ENGINE),
                         ("2026-10-06", "16:30"))

    def test_every_five_minutes_of_the_salon_day_goes_and_comes_back_the_same(self):
        day = date(2026, 10, 6)
        for minute in range(0, 24 * 60, 5):
            hhmm = f"{minute // 60:02d}:{minute % 60:02d}"
            there_day, there = st.to_engine_time(day, hhmm, DUBAI, CLOCK)
            self.assertEqual(
                st.from_engine_time(there_day, there, DUBAI, self.ENGINE),
                (day.isoformat(), hhmm),
            )

    def test_booking_apis_offset_is_read_off_its_answer(self):
        self.assertEqual(self.ENGINE.utcoffset(None).total_seconds(), 6 * 3600)
        self.assertIsNone(st.engine_zone_of({}))
        self.assertIsNone(st.engine_zone_of({"created_at": "2026-09-26T09:00:00"}))

    def test_a_clock_that_changes_between_the_days_is_refused(self):
        # London leaves summer time on 2026-10-25: 16:30 is two different
        # booking-api times on the 24th and the 31st.
        london = zoneinfo.ZoneInfo("Europe/London")
        data = {
            "services": [{"id": uuid.UUID(CUT)}], "stylist_id": None, "frequency": "CUSTOM",
            "payment_plan": "PAY_AT_SALON", "time": "16:30",
            "dates": [date(2026, 10, 24), date(2026, 10, 31)],
        }
        with self.assertRaises(st.TimeNotConstant):
            st.engine_body(data, branch_id=BRANCH, tz=london, clock=CLOCK, sent={})

    def test_a_free_time_that_lands_on_another_salon_day_is_dropped(self):
        # A salon at -10:00: booking-api's 10:00 on the 6th is 18:00 on the 5th.
        honolulu = zoneinfo.ZoneInfo("Pacific/Honolulu")
        out = st.present_preview(
            {"sessions": [{"index": 0, "date": "2026-10-06"}],
             "available_times": ["10:00", "21:00"]},
            tz=honolulu, engine_tz=self.ENGINE,
        )
        # 21:00 at +06:00 is 05:00 at -10:00 on the 6th: kept.
        self.assertEqual(out["available_times"], ["05:00"])

    def test_a_clock_is_the_engines_numbers(self):
        self.assertEqual(CLOCK, EngineClock(offset_min=360, from_min=600, to_min=1320))
