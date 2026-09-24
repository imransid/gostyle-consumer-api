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
from decimal import Decimal
from unittest import mock

from django.core.cache import cache
from django.test import SimpleTestCase, override_settings
from rest_framework.test import APIRequestFactory, force_authenticate

from apps.salons import booking_api
from apps.salons import group_translate as gt
from apps.salons.booking_api import BookingApiUnavailable
from apps.salons.group_serializers import (
    GroupAvailabilityRequestSerializer,
    GroupBookingRequestSerializer,
)
from apps.salons.group_translate import EngineClock
from apps.salons.group_views import GroupAvailabilityView, GroupBookingCreateView

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

GROUP_ID = "b2c6b0a9-17aa-4be8-91b2-11668d3b2e5a"
HOLD_ID = "05b9f8df-2c02-4178-8aaa-688f803bed6e"


def member(ref, kind, services, *, age_group="adult", **extra):
    return {
        "ref": ref,
        "kind": kind,
        "age_group": age_group,
        "services": [{"id": s, "amount": 120} for s in services],
        **extra,
    }


def party():
    """The booker in the MIDDLE, so every reorder is visible."""
    return [
        member(0, "guest", [CUT], name="Amal"),
        member(1, "self", [NAILS], id=str(USER_ID), stylist_id=MAYA),
        member(2, "registered", [CUT], id=RANA, name="typed by the booker", age_group="child"),
    ]


def resolved():
    """party() as the view holds it: validated, and every name filled in."""
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

    def test_each_engine_body_holds_only_what_its_route_accepts(self):
        """
        booking-api refuses any key it does not declare (forbidNonWhitelisted),
        so these sets ARE its DTOs. Nothing the app sent beyond them -- ages,
        kinds, refs, products, amounts -- may leak into one.
        """
        members, order = resolved(), [1, 0, 2]
        plan = gt.plan_body(BRANCH, "2026-10-11", [660, 690], members, order)
        hold = gt.hold_body(BRANCH, "2026-10-11", 1020, members, order)
        confirm = gt.confirm_body(HOLD_ID, members, order)

        self.assertEqual(set(plan), {"branchId", "day", "targetMins", "mode", "participants"})
        self.assertEqual(set(hold), {"branchId", "day", "targetMin", "mode", "arrangement", "participants"})
        self.assertEqual(set(confirm), {"holdId", "participants"})
        for p in plan["participants"] + confirm["participants"]:
            self.assertLessEqual(set(p), {"label", "serviceIds", "preferredStaffId"})
        for p in hold["participants"]:
            self.assertLessEqual(set(p), {"label", "serviceIds", "customerId", "guestName", "preferredStaffId"})

        wire = json.dumps([plan, hold, confirm])
        for leaked in ("age_group", "adult", "child", "amount", "products", "kind", "registered", "ref"):
            self.assertNotIn(f'"{leaked}"', wire)

    def test_everyone_arrives_together_and_the_booker_pays(self):
        hold = gt.hold_body(BRANCH, "2026-10-11", 1020, resolved(), [1, 0, 2])
        self.assertEqual((hold["mode"], hold["arrangement"]), ("TOGETHER", "ORGANIZER"))

    def test_confirm_repeats_the_hold_in_the_same_order(self):
        hold = gt.hold_body(BRANCH, "2026-10-11", 1020, resolved(), [1, 0, 2])
        confirm = gt.confirm_body(HOLD_ID, resolved(), [1, 0, 2])
        self.assertEqual(
            [(p["label"], p["serviceIds"]) for p in confirm["participants"]],
            [(p["label"], p["serviceIds"]) for p in hold["participants"]],
        )

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


class StartRefusalTests(SimpleTestCase):
    """What POST /booking/group holds a start to: the day view's own rule."""

    OPEN = (at("09:00"), at("21:00"))
    ENGINE = gt.engine_span(DAY, CLOCK)

    def refusal(self, start, *, open_span=OPEN, earliest=at("07:00"), longest=60):
        found = gt.start_refusal(start, open_span, self.ENGINE, earliest, longest)
        return found and found[0]

    def test_a_start_the_day_view_would_offer(self):
        self.assertIsNone(self.refusal(at("15:00")))

    def test_off_the_half_hour_is_still_a_time(self):
        self.assertIsNone(self.refusal(at("15:10")))

    def test_the_refusals(self):
        self.assertEqual(self.refusal(at("15:00"), open_span=None), "salon_closed")
        self.assertEqual(self.refusal(at("15:00"), earliest=at("15:30")), "too_soon")
        self.assertEqual(self.refusal(at("08:30")), "outside_hours")     # before opening
        self.assertEqual(self.refusal(at("19:30")), "outside_hours")     # ends after 20:00, the engine's close


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


def held_answer(body):
    """booking-api's 201 to POST /v1/groups/holds, for this body."""
    minute = body["targetMin"]
    lanes = []
    for i, p in enumerate(body["participants"]):
        end = minute + sum(MINUTES.get(s, 0) for s in p["serviceIds"])
        lanes.append({"label": p["label"], "staffId": STAFF[i],
                      "start": f"{minute // 60:02d}:{minute % 60:02d}",
                      "end": f"{end // 60:02d}:{end % 60:02d}"})
    return {"groupId": GROUP_ID, "holdId": HOLD_ID, "expiresAt": "2026-10-11T09:15:00.000Z",
            "expiresInSeconds": 900, "mode": "TOGETHER", "lanes": lanes}


def confirmed_answer(body, shares=("AED 415.00", "AED 0.00", "AED 0.00")):
    """booking-api's 201 to POST /v1/groups/:id/confirm. ORGANIZER: the booker carries it all."""
    return {"groupId": GROUP_ID, "status": "CONFIRMED", "bookings": [
        {"label": p["label"], "code": f"GS-{1280 + i}", "staffId": STAFF[i],
         "start": "17:00", "share": shares[i]}
        for i, p in enumerate(body["participants"])
    ]}


CATALOGUE = {
    CUT: {"name": "Cut", "price": Decimal("120.00")},
    NAILS: {"name": "Nails", "price": Decimal("175.00")},
}


class BookingFromConfirmTests(SimpleTestCase):
    ORDER = [1, 0, 2]

    def translate(self, catalogue=CATALOGUE, **kw):
        hold = gt.hold_body(BRANCH, "2026-10-11", 1020, resolved(), self.ORDER)
        confirm = gt.confirm_body(HOLD_ID, resolved(), self.ORDER)
        return gt.booking_from_confirm(
            held_answer(hold), confirmed_answer(confirm, **kw),
            day_iso="2026-10-11", members=resolved(), order=self.ORDER, clock=CLOCK,
            tz=DUBAI, cards=CARDS, catalogue=catalogue, salon_id=SALON_ID,
            date_iso="2026-10-11", created_at="2026-10-09T14:02:11+04:00",
        )

    def test_the_party_in_the_apps_order_and_dialect(self):
        body, problems = self.translate()
        self.assertEqual(problems, [])
        self.assertEqual(
            {k: body[k] for k in ("id", "salon_id", "booking_type", "status", "payment_status",
                                  "date", "start_time", "end_time", "created_at")},
            {"id": GROUP_ID, "salon_id": SALON_ID, "booking_type": "GROUP",
             "status": "CONFIRMED_BY_SALON", "payment_status": "PAY_AFTER_CHECK_IN",
             "date": "2026-10-11", "start_time": "2026-10-11T15:00:00+04:00",
             "end_time": "2026-10-11T16:00:00+04:00", "created_at": "2026-10-09T14:02:11+04:00"},
        )
        amal, dana, rana = body["members"]
        self.assertEqual([m["ref"] for m in body["members"]], [0, 1, 2])
        self.assertEqual(dana, {
            "ref": 1, "id": None, "booking_code": "GS-1280", "user_id": str(USER_ID),
            "name": "Dana", "kind": "self", "age_group": "adult",
            "services": [{"id": NAILS, "name": "Nails", "amount": Decimal("175.00")}],
            "products": [], "stylist": CARDS[MAYA],
            "start_time": "2026-10-11T15:00:00+04:00", "end_time": "2026-10-11T16:00:00+04:00",
            "total": Decimal("175.00"),
        })
        # The confirm answer has no end; it is the start plus the held length.
        self.assertEqual(amal["end_time"], "2026-10-11T15:45:00+04:00")
        self.assertEqual((amal["user_id"], amal["kind"], amal["booking_code"]), (None, "guest", "GS-1281"))
        self.assertEqual((rana["user_id"], rana["age_group"]), (RANA, "child"))
        self.assertEqual(rana["stylist"], {"id": STRANGER, "name": None})

    def test_member_totals_add_up_to_what_was_booked(self):
        body, _ = self.translate()
        totals = [m["total"] for m in body["members"]]
        self.assertEqual(totals, [Decimal("120.00"), Decimal("175.00"), Decimal("120.00")])
        self.assertEqual(sum(totals), body["amount_without_tax"])
        self.assertEqual(body["total"], Decimal("415.00"))
        self.assertEqual(body["currency"], "AED")

    def test_what_a_group_does_not_carry(self):
        body, _ = self.translate()
        self.assertIsNone(body["tax_amount"])        # not computed, which is not zero
        self.assertEqual((body["discount"], body["promo_code"]), (Decimal("0.00"), None))
        self.assertEqual((body["deposit_percent"], body["deposit_amount"]), (0, Decimal("0.00")))
        self.assertEqual((body["advance_paid_amount"], body["due_amount"]), (Decimal("0.00"), body["total"]))
        self.assertEqual((body["pass_qr_code"], body["expires_at"]), (None, None))

    def test_prices_that_do_not_add_up_are_null_never_wrong(self):
        body, problems = self.translate(shares=("AED 400.00", "AED 0.00", "AED 0.00"))
        self.assertEqual(body["total"], Decimal("400.00"))  # booking-api's figure stands
        self.assertEqual([m["total"] for m in body["members"]], [None, None, None])
        self.assertEqual(body["members"][0]["services"], [{"id": CUT, "name": "Cut", "amount": None}])
        self.assertEqual(len(problems), 1)

    def test_a_service_without_a_price_breaks_down_nothing(self):
        catalogue = {**CATALOGUE, CUT: {"name": "Cut", "price": None}}
        body, problems = self.translate(catalogue=catalogue)
        self.assertEqual([m["total"] for m in body["members"]], [None, None, None])
        self.assertTrue(problems)

    def test_an_unreadable_share_is_null_and_reported_never_zero(self):
        body, problems = self.translate(shares=("320", "AED 0.00", "AED 0.00"))
        self.assertIsNone(body["total"])
        self.assertIsNone(body["due_amount"])
        self.assertEqual([m["total"] for m in body["members"]], [None, None, None])
        self.assertEqual(len(problems), 1)

    def test_two_currencies_have_no_total(self):
        body, problems = self.translate(shares=("AED 1.00", "BDT 0.00", "AED 0.00"))
        self.assertIsNone(body["total"])
        self.assertTrue(problems)

    def test_the_share_is_read_strictly(self):
        self.assertEqual(gt.share_amount("AED 320.00"), ("AED", Decimal("320.00")))
        for bad in ("320.00", "AED 320", "AED 3.2", "aed 320.00", "", None, 320):
            with self.subTest(bad), self.assertRaises(ValueError):
                gt.share_amount(bad)


# ------------------------------------------------------------ the request


def availability_body(**over):
    return {"salon_id": SALON_ID, "date": "2026-10-11", "members": party_to_plan(), **over}


def booking_body(user_id=USER_ID, **over):
    # The app's extras ride along: money figures, a promo code, the statuses.
    # All accepted, none forwarded; the server decides every one.
    members = party()
    members[1]["id"] = str(user_id)
    return {
        "salon_id": SALON_ID, "date": "2026-10-11",
        "start_time": "2026-10-11T15:00:00+04:00", "members": members,
        "amount_without_tax": 999, "tax_amount": 1, "discount": 5, "promo_code": "X",
        "total": 999, "deposit_percent": 20, "advance_paid_amount": 0, "due_amount": 999,
        "payment_status": "DRAFT", "status": "BOOKED", "booking_type": "GROUP",
        **over,
    }


class GroupRequestTests(SimpleTestCase):
    def check(self, serializer_class, body):
        context = {"booker_id": USER_ID} if serializer_class is GroupBookingRequestSerializer else {}
        return serializer_class(data=body, context=context)

    def codes(self, serializer_class, body):
        s = self.check(serializer_class, body)
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

    def book_codes(self, members):
        return self.codes(GroupBookingRequestSerializer, booking_body(members=members))

    def test_a_good_party_is_accepted(self):
        for cls, body in ((GroupAvailabilityRequestSerializer, availability_body()),
                          (GroupBookingRequestSerializer, booking_body())):
            with self.subTest(cls.__name__):
                s = self.check(cls, body)
                self.assertTrue(s.is_valid(), s.errors)

    def test_two_to_eight(self):
        one = [{"ref": 0, "service_ids": [CUT]}]
        nine = [{"ref": i, "service_ids": [CUT]} for i in range(9)]
        for members in (one, nine):
            with self.subTest(len(members)):
                self.assertEqual(self.codes(GroupAvailabilityRequestSerializer,
                                            availability_body(members=members)),
                                 ["invalid_party_size"])
        self.assertEqual(self.book_codes(party()[:1]), ["invalid_party_size"])

    def test_every_ref_is_its_own(self):
        members = party_to_plan()
        members[1]["ref"] = members[0]["ref"]
        self.assertEqual(self.codes(GroupAvailabilityRequestSerializer, availability_body(members=members)),
                         ["duplicate_ref"])

    def test_every_member_needs_a_service(self):
        planned = party_to_plan()
        planned[0]["service_ids"] = []
        self.assertEqual(self.codes(GroupAvailabilityRequestSerializer, availability_body(members=planned)),
                         ["member_no_services"])
        booked = party()
        booked[0]["services"] = []
        self.assertEqual(self.book_codes(booked), ["member_no_services"])

    def test_exactly_one_self_and_it_is_the_caller(self):
        none = party()
        none[1]["kind"] = "registered"
        two = party()
        two[0].update(kind="self", id=str(USER_ID))
        someone_else = party()
        someone_else[1]["id"] = RANA
        twice = party()
        twice[0].update(kind="registered", id=RANA)
        for name, members in (("none", none), ("two", two), ("not me", someone_else),
                              ("one account twice", twice)):
            with self.subTest(name):
                self.assertEqual(self.book_codes(members), ["invalid_member_kind"])

    def test_self_matches_however_the_uuid_is_spelled(self):
        members = party()
        members[1]["id"] = str(USER_ID).upper()
        self.assertTrue(self.check(GroupBookingRequestSerializer, booking_body(members=members)).is_valid())

    def test_an_account_member_needs_its_id(self):
        for position in (1, 2):
            members = party()
            members[position]["id"] = None
            with self.subTest(members[position]["kind"]):
                self.assertIn("member_id_required", self.book_codes(members))

    def test_a_guest_needs_a_name_and_loses_any_id(self):
        members = party()
        members[0]["name"] = "  "
        self.assertEqual(self.book_codes(members), ["required"])

        members = party()
        members[0]["id"] = RANA
        s = self.check(GroupBookingRequestSerializer, booking_body(members=members))
        self.assertTrue(s.is_valid(), s.errors)
        self.assertIsNone(s.validated_data["members"][0]["id"])

    def test_age_group_is_adult_or_child(self):
        members = party()
        members[0]["age_group"] = "ADULT"
        self.assertEqual(self.book_codes(members), ["invalid_choice"])
        del members[0]["age_group"]
        self.assertEqual(self.book_codes(members), ["required"])

    def test_products_and_packages_are_refused_never_dropped(self):
        members = party()
        members[0]["products"] = [{"id": "variant-1", "amount": 40, "quantity": 1}]
        self.assertEqual(self.book_codes(members), ["products_not_supported"])
        members = party()
        members[2]["packages"] = [{"id": "pkg-1"}]
        self.assertEqual(self.book_codes(members), ["packages_not_supported"])

    def test_empty_products_are_fine(self):
        members = party()
        members[0]["products"] = []
        self.assertTrue(self.check(GroupBookingRequestSerializer, booking_body(members=members)).is_valid())

    def test_ids_are_uuids(self):
        planned = party_to_plan()
        planned[0]["service_ids"] = ["svc_fade"]
        self.assertEqual(self.codes(GroupAvailabilityRequestSerializer, availability_body(members=planned)),
                         ["invalid"])

    def test_a_date_that_is_not_a_date(self):
        # booking-api answers this one with a 500. It never gets the chance.
        self.assertIn("invalid", self.codes(GroupAvailabilityRequestSerializer, availability_body(date="2027-13-01")))

    def test_a_start_needs_an_offset(self):
        self.assertEqual(self.codes(GroupBookingRequestSerializer, booking_body(start_time="2026-10-11T15:00:00")),
                         ["offset_required"])
        self.assertEqual(self.codes(GroupBookingRequestSerializer, booking_body(start_time="at three")),
                         ["invalid"])

    def test_the_money_and_statuses_are_accepted_and_go_nowhere(self):
        s = self.check(GroupBookingRequestSerializer, booking_body())
        self.assertTrue(s.is_valid(), s.errors)
        for dropped in ("total", "promo_code", "deposit_percent", "payment_status", "booking_type"):
            self.assertNotIn(dropped, s.validated_data)

    def test_what_the_view_is_handed(self):
        s = self.check(GroupBookingRequestSerializer, booking_body())
        self.assertTrue(s.is_valid(), s.errors)
        amal, dana, rana = s.validated_data["members"]
        self.assertEqual((amal["service_ids"], amal["stylist_id"], amal["name"]), ([CUT], None, "Amal"))
        self.assertEqual((dana["id"], dana["stylist_id"]), (str(USER_ID), MAYA))
        self.assertEqual((rana["kind"], rana["id"], rana["age_group"]), ("registered", RANA, "child"))


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
            (lambda: booking_api.hold_group({"a": 1}, authorization="Bearer t", tenant_id=TENANT),
             "POST", "http://booking/v1/groups/holds"),
            (lambda: booking_api.confirm_group(GROUP_ID, {"a": 1}, authorization="Bearer t", tenant_id=TENANT),
             "POST", f"http://booking/v1/groups/{GROUP_ID}/confirm"),
            (lambda: booking_api.release_group_hold(HOLD_ID, authorization="Bearer t", tenant_id=TENANT),
             "DELETE", f"http://booking/v1/groups/holds/{HOLD_ID}"),
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
            status, body = booking_api.hold_group({}, authorization="Bearer t")
        self.assertEqual(status, 409)
        self.assertIn("Nobody available", body["message"])

    def test_unreachable_or_html_raises(self):
        with mock.patch.object(booking_api.urllib.request, "urlopen", side_effect=TimeoutError("slow")):
            with self.assertRaises(BookingApiUnavailable):
                booking_api.confirm_group(GROUP_ID, {}, authorization="Bearer t")
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
            "_bookable_accounts": mock.Mock(return_value={RANA: "Rana Hassan"}),
            "_open_span": mock.Mock(side_effect=lambda salon, tz, day, day_start: (
                day_start + timedelta(hours=9), day_start + timedelta(hours=21))),
            "_now": mock.Mock(side_effect=lambda tz: self.NOW.astimezone(tz)),
            "engine_clock": mock.Mock(return_value=CLOCK),
            "plan_group": mock.Mock(side_effect=lambda body, **kw: planned(body)),
            "hold_group": mock.Mock(side_effect=lambda body, **kw: (201, held_answer(body))),
            "confirm_group": mock.Mock(side_effect=lambda gid, body, **kw: (201, confirmed_answer(body))),
            "release_group_hold": mock.Mock(return_value=(200, {"released": True})),
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


@override_settings(CACHES={"default": {
    "BACKEND": "django.core.cache.backends.locmem.LocMemCache",
    "LOCATION": "group-booking-tests",
}})
class GroupBookingViewTests(PartyChecks, ViewSeams, SimpleTestCase):
    PATH = "/api/v1/booking/group"
    SERVICES_FIELD = "services"

    def setUp(self):
        cache.clear()

    def send(self, body=None, **kw):
        return self.post(GroupBookingCreateView, self.PATH, body or booking_body(), **kw)

    book = send

    def with_services(self, service_id):
        body = booking_body()
        body["members"][0]["services"] = [{"id": service_id, "amount": 1}]
        return body

    def with_stylist(self, stylist_id, everyone=False):
        body = booking_body()
        # The self member asks for NAILS, so it is the one a cut-only stylist fails.
        for m in body["members"] if everyone else body["members"][1:2]:
            m["stylist_id"] = stylist_id
        return body

    # ---- the happy path

    def test_hold_then_confirm_then_the_party(self):
        with self.seams() as s:
            response = self.book()
        self.assertEqual(response.status_code, 201, response.data)
        s.hold_group.assert_called_once()
        s.confirm_group.assert_called_once()
        s.release_group_hold.assert_not_called()

        data = response.data
        self.assertEqual((data["id"], data["booking_type"], data["status"], data["salon_id"], data["date"]),
                         (GROUP_ID, "GROUP", "CONFIRMED_BY_SALON", SALON_ID, "2026-10-11"))
        self.assertEqual([(m["ref"], m["name"]) for m in data["members"]],
                         [(0, "Amal"), (1, "Dana"), (2, "Rana Hassan")])
        self.assertEqual(data["members"][1]["booking_code"], "GS-1280")
        self.assertEqual([m["total"] for m in data["members"]],
                         [Decimal("120.00"), Decimal("175.00"), Decimal("120.00")])
        self.assertEqual((data["total"], data["deposit_amount"], data["payment_status"]),
                         (Decimal("415.00"), Decimal("0.00"), "PAY_AFTER_CHECK_IN"))
        self.assertEqual(data["created_at"], "2026-10-11T07:00:00+04:00")

    def test_what_the_engine_is_asked(self):
        with self.seams() as s:
            self.book()
        hold = s.hold_group.call_args.args[0]
        self.assertEqual((hold["day"], hold["targetMin"]), ("2026-10-11", 1020))  # 15:00 Dubai, 17:00 engine
        self.assertEqual(hold["participants"], [
            {"label": "Dana", "serviceIds": [NAILS], "customerId": str(USER_ID), "preferredStaffId": MAYA},
            {"label": "Amal", "serviceIds": [CUT], "guestName": "Amal"},
            {"label": "Rana Hassan", "serviceIds": [CUT], "customerId": RANA},
        ])
        for leaked in ("deposit_percent", "promo_code", "child"):
            self.assertNotIn(leaked, json.dumps(hold))
        gid, confirm = s.confirm_group.call_args.args
        self.assertEqual((gid, confirm["holdId"]), (GROUP_ID, HOLD_ID))
        s._bookable_accounts.assert_called_once_with([RANA])

    def test_an_account_the_party_cannot_book_is_unknown(self):
        with self.seams(_bookable_accounts=mock.Mock(return_value={})) as s:
            response = self.book()
        self.assert_refused(response, "unknown_user", "id")
        s.hold_group.assert_not_called()

    def test_an_account_without_a_name_keeps_the_one_typed(self):
        with self.seams(_bookable_accounts=mock.Mock(return_value={RANA: ""})):
            response = self.book(user=Customer(full_name=""))
        self.assertEqual(response.status_code, 201, response.data)
        self.assertEqual(response.data["members"][2]["name"], "typed by the booker")
        self.assertIsNone(response.data["members"][1]["name"])

    # ---- refusals

    def test_a_party_that_does_not_fit_is_slot_taken_in_words(self):
        refusal = {"statusCode": 409, "message": "Only 1 professional can cover this party.",
                   "error": "Conflict"}
        with self.seams(hold_group=mock.Mock(return_value=(409, refusal))) as s:
            response = self.book()
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.data["errors"], [
            {"field": None, "code": "slot_taken", "message": "Only 1 professional can cover this party."}])
        s.confirm_group.assert_not_called()
        s.release_group_hold.assert_not_called()

    def test_slot_taken_without_the_engines_words(self):
        with self.seams(hold_group=mock.Mock(return_value=(409, {"message": ["a", "list"]}))):
            response = self.book()
        self.assertEqual(response.data["errors"][0]["code"], "slot_taken")
        self.assertEqual(response.data["detail"], "That time no longer fits the whole party. Pick another time.")

    def test_a_confirm_that_no_longer_fits_releases_and_is_slot_taken(self):
        for code in (409, 410):
            with self.subTest(code):
                cache.clear()
                refusal = {"statusCode": code, "message": f"refused {code}"}
                with self.seams(confirm_group=mock.Mock(return_value=(code, refusal))) as s:
                    response = self.book()
                self.assertEqual((response.status_code, response.data["errors"][0]["code"]), (409, "slot_taken"))
                self.assertEqual(s.release_group_hold.call_args.args, (HOLD_ID,))

    def test_any_other_confirm_refusal_releases_the_hold_and_is_forwarded(self):
        for code in (422, 500):
            refusal = {"statusCode": code, "message": f"refused {code}"}
            with self.subTest(code):
                cache.clear()
                with self.seams(confirm_group=mock.Mock(return_value=(code, refusal))) as s:
                    response = self.book()
                self.assertEqual((response.status_code, response.data), (code, refusal))
                s.release_group_hold.assert_called_once()

    def test_a_refusal_is_not_remembered(self):
        with self.seams(hold_group=mock.Mock(return_value=(409, {"message": "no"}))):
            self.book()
        with self.seams() as s:
            response = self.book()
        self.assertEqual(response.status_code, 201)
        s.hold_group.assert_called_once()

    # ---- the ambiguous moment

    def test_confirm_timeout_and_the_hold_freed_means_nothing_was_booked(self):
        with self.seams(confirm_group=mock.Mock(side_effect=BookingApiUnavailable("timed out"))) as s:
            response = self.book()
        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.data["errors"][0]["code"], "booking_api_unavailable")
        s.release_group_hold.assert_called_once()
        with self.seams() as s:
            self.assertEqual(self.book().status_code, 201)   # a retry is a fresh try
        s.hold_group.assert_called_once()

    def test_confirm_timeout_and_the_hold_already_gone_is_unknown(self):
        for release in (mock.Mock(return_value=(200, {"released": False})),
                        mock.Mock(side_effect=BookingApiUnavailable("down")),
                        mock.Mock(return_value=(500, None))):
            with self.subTest(release=release):
                cache.clear()
                with self.seams(confirm_group=mock.Mock(side_effect=BookingApiUnavailable("timed out")),
                                release_group_hold=release):
                    response = self.book()
                self.assertEqual(response.status_code, 503)
                self.assertEqual(response.data["errors"][0]["code"], "group_status_unknown")
                self.assertEqual(response.data["group_id"], GROUP_ID)
                self.assertEqual(response.data["code"], "service_unavailable")

    def test_unknown_stays_unknown(self):
        with self.seams(confirm_group=mock.Mock(side_effect=BookingApiUnavailable("timed out")),
                        release_group_hold=mock.Mock(return_value=(200, {"released": False}))):
            self.book()
        with self.seams() as s:
            response = self.book()
        self.assertEqual(response.data["errors"][0]["code"], "group_status_unknown")
        s.hold_group.assert_not_called()   # never a second party beside the first

    def test_booked_but_unreadable_is_unknown_not_failed(self):
        with self.seams(confirm_group=mock.Mock(return_value=(201, {"groupId": GROUP_ID, "bookings": []}))) as s:
            response = self.book()
        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.data["errors"][0]["code"], "group_status_unknown")
        s.release_group_hold.assert_not_called()   # it IS booked

    def test_unreachable_before_anything_is_held_is_a_plain_503(self):
        for name in ("engine_clock", "hold_group"):
            with self.subTest(name):
                cache.clear()
                with self.seams(**{name: mock.Mock(side_effect=BookingApiUnavailable("down"))}) as s:
                    response = self.book()
                self.assertEqual(response.data["errors"][0]["code"], "booking_api_unavailable")
                s.confirm_group.assert_not_called()

    # ---- one Redis key for the pair

    def test_the_same_request_twice_books_once(self):
        with self.seams() as s:
            first = self.book()
            second = self.book()
        self.assertEqual((first.status_code, second.status_code), (201, 201))
        self.assertEqual(json.dumps(first.data, default=str), json.dumps(second.data, default=str))
        s.hold_group.assert_called_once()
        s.confirm_group.assert_called_once()

    def test_while_the_first_is_running_the_second_is_409(self):
        from apps.salons import group_views

        def in_flight(body, **kw):
            # The second tap lands while the first is between hold and confirm.
            inner = self.book()
            self.assertEqual(inner.status_code, 409)
            self.assertEqual(inner.data["errors"][0]["code"], "request_in_progress")
            return 201, held_answer(body)

        with self.seams(hold_group=mock.Mock(side_effect=in_flight)):
            self.assertEqual(self.book().status_code, 201)
        self.assertTrue(group_views.IN_PROGRESS_SECONDS < group_views.RECEIPT_SECONDS)

    def test_a_callers_key_reused_for_another_party_is_refused(self):
        with self.seams():
            self.book(HTTP_IDEMPOTENCY_KEY="k-1")
            other = booking_body(start_time="2026-10-11T16:00:00+04:00")
            response = self.book(other, HTTP_IDEMPOTENCY_KEY="k-1")
        self.assert_refused(response, "idempotency_key_reused")

    def test_two_customers_never_share_a_key(self):
        with self.seams() as s:
            for someone in (uuid.uuid4(), uuid.uuid4()):
                response = self.book(booking_body(user_id=someone), HTTP_IDEMPOTENCY_KEY="same",
                                     user=Customer(someone))
                self.assertEqual(response.status_code, 201, response.data)
        self.assertEqual(s.hold_group.call_count, 2)

    def test_the_cache_down_is_not_a_refusal(self):
        broken = mock.Mock(side_effect=ConnectionError("redis down"))
        with self.seams(), mock.patch("apps.salons.group_views.cache.add", broken), \
                mock.patch("apps.salons.group_views.cache.set", broken), \
                mock.patch("apps.salons.group_views.cache.delete", broken):
            self.assertEqual(self.book().status_code, 201)

    # ---- the start, held to the day view's rule

    def test_a_start_on_another_day_is_422_and_asks_nobody(self):
        with self.seams() as s:
            response = self.book(booking_body(start_time="2026-10-12T15:00:00+04:00"))
        self.assert_refused(response, "date_mismatch", "start_time")
        s.engine_clock.assert_not_called()

    def test_starts_the_day_view_would_not_offer(self):
        cases = (
            ("2026-10-11T07:15:00+04:00", "too_soon"),        # before now plus notice
            ("2026-10-11T19:30:00+04:00", "outside_hours"),   # an hour of nails ends after 20:00
        )
        for start, code in cases:
            with self.subTest(code):
                cache.clear()
                with self.seams() as s:
                    response = self.book(booking_body(start_time=start))
                self.assert_refused(response, code, "start_time")
                s.hold_group.assert_not_called()
        with self.seams(_open_span=mock.Mock(return_value=None)) as s:
            self.assertEqual(self.book().data["errors"][0]["code"], "salon_closed")

    def test_a_refused_start_can_be_fixed_and_sent_again(self):
        with self.seams():
            self.book(booking_body(start_time="2026-10-11T19:30:00+04:00"))
        with self.seams() as s:
            self.assertEqual(self.book(booking_body(start_time="2026-10-11T19:30:00+04:00")).status_code, 422)
        s.engine_clock.assert_called_once()   # asked again, not replayed

    def test_the_door(self):
        with self.seams() as s:
            self.assertEqual(self.book(authenticated=False).status_code, 401)
            self.assertEqual(self.book(body=b"x=1", content_type="application/x-www-form-urlencoded").status_code, 415)
            body = booking_body()
            body["members"][1]["products"] = [{"id": "v1", "amount": 25, "quantity": 1}]
            self.assert_refused(self.book(body), "products_not_supported")
            self.assert_refused(self.book(booking_body(user_id=uuid.uuid4())), "invalid_member_kind")
        s.hold_group.assert_not_called()
