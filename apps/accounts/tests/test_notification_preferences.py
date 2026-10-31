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

from django.test import override_settings  # noqa: E402
from django.utils import timezone  # noqa: E402

from apps.accounts.models import NotificationPreference  # noqa: E402


@override_settings(NOTIFICATION_CHANNELS_V1=True, REMINDER_TIMING_V1=False, PUSH_API_KEY="k")
class ChannelSwitchTests(APITestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user(phone="+971500000701", password="x")
        self.client.force_authenticate(self.user)

    def patch(self, body):
        return self.client.patch(URL, body, format="json")

    def item(self, res):
        return res.json()["errors"][0]

    def test_get_adds_the_channels_and_what_can_reach_them(self):
        res = self.client.get(URL)
        self.assertEqual(
            res.json(),
            {
                **DEFAULTS,
                "email": True,
                "sms": False,
                "channels_available": {"push": True, "email": False, "sms": False},
            },
        )

    def test_a_verified_email_is_available(self):
        self.user.email = "sara@example.com"
        self.user.email_verified_at = timezone.now()
        self.user.save()
        self.assertTrue(self.client.get(URL).json()["channels_available"]["email"])

    def test_email_off_always_works(self):
        res = self.patch({"email": False})
        self.assertEqual(res.status_code, 200)
        self.assertFalse(res.json()["email"])

    def test_email_sent_back_unchanged_is_not_asked_again(self):
        # Default on, and no address: an app echoing the object still saves.
        res = self.patch({"email": True, "special_offers": True})
        self.assertEqual(res.status_code, 200)
        self.assertTrue(res.json()["special_offers"])

    def test_email_back_on_needs_an_address_then_a_verified_one(self):
        self.patch({"email": False})

        res = self.patch({"email": True})
        self.assertEqual(res.status_code, 400)
        self.assertEqual(self.item(res)["field"], "email")
        self.assertEqual(self.item(res)["code"], "no_email_on_account")

        self.user.email = "sara@example.com"
        self.user.save()
        self.assertEqual(self.item(self.patch({"email": True}))["code"], "email_not_verified")

        self.user.email_verified_at = timezone.now()
        self.user.save()
        self.assertEqual(self.patch({"email": True}).status_code, 200)

    def test_sms_on_needs_a_verified_phone_then_a_provider(self):
        res = self.patch({"sms": True})
        self.assertEqual(self.item(res)["code"], "phone_not_verified")

        self.user.phone_verified_at = timezone.now()
        self.user.save()
        res = self.patch({"sms": True})
        self.assertEqual(res.status_code, 400)
        self.assertEqual(self.item(res)["code"], "sms_unavailable")
        self.assertFalse(NotificationPreference.objects.get(account=self.user).sms)

    def test_sms_off_always_works(self):
        self.assertEqual(self.patch({"sms": False}).status_code, 200)

    def test_channels_available_sent_back_is_ignored(self):
        res = self.patch({"push": False, "channels_available": {"push": True}})
        self.assertEqual(res.status_code, 200)
        self.assertFalse(res.json()["push"])

    def test_a_refused_switch_saves_nothing_else(self):
        self.patch({"email": False})
        res = self.patch({"email": True, "loyalty": False})
        self.assertEqual(res.status_code, 400)
        self.assertTrue(NotificationPreference.objects.get(account=self.user).loyalty)

    def test_every_error_item_carries_its_code(self):
        res = self.patch({"special_offers": "yes"})
        self.assertEqual(self.item(res)["code"], "invalid")
        res = self.patch({"specail_offers": True})
        self.assertEqual(self.item(res)["code"], "unknown_field")

    def test_reminder_time_is_still_unknown_while_its_switch_is_off(self):
        res = self.patch({"reminder_lead_minutes": 60})
        self.assertEqual(res.status_code, 400)
        self.assertEqual(self.item(res)["code"], "unknown_field")


@override_settings(NOTIFICATION_CHANNELS_V1=False, REMINDER_TIMING_V1=True)
class ReminderTimingTests(APITestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user(phone="+971500000702", password="x")
        self.client.force_authenticate(self.user)

    def test_get_adds_the_reminder_time_only(self):
        self.assertEqual(self.client.get(URL).json(), {**DEFAULTS, "reminder_lead_minutes": 1440})

    def test_each_of_the_four_times_is_kept(self):
        for minutes in (30, 60, 120, 1440):
            res = self.client.patch(URL, {"reminder_lead_minutes": minutes}, format="json")
            self.assertEqual(res.status_code, 200)
            self.assertEqual(
                NotificationPreference.objects.get(account=self.user).reminder_lead_minutes,
                minutes,
            )

    def test_anything_else_is_refused(self):
        for bad in (45, "60", 60.0, True, 0):
            res = self.client.patch(URL, {"reminder_lead_minutes": bad}, format="json")
            self.assertEqual(res.status_code, 400, bad)
            item = res.json()["errors"][0]
            self.assertEqual(item["field"], "reminder_lead_minutes")
            self.assertEqual(item["message"], "Choose 30, 60, 120 or 1440.")
        # null has DRF's own sentence, on the same field.
        res = self.client.patch(URL, {"reminder_lead_minutes": None}, format="json")
        self.assertEqual(res.status_code, 400)
        self.assertEqual(res.json()["errors"][0]["field"], "reminder_lead_minutes")

    def test_the_channels_stay_unknown_while_their_switch_is_off(self):
        res = self.client.patch(URL, {"email": False}, format="json")
        self.assertEqual(res.status_code, 400)
        self.assertEqual(res.json()["detail"], "Unknown notification preference.")
