"""
The enter/exit state and the alert decision (apps/geofence/service.py), on
this service's real tables: GeofenceState and the Notification Center.
"""

import uuid
from datetime import timedelta

from django.test import TestCase, override_settings
from django.utils import timezone

from apps.accounts.models import ConsumerAccount, NotificationPreference
from apps.geofence import service
from apps.geofence.models import GeofenceState
from apps.notifications.models import Notification, NotificationDelivery

from .fixtures import (
    BRANCH,
    OTHER_BRANCH,
    OTHER_POINT,
    OTHER_STOREFRONT,
    SALON_POINT,
    STOREFRONT,
    north_of,
    salon,
    visit,
    world,
)


class GeofenceTestCase(TestCase):
    def setUp(self):
        self.now = timezone.now()
        self.sara = ConsumerAccount.objects.create(phone="+971500000301")

    def at(self, metres, accuracy_m=None, point=SALON_POINT, now=None):
        """A reading `metres` north of the salon, measured at `now`."""
        lat, lng = north_of(point, metres)
        when = now or self.now
        return service.evaluate(self.sara, service.Reading(lat, lng, accuracy_m, when), when)

    def types(self):
        return list(Notification.objects.filter(account=self.sara).order_by("created_at").values_list("type", flat=True))

    def state(self, zone, branch=BRANCH):
        return GeofenceState.objects.get(account=self.sara, branch_id=branch, zone=zone)


class FirstAlertTests(GeofenceTestCase):
    def test_inside_the_primary_ring_alerts_and_queues_the_push(self):
        v = visit(now=self.now)
        with world([v], [salon()]):
            [result] = self.at(500)

        self.assertEqual(result["sent"], ["visit.nearby"])
        self.assertEqual(result["distance_m"], 500)
        self.assertEqual(
            result["zones"],
            [
                {"zone": "arrival", "radius_m": 50, "inside": False, "notified": False},
                {"zone": "auto_trigger", "radius_m": 200, "inside": False, "notified": False},
                {"zone": "primary", "radius_m": 800, "inside": True, "notified": True},
            ],
        )
        item = Notification.objects.get(account=self.sara)
        self.assertEqual(item.type, "visit.nearby")
        self.assertEqual(item.title, "You're near Demo Salon & Spa")
        self.assertRegex(item.body, r"^Your booking GS-1500 is (today|tomorrow) at \d{1,2}:\d\d (AM|PM)\.$")
        self.assertEqual((item.target_type, item.target_id), ("booking", str(v.booking_id)))
        self.assertEqual(item.data["zone"], "primary")
        self.assertEqual(item.data["salonId"], STOREFRONT)
        self.assertEqual(item.dedupe_key, f"geo:primary:{BRANCH}:{v.trading_day.isoformat()}")
        delivery = NotificationDelivery.objects.get(notification=item)
        self.assertEqual(delivery.channel, NotificationDelivery.Channel.PUSH)
        self.assertEqual(delivery.status, NotificationDelivery.Status.PENDING)
        self.assertLessEqual(delivery.expires_at, self.now + service.DELIVER_WITHIN)

    def test_outside_every_ring_sends_nothing(self):
        with world([visit(now=self.now)], [salon()]):
            [result] = self.at(2000)
        self.assertEqual(result["sent"], [])
        self.assertEqual(Notification.objects.count(), 0)
        self.assertFalse(GeofenceState.objects.filter(inside=True).exists())

    def test_on_the_line_counts(self):
        with world([visit(now=self.now)], [salon()]):
            [result] = self.at(800 - 1e-6)
        self.assertEqual(result["sent"], ["visit.nearby"])

    def test_no_visit_no_work(self):
        with world([], [salon()]):
            self.assertEqual(self.at(10), [])
        self.assertEqual(GeofenceState.objects.count(), 0)

    def test_already_at_the_door_is_one_alert_not_three(self):
        with world([visit(now=self.now)], [salon()]):
            [result] = self.at(20)
            self.assertEqual(result["sent"], ["visit.arrived"])
            # A later fix that wobbles out to 500 m says nothing more.
            self.at(500)
            self.at(150)
        self.assertEqual(self.types(), ["visit.arrived"])
        self.assertEqual(self.state("primary").suppressed_reason, service.INNER_RING_FIRST)
        self.assertEqual(self.state("auto_trigger").suppressed_reason, service.INNER_RING_FIRST)


class WalkingInTests(GeofenceTestCase):
    def test_each_ring_once_from_the_outside_in(self):
        with world([visit(now=self.now)], [salon()]):
            sent = [self.at(metres)[0]["sent"] for metres in (3000, 700, 600, 150, 120, 30, 10)]
        self.assertEqual(sent, [[], ["visit.nearby"], [], ["visit.almost_there"], [], ["visit.arrived"], []])
        self.assertEqual(self.types(), ["visit.nearby", "visit.almost_there", "visit.arrived"])

    def test_leaving_and_coming_back_alerts_no_more(self):
        with world([visit(now=self.now)], [salon()]):
            self.at(500)
            self.at(2000)
            left = self.state("primary")
            self.assertFalse(left.inside)
            self.assertIsNotNone(left.exited_at)
            [result] = self.at(500)
        self.assertEqual(result["sent"], [])
        self.assertTrue(self.state("primary").inside)
        self.assertEqual(self.types(), ["visit.nearby"])

    def test_wobbling_on_the_line_does_not_flap(self):
        with world([visit(now=self.now)], [salon()]):
            self.at(790)
            entered = self.state("primary").entered_at
            self.at(850)  # inside the 80 m exit band: still inside
            self.assertTrue(self.state("primary").inside)
            self.at(870)
            self.assertTrue(self.state("primary").inside)
            self.at(900)  # past it: gone
            self.assertFalse(self.state("primary").inside)
        self.assertEqual(self.state("primary").entered_at, entered)

    def test_a_blurred_reading_holds_the_small_ring(self):
        with world([visit(now=self.now)], [salon()]):
            [result] = self.at(30, accuracy_m=80)
            # 80 m blur cannot place anyone in the 50 m ring, but the 200 m
            # ring is clear.
            self.assertEqual(result["sent"], ["visit.almost_there"])
            self.assertFalse(self.state("arrival").inside)
            [result] = self.at(30, accuracy_m=8)
        self.assertEqual(result["sent"], ["visit.arrived"])


class GateTests(GeofenceTestCase):
    def test_no_you_are_near_once_the_visit_has_begun(self):
        v = visit(starts_in=-timedelta(minutes=10), now=self.now)
        with world([v], [salon()]):
            [result] = self.at(500)
            self.assertEqual(result["sent"], [])
            self.assertEqual(self.state("primary").suppressed_reason, service.VISIT_STARTED)
            self.assertIsNone(self.state("primary").notified_at)
            # The inner rings still speak to a customer running late.
            [result] = self.at(100)
        self.assertEqual(result["sent"], ["visit.almost_there"])

    def test_reminders_switched_off_keeps_the_item_and_sends_no_push(self):
        NotificationPreference.objects.create(account=self.sara, appointment_reminder=False)
        with world([visit(now=self.now)], [salon()]):
            [result] = self.at(500)
        self.assertEqual(result["sent"], ["visit.nearby"])
        self.assertEqual(self.types(), ["visit.nearby"])
        self.assertFalse(NotificationDelivery.objects.exists())

    def test_push_switched_off_sends_no_push(self):
        NotificationPreference.objects.create(account=self.sara, push=False)
        with world([visit(now=self.now)], [salon()]):
            self.at(500)
        self.assertFalse(NotificationDelivery.objects.exists())

    def test_the_push_never_outlives_the_visit(self):
        v = visit(starts_in=-timedelta(minutes=55), minutes=60, now=self.now)
        with world([v], [salon()]):
            self.at(20)
        delivery = NotificationDelivery.objects.get()
        self.assertEqual(delivery.expires_at, v.end_at)


class SalonTests(GeofenceTestCase):
    def test_no_service_area(self):
        with world([visit(now=self.now)], [salon(payload=None)]):
            [result] = self.at(10)
        self.assertEqual(result["skipped"], service.NO_SERVICE_AREA)
        self.assertEqual(GeofenceState.objects.count(), 0)

    def test_a_service_area_with_no_usable_ring(self):
        with world([visit(now=self.now)], [salon(payload={"primaryRadiusM": 0.8, "homeRadiusKm": 15})]):
            with self.assertLogs("apps.geofence.service", "WARNING") as logs:
                [result] = self.at(10)
        self.assertEqual(result["skipped"], service.NO_SERVICE_AREA)
        self.assertIn("primaryRadiusM=0.8 m is outside 10..50000 m", "\n".join(logs.output))

    def test_bad_inner_ring_is_logged_and_the_rest_work(self):
        payload = {"primaryRadiusM": 800, "autoTriggerM": 900, "arrivalRadiusM": 50}
        with world([visit(now=self.now)], [salon(payload=payload)]):
            with self.assertLogs("apps.geofence.service", "WARNING") as logs:
                [result] = self.at(150)
        self.assertEqual([z["zone"] for z in result["zones"]], ["arrival", "primary"])
        self.assertEqual(result["sent"], ["visit.nearby"])
        self.assertIn("auto_trigger ring 900 m is not inside the primary ring 800 m", "\n".join(logs.output))

    def test_no_map_pin(self):
        with world([visit(now=self.now)], [salon(point=None)]):
            with self.assertLogs("apps.geofence.service", "WARNING"):
                [result] = self.at(10)
        self.assertEqual(result["skipped"], service.NO_COORDINATES)

    def test_a_pin_at_zero_zero_is_no_pin(self):
        with world([visit(now=self.now)], [salon(point=(0, 0))]):
            with self.assertLogs("apps.geofence.service", "WARNING"):
                [result] = self.at(10, point=(0, 0))
        self.assertEqual(result["skipped"], service.NO_COORDINATES)
        self.assertFalse(Notification.objects.exists())

    def test_no_storefront(self):
        with world([visit(now=self.now)], []):
            [result] = self.at(10)
        self.assertEqual((result["skipped"], result["salon_id"]), (service.SALON_NOT_FOUND, None))

    def test_two_bookings_at_one_salon_one_day_are_one_walk(self):
        party = [visit(now=self.now, code="GS-1501"), visit(now=self.now, code="GS-1502")]
        with world(party, [salon()]):
            results = self.at(500)
        self.assertEqual(len(results), 1)
        self.assertEqual(self.types(), ["visit.nearby"])

    def test_two_salons_each_measured_from_their_own_pin(self):
        here, there = visit(now=self.now), visit(now=self.now, branch=OTHER_BRANCH, code="GS-1600")
        salons = [salon(), salon(branch=OTHER_BRANCH, storefront=OTHER_STOREFRONT, point=OTHER_POINT)]
        with world([here, there], salons):
            results = self.at(500)
        by_salon = {r["salon_id"]: r for r in results}
        self.assertEqual(by_salon[STOREFRONT]["sent"], ["visit.nearby"])
        self.assertEqual(by_salon[OTHER_STOREFRONT]["sent"], [])
        self.assertGreater(by_salon[OTHER_STOREFRONT]["distance_m"], 15_000)


class OnceOnlyTests(GeofenceTestCase):
    def test_the_dedupe_key_holds_even_if_the_state_is_lost(self):
        with world([visit(now=self.now)], [salon()]):
            self.at(500)
            GeofenceState.objects.all().delete()
            [result] = self.at(500)
        self.assertEqual(result["sent"], [])
        self.assertEqual(self.types(), ["visit.nearby"])

    def test_a_new_day_at_the_same_salon_alerts_again(self):
        today = visit(now=self.now)
        with world([today], [salon()]):
            self.at(500)
        later = self.now + timedelta(days=1)
        with world([visit(now=later)], [salon()]):
            self.at(500, now=later)
        self.assertEqual(self.types(), ["visit.nearby", "visit.nearby"])

    def test_old_days_are_forgotten(self):
        with world([visit(now=self.now)], [salon()]):
            self.at(500)
        later = self.now + timedelta(days=9)
        with world([], [salon()]):
            self.at(500, now=later)
        self.assertEqual(GeofenceState.objects.count(), 0)

    def test_deleting_the_account_wipes_the_states(self):
        with world([visit(now=self.now)], [salon()]):
            self.at(500)
        self.assertTrue(GeofenceState.objects.exists())
        self.sara.purge()
        self.assertFalse(GeofenceState.objects.exists())


@override_settings(SELF_CHECK_IN_V1=True)
class CheckInWordsTests(GeofenceTestCase):
    def test_at_the_door_inside_the_check_in_window(self):
        with world([visit(starts_in=timedelta(minutes=10), now=self.now)], [salon()]):
            self.at(20)
        item = Notification.objects.get()
        self.assertEqual(item.title, "Welcome to Demo Salon & Spa")
        self.assertEqual(item.body, "Tap to check in so the desk knows you're here.")
        self.assertEqual(item.data["canCheckIn"], "true")

    def test_before_the_window_opens(self):
        with world([visit(starts_in=timedelta(hours=2), now=self.now)], [salon()]):
            self.at(20)
        item = Notification.objects.get()
        self.assertEqual(item.body, "Show booking GS-1500 at the desk.")
        self.assertEqual(item.data["canCheckIn"], "false")

    def test_a_party_lane_checks_in_through_the_party(self):
        lane = visit(starts_in=timedelta(minutes=10), group_id=uuid.uuid4(), now=self.now)
        with world([lane], [salon()]):
            self.at(150)
        item = Notification.objects.get()
        self.assertEqual(item.title, "Almost there")
        self.assertRegex(item.body, r"^You're a short walk from Demo Salon & Spa\. Your booking GS-1500 is ")

    @override_settings(SELF_CHECK_IN_V1=False)
    def test_self_check_in_off(self):
        with world([visit(starts_in=timedelta(minutes=10), now=self.now)], [salon()]):
            self.at(20)
        self.assertEqual(Notification.objects.get().data["canCheckIn"], "false")


class RegionsTests(GeofenceTestCase):
    def test_the_rings_of_the_next_day(self):
        v = visit(starts_in=timedelta(hours=6), now=self.now)
        with world([v], [salon(), salon(branch=OTHER_BRANCH, storefront=OTHER_STOREFRONT, payload=None)]) as visits_for:
            regions = service.regions(self.sara, self.now)
        self.assertEqual(visits_for.call_args.args[2], service.REGION_LOOKAHEAD)
        self.assertEqual([(r["zone"], r["radius_m"]) for r in regions], [("arrival", 50), ("auto_trigger", 200), ("primary", 800)])
        self.assertEqual(regions[0]["id"], f"{v.booking_id}:arrival")
        self.assertEqual((regions[0]["latitude"], regions[0]["longitude"]), SALON_POINT)
        self.assertEqual(regions[0]["salon_id"], STOREFRONT)

    def test_evaluate_reads_only_the_next_three_hours(self):
        with world([], [salon()]) as visits_for:
            self.at(500)
        self.assertEqual(visits_for.call_args.args[2], service.ALERT_LOOKAHEAD)
