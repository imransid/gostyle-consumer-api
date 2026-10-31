import threading
from datetime import timedelta
from io import StringIO
from unittest import mock

from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.db import connection, transaction
from django.test import TestCase, TransactionTestCase, override_settings
from django.utils import timezone

from apps.accounts import push_api
from apps.accounts.models import NotificationPreference
from apps.notifications import worker
from apps.notifications.models import Notification, NotificationDelivery
from apps.notifications.senders import Result
from apps.notifications.service import notify

SEND = "apps.notifications.senders.push_api.send_to_user"


def queued(user, type_="booking.confirmed", key="k-1", **over):
    out = notify(
        user,
        type_,
        dedupe_key=key,
        title="Booking confirmed",
        body="Your booking GS-1 is confirmed.",
        target_type="booking",
        target_id="b-1",
        **over,
    )
    return out.notification.deliveries.get()


def claimed(row):
    rows = worker.claim(timezone.now())
    return next(r for r in rows if r.pk == row.pk)


@override_settings(PUSH_API_KEY="push-key")
class DeliverTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user(phone="+971500000201", password="x")

    def test_claim_counts_the_attempt_and_takes_a_lease(self):
        row = queued(self.user)
        before = timezone.now()

        got = worker.claim(before)

        self.assertEqual([r.pk for r in got], [row.pk])
        row.refresh_from_db()
        self.assertEqual(row.attempts, 1)
        self.assertGreaterEqual(row.next_attempt_at, before + worker.LEASE)
        # Leased: a second claim now finds nothing.
        self.assertEqual(worker.claim(timezone.now()), [])

    def test_a_push_the_service_took_is_sent(self):
        row = claimed(queued(self.user))
        with mock.patch(SEND, return_value=(202, {"devices": 2, "queued": 2})) as send:
            self.assertEqual(worker.deliver(row, timezone.now()), "sent")

        kwargs = send.call_args.kwargs
        notification = row.notification
        self.assertEqual(kwargs["user_id"], self.user.pk)
        self.assertEqual(kwargs["event_id"], f"ntf:{notification.pk}")
        self.assertEqual(kwargs["title"], "Booking confirmed")
        self.assertEqual(
            kwargs["data"],
            {
                "notificationId": str(notification.pk),
                "type": "booking.confirmed",
                "category": "orders",
                "targetType": "booking",
                "targetId": "b-1",
            },
        )
        row.refresh_from_db()
        self.assertEqual((row.status, row.provider_ref), ("sent", "devices=2"))
        self.assertIsNotNone(row.sent_at)

    def test_a_kind_that_pushed_before_keeps_its_old_type(self):
        row = claimed(queued(self.user, "account.new_sign_in"))
        with mock.patch(SEND, return_value=(202, {"devices": 1})) as send:
            worker.deliver(row, timezone.now())
        self.assertEqual(send.call_args.kwargs["data"]["type"], "new_sign_in")

    def test_no_phone_registered_is_skipped(self):
        row = claimed(queued(self.user))
        with mock.patch(SEND, return_value=(202, {"devices": 0})):
            self.assertEqual(worker.deliver(row, timezone.now()), "skipped")
        row.refresh_from_db()
        self.assertEqual((row.status, row.skip_reason), ("skipped", "no_devices"))

    def test_a_refusal_fails_without_a_retry(self):
        row = claimed(queued(self.user))
        with mock.patch(SEND, return_value=(400, {"message": "eventId must be a string"})):
            self.assertEqual(worker.deliver(row, timezone.now()), "failed")
        row.refresh_from_db()
        self.assertEqual(row.status, "failed")
        self.assertIn("400", row.last_error)

    def test_no_answer_waits_and_goes_again(self):
        row = claimed(queued(self.user))
        now = timezone.now()
        with mock.patch(SEND, side_effect=push_api.PushApiUnavailable("timed out")):
            self.assertEqual(worker.deliver(row, now), "retry")
        row.refresh_from_db()
        self.assertEqual(row.status, "pending")
        self.assertEqual(row.next_attempt_at, now + worker.BACKOFF[0])
        self.assertIn("timed out", row.last_error)

    def test_a_server_error_is_a_retry_too(self):
        row = claimed(queued(self.user))
        with mock.patch(SEND, return_value=(503, {})):
            self.assertEqual(worker.deliver(row, timezone.now()), "retry")

    def test_the_last_attempt_fails_for_good(self):
        row = queued(self.user)
        NotificationDelivery.objects.filter(pk=row.pk).update(attempts=worker.MAX_ATTEMPTS - 1)
        row = claimed(row)
        self.assertEqual(row.attempts, worker.MAX_ATTEMPTS)
        with mock.patch(SEND, return_value=(503, {})):
            worker.deliver(row, timezone.now())
        row.refresh_from_db()
        self.assertEqual(row.status, "failed")

    def test_an_expired_alert_is_never_sent(self):
        row = queued(self.user, deliver_by=timezone.now() - timedelta(seconds=1))
        row = claimed(row)
        with mock.patch(SEND) as send:
            self.assertEqual(worker.deliver(row, timezone.now()), "skipped")
        send.assert_not_called()
        row.refresh_from_db()
        self.assertEqual(row.skip_reason, "expired")

    def test_push_switched_off_since_it_was_queued_is_skipped(self):
        row = claimed(queued(self.user))
        prefs = NotificationPreference.for_account(self.user)
        prefs.push = False
        prefs.save()
        with mock.patch(SEND) as send:
            self.assertEqual(worker.deliver(row, timezone.now()), "skipped")
        send.assert_not_called()

    def test_a_purged_account_is_skipped(self):
        row = claimed(queued(self.user))
        type(self.user).all_objects.filter(pk=self.user.pk).update(deleted_at=timezone.now())
        row.notification.account.refresh_from_db()
        with mock.patch(SEND) as send:
            self.assertEqual(worker.deliver(row, timezone.now()), "skipped")
        send.assert_not_called()

    @override_settings(PUSH_API_KEY="")
    def test_the_key_removed_since_it_was_queued_is_skipped(self):
        with override_settings(PUSH_API_KEY="push-key"):
            row = queued(self.user)
        row = claimed(row)
        with mock.patch(SEND) as send:
            self.assertEqual(worker.deliver(row, timezone.now()), "skipped")
        send.assert_not_called()

    def test_a_sender_bug_is_retried_and_capped_like_no_answer(self):
        row = claimed(queued(self.user))
        with mock.patch(SEND, side_effect=KeyError("boom")):
            self.assertEqual(worker.deliver(row, timezone.now()), "retry")
        row.refresh_from_db()
        self.assertEqual(row.status, "pending")
        self.assertIn("KeyError", row.last_error)

    def test_a_late_write_back_cannot_overwrite_a_newer_claim(self):
        row = claimed(queued(self.user))
        # Another worker took the row over after this one's lease ran out.
        NotificationDelivery.objects.filter(pk=row.pk).update(attempts=row.attempts + 1)

        self.assertFalse(worker.finish(row, Result("sent", "devices=1"), timezone.now()))
        row.refresh_from_db()
        self.assertEqual(row.status, "pending")

    def test_run_once_sends_what_is_due_and_counts_it(self):
        queued(self.user, key="a")
        queued(self.user, key="b")
        with mock.patch(SEND, return_value=(202, {"devices": 1})):
            counts = worker.run_once()
        self.assertEqual(counts["sent"], 2)
        self.assertEqual(worker.run_once(), {})

    def test_the_command_once(self):
        queued(self.user)
        out = StringIO()
        with mock.patch(SEND, return_value=(202, {"devices": 1})):
            call_command("notification_worker", "--once", stdout=out)
        self.assertIn("'sent': 1", out.getvalue())

    def test_old_items_are_deleted_with_their_deliveries(self):
        old = queued(self.user, key="old").notification
        Notification.objects.filter(pk=old.pk).update(
            created_at=timezone.now() - worker.RETENTION - timedelta(days=1)
        )
        recent = queued(self.user, key="recent").notification

        self.assertEqual(worker.purge_old(timezone.now()), 1)
        self.assertEqual(list(Notification.objects.values_list("pk", flat=True)), [recent.pk])
        self.assertEqual(NotificationDelivery.objects.count(), 1)

    def test_queue_stats(self):
        queued(self.user)
        stats = worker.queue_stats(timezone.now() + timedelta(seconds=30))
        self.assertEqual(stats["pending"], 1)
        self.assertGreaterEqual(stats["oldest_due_seconds"], 29)


@override_settings(PUSH_API_KEY="push-key")
class SkipLockedTests(TransactionTestCase):
    """Two workers on two real connections: a row one of them holds is not
    the other's to take."""

    def test_a_row_locked_by_another_worker_is_skipped(self):
        user = get_user_model().objects.create_user(phone="+971500000301", password="x")
        held = queued(user, key="held")
        free = queued(user, key="free")
        locked, release = threading.Event(), threading.Event()

        def other_worker():
            try:
                with transaction.atomic():
                    NotificationDelivery.objects.select_for_update().get(pk=held.pk)
                    locked.set()
                    release.wait(5)
            finally:
                connection.close()

        thread = threading.Thread(target=other_worker)
        thread.start()
        try:
            self.assertTrue(locked.wait(5))
            got = worker.claim(timezone.now())
        finally:
            release.set()
            thread.join(5)

        self.assertEqual([r.pk for r in got], [free.pk])


from django.core import mail  # noqa: E402
from django.core.exceptions import ImproperlyConfigured  # noqa: E402

from apps.accounts.sendgrid_backend import SendGridError  # noqa: E402

MAIL_SEND = "apps.notifications.senders.EmailMultiAlternatives.send"


@override_settings(PUSH_API_KEY="push-key", NOTIFICATION_CHANNELS_V1=True)
class EmailDeliverTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user(
            phone="+971500000801",
            email="sara@example.com",
            password="x",
        )
        self.user.email_verified_at = timezone.now()
        self.user.save()

    def email_row(self):
        out = notify(
            self.user,
            "booking.confirmed",
            dedupe_key="k-1",
            title="Booking confirmed",
            body="Your booking GS-1 is confirmed.",
        )
        delivery = out.notification.deliveries.get(channel="email")
        return next(r for r in worker.claim(timezone.now()) if r.pk == delivery.pk)

    def test_the_branded_email_goes_to_the_verified_address(self):
        row = self.email_row()

        self.assertEqual(worker.deliver(row, timezone.now()), "sent")

        self.assertEqual(len(mail.outbox), 1)
        sent = mail.outbox[0]
        self.assertEqual(sent.to, ["sara@example.com"])
        self.assertEqual(sent.subject, "Booking confirmed")
        self.assertIn("Your booking GS-1 is confirmed.", sent.body)
        self.assertEqual(sent.alternatives[0][1], "text/html")
        row.refresh_from_db()
        self.assertEqual((row.status, row.provider_ref), ("sent", "sendgrid"))

    def test_email_turned_off_since_is_skipped(self):
        row = self.email_row()
        prefs = NotificationPreference.for_account(self.user)
        prefs.email = False
        prefs.save()

        self.assertEqual(worker.deliver(row, timezone.now()), "skipped")
        self.assertEqual(mail.outbox, [])
        row.refresh_from_db()
        self.assertEqual(row.skip_reason, "opted_out")

    def test_an_address_no_longer_verified_is_not_written_to(self):
        row = self.email_row()
        type(self.user).objects.filter(pk=self.user.pk).update(email_verified_at=None)
        row.notification.account.refresh_from_db()

        self.assertEqual(worker.deliver(row, timezone.now()), "skipped")
        self.assertEqual(mail.outbox, [])

    def test_a_refusal_fails_and_no_answer_retries(self):
        for error, kind in (
            (SendGridError("SendGrid HTTP 400: bad from", 400), "failed"),
            (SendGridError("SendGrid HTTP 503: down", 503), "retry"),
            (SendGridError("SendGrid HTTP 429: slow down", 429), "retry"),
            (SendGridError("SendGrid unreachable: TimeoutError"), "retry"),
        ):
            Notification.objects.all().delete()
            row = self.email_row()
            with mock.patch(MAIL_SEND, side_effect=error):
                self.assertEqual(worker.deliver(row, timezone.now()), kind, error)

    def test_no_sendgrid_key_is_skipped(self):
        row = self.email_row()
        with mock.patch(MAIL_SEND, side_effect=ImproperlyConfigured("no key")):
            self.assertEqual(worker.deliver(row, timezone.now()), "skipped")
        row.refresh_from_db()
        self.assertEqual(row.skip_reason, "email_not_configured")
