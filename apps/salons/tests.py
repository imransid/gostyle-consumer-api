from django.test import TestCase
from django.contrib.auth import get_user_model
from rest_framework.test import APIClient
from apps.salons.models import Salon

User = get_user_model()

class DiscoverMapViewTests(TestCase):
    def setUp(self):
        self.client = APIClient()
        self.user = User.objects.create_user(phone="+8801700000000", password="password")
        self.client.force_authenticate(user=self.user)

        # Inside Mymensingh region: 24.5547702, 90.4080668 ± 0.06
        self.salon1 = Salon.objects.create(
            id="salon-near-center",
            name="Salon Near Center",
            category="gents",
            latitude=24.554,
            longitude=90.408,
            rating=4.5,
        )
        # Outside region
        self.salon2 = Salon.objects.create(
            id="far-salon",
            name="Far Salon",
            category="gents",
            latitude=25.554,
            longitude=91.408,
            rating=4.0,
        )

    def test_map_with_region_params_filters_correctly(self):
        """Only salon1 (inside region) should be returned."""
        response = self.client.get("/api/v1/discover/map", {
            "latitude": 24.5547702,
            "longitude": 90.4080668,
            "latitudeDelta": 0.12,
            "longitudeDelta": 0.12,
        })
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertIn("venues", data)
        self.assertEqual(data["count"], 1)
        self.assertEqual(data["venues"][0]["lat"], 24.554)

    def test_map_without_params_returns_all(self):
        """No region params → returns all salons."""
        response = self.client.get("/api/v1/discover/map")
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertIn("venues", data)
        self.assertEqual(data["count"], 2)

    def test_map_unauthenticated_returns_401(self):
        """Unauthenticated requests should be rejected."""
        self.client.force_authenticate(user=None)
        response = self.client.get("/api/v1/discover/map", {
            "latitude": 24.5547702,
            "longitude": 90.4080668,
            "latitudeDelta": 0.12,
            "longitudeDelta": 0.12,
        })
        self.assertEqual(response.status_code, 401)

