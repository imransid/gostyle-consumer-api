from datetime import datetime, timezone
from unittest import mock

import grpc
from django.contrib.auth import get_user_model
from django.db import DatabaseError
from django.test import TestCase, override_settings

from apps.accounts.models import NotificationPreference
from apps.grpc_gen import customer_notifications_pb2 as pb
from apps.grpc_gen.servicer import CustomerNotificationsServicer
from apps.notifications.models import Notification

Reply = pb.NotifyReply
BOOKING = "e2205c30-3f9a-4cdd-ae09-21adee041d15"
BRANCH = "2e8ca45d-39d2-4d94-b251-1d60f4965184"
DEMO = {"id": "7dd88cda-9a17-4ac8-9f78-41c2903e23c8", "name": "Demo Salon & Spa", "timezone": "Asia/Dubai"}


class _Aborted(Exception):
    def __init__(self, code, details):
        super().__init__(details)
        self.code = code


def _context(key="k-123"):
    context = mock.Mock()
    context.invocation_metadata.return_value = [] if key is None else [("x-internal-key", key)]

    def abort(code, details):
        raise _Aborted(code, details)

    context.abort.side_effect = abort
    return context


@override_settings(INTERNAL_GRPC_KEY="k-123", PUSH_API_KEY="")
class NotifyTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user(phone="+971500000901", password="x")
        self.cards = {BRANCH: DEMO}

    def call(self, key="k-123", salons=None, **over):
        fields = {
            "consumer_id": str(self.user.pk),
            "type": "booking.confirmed",
            "dedupe_key": f"booking.confirmed:{BOOKING}",
            "params": {
                "booking_id": BOOKING,
                "booking_code": "GS-1481",
                "branch_id": BRANCH,
                "start_at": "2026-11-02T07:00:00.000Z",
            },
            "target_type": "booking",
            "target_id": BOOKING,
            "source": "booking-api",
        }
        fields.update(over)
        request = pb.NotifyRequest(**fields)
        cards = self.cards if salons is None else salons
        side_effect = cards if isinstance(cards, Exception) else None
        with mock.patch("apps.grpc_gen.servicer.close_old_connections"), mock.patch(
            "apps.notifications.wording.salon_cards_for_refs",
            return_value=None if side_effect else cards,
            side_effect=side_effect,
        ):
            return CustomerNotificationsServicer().Notify(request, _context(key))

    def refused(self, **over):
        with self.assertRaises(_Aborted) as caught:
            self.call(**over)
        return caught.exception.code

    def test_confirmed_is_kept_in_the_salons_words_and_time(self):
        reply = self.call()

        self.assertEqual(reply.outcome, Reply.ACCEPTED)
        row = Notification.objects.get(pk=reply.notification_id)
        self.assertEqual(row.type, "booking.confirmed")
        self.assertEqual(row.category, "orders")
        self.assertEqual(row.title, "Booking confirmed")
        # 07:00 UTC is 11:00 in Dubai, the salon's own clock.
        self.assertEqual(
            row.body, "Your booking GS-1481 at Demo Salon & Spa on Mon 2 Nov at 11:00 AM is confirmed."
        )
        self.assertEqual((row.target_type, row.target_id), ("booking", BOOKING))
        self.assertEqual(
            row.data,
            {"bookingId": BOOKING, "bookingCode": "GS-1481", "startAt": "2026-11-02T07:00:00.000Z", "salonId": DEMO["id"]},
        )
        self.assertEqual(row.source, "booking-api")
        self.assertEqual(list(row.deliveries.values_list("channel", flat=True)), ["push"])

    def test_the_same_key_again_is_a_duplicate_of_the_same_item(self):
        first = self.call()
        second = self.call()
        self.assertEqual(second.outcome, Reply.DUPLICATE)
        self.assertEqual(second.notification_id, first.notification_id)
        self.assertEqual(Notification.objects.count(), 1)

    def test_moved_and_cancelled_have_their_own_words(self):
        moved = self.call(type="booking.rescheduled", dedupe_key="booking.rescheduled:evt-2")
        cancelled = self.call(type="booking.cancelled", dedupe_key=f"booking.cancelled:{BOOKING}")
        self.assertEqual(
            Notification.objects.get(pk=moved.notification_id).body,
            "Your booking GS-1481 at Demo Salon & Spa has a new time: Mon 2 Nov at 11:00 AM.",
        )
        self.assertEqual(
            Notification.objects.get(pk=cancelled.notification_id).body,
            "Your booking GS-1481 at Demo Salon & Spa on Mon 2 Nov at 11:00 AM was cancelled.",
        )

    def test_another_salons_clock(self):
        reply = self.call(salons={BRANCH: {**DEMO, "timezone": "Europe/London"}})
        self.assertIn("on Mon 2 Nov at 7:00 AM", Notification.objects.get(pk=reply.notification_id).body)

    def test_an_unknown_salon_leaves_its_name_out(self):
        reply = self.call(salons={})
        row = Notification.objects.get(pk=reply.notification_id)
        # No salon, so no name, and the default clock (Dubai).
        self.assertEqual(row.body, "Your booking GS-1481 on Mon 2 Nov at 11:00 AM is confirmed.")
        self.assertIsNone(row.data.get("salonId"))

    def test_a_failed_salon_lookup_costs_the_name_not_the_notification(self):
        reply = self.call(salons=DatabaseError("platform db down"))
        self.assertEqual(reply.outcome, Reply.ACCEPTED)
        self.assertNotIn(" at Demo", Notification.objects.get(pk=reply.notification_id).body)

    def test_the_switch_off_keeps_the_item_and_queues_no_alert(self):
        NotificationPreference.objects.create(account=self.user, booking_confirmation=False)
        reply = self.call()
        self.assertEqual(reply.outcome, Reply.ACCEPTED)
        self.assertFalse(Notification.objects.get(pk=reply.notification_id).deliveries.exists())

    def test_deliver_by_caps_the_alert(self):
        reply = self.call(deliver_by="2026-11-01T07:00:00+00:00")
        delivery = Notification.objects.get(pk=reply.notification_id).deliveries.get()
        self.assertEqual(delivery.expires_at, datetime(2026, 11, 1, 7, tzinfo=timezone.utc))

    def test_no_such_customer_is_an_answer(self):
        for consumer_id in ("not-an-id", "22222222-2222-4222-8222-222222222222"):
            reply = self.call(consumer_id=consumer_id)
            self.assertEqual(reply.outcome, Reply.NOT_FOUND)
            self.assertEqual(reply.notification_id, "")
        self.user.is_active = False
        self.user.save()
        self.assertEqual(self.call().outcome, Reply.NOT_FOUND)
        self.assertEqual(Notification.objects.count(), 0)

    def test_producer_mistakes_are_invalid_argument_and_keep_nothing(self):
        for over in (
            {"type": "booking.exploded"},
            {"dedupe_key": ""},
            {"params": {"booking_id": BOOKING}},
            {"params": {"booking_code": "GS-1", "start_at": "tomorrow"}},
            {"params": {"booking_code": "GS-1", "start_at": "2026-11-02T07:00:00"}},
            {"deliver_by": "soon"},
            # A kind the center knows but has no words for from outside.
            {"type": "account.new_sign_in"},
        ):
            self.assertEqual(self.refused(**over), grpc.StatusCode.INVALID_ARGUMENT, over)
        self.assertEqual(Notification.objects.count(), 0)

    def test_no_key_is_refused(self):
        self.assertEqual(self.refused(key=None), grpc.StatusCode.UNAUTHENTICATED)
        self.assertEqual(Notification.objects.count(), 0)
