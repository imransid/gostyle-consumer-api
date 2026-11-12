"""
POST /api/v1/me/location and GET /api/v1/me/geofences, end to end through
the API: a reading inside a ring queues a push delivery for the worker; one
outside queues nothing.
"""

from datetime import timedelta
from unittest import mock

from django.core.cache import cache
from django.test import TestCase, override_settings
from django.utils import timezone
from rest_framework.test import APIClient

from apps.accounts.models import ConsumerAccount
from apps.accounts.services import tokens_for
from apps.geofence import sources
from apps.notifications.models import Notification, NotificationDelivery

from .fixtures import SALON_POINT, north_of, salon, visit, world

LOCATION = "/api/v1/me/location"
GEOFENCES = "/api/v1/me/geofences"


def body(metres, **extra):
    lat, lng = north_of(SALON_POINT, metres)
    return {"latitude": lat, "longitude": lng, **extra}


@override_settings(SERVICE_AREA_ALERTS_V1=True)
class LocationApiTests(TestCase):
    def setUp(self):
        cache.clear()
        self.client = APIClient()
        self.sara = ConsumerAccount.objects.create(phone="+971500000401")
        self.client.credentials(HTTP_AUTHORIZATION=f"Bearer {tokens_for(self.sara)['access']}")

    def post(self, data):
        return self.client.post(LOCATION, data, format="json")

    def errors(self, response):
        return [(e["field"], e["code"]) for e in response.json()["errors"]]

    def test_inside_the_radius_dispatches_a_push(self):
        with world([visit()], [salon()]):
            response = self.post(body(500, accuracy_m=12))
        self.assertEqual(response.status_code, 200, response.content)
        [one] = response.json()["visits"]
        self.assertEqual(one["sent"], ["visit.nearby"])
        self.assertIsNone(response.json()["ignored"])
        delivery = NotificationDelivery.objects.get(notification__account=self.sara)
        self.assertEqual(delivery.channel, NotificationDelivery.Channel.PUSH)
        self.assertEqual(delivery.status, NotificationDelivery.Status.PENDING)
        self.assertEqual(delivery.notification.type, "visit.nearby")

    def test_outside_the_radius_dispatches_nothing(self):
        with world([visit()], [salon()]):
            response = self.post(body(5000))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["visits"][0]["sent"], [])
        self.assertFalse(Notification.objects.exists())
        self.assertFalse(NotificationDelivery.objects.exists())

    def test_the_same_reading_twice_is_one_push(self):
        with world([visit()], [salon()]):
            self.post(body(500))
            second = self.post(body(500))
        self.assertEqual(second.json()["visits"][0]["sent"], [])
        self.assertEqual(NotificationDelivery.objects.count(), 1)

    def test_no_visit_is_an_empty_answer(self):
        with world([], [salon()]):
            response = self.post(body(10))
        self.assertEqual(response.json(), {"ignored": None, "visits": []})

    def test_a_stale_reading_is_not_measured(self):
        old = (timezone.now() - timedelta(minutes=6)).isoformat()
        with world([visit()], [salon()]) as visits_for:
            response = self.post(body(10, recorded_at=old))
        self.assertEqual(response.json(), {"ignored": "stale", "visits": []})
        visits_for.assert_not_called()

    def test_a_recent_reading_with_its_time(self):
        recent = (timezone.now() - timedelta(minutes=1)).isoformat().replace("+00:00", "Z")
        with world([visit()], [salon()]):
            response = self.post(body(500, recorded_at=recent))
        self.assertEqual(response.json()["visits"][0]["sent"], ["visit.nearby"])

    def test_bad_fields(self):
        future = (timezone.now() + timedelta(minutes=10)).isoformat()
        cases = [
            ({}, [("latitude", "required"), ("longitude", "required")]),
            ({"latitude": 91, "longitude": 55}, [("latitude", "invalid_latitude")]),
            ({"latitude": 25, "longitude": "abc"}, [("longitude", "invalid_longitude")]),
            ({"latitude": "nan", "longitude": 181}, [("latitude", "invalid_latitude"), ("longitude", "invalid_longitude")]),
            ({"latitude": True, "longitude": 55}, [("latitude", "invalid_latitude")]),
            ({**body(1), "accuracy_m": -1}, [("accuracy_m", "invalid_accuracy")]),
            ({**body(1), "accuracy_m": 20_000}, [("accuracy_m", "invalid_accuracy")]),
            ({**body(1), "recorded_at": "yesterday"}, [("recorded_at", "invalid_recorded_at")]),
            ({**body(1), "recorded_at": "2026-11-12T10:00:00"}, [("recorded_at", "invalid_recorded_at")]),
            ({**body(1), "recorded_at": future}, [("recorded_at", "invalid_recorded_at")]),
        ]
        with world([visit()], [salon()]) as visits_for:
            for data, expected in cases:
                with self.subTest(data=data):
                    response = self.post(data)
                    self.assertEqual(response.status_code, 422)
                    self.assertEqual(response.json()["code"], "validation_error")
                    self.assertEqual(sorted(self.errors(response)), sorted(expected))
        visits_for.assert_not_called()
        self.assertFalse(Notification.objects.exists())

    def test_the_edges_of_the_map_are_fine(self):
        with world([], [salon()]):
            for lat, lng in ((90, 180), (-90, -180), (0, 0)):
                with self.subTest(lat=lat, lng=lng):
                    self.assertEqual(self.post({"latitude": lat, "longitude": lng}).status_code, 200)

    def test_visits_unreadable_is_503(self):
        with mock.patch.object(sources, "visits_for", side_effect=sources.Unavailable("no booking db")):
            with self.assertLogs("apps.geofence.views", "ERROR"):
                response = self.post(body(10))
        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.json()["code"], "service_unavailable")

    def test_not_signed_in(self):
        self.client.credentials()
        self.assertEqual(self.post(body(10)).status_code, 401)
        self.assertEqual(self.client.get(GEOFENCES).status_code, 401)

    @override_settings(SERVICE_AREA_ALERTS_V1=False)
    def test_switched_off_is_a_404_and_does_nothing(self):
        with world([visit()], [salon()]) as visits_for:
            self.assertEqual(self.post(body(10)).status_code, 404)
            self.assertEqual(self.client.get(GEOFENCES).status_code, 404)
        visits_for.assert_not_called()

    def test_too_many_readings(self):
        with world([], [salon()]):
            codes = [self.post(body(10)).status_code for _ in range(21)]
        self.assertEqual(codes[:20], [200] * 20)
        self.assertEqual(codes[20], 429)

    def test_geofences(self):
        v = visit(starts_in=timedelta(hours=5))
        with world([v], [salon()]):
            response = self.client.get(GEOFENCES)
        self.assertEqual(response.status_code, 200)
        regions = response.json()["regions"]
        self.assertEqual([(r["zone"], r["radius_m"]) for r in regions], [("arrival", 50), ("auto_trigger", 200), ("primary", 800)])
        self.assertEqual(regions[0]["booking_id"], str(v.booking_id))
