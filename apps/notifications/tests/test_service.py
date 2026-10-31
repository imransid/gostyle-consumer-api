import uuid
from datetime import timedelta
from types import SimpleNamespace
from unittest import mock

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from django.utils import timezone

from apps.accounts.models import NotificationPreference
from apps.accounts.notifications import send_new_sign_in_push
from apps.notifications import registry
from apps.notifications.models import Notification, NotificationDelivery
from apps.notifications.service import notify


def account(phone="+971500000101"):
    return get_user_model().objects.create_user(phone=phone, password="x")


def kept(user, type_="booking.confirmed", key="k-1", **over):
    return notify(
        user,
        type_,
        dedupe_key=key,
        title=over.pop("title", "Booking confirmed"),
        body=over.pop("body", "Your booking GS-1 is confirmed."),
        **over,
    )


@override_settings(PUSH_API_KEY="push-key")
class NotifyTests(TestCase):
    def setUp(self):
        self.user = account()

    def test_keeps_the_item_and_queues_a_push(self):
        out = kept(self.user, target_type="booking", target_id=uuid.UUID(int=7))

        self.assertTrue(out.created)
        row = out.notification
        self.assertEqual(row.category, "orders")
        self.assertEqual(row.type, "booking.confirmed")
        self.assertEqual(row.target_type, "booking")
        self.assertEqual(row.target_id, str(uuid.UUID(int=7)))
        self.assertIsNone(row.read_at)
        deliveries = list(row.deliveries.all())
        self.assertEqual([d.channel for d in deliveries], ["push"])
        self.assertEqual(deliveries[0].status, "pending")
        self.assertEqual(deliveries[0].attempts, 0)

    def test_the_same_key_twice_is_one_item_and_one_push(self):
        first = kept(self.user)
        second = kept(self.user, title="Something else")

        self.assertFalse(second.created)
        self.assertEqual(second.notification.pk, first.notification.pk)
        self.assertEqual(Notification.objects.count(), 1)
        self.assertEqual(NotificationDelivery.objects.count(), 1)
        self.assertEqual(second.notification.title, "Booking confirmed")

    def test_the_same_key_for_another_account_is_its_own_item(self):
        kept(self.user)
        kept(account("+971500000102"))
        self.assertEqual(Notification.objects.count(), 2)

    def test_its_event_switch_off_keeps_the_item_but_sends_nothing(self):
        prefs = NotificationPreference.for_account(self.user)
        prefs.booking_confirmation = False
        prefs.save()

        out = kept(self.user)

        self.assertTrue(out.created)
        self.assertFalse(out.notification.deliveries.exists())

    def test_push_off_keeps_the_item_but_sends_nothing(self):
        prefs = NotificationPreference.for_account(self.user)
        prefs.push = False
        prefs.save()

        out = kept(self.user, "payment.completed")

        self.assertTrue(out.created)
        self.assertFalse(out.notification.deliveries.exists())

    @override_settings(PUSH_API_KEY="")
    def test_no_push_service_keeps_the_item_but_queues_nothing(self):
        out = kept(self.user)
        self.assertTrue(out.created)
        self.assertFalse(out.notification.deliveries.exists())

    def test_money_ignores_the_event_switches(self):
        prefs = NotificationPreference.for_account(self.user)
        prefs.booking_confirmation = prefs.booking_changes = False
        prefs.save()

        out = kept(self.user, "payment.refunded")

        self.assertEqual(out.notification.category, "payments")
        self.assertEqual(list(out.notification.deliveries.values_list("channel", flat=True)), ["push"])

    def test_a_promotion_with_its_switch_off_is_not_kept_at_all(self):
        # special_offers defaults to False.
        out = kept(self.user, "promo.offer")

        self.assertIsNone(out.notification)
        self.assertFalse(out.created)
        self.assertEqual(Notification.objects.count(), 0)

    def test_a_promotion_with_its_switch_on_is_kept_and_sent(self):
        prefs = NotificationPreference.for_account(self.user)
        prefs.special_offers = True
        prefs.save()

        out = kept(self.user, "promo.offer")

        self.assertEqual(out.notification.category, "promotions")
        self.assertTrue(out.notification.deliveries.exists())

    def channels(self, out):
        return sorted(out.notification.deliveries.values_list("channel", flat=True))

    def verify_email(self):
        self.user.email = "sara@example.com"
        self.user.email_verified_at = timezone.now()
        self.user.save()

    @override_settings(NOTIFICATION_CHANNELS_V1=False)
    def test_email_is_not_queued_while_its_switch_is_off(self):
        self.verify_email()
        self.assertEqual(self.channels(kept(self.user)), ["push"])

    @override_settings(NOTIFICATION_CHANNELS_V1=True)
    def test_email_is_queued_to_a_verified_address(self):
        self.verify_email()
        self.assertEqual(self.channels(kept(self.user)), ["email", "push"])

    @override_settings(NOTIFICATION_CHANNELS_V1=True)
    def test_email_is_not_queued_to_an_unverified_address(self):
        self.user.email = "sara@example.com"
        self.user.save()
        self.assertEqual(self.channels(kept(self.user)), ["push"])

    @override_settings(NOTIFICATION_CHANNELS_V1=True)
    def test_email_is_not_queued_when_the_customer_turned_it_off(self):
        self.verify_email()
        prefs = NotificationPreference.for_account(self.user)
        prefs.email = False
        prefs.save()
        self.assertEqual(self.channels(kept(self.user)), ["push"])

    @override_settings(NOTIFICATION_CHANNELS_V1=True)
    def test_a_push_only_kind_never_emails(self):
        self.verify_email()
        self.assertEqual(self.channels(kept(self.user, "account.new_sign_in")), ["push"])

    def test_deciding_never_writes_a_preferences_row(self):
        kept(self.user)
        self.assertFalse(NotificationPreference.objects.filter(account=self.user).exists())

    def test_an_unknown_type_is_refused_and_nothing_is_kept(self):
        with self.assertRaises(registry.UnknownKind):
            kept(self.user, "booking.exploded")
        self.assertEqual(Notification.objects.count(), 0)

    def test_data_is_strings_and_nones_are_dropped(self):
        out = kept(self.user, data={"code": "GS-1", "amount": 12.5, "gone": None})
        self.assertEqual(out.notification.data, {"code": "GS-1", "amount": "12.5"})

    def test_half_a_target_is_no_target(self):
        out = kept(self.user, target_type="booking")
        self.assertIsNone(out.notification.target_type)
        self.assertIsNone(out.notification.target_id)

    def test_deliver_by_caps_the_alert(self):
        visit = timezone.now() + timedelta(hours=1)
        out = kept(self.user, "booking.reminder", deliver_by=visit)
        self.assertEqual(out.notification.deliveries.get().expires_at, visit)

    def test_without_deliver_by_the_alert_may_wait_a_day(self):
        before = timezone.now()
        out = kept(self.user)
        expires = out.notification.deliveries.get().expires_at
        self.assertGreaterEqual(expires, before + timedelta(days=1))
        self.assertLessEqual(expires, timezone.now() + timedelta(days=1))

    def test_too_long_words_are_cut_rather_than_refused(self):
        out = kept(self.user, title="t" * 300, body="b" * 3000)
        self.assertEqual(len(out.notification.title), 140)
        self.assertEqual(len(out.notification.body), 1000)


@override_settings(PUSH_API_KEY="push-key")
class SignInAlertTests(TestCase):
    def setUp(self):
        self.user = account()
        self.session = SimpleNamespace(
            pk=uuid.UUID(int=42), device_name="iPhone 15", location="Dubai, UAE"
        )

    @override_settings(NOTIFICATION_CENTER_V1=True)
    def test_on_it_is_kept_in_the_center_and_queued_not_sent_inline(self):
        with mock.patch("apps.accounts.notifications.push_api.send_to_user") as send:
            send_new_sign_in_push(self.user, self.session)

        send.assert_not_called()
        row = Notification.objects.get(account=self.user)
        self.assertEqual(row.type, "account.new_sign_in")
        self.assertEqual(row.category, "updates")
        self.assertEqual(row.dedupe_key, f"new-sign-in:{self.session.pk}")
        self.assertEqual((row.target_type, row.target_id), ("session", str(self.session.pk)))
        self.assertIn("iPhone 15 in Dubai, UAE", row.body)
        self.assertEqual(row.deliveries.get().channel, "push")

    @override_settings(NOTIFICATION_CENTER_V1=True)
    def test_on_the_same_session_twice_is_one_item(self):
        send_new_sign_in_push(self.user, self.session)
        send_new_sign_in_push(self.user, self.session)
        self.assertEqual(Notification.objects.count(), 1)

    @override_settings(NOTIFICATION_CENTER_V1=True)
    def test_on_a_failure_never_reaches_the_sign_in(self):
        with mock.patch(
            "apps.notifications.service.Notification.objects.get_or_create",
            side_effect=RuntimeError("db down"),
        ):
            send_new_sign_in_push(self.user, self.session)  # does not raise

    @override_settings(NOTIFICATION_CENTER_V1=False)
    def test_off_it_pushes_inline_as_before_and_keeps_nothing(self):
        with mock.patch(
            "apps.accounts.notifications.push_api.send_to_user", return_value=(202, {})
        ) as send:
            send_new_sign_in_push(self.user, self.session)

        send.assert_called_once()
        self.assertEqual(send.call_args.kwargs["data"]["type"], "new_sign_in")
        self.assertEqual(Notification.objects.count(), 0)
