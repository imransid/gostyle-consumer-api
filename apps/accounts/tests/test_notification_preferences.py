from django.contrib.auth import get_user_model
from rest_framework.test import APITestCase

URL = "/api/v1/notifications/preferences"
DEFAULTS = {
    "booking_confirmation": True,
    "appointment_reminder": True,
    "booking_changes": True,
    "special_offers": False,
    "new_salons": False,
    "loyalty": True,
    "push": True,
}


class NotificationPreferencesTests(APITestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user(
            phone="+971500000001", password="x"
        )
        self.client.force_authenticate(self.user)

    def test_get_creates_defaults(self):
        res = self.client.get(URL)
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.json(), DEFAULTS)

    def test_patch_changes_only_sent_fields(self):
        res = self.client.patch(URL, {"push": False}, format="json")
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.json(), {**DEFAULTS, "push": False})
        self.assertEqual(self.client.get(URL).json(), {**DEFAULTS, "push": False})

    def test_non_boolean_is_400(self):
        for bad in ("true", 1, None):
            res = self.client.patch(URL, {"special_offers": bad}, format="json")
            self.assertEqual(res.status_code, 400)
            self.assertEqual(res.json()["detail"], "Invalid notification preference.")
            self.assertEqual(res.json()["errors"][0]["field"], "special_offers")

    def test_unknown_field_is_400_and_saves_nothing(self):
        res = self.client.patch(URL, {"email": True, "push": False}, format="json")
        self.assertEqual(res.status_code, 400)
        self.assertEqual(res.json()["detail"], "Unknown notification preference.")
        self.assertEqual(self.client.get(URL).json(), DEFAULTS)

    def test_requires_auth(self):
        self.client.force_authenticate(None)
        self.assertEqual(self.client.get(URL).status_code, 401)