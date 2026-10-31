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


@override_settings(INTERNAL_GRPC_KEY="k-123", PUSH_API_KEY="")
class ReminderWordsTests(TestCase):
    """booking.reminder: 'today', 'tomorrow' or the date, as the salon's
    clock has them, from the moment it is written."""

    def setUp(self):
        self.user = get_user_model().objects.create_user(phone="+971500000902", password="x")

    def remind(self, start_at, now, **over):
        fields = {
            "consumer_id": str(self.user.pk),
            "type": "booking.reminder",
            "dedupe_key": f"booking.reminder:{BOOKING}:{start_at}",
            "params": {
                "booking_id": BOOKING,
                "booking_code": "GS-1483",
                "branch_id": BRANCH,
                "start_at": start_at,
            },
            "target_type": "booking",
            "target_id": BOOKING,
            "source": "booking-api",
        }
        fields.update(over)
        with mock.patch("apps.grpc_gen.servicer.close_old_connections"), mock.patch(
            "apps.notifications.wording.salon_cards_for_refs", return_value={BRANCH: DEMO}
        ), mock.patch("apps.notifications.wording.timezone.now", return_value=now):
            reply = CustomerNotificationsServicer().Notify(pb.NotifyRequest(**fields), _context())
        return Notification.objects.get(pk=reply.notification_id)

    def at(self, iso):
        return datetime.fromisoformat(iso)

    def test_today(self):
        row = self.remind("2026-11-02T07:30:00.000Z", self.at("2026-11-02T06:30:00+00:00"))
        self.assertEqual(row.title, "Appointment reminder")
        self.assertEqual(row.body, "Your booking GS-1483 at Demo Salon & Spa is today at 11:30 AM.")
        self.assertEqual(row.category, "updates")
        self.assertEqual(list(row.deliveries.values_list("channel", flat=True)), ["push"])

    def test_tomorrow(self):
        row = self.remind("2026-11-02T07:30:00.000Z", self.at("2026-11-01T07:30:00+00:00"))
        self.assertEqual(row.body, "Your booking GS-1483 at Demo Salon & Spa is tomorrow at 11:30 AM.")

    def test_further_off_says_the_date(self):
        row = self.remind("2026-11-02T07:30:00.000Z", self.at("2026-10-30T07:30:00+00:00"))
        self.assertEqual(
            row.body, "Your booking GS-1483 at Demo Salon & Spa is on Mon 2 Nov at 11:30 AM."
        )

    def test_the_day_is_the_salons_not_utcs(self):
        # 00:30 on 3 Nov in Dubai is still 2 Nov in UTC; at 23:30 Dubai the
        # visit is tomorrow, not today.
        row = self.remind("2026-11-02T20:30:00.000Z", self.at("2026-11-02T19:30:00+00:00"))
        self.assertEqual(
            row.body, "Your booking GS-1483 at Demo Salon & Spa is tomorrow at 12:30 AM."
        )

    def test_reminders_off_keeps_it_and_sends_nothing(self):
        NotificationPreference.objects.create(account=self.user, appointment_reminder=False)
        row = self.remind("2026-11-02T07:30:00.000Z", self.at("2026-11-02T06:30:00+00:00"))
        self.assertFalse(row.deliveries.exists())

    def test_a_reminder_without_a_start_is_refused(self):
        with self.assertRaises(_Aborted) as caught, mock.patch(
            "apps.grpc_gen.servicer.close_old_connections"
        ), mock.patch("apps.notifications.wording.salon_cards_for_refs", return_value={}):
            CustomerNotificationsServicer().Notify(
                pb.NotifyRequest(
                    consumer_id=str(self.user.pk),
                    type="booking.reminder",
                    dedupe_key="k",
                    params={"booking_code": "GS-1"},
                ),
                _context(),
            )
        self.assertEqual(caught.exception.code, grpc.StatusCode.INVALID_ARGUMENT)


@override_settings(INTERNAL_GRPC_KEY="k-123", PUSH_API_KEY="")
class CheckInWordsTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user(phone="+971500000903", password="x")

    def notify(self, answer="approved", **params):
        fields = {
            "consumer_id": str(self.user.pk),
            "type": f"check_in.{answer}",
            "dedupe_key": f"check_in:req-1:{answer}",
            "params": {
                "booking_id": BOOKING,
                "booking_code": "GS-1483",
                "branch_id": BRANCH,
                "start_at": "2026-11-02T07:30:00.000Z",
                "request_id": "req-1",
                "title": "You are checked in",
                "body": "Welcome! Booking GS-1483.",
                **params,
            },
            "target_type": "booking",
            "target_id": BOOKING,
            "source": "booking-api",
        }
        with mock.patch("apps.grpc_gen.servicer.close_old_connections"), mock.patch(
            "apps.notifications.wording.salon_cards_for_refs", return_value={BRANCH: DEMO}
        ):
            return CustomerNotificationsServicer().Notify(pb.NotifyRequest(**fields), _context())

    def test_kept_in_booking_apis_words_in_the_orders_tab(self):
        reply = self.notify()
        row = Notification.objects.get(pk=reply.notification_id)
        self.assertEqual((row.title, row.body), ("You are checked in", "Welcome! Booking GS-1483."))
        self.assertEqual(row.category, "orders")
        self.assertEqual(row.data["requestId"], "req-1")
        self.assertEqual(row.data["code"], "GS-1483")
        self.assertEqual(list(row.deliveries.values_list("channel", flat=True)), ["push"])

    def test_the_push_keeps_the_type_and_keys_the_app_already_reads(self):
        from apps.notifications.senders import push_data

        reply = self.notify(group_id="g-1")
        data = push_data(Notification.objects.get(pk=reply.notification_id))
        self.assertEqual(data["type"], "check_in_request.approved")
        for key in ("bookingId", "code", "requestId", "groupId"):
            self.assertIn(key, data)

    def test_not_silenced_by_booking_changes(self):
        NotificationPreference.objects.create(account=self.user, booking_changes=False)
        reply = self.notify("rejected", title="Please speak to the desk", body="The salon could not confirm your arrival. Booking GS-1483.")
        self.assertTrue(Notification.objects.get(pk=reply.notification_id).deliveries.exists())

    def test_without_its_words_it_is_refused(self):
        with self.assertRaises(_Aborted) as caught:
            self.notify(title="")
        self.assertEqual(caught.exception.code, grpc.StatusCode.INVALID_ARGUMENT)


@override_settings(INTERNAL_GRPC_KEY="k-123", PUSH_API_KEY="")
class RoutineAndPartyWordsTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user(phone="+971500000904", password="x")

    def confirm(self, key, **params):
        fields = {
            "consumer_id": str(self.user.pk),
            "type": "booking.confirmed",
            "dedupe_key": key,
            "params": {
                "booking_id": BOOKING,
                "booking_code": "GS-1490",
                "branch_id": BRANCH,
                "start_at": "2026-11-02T07:00:00.000Z",
                **params,
            },
            "target_type": "booking",
            "target_id": BOOKING,
            "source": "booking-api",
        }
        with mock.patch("apps.grpc_gen.servicer.close_old_connections"), mock.patch(
            "apps.notifications.wording.salon_cards_for_refs", return_value={BRANCH: DEMO}
        ):
            reply = CustomerNotificationsServicer().Notify(pb.NotifyRequest(**fields), _context())
        return reply, Notification.objects.get(pk=reply.notification_id)

    def test_a_routine_is_one_item_with_its_first_visit_and_count(self):
        _, row = self.confirm("booking.confirmed:series:ser-1", series_id="ser-1", visits="4")
        self.assertEqual(row.title, "Routine confirmed")
        self.assertEqual(
            row.body,
            "Your routine at Demo Salon & Spa is confirmed: 4 visits, starting Mon 2 Nov at 11:00 AM.",
        )
        self.assertEqual(row.data["seriesId"], "ser-1")
        again, _ = self.confirm("booking.confirmed:series:ser-1", series_id="ser-1", visits="4")
        self.assertEqual(again.outcome, Reply.DUPLICATE)

    def test_a_party_is_one_item(self):
        _, row = self.confirm("booking.confirmed:group:g-1", group_id="g-1")
        self.assertEqual(
            row.body, "Your group booking at Demo Salon & Spa on Mon 2 Nov at 11:00 AM is confirmed."
        )
        self.assertEqual(row.data["groupId"], "g-1")
