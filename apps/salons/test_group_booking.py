"""
Group bookings: the translation, the request rules, the client and the views.

SimpleTestCase throughout, like apps/salons/tests.py: nothing here may touch a
database. The views are driven with their seams replaced -- the salon
selectors, the account lookup, the clock, and every booking-api call --
because what is worth asserting is what crosses each boundary, and in which
order.
"""

import json
import types
import urllib.error
import uuid
import zoneinfo
from contextlib import contextmanager
from datetime import date, datetime, timedelta
from unittest import mock

from django.test import SimpleTestCase, override_settings
from rest_framework.test import APIRequestFactory, force_authenticate

from apps.salons import booking_api
from apps.salons import group_translate as gt
from apps.salons.booking_api import BookingApiUnavailable
from apps.salons.group_serializers import GroupAvailabilityRequestSerializer
from apps.salons.group_translate import EngineClock
from apps.salons.group_views import GroupAvailabilityView

# ------------------------------------------------------------ the fixture

DUBAI = zoneinfo.ZoneInfo("Asia/Dubai")

# booking-api's clock as its settings publish it today: +06:00, 10:00-22:00.
CLOCK = EngineClock(offset_min=360, from_min=600, to_min=1320)

DAY = date(2026, 10, 11)
SALON_ID = "c6c248ab-f2cd-4f12-a31e-243c6e64b3b5"
TENANT, BRANCH = "tenant-1", "branch-1"
USER_ID = uuid.UUID("11111111-1111-4111-8111-111111111111")
RANA = "22222222-2222-4222-8222-222222222222"      # a registered member

CUT = "0b7c0000-0000-4000-8000-000000000001"      # 45 minutes, no notice, 120.00
NAILS = "0b7c0000-0000-4000-8000-000000000002"    # 60 staged minutes, 30 notice, 175.00
ELSEWHERE = "0b7c0000-0000-4000-8000-0000000000ff"  # another salon's service
MAYA = "7d1e0000-0000-4000-8000-00000000000a"
ANYA = "7d1e0000-0000-4000-8000-00000000000b"
STRANGER = "7d1e0000-0000-4000-8000-00000000000c"  # not on this salon's roster

MINUTES = {CUT: 45, NAILS: 60}

def resolved():
    """A party as the booking view holds it: validated, and every name filled in."""
    return [
        {"ref": 0, "kind": "guest", "id": None, "name": "Amal", "age_group": "adult",
         "service_ids": [CUT], "stylist_id": None},
        {"ref": 1, "kind": "self", "id": str(USER_ID), "name": "Dana", "age_group": "adult",
         "service_ids": [NAILS], "stylist_id": MAYA},
        {"ref": 2, "kind": "registered", "id": RANA, "name": "Rana Hassan", "age_group": "child",
         "service_ids": [CUT], "stylist_id": None},
    ]


def party_to_plan():
    """An availability party: refs out of order, so matching by ref is visible."""
    return [
        {"ref": 2, "service_ids": [NAILS], "stylist_id": MAYA},
        {"ref": 0, "service_ids": [CUT]},
        {"ref": 1, "service_ids": [CUT], "stylist_id": None},
    ]


def at(hhmm, tz=DUBAI, day=DAY):
    hour, minute = map(int, hhmm.split(":"))
    return datetime(day.year, day.month, day.day, hour, minute, tzinfo=tz)


# ------------------------------------------------------------ the clocks


class EngineClockTranslationTests(SimpleTestCase):
    def test_an_instant_becomes_the_engines_minute(self):
        # 15:00 in Dubai is 11:00Z is 17:00 on the engine's +06:00 clock.
        self.assertEqual(gt.to_engine(at("15:00"), CLOCK), ("2026-10-11", 1020))

    def test_the_offset_is_the_one_read_not_one_assumed(self):
        at_dubai_offset = CLOCK._replace(offset_min=240)
        self.assertEqual(gt.to_engine(at("15:00"), at_dubai_offset), ("2026-10-11", 900))

    def test_and_back_again_to_the_same_instant(self):
        back = gt.from_engine("2026-10-11", 1020, CLOCK)
        self.assertEqual(back, at("15:00"))
        self.assertEqual(back.astimezone(DUBAI).isoformat(), "2026-10-11T15:00:00+04:00")

    def test_the_engines_day_in_the_salons_hours(self):
        # 10:00-22:00 at +06:00 is 08:00-20:00 in Dubai.
        self.assertEqual(gt.engine_span(DAY, CLOCK), (at("08:00"), at("20:00")))

    def test_hhmm(self):
        self.assertEqual(gt.hhmm_minutes("17:30"), 1050)
        for bad in ("5:30", "17.30", "", None):
            with self.subTest(bad), self.assertRaises(ValueError):
                gt.hhmm_minutes(bad)


# ------------------------------------------------------------ the party


class PartyTranslationTests(SimpleTestCase):
    def test_the_booker_goes_first_and_the_rest_keep_their_order(self):
        self.assertEqual(gt.booker_first(resolved()), [1, 0, 2])

    def test_a_party_with_no_booker_keeps_the_apps_order(self):
        self.assertEqual(gt.booker_first(party_to_plan()), [0, 1, 2])

    def test_accounts_carry_their_ids_and_guests_their_names(self):
        people = gt.participants(resolved(), [1, 0, 2])
        self.assertEqual(people, [
            {"label": "Dana", "serviceIds": [NAILS], "customerId": str(USER_ID),
             "preferredStaffId": MAYA},
            {"label": "Amal", "serviceIds": [CUT], "guestName": "Amal"},
            {"label": "Rana Hassan", "serviceIds": [CUT], "customerId": RANA},
        ])

    def test_planning_sends_no_identity_and_labels_by_ref(self):
        # booking-api's availability route answers 400 to either field.
        people = gt.participants(party_to_plan(), [0, 1, 2], planning=True)
        self.assertEqual([p["label"] for p in people], ["Member 2", "Member 0", "Member 1"])
        for p in people:
            self.assertNotIn("customerId", p)
            self.assertNotIn("guestName", p)

    def test_the_plan_body_holds_only_what_its_route_accepts(self):
        """
        booking-api refuses any key it does not declare (forbidNonWhitelisted),
        so this set IS its DTO. Nothing the app sent beyond it -- kinds, refs,
        ages -- may leak into it.
        """
        plan = gt.plan_body(BRANCH, "2026-10-11", [660, 690], resolved(), [1, 0, 2])
        self.assertEqual(set(plan), {"branchId", "day", "targetMins", "mode", "participants"})
        for p in plan["participants"]:
            self.assertLessEqual(set(p), {"label", "serviceIds", "preferredStaffId"})
        wire = json.dumps(plan)
        for leaked in ("age_group", "adult", "child", "kind", "registered", "ref"):
            self.assertNotIn(f'"{leaked}"', wire)

    def test_a_visit_is_the_sum_of_its_services(self):
        members = [{"service_ids": [CUT, NAILS]}, {"service_ids": ["not-on-the-menu"]}]
        self.assertEqual(gt.member_minutes(members, MINUTES), [105, 0])


# ------------------------------------------------------------ when


class OfferedStartTests(SimpleTestCase):
    """The day's starts: salon hours and the engine's day. Not the clock."""

    DAY_START = at("00:00")
    OPEN = (at("09:00"), at("21:00"))

    def starts(self, *, open_span=OPEN, clock=CLOCK, longest=60):
        return gt.offered_starts(
            open_span, gt.engine_span(DAY, clock), self.DAY_START, longest,
        )

    def test_the_overlap_of_the_salons_hours_and_the_engines_day(self):
        found = self.starts()
        # Open 09:00-21:00; the engine plans until 20:00 Dubai; an hour-long
        # visit must END by then, so the last start is 19:00.
        self.assertEqual(found[0], at("09:00"))
        self.assertEqual(found[-1], at("19:00"))
        self.assertEqual(len(found), 21)
        self.assertTrue(all((s - found[0]) % timedelta(minutes=30) == timedelta(0) for s in found))

    def test_the_longest_visit_decides_the_last_start(self):
        self.assertEqual(self.starts(longest=120)[-1], at("18:00"))

    def test_a_closed_salon_has_none(self):
        self.assertEqual(self.starts(open_span=None), [])

    def test_hours_that_never_meet_the_engines_day(self):
        self.assertEqual(self.starts(open_span=(at("20:30"), at("23:30"))), [])

    def test_a_long_day_is_asked_in_runs_the_engine_accepts(self):
        runs = gt.batches(list(range(50)))
        self.assertEqual([len(r) for r in runs], [24, 24, 2])
        self.assertEqual(sum(runs, []), list(range(50)))
        self.assertEqual(gt.batches([]), [])


# ------------------------------------------------------------ answers back


CARDS = {
    MAYA: {"id": MAYA, "name": "Maya E."},
    ANYA: {"id": ANYA, "name": "Anya"},
}
STAFF = [MAYA, ANYA, STRANGER]  # the engine's pick for participants 0, 1, 2


def plan_entry(minute, participants, *, fits=True):
    """One entry of booking-api's {starts: [...]}, as it writes one."""
    base = {
        "branchId": BRANCH, "tradingDay": "2026-10-11", "targetMin": minute,
        "target": f"{minute // 60:02d}:{minute % 60:02d}", "mode": "TOGETHER",
        "advisory": "Advisory. Nothing is held.",
    }
    if not fits:
        return {**base, "feasible": False, "lanes": [],
                "reason": "Nobody available covers Member 2's services at this time.",
                "remedy": "Change that service, or try another time."}
    lanes = []
    for i, p in enumerate(participants):
        length = sum(MINUTES.get(s, 0) for s in p["serviceIds"])
        lanes.append({"label": p["label"], "staffId": STAFF[i],
                      "start": base["target"], "end": "", "startMin": minute,
                      "endMin": minute + length, "resourceType": "styling"})
    return {**base, "feasible": True, "lanes": lanes}


class DaySlotsTests(SimpleTestCase):
    ORDER = [0, 1, 2]
    OFFERED = [at("11:30"), at("15:00"), at("15:30"), at("17:00")]

    def slots(self, answers):
        return gt.day_slots(self.OFFERED, answers, "2026-10-11", party_to_plan(),
                            self.ORDER, CLOCK, DUBAI, CARDS, 60)

    def people(self):
        return gt.participants(party_to_plan(), self.ORDER, planning=True)

    def test_a_start_that_fits_carries_the_plan_by_ref(self):
        found = self.slots({at("15:00"): plan_entry(1020, self.people())})
        slot = found[1]
        self.assertEqual((slot["start"], slot["available"]), ("2026-10-11T15:00:00+04:00", True))
        # The longest visit ends it: the hour of nails.
        self.assertEqual(slot["end"], "2026-10-11T16:00:00+04:00")
        self.assertEqual(slot["plan"], [
            {"ref": 2, "stylist": CARDS[MAYA],
             "start": "2026-10-11T15:00:00+04:00", "end": "2026-10-11T16:00:00+04:00"},
            {"ref": 0, "stylist": CARDS[ANYA],
             "start": "2026-10-11T15:00:00+04:00", "end": "2026-10-11T15:45:00+04:00"},
            {"ref": 1, "stylist": {"id": STRANGER, "name": None},
             "start": "2026-10-11T15:00:00+04:00", "end": "2026-10-11T15:45:00+04:00"},
        ])

    def test_every_offered_start_comes_back_and_the_rest_are_struck_through(self):
        found = self.slots({
            at("15:00"): plan_entry(1020, self.people()),
            at("15:30"): plan_entry(1050, self.people(), fits=False),
        })
        self.assertEqual([s["available"] for s in found], [False, True, False, False])
        unasked, _, refused, _ = found
        for slot in (unasked, refused):
            self.assertEqual(slot["plan"], [])
        # An unavailable row still shows how long the party would take.
        self.assertEqual((unasked["start"], unasked["end"]),
                         ("2026-10-11T11:30:00+04:00", "2026-10-11T12:30:00+04:00"))

    def test_an_answer_missing_a_lane_is_not_guessed_at(self):
        short = plan_entry(1020, self.people()[:2])
        with self.assertRaises(ValueError):
            self.slots({at("15:00"): short})

    def test_three_bands_in_order_and_an_empty_one_left_out(self):
        found = self.slots({})
        bands = gt.banded(self.OFFERED, found, at("00:00"))
        self.assertEqual([b["label"] for b in bands], ["Morning", "Afternoon", "Evening"])
        self.assertEqual([len(b["slots"]) for b in bands], [1, 2, 1])

        afternoon_only = gt.banded(self.OFFERED[1:3], found[1:3], at("00:00"))
        self.assertEqual([b["label"] for b in afternoon_only], ["Afternoon"])

    def test_noon_is_afternoon_and_five_is_evening(self):
        edges = [at("11:59"), at("12:00"), at("16:59"), at("17:00")]
        rows = [{"start": e.isoformat()} for e in edges]
        bands = {b["label"]: [r["start"][11:16] for r in b["slots"]]
                 for b in gt.banded(edges, rows, at("00:00"))}
        self.assertEqual(bands, {"Morning": ["11:59"], "Afternoon": ["12:00", "16:59"],
                                 "Evening": ["17:00"]})


# ------------------------------------------------------------ the request


def availability_body(**over):
    return {"salon_id": SALON_ID, "date": "2026-10-11", "members": party_to_plan(), **over}


class GroupRequestTests(SimpleTestCase):
    def codes(self, body):
        s = GroupAvailabilityRequestSerializer(data=body)
        self.assertFalse(s.is_valid())
        found = []

        def walk(node):
            if isinstance(node, dict):
                for v in node.values():
                    walk(v)
            elif isinstance(node, list):
                for v in node:
                    walk(v)
            else:
                found.append(node.code)

        walk(s.errors)
        return found

    def test_a_good_party_is_accepted(self):
        s = GroupAvailabilityRequestSerializer(data=availability_body())
        self.assertTrue(s.is_valid(), s.errors)

    def test_two_to_eight(self):
        one = [{"ref": 0, "service_ids": [CUT]}]
        nine = [{"ref": i, "service_ids": [CUT]} for i in range(9)]
        for members in (one, nine):
            with self.subTest(len(members)):
                self.assertEqual(self.codes(availability_body(members=members)), ["invalid_party_size"])

    def test_every_ref_is_its_own(self):
        members = party_to_plan()
        members[1]["ref"] = members[0]["ref"]
        self.assertEqual(self.codes(availability_body(members=members)), ["duplicate_ref"])

    def test_every_member_needs_a_service(self):
        members = party_to_plan()
        members[0]["service_ids"] = []
        self.assertEqual(self.codes(availability_body(members=members)), ["member_no_services"])

    def test_ids_are_uuids(self):
        members = party_to_plan()
        members[0]["service_ids"] = ["svc_fade"]
        self.assertEqual(self.codes(availability_body(members=members)), ["invalid"])

    def test_a_date_that_is_not_a_date(self):
        # booking-api answers this one with a 500. It never gets the chance.
        self.assertIn("invalid", self.codes(availability_body(date="2027-13-01")))


# ------------------------------------------------------------ the client


@override_settings(BOOKING_API_URL="http://booking/")
class GroupClientTests(SimpleTestCase):
    """The five new calls, with the network replaced."""

    def respond(self, status=201, body=b"{}"):
        response = mock.MagicMock()
        response.status = status
        response.read.return_value = body
        response.__enter__.return_value = response
        return mock.patch.object(booking_api.urllib.request, "urlopen", return_value=response)

    def refuse(self, status, body):
        error = urllib.error.HTTPError("http://booking/", status, "", {}, None)
        error.read = lambda: body
        return mock.patch.object(booking_api.urllib.request, "urlopen", side_effect=error)

    @staticmethod
    def sent(urlopen):
        request = urlopen.call_args.args[0]
        return request, {k.lower(): v for k, v in request.header_items()}

    SETTINGS = json.dumps({
        "branch": {"utcOffsetMinutes": 360, "timezone": "Asia/Dhaka"},
        "tradingWindow": {"fromMin": 600, "toMin": 1320},
    }).encode()

    def test_the_clock_is_read_from_settings_with_the_callers_token(self):
        with self.respond(200, self.SETTINGS) as urlopen:
            clock = booking_api.engine_clock(authorization="Bearer t", tenant_id=TENANT, branch_id=BRANCH)
        self.assertEqual(clock, EngineClock(360, 600, 1320))
        request, headers = self.sent(urlopen)
        self.assertEqual(request.get_method(), "GET")
        self.assertEqual(request.full_url, f"http://booking/v1/bookings/settings?branchId={BRANCH}")
        self.assertEqual((headers["authorization"], headers["x-tenant-id"]), ("Bearer t", TENANT))

    def test_a_clock_is_never_guessed(self):
        for status, body in (
            (200, json.dumps({"branch": {}, "tradingWindow": {"fromMin": 600, "toMin": 1320}}).encode()),
            (200, json.dumps({"branch": {"utcOffsetMinutes": True},
                              "tradingWindow": {"fromMin": 600, "toMin": 1320}}).encode()),
            (200, b"[]"),
            (401, b'{"message":"Missing bearer token"}'),
        ):
            with self.subTest(status=status, body=body):
                patch = self.respond(status, body) if status == 200 else self.refuse(status, body)
                with patch, self.assertRaises(BookingApiUnavailable):
                    booking_api.engine_clock(authorization="Bearer t")

    def test_each_call_goes_where_it_should(self):
        cases = (
            (lambda: booking_api.plan_group({"a": 1}, authorization="Bearer t", tenant_id=TENANT),
             "POST", "http://booking/v1/bookings/availability/group"),
        )
        for call, method, url in cases:
            with self.subTest(url), self.respond(201, b'{"ok":true}') as urlopen:
                self.assertEqual(call(), (201, {"ok": True}))
                request, headers = self.sent(urlopen)
                self.assertEqual((request.get_method(), request.full_url), (method, url))
                self.assertEqual(headers["authorization"], "Bearer t")
                self.assertEqual(headers["x-tenant-id"], TENANT)
                self.assertNotIn("idempotency-key", headers)
                if method == "POST":
                    self.assertEqual(json.loads(request.data), {"a": 1})

    def test_a_refusal_is_an_answer(self):
        with self.refuse(409, b'{"message":"Nobody available covers Dana\'s services at this time."}'):
            status, body = booking_api.plan_group({}, authorization="Bearer t")
        self.assertEqual(status, 409)
        self.assertIn("Nobody available", body["message"])

    def test_unreachable_or_html_raises(self):
        with mock.patch.object(booking_api.urllib.request, "urlopen", side_effect=TimeoutError("slow")):
            with self.assertRaises(BookingApiUnavailable):
                booking_api.plan_group({}, authorization="Bearer t")
        with self.respond(502, b"<html>Bad Gateway</html>"), self.assertRaises(BookingApiUnavailable):
            booking_api.plan_group({}, authorization="Bearer t")


# ------------------------------------------------------------ the views


class Customer:
    """The signed-in caller, with an id and no database row."""

    is_authenticated = True

    def __init__(self, id=USER_ID, full_name="Dana"):
        self.id = id
        self.full_name = full_name


SALON = types.SimpleNamespace(id=uuid.UUID(SALON_ID), branch_timezone="Asia/Dubai")
TIMING = [
    {"id": uuid.UUID(CUT), "name": "Cut", "price_minor": 12000,
     "duration_minutes": 45, "stage_minutes": None, "lead_time_minutes": None},
    {"id": uuid.UUID(NAILS), "name": "Nails", "price_minor": 17500,
     "duration_minutes": 50, "stage_minutes": 60, "lead_time_minutes": 30},
]
STYLISTS = [
    types.SimpleNamespace(id=uuid.UUID(MAYA), first_name="Maya", last_name="E.",
                          job_title="Senior Stylist", avatar_url=None),
    types.SimpleNamespace(id=uuid.UUID(ANYA), first_name="Anya", last_name=None,
                          job_title=None, avatar_url="https://cdn/anya.png"),
]
# Who can do what, as stylist_service_coverage answers: Maya everything,
# Anya only the cut.
COVERAGE = {
    uuid.UUID(MAYA): [uuid.UUID(CUT), uuid.UUID(NAILS)],
    uuid.UUID(ANYA): [uuid.UUID(CUT)],
}


def planned(body, fits=frozenset({660, 1020})):
    """booking-api's {starts: [...]} for this body: two starts fit, the rest do not."""
    return 201, {"starts": [
        plan_entry(m, body["participants"], fits=m in fits) for m in body["targetMins"]
    ]}


class ViewSeams:
    """Every seam group_views has, replaced. Each test overrides what it needs."""

    NOW = at("07:00")

    @contextmanager
    def seams(self, **over):
        seams = {
            "salon_profile": mock.Mock(return_value=SALON),
            "booking_route": mock.Mock(return_value={"tenant_id": TENANT, "branch_id": BRANCH}),
            "service_timing_rows": mock.Mock(side_effect=lambda salon, ids: [
                row for row in TIMING if str(row["id"]) in ids]),
            "salon_stylists": mock.Mock(return_value=STYLISTS),
            "stylist_service_coverage": mock.Mock(return_value=COVERAGE),
            "_open_span": mock.Mock(side_effect=lambda salon, tz, day, day_start: (
                day_start + timedelta(hours=9), day_start + timedelta(hours=21))),
            "_now": mock.Mock(side_effect=lambda tz: self.NOW.astimezone(tz)),
            "engine_clock": mock.Mock(return_value=CLOCK),
            "plan_group": mock.Mock(side_effect=lambda body, **kw: planned(body)),
        }
        seams.update(over)
        patches = [mock.patch(f"apps.salons.group_views.{name}", fake) for name, fake in seams.items()]
        for p in patches:
            p.start()
        try:
            yield types.SimpleNamespace(**seams)
        finally:
            for p in patches:
                p.stop()

    def post(self, view, path, body, *, user=None, authenticated=True,
             content_type="application/json", **headers):
        request = APIRequestFactory().post(
            path, data=json.dumps(body) if isinstance(body, dict) else body,
            content_type=content_type, HTTP_AUTHORIZATION="Bearer customer-token", **headers,
        )
        if authenticated:
            force_authenticate(request, user=user or Customer())
        return view.as_view()(request)

    def assert_refused(self, response, code, field=None):
        self.assertEqual(response.status_code, 422, response.data)
        self.assertEqual(response.data["errors"][0]["code"], code)
        if field is not None:
            self.assertEqual(response.data["errors"][0]["field"], field)


class PartyChecks:
    """The 422s both views give before booking-api is asked anything."""

    SERVICES_FIELD = None

    def test_a_service_from_another_salon_is_foreign(self):
        with self.seams() as s:
            response = self.send(self.with_services(ELSEWHERE))
        self.assert_refused(response, "foreign_id", self.SERVICES_FIELD)
        s.engine_clock.assert_not_called()

    def test_a_stylist_off_the_roster_is_foreign(self):
        with self.seams() as s:
            response = self.send(self.with_stylist(STRANGER))
        self.assert_refused(response, "foreign_id", "stylist_id")
        s.engine_clock.assert_not_called()

    def test_a_stylist_who_cannot_do_the_work(self):
        with self.seams() as s:
            response = self.send(self.with_stylist(ANYA))   # Anya does not do nails
        self.assert_refused(response, "stylist_mismatch", "stylist_id")
        s.engine_clock.assert_not_called()

    def test_one_stylist_for_two_members(self):
        with self.seams() as s:
            response = self.send(self.with_stylist(MAYA, everyone=True))
        self.assert_refused(response, "stylist_repeated", "stylist_id")
        s.engine_clock.assert_not_called()

    def test_the_skill_check_asks_about_the_chosen_stylists_only(self):
        with self.seams() as s:
            self.send(self.with_stylist(MAYA))
        _, service_ids, staff_ids = s.stylist_service_coverage.call_args.args
        self.assertEqual(staff_ids, [uuid.UUID(MAYA)])
        self.assertEqual(service_ids, [uuid.UUID(NAILS)])


class GroupAvailabilityViewTests(PartyChecks, ViewSeams, SimpleTestCase):
    PATH = "/api/v1/booking/group-availability"
    SERVICES_FIELD = "service_ids"

    def send(self, body=None, **kw):
        return self.post(GroupAvailabilityView, self.PATH, body or availability_body(), **kw)

    def with_services(self, service_id):
        members = party_to_plan()
        members[1]["service_ids"] = [service_id]
        return availability_body(members=members)

    def with_stylist(self, stylist_id, everyone=False):
        members = party_to_plan()
        for m in members if everyone else members[:1]:
            m["stylist_id"] = stylist_id
        return availability_body(members=members)

    def slots(self, data):
        return [slot for band in data["bands"] for slot in band["slots"]]

    def test_one_engine_call_for_the_whole_day(self):
        with self.seams() as s:
            response = self.send()
        self.assertEqual(response.status_code, 200)
        s.plan_group.assert_called_once()
        body = s.plan_group.call_args.args[0]
        # 09:00-19:00 Dubai at half hours is 11:00-21:00 on the engine's clock.
        self.assertEqual(body["targetMins"], list(range(660, 1261, 30)))
        self.assertEqual(body["day"], "2026-10-11")
        self.assertEqual(body["branchId"], BRANCH)
        self.assertEqual([p["label"] for p in body["participants"]], ["Member 2", "Member 0", "Member 1"])
        kw = s.plan_group.call_args.kwargs
        self.assertEqual((kw["authorization"], kw["tenant_id"]), ("Bearer customer-token", TENANT))
        s.engine_clock.assert_called_once_with(
            authorization="Bearer customer-token", tenant_id=TENANT, branch_id=BRANCH)

    def test_the_day_in_three_bands_every_start_in_it(self):
        with self.seams():
            data = self.send().data
        self.assertEqual(set(data), {"date", "bands"})
        self.assertEqual(data["date"], "2026-10-11")
        self.assertEqual([(b["label"], len(b["slots"])) for b in data["bands"]],
                         [("Morning", 6), ("Afternoon", 10), ("Evening", 5)])
        available = [s["start"] for s in self.slots(data) if s["available"]]
        self.assertEqual(available, ["2026-10-11T09:00:00+04:00", "2026-10-11T15:00:00+04:00"])

    def test_the_plan_names_each_member_by_ref(self):
        with self.seams():
            data = self.send().data
        three = next(s for s in self.slots(data) if s["start"].startswith("2026-10-11T15:00"))
        self.assertEqual([p["ref"] for p in three["plan"]], [2, 0, 1])
        self.assertEqual(three["plan"][0]["stylist"], {"id": MAYA, "name": "Maya E."})
        self.assertEqual(three["plan"][1]["stylist"], {"id": ANYA, "name": "Anya"})
        self.assertEqual(three["end"], "2026-10-11T16:00:00+04:00")

    def test_starts_already_gone_are_offered_but_never_asked(self):
        self.NOW = at("13:50")  # + 30 minutes' notice for the nails = 14:20
        with self.seams() as s:
            data = self.send().data
        self.assertEqual(s.plan_group.call_args.args[0]["targetMins"][0], 16 * 60 + 30)  # 14:30 Dubai
        slots = self.slots(data)
        self.assertEqual(slots[0]["start"], "2026-10-11T09:00:00+04:00")
        self.assertFalse(slots[0]["available"])     # fits, per the engine, but it has passed

    def test_a_closed_salon_asks_nobody(self):
        with self.seams(_open_span=mock.Mock(return_value=None)) as s:
            response = self.send()
        self.assertEqual((response.status_code, response.data), (200, {"date": "2026-10-11", "bands": []}))
        s.engine_clock.assert_not_called()
        s.plan_group.assert_not_called()

    def test_a_day_with_nothing_left_asks_nobody(self):
        self.NOW = at("19:45")  # plus 30 minutes' notice: past the last start
        with self.seams() as s:
            data = self.send().data
        self.assertEqual(len(self.slots(data)), 21)
        self.assertFalse(any(slot["available"] for slot in self.slots(data)))
        s.plan_group.assert_not_called()

    def test_a_day_longer_than_one_call_is_asked_in_several(self):
        wide = EngineClock(offset_min=240, from_min=0, to_min=1440)
        whole_day = mock.Mock(side_effect=lambda salon, tz, day, ds: (ds, ds + timedelta(hours=23, minutes=59)))
        self.NOW = at("00:00") - timedelta(hours=1)
        with self.seams(engine_clock=mock.Mock(return_value=wide), _open_span=whole_day) as s:
            data = self.send().data
        asked = [c.args[0]["targetMins"] for c in s.plan_group.call_args_list]
        self.assertEqual([len(a) for a in asked], [24, 22])
        self.assertEqual(sum(asked, []), list(range(0, 22 * 60 + 31, 30)))
        self.assertEqual(len(self.slots(data)), 46)

    def test_the_engines_refusal_is_forwarded_as_it_is(self):
        refusal = {"statusCode": 422, "message": "Member 0: one or more services do not exist.",
                   "error": "Unprocessable Entity"}
        with self.seams(plan_group=mock.Mock(return_value=(422, refusal))):
            response = self.send()
        self.assertEqual((response.status_code, response.data), (422, refusal))

    def test_booking_api_down_is_503(self):
        for name in ("engine_clock", "plan_group"):
            with self.subTest(name), self.seams(**{name: mock.Mock(side_effect=BookingApiUnavailable("down"))}):
                response = self.send()
            self.assertEqual(response.status_code, 503)
            self.assertEqual(response.data["errors"][0]["code"], "booking_api_unavailable")

    def test_an_answer_that_does_not_line_up_is_503_not_a_guess(self):
        for answer in ({"starts": []}, {"starts": None}, []):
            with self.subTest(answer), self.seams(plan_group=mock.Mock(return_value=(201, answer))):
                self.assertEqual(self.send().status_code, 503)

    def test_the_door(self):
        with self.seams() as s:
            self.assertEqual(self.send(authenticated=False).status_code, 401)
            self.assertEqual(self.send(body=b"salon_id=x", content_type="application/x-www-form-urlencoded").status_code, 415)
        with self.seams(salon_profile=mock.Mock(return_value=None)):
            self.assertEqual(self.send().status_code, 404)
        with self.seams(booking_route=mock.Mock(return_value=None)):
            self.assertEqual(self.send().status_code, 404)
        s.plan_group.assert_not_called()

    def test_a_bad_party_is_422_in_this_projects_envelope(self):
        with self.seams() as s:
            response = self.send(availability_body(members=party_to_plan()[:1]))
        self.assert_refused(response, "invalid_party_size", "members")
        self.assertEqual(response.data["code"], "validation_error")
        s.plan_group.assert_not_called()
