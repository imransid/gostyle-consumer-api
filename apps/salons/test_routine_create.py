"""
The routine contract's create, step C4 (docs/ROUTINE_FE_CONTRACT_AUDIT.md):

    POST /api/v1/booking/routine  ->  POST /v1/mobile-booking/series (the plan, a dry run)
                                  ->  POST /v1/mobile-booking/series (the routine)
                                  ->  GET  /v1/mobile-booking/series/:id?view=booking

Through the view, with the old routine tests' seams: no database, no
network. The salon is in Dubai (+04:00) and booking-api reads every branch
at +06:00. The salon opens 09:00 to 21:00 unless a test says otherwise.
"""

import json
import types
from contextlib import contextmanager
from unittest import mock

from django.test import SimpleTestCase, override_settings
from rest_framework.test import APIRequestFactory, force_authenticate

from apps.salons import routine_contract as rc
from apps.salons.booking_api import BookingApiUnavailable
from apps.salons.routine_views import RoutineCreateView
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
    STYLISTS,
    TENANT,
    TIMING,
    Customer,
)
from apps.salons.test_routine_contract import (
    DUBAI,
    ROUTINE_ID,
    engine_preview,
    engine_routine,
    engine_session,
)
from apps.salons.test_series_booking import CARD, NOW, opens
from apps.salons.views import _salon_card

PATH = "/api/v1/booking/routine"

# booking-api's plan for create(): weekly, 6 and 13 October at 18:30 (+06:00),
# both free, AED 105 each.
PLAN = engine_preview(
    [engine_session(0, "2026-10-06"), engine_session(1, "2026-10-13")], frequency="WEEKLY",
)
# What booking-api's real create answers (the hub); the view reads it back.
CREATED = {"id": ROUTINE_ID, "booking_type": "ROUTINE", "salon_id": BRANCH, "status": "ACTIVE"}


def salon_session(index, day, hhmm="16:30", minutes=45):
    start = f"{day}T{hhmm}:00+04:00"
    h, m = map(int, hhmm.split(":"))
    end_min = h * 60 + m + minutes
    return {
        "index": index, "date": day, "start_time": start,
        "end_time": f"{day}T{end_min // 60:02d}:{end_min % 60:02d}:00+04:00",
    }


def create(**over):
    """The app's create, as the contract's section 3 payload, for PLAN."""
    body = {
        "salon_id": SALON_ID,
        "cadence": "week",
        "services": [{"id": CUT, "amount": 100}],
        "products": [],
        "stylist_id": MAYA,
        "start_time": "2026-10-06T16:30:00+04:00",
        "sessions": [salon_session(0, "2026-10-06"), salon_session(1, "2026-10-13")],
        "amount_without_tax": 200, "tax_amount": 10, "discount": 0, "promo_code": None,
        "total": 210, "advance_paid_amount": 0, "due_amount": 210,
        "payment_status": "DRAFT", "status": "BOOKED", "booking_type": "ROUTINE",
    }
    body.update(over)
    return {k: v for k, v in body.items() if v is not ...}


def booking_api(plan=PLAN, created=(201, CREATED)):
    """POST /v1/mobile-booking/series: the plan for a dry run, then the routine."""
    def answer(body, **kw):
        if body["dry_run"]:
            return 200, json.loads(json.dumps(plan))
        code, routine = created
        return code, json.loads(json.dumps(routine))
    return mock.Mock(side_effect=answer)


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
            "create_series_booking": booking_api(),
            "read_routine_booking": mock.Mock(return_value=(200, engine_routine())),
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

    def post(self, body, *, content_type="application/json", user=None, **headers):
        request = APIRequestFactory().post(
            PATH, data=json.dumps(body) if isinstance(body, dict) else body,
            content_type=content_type, HTTP_AUTHORIZATION="Bearer customer-token", **headers,
        )
        force_authenticate(request, user=user or Customer())
        return RoutineCreateView.as_view()(request)

    def calls(self, s):
        """(plan asked, real creates) as booking-api received them: (body, kwargs)."""
        all_calls = [(c.args[0], c.kwargs) for c in s.create_series_booking.call_args_list]
        return ([c for c in all_calls if c[0]["dry_run"]], [c for c in all_calls if not c[0]["dry_run"]])

    def assert_refused(self, response, code, field, status=422):
        self.assertEqual(response.status_code, status, response.data)
        self.assertEqual(response.data["errors"][0]["code"], code)
        self.assertEqual(response.data["errors"][0]["field"], field)


def refused_by_booking_api(status, *errors):
    first = errors[0]
    return (status, {
        "detail": first["message"] if status == 409 else "Please correct the highlighted fields.",
        "code": "validation_error" if status == 422 else first["code"],
        "errors": list(errors),
    })


@override_settings(ROUTINE_CONTRACT_V1=False)
class FlagOffTests(Seams, SimpleTestCase):
    def test_off_404_and_nothing_is_asked(self):
        with self.seams() as s:
            response = self.post(create())
        self.assertEqual(response.status_code, 404)
        s.create_series_booking.assert_not_called()
        s.read_routine_booking.assert_not_called()
        s.engine_clock.assert_not_called()


@override_settings(ROUTINE_CONTRACT_V1=True, SERIES_BOOKING_CREATE_TIMEOUT=45)
class PaymentTests(Seams, SimpleTestCase):
    """Section 4: refused before anything is asked of booking-api."""

    def test_each_payment_field_refused_with_its_code(self):
        cases = [
            ({"payment_status": "PARTIALLY"}, "payment_status", "invalid_payment_status", None),
            ({"advance_paid_amount": 42}, "advance_paid_amount", "amount_mismatch", 0),
            ({"due_amount": 200}, "due_amount", "amount_mismatch", 210),
            ({"products": [{"id": "pomade", "amount": 20}]}, "products", "products_not_supported", None),
            ({"promo_code": "GOSTYLE20"}, "promo_code", "invalid_promo", None),
            ({"status": "CHECKED_IN"}, "status", "invalid_status", None),
            ({"booking_type": "SINGLE"}, "booking_type", "invalid_booking_type", None),
        ]
        for over, field, code, expected in cases:
            with self.subTest(over=over), self.seams() as s:
                response = self.post(create(**over))
                self.assert_refused(response, code, field)
                self.assertEqual(response.data["errors"][0].get("expected"), expected)
                s.create_series_booking.assert_not_called()
                s.engine_clock.assert_not_called()

    def test_the_whole_envelope_of_a_refusal_with_its_expected_figure(self):
        with self.seams():
            response = self.post(create(due_amount=200))
        self.assertEqual(response.status_code, 422)
        self.assertEqual(response.data, {
            "detail": "Please correct the highlighted fields.", "code": "validation_error",
            "errors": [{
                "field": "due_amount", "code": "amount_mismatch",
                "message": "Everything is paid at the salon: due_amount is the total.",
                "expected": 210,
            }],
        })

    def test_a_service_line_amount_is_never_checked(self):
        with self.seams() as s:
            response = self.post(create(services=[{"id": CUT, "amount": 1}]))
        self.assertEqual(response.status_code, 201, response.data)
        _, (real,) = self.calls(s)
        self.assertEqual(real[0]["services"], [{"id": CUT}])


@override_settings(ROUTINE_CONTRACT_V1=True, SERIES_BOOKING_CREATE_TIMEOUT=45)
class CreateTests(Seams, SimpleTestCase):
    def test_the_plan_first_then_the_routine_every_session_on_its_cadence(self):
        with self.seams() as s:
            response = self.post(create(), HTTP_IDEMPOTENCY_KEY="app-key-1")
        self.assertEqual(response.status_code, 201, response.data)
        (plan,), (real,) = self.calls(s)
        # The plan: a dry run from start_time, as many sessions as were sent, no key.
        self.assertEqual(plan[0]["dry_run"], True)
        self.assertEqual((plan[0]["start_date"], plan[0]["time"], plan[0]["sessions"]),
                         ("2026-10-06", "18:30", 2))
        self.assertEqual(plan[1], {
            "authorization": "Bearer customer-token", "idempotency_key": None, "tenant_id": TENANT,
        })
        # The routine: no picks, the four totals, the app's key, the long wait.
        self.assertEqual(real[0], {
            "dry_run": False, "salon_id": BRANCH, "services": [{"id": CUT}],
            "stylist_id": MAYA, "frequency": "WEEKLY",
            "start_date": "2026-10-06", "sessions": 2, "dates": None, "time": "18:30",
            "payment_plan": "PAY_AT_SALON", "picks": [], **rc.CONTRACT_OPTIONS,
            "amount_without_tax": 200, "tax_amount": 10, "discount": 0, "total": 210,
        })
        self.assertEqual(real[1], {
            "authorization": "Bearer customer-token", "idempotency_key": "app-key-1",
            "tenant_id": TENANT, "timeout": 45,
        })

    def test_one_moved_session_becomes_a_pick(self):
        sessions = [salon_session(0, "2026-10-06"), salon_session(1, "2026-10-14")]
        with self.seams() as s:
            response = self.post(create(sessions=sessions))
        self.assertEqual(response.status_code, 201, response.data)
        _, (real,) = self.calls(s)
        self.assertEqual(real[0]["picks"], [
            {"index": 1, "date": "2026-10-14", "time": "18:30", "stylist_id": None},
        ])

    def test_session_not_offered_comes_back_on_the_session(self):
        sessions = [salon_session(0, "2026-10-06"), salon_session(1, "2026-10-28")]
        refusal = refused_by_booking_api(422, {
            "field": "picks[0]", "code": "session_not_offered", "message": "Not a time the rule allows.",
        })
        with self.seams(routine={"create_series_booking": booking_api(created=refusal)}):
            response = self.post(create(sessions=sessions))
        self.assert_refused(response, "session_not_offered", "sessions[1]")

    def test_session_not_free_is_slot_taken_409_on_the_session(self):
        refusal = refused_by_booking_api(409, {
            "field": "sessions[1]", "code": "session_not_free",
            "message": "Session 2 on 2026-10-13 was taken while booking. Nothing was booked.",
        })
        with self.seams(routine={"create_series_booking": booking_api(created=refusal)}) as s:
            response = self.post(create())
        self.assert_refused(response, "slot_taken", "sessions[1]", status=409)
        self.assertEqual(response.data["code"], "slot_taken")
        s.read_routine_booking.assert_not_called()

    def test_amount_mismatch_keeps_its_expected_figure(self):
        refusal = refused_by_booking_api(422, {
            "field": "total", "code": "amount_mismatch",
            "message": "Prices changed since this routine was started.", "expected": 220,
        })
        with self.seams(routine={"create_series_booking": booking_api(created=refusal)}):
            response = self.post(create())
        self.assert_refused(response, "amount_mismatch", "total")
        self.assertEqual(response.data["errors"][0]["expected"], 220)

    def test_a_session_outside_the_salons_hours_is_refused_and_nothing_is_booked(self):
        # Session 2 moved to 20:15 in Dubai: the day ends at 20:00.
        sessions = [salon_session(0, "2026-10-06"), salon_session(1, "2026-10-14", "20:15")]
        with self.seams() as s:
            response = self.post(create(sessions=sessions))
        self.assert_refused(response, "outside_hours", "sessions[1]")
        self.assertEqual(response.data["errors"][0]["message"],
                         "A visit on 2026-10-14 at 20:15 would not start and finish within the salon's hours.")
        plans, reals = self.calls(s)
        self.assertEqual((len(plans), len(reals)), (1, 0))

    def test_a_session_on_a_closed_day_is_salon_closed_and_nothing_is_booked(self):
        with self.seams(hours=opens(closed=("2026-10-13",))) as s:
            response = self.post(create())
        self.assert_refused(response, "salon_closed", "sessions[1]")
        self.assertEqual(self.calls(s)[1], [])

    def test_the_contracts_session_checks(self):
        cases = [
            ([salon_session(1, "2026-10-06"), salon_session(0, "2026-10-13")], "invalid_sessions", "sessions"),
            ([salon_session(0, "2026-10-06"), {**salon_session(1, "2026-10-13"), "date": "2026-10-12"}],
             "date_mismatch", "sessions[1].date"),
            ([salon_session(0, "2026-10-06"), salon_session(1, "2026-10-13", minutes=60)],
             "invalid_window", "sessions[1].end_time"),
        ]
        for sessions, code, field in cases:
            with self.subTest(code=code), self.seams() as s:
                self.assert_refused(self.post(create(sessions=sessions)), code, field)
                self.assertEqual(self.calls(s)[1], [])

    def test_any_available_expert_sends_the_candidates_to_both_calls(self):
        with self.seams() as s:
            response = self.post(create(stylist_id=None))
        self.assertEqual(response.status_code, 201, response.data)
        (plan,), (real,) = self.calls(s)
        for body in (plan[0], real[0]):
            self.assertIsNone(body["stylist_id"])
            self.assertEqual(body["stylist_candidates"], sorted([MAYA, ANYA]))

    def test_booking_apis_plan_refusal_comes_back_in_our_words(self):
        refusal = refused_by_booking_api(422, {
            "field": "sessions", "code": "invalid_session_count", "message": "A routine is 2 to 6 sessions.",
        })
        api = mock.Mock(return_value=refusal)
        with self.seams(routine={"create_series_booking": api}):
            response = self.post(create(sessions=[salon_session(0, "2026-10-06")]))
        self.assert_refused(response, "invalid_session_count", "sessions")
        self.assertEqual(api.call_count, 1)


@override_settings(ROUTINE_CONTRACT_V1=True, SERIES_BOOKING_CREATE_TIMEOUT=45)
class IdempotencyTests(Seams, SimpleTestCase):
    def real_key(self, body, **headers):
        with self.seams() as s:
            self.post(body, **headers)
        _, (real,) = self.calls(s)
        return real[1]["idempotency_key"]

    def test_the_apps_key_is_passed_through(self):
        self.assertEqual(self.real_key(create(), HTTP_IDEMPOTENCY_KEY="k-42"), "k-42")

    def test_without_one_the_same_body_carries_the_same_derived_key(self):
        first, again = self.real_key(create()), self.real_key(create())
        self.assertTrue(first)
        self.assertEqual(first, again)
        self.assertNotEqual(first, self.real_key(create(total=210.0, due_amount=210.0)))

    def test_another_customer_same_body_another_key(self):
        from apps.salons.test_group_booking import RANA
        with self.seams() as s:
            self.post(create(), user=Customer(id=RANA))
        _, (real,) = self.calls(s)
        self.assertNotEqual(real[1]["idempotency_key"], self.real_key(create()))


@override_settings(ROUTINE_CONTRACT_V1=True, SERIES_BOOKING_CREATE_TIMEOUT=45)
class AnswerTests(Seams, SimpleTestCase):
    def test_the_whole_201_the_routine_as_get_booking_id_reads_it(self):
        with self.seams() as s:
            response = self.post(create())
        self.assertEqual(response.status_code, 201)
        expected = rc.present_routine(
            engine_routine(), salon_id=SALON_ID, tz=DUBAI,
            avatars={MAYA: None, ANYA: "https://cdn/anya.png"},
        )
        expected["salon"] = _salon_card(CARD, full=True)
        self.assertEqual(response.data, expected)
        self.assertEqual(response.data["cadence"], "week")
        self.assertEqual(response.data["stylists"][1]["avatar_url"], "https://cdn/anya.png")
        self.assertEqual(response.data["salon"]["latitude"], CARD["lat"])
        self.assertEqual(response.data["start_time"], "2026-10-27T16:30:00+04:00")
        s.read_routine_booking.assert_called_once_with(
            ROUTINE_ID, authorization="Bearer customer-token", tenant_id=TENANT,
        )

    def test_booking_apis_replay_of_the_same_create_is_still_201(self):
        with self.seams(routine={"create_series_booking": booking_api(created=(200, CREATED))}):
            self.assertEqual(self.post(create()).status_code, 201)

    def test_the_read_back_failing_still_answers_201_with_the_routine(self):
        failures = {
            "an error answer": {"read_routine_booking": mock.Mock(return_value=(500, None))},
            "a 404": {"read_routine_booking": mock.Mock(return_value=(404, {"code": "not_found"}))},
            "booking-api down": {"read_routine_booking": mock.Mock(side_effect=BookingApiUnavailable("x"))},
            "the salon card lookup failing": {"salon_cards_for_refs": mock.Mock(side_effect=RuntimeError("db"))},
        }
        for name, fakes in failures.items():
            with self.subTest(name=name), self.seams(routine=fakes):
                with self.assertLogs("apps.salons.routine_views", level="WARNING") as logs:
                    response = self.post(create())
            self.assertEqual(response.status_code, 201)
            self.assertEqual(response.data, {"id": ROUTINE_ID, "booking_type": "ROUTINE"})
            self.assertIn(ROUTINE_ID, logs.output[0])

    def test_booking_api_down_is_our_503(self):
        down = mock.Mock(side_effect=BookingApiUnavailable("down"))
        with self.seams(routine={"create_series_booking": down}):
            response = self.post(create())
        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.data["errors"][0]["code"], "booking_api_unavailable")

    def test_booking_api_down_on_the_real_create_is_503_too(self):
        def plan_then_down(body, **kw):
            if body["dry_run"]:
                return 200, json.loads(json.dumps(PLAN))
            raise BookingApiUnavailable("down")
        with self.seams(routine={"create_series_booking": mock.Mock(side_effect=plan_then_down)}) as s:
            response = self.post(create())
        self.assertEqual(response.status_code, 503)
        s.read_routine_booking.assert_not_called()


@override_settings(ROUTINE_CONTRACT_V1=True, SERIES_BOOKING_CREATE_TIMEOUT=45)
class OldChecksTests(Seams, SimpleTestCase):
    """The preview's checks, in its order, before booking-api is asked."""

    def test_json_only(self):
        with self.seams() as s:
            response = self.post("x=1", content_type="application/x-www-form-urlencoded")
        self.assertEqual(response.status_code, 415)
        s.create_series_booking.assert_not_called()

    def test_an_unknown_salon_is_404(self):
        with self.seams(group={"salon_profile": mock.Mock(return_value=None)}) as s:
            self.assertEqual(self.post(create()).status_code, 404)
        s.create_series_booking.assert_not_called()

    def test_a_service_not_sold_here_is_foreign_id(self):
        with self.seams() as s:
            response = self.post(create(services=[{"id": CUT}, {"id": ELSEWHERE}]))
        self.assert_refused(response, "foreign_id", "services")
        s.create_series_booking.assert_not_called()

    def test_a_stylist_who_cannot_do_the_services_is_stylist_mismatch(self):
        with self.seams() as s:
            response = self.post(create(stylist_id=ANYA, services=[{"id": CUT}, {"id": NAILS}]))
        self.assert_refused(response, "stylist_mismatch", "stylist_id")
        s.create_series_booking.assert_not_called()

    def test_a_start_time_without_an_offset_is_invalid_time_before_booking_api(self):
        with self.seams() as s:
            response = self.post(create(start_time="2026-10-06T16:30:00"))
        self.assert_refused(response, "invalid_time", "start_time")
        s.create_series_booking.assert_not_called()

    def test_no_services_is_no_services(self):
        with self.seams() as s:
            response = self.post(create(services=[]))
        self.assert_refused(response, "no_services", "services")
        s.create_series_booking.assert_not_called()


class ReadRoutineBookingTests(SimpleTestCase):
    def test_it_reads_the_routine_as_one_booking(self):
        from apps.salons import booking_api
        with mock.patch("apps.salons.booking_api._send", return_value=(200, {})) as send:
            booking_api.read_routine_booking(ROUTINE_ID, authorization="Bearer t", tenant_id=TENANT)
        send.assert_called_once_with(
            "GET", f"/v1/mobile-booking/series/{ROUTINE_ID}?view=booking",
            headers={"Authorization": "Bearer t", "X-Tenant-Id": TENANT},
        )
