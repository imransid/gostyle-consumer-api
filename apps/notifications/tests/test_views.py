from datetime import timedelta

from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.test import override_settings
from django.utils import timezone
from rest_framework.test import APITestCase

from apps.accounts import ratelimit
from apps.notifications.models import Notification

BASE = "/api/v1/notifications"


def item(account, n, category="orders", type_="booking.confirmed", **over):
    """A kept notification, n minutes old."""
    when = timezone.now() - timedelta(minutes=n)
    return Notification.objects.create(
        account=account,
        category=category,
        type=type_,
        title=f"Item {n}",
        body="body",
        source="test",
        dedupe_key=f"k-{n}-{category}",
        occurred_at=when,
        created_at=when,
        **over,
    )


@override_settings(NOTIFICATION_CENTER_V1=True)
class NotificationCenterTests(APITestCase):
    def setUp(self):
        cache.clear()
        users = get_user_model().objects
        self.me = users.create_user(phone="+971500000401", password="x")
        self.other = users.create_user(phone="+971500000402", password="x")
        self.client.force_authenticate(self.me)

    def ids(self, res):
        return [r["title"] for r in res.json()["results"]]

    # The list.

    def test_empty_is_200_with_no_results(self):
        res = self.client.get(BASE)
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.json()["results"], [])
        self.assertIsNone(res.json()["next"])

    def test_newest_first_and_only_mine(self):
        item(self.me, 30)
        item(self.me, 5)
        item(self.other, 1)

        res = self.client.get(BASE)

        self.assertEqual(self.ids(res), ["Item 5", "Item 30"])

    def test_the_item_shape(self):
        n = item(self.me, 1, target_type="booking", target_id="b-1", data={"code": "GS-1"})
        got = self.client.get(BASE).json()["results"][0]
        self.assertEqual(
            set(got),
            {"id", "category", "type", "title", "body", "is_read", "read_at",
             "created_at", "occurred_at", "target", "data"},
        )
        self.assertEqual(got["id"], str(n.pk))
        self.assertFalse(got["is_read"])
        self.assertEqual(got["target"], {"type": "booking", "id": "b-1"})
        self.assertEqual(got["data"], {"code": "GS-1"})

    def test_no_target_is_null(self):
        item(self.me, 1)
        self.assertIsNone(self.client.get(BASE).json()["results"][0]["target"])

    def test_one_tab(self):
        item(self.me, 1, "orders")
        item(self.me, 2, "payments", "payment.completed")

        res = self.client.get(BASE, {"category": "payments"})

        self.assertEqual(self.ids(res), ["Item 2"])

    def test_unread_only(self):
        item(self.me, 1, read_at=timezone.now())
        item(self.me, 2)
        self.assertEqual(self.ids(self.client.get(BASE, {"unread": "true"})), ["Item 2"])
        self.assertEqual(self.ids(self.client.get(BASE, {"unread": "false"})), ["Item 1"])

    def test_an_offer_past_its_end_is_gone(self):
        item(self.me, 1, "promotions", "promo.offer", expires_at=timezone.now() - timedelta(seconds=1))
        item(self.me, 2, "promotions", "promo.offer", expires_at=timezone.now() + timedelta(days=1))
        self.assertEqual(self.ids(self.client.get(BASE)), ["Item 2"])

    def test_pages_by_cursor_without_repeats(self):
        for n in range(1, 6):
            item(self.me, n)

        first = self.client.get(BASE, {"page_size": 2}).json()
        second = self.client.get(first["next"]).json()
        # A new item at the top while paging does not shift the next page.
        item(self.me, 0)
        third = self.client.get(second["next"]).json()

        seen = [r["title"] for page in (first, second, third) for r in page["results"]]
        self.assertEqual(seen, ["Item 1", "Item 2", "Item 3", "Item 4", "Item 5"])
        self.assertIsNone(third["next"])

    def test_bad_query_values_are_422(self):
        for params in ({"category": "shoes"}, {"page_size": 0}, {"page_size": 51}, {"unread": "maybe"}):
            res = self.client.get(BASE, params)
            self.assertEqual(res.status_code, 422, params)
            self.assertEqual(res.json()["code"], "validation_error")

    def test_a_broken_cursor_is_422(self):
        res = self.client.get(BASE, {"cursor": "not-a-cursor"})
        self.assertEqual(res.status_code, 422)
        self.assertEqual(res.json()["errors"][0]["code"], "invalid_cursor")

    # One item.

    def test_detail_is_mine_and_does_not_mark_it_read(self):
        n = item(self.me, 1)
        res = self.client.get(f"{BASE}/{n.pk}")
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.json()["title"], "Item 1")
        n.refresh_from_db()
        self.assertIsNone(n.read_at)

    def test_someone_elses_or_a_bad_id_is_404(self):
        theirs = item(self.other, 1)
        for path in (f"{BASE}/{theirs.pk}", f"{BASE}/not-a-uuid", f"{BASE}/{theirs.pk}/read"):
            method = self.client.post if path.endswith("/read") else self.client.get
            res = method(path)
            self.assertEqual(res.status_code, 404, path)
            self.assertEqual(res.json()["code"], "not_found")

    def test_read_marks_it_once(self):
        n = item(self.me, 1)

        first = self.client.post(f"{BASE}/{n.pk}/read")
        second = self.client.post(f"{BASE}/{n.pk}/read")

        self.assertEqual(first.status_code, 200)
        self.assertTrue(first.json()["is_read"])
        self.assertEqual(second.json()["read_at"], first.json()["read_at"])

    # All read, and the counts.

    def test_read_all(self):
        item(self.me, 1, "orders")
        item(self.me, 2, "payments", "payment.completed")
        item(self.other, 3)

        res = self.client.post(f"{BASE}/read-all", {}, format="json")

        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.json()["updated"], 2)
        self.assertEqual(res.json()["unread"]["total"], 0)
        self.assertTrue(Notification.objects.filter(account=self.other, read_at__isnull=True).exists())

    def test_read_all_one_tab(self):
        item(self.me, 1, "orders")
        item(self.me, 2, "payments", "payment.completed")

        res = self.client.post(f"{BASE}/read-all", {"category": "orders"}, format="json")

        self.assertEqual(res.json()["updated"], 1)
        self.assertEqual(res.json()["unread"], {"total": 1, "orders": 0, "updates": 0, "payments": 1, "promotions": 0})

    def test_read_all_leaves_what_arrived_after_the_screen(self):
        shown = item(self.me, 5)
        item(self.me, 1)  # arrived after the screen was drawn

        res = self.client.post(f"{BASE}/read-all", {"before": shown.created_at.isoformat()}, format="json")

        self.assertEqual(res.json()["updated"], 1)
        self.assertEqual(res.json()["unread"]["total"], 1)

    def test_read_all_bad_values_are_422(self):
        for body in ({"category": "shoes"}, {"before": "yesterday"}):
            self.assertEqual(self.client.post(f"{BASE}/read-all", body, format="json").status_code, 422)

    def test_read_all_is_rate_limited(self):
        for _ in range(ratelimit.READ_ALL_PER_ACCOUNT_MINUTE):
            self.assertEqual(self.client.post(f"{BASE}/read-all", {}, format="json").status_code, 200)
        res = self.client.post(f"{BASE}/read-all", {}, format="json")
        self.assertEqual(res.status_code, 429)
        self.assertEqual(res.json()["code"], "rate_limited")

    def test_unread_count(self):
        item(self.me, 1, "orders")
        item(self.me, 2, "orders")
        item(self.me, 3, "updates", "booking.reminder")
        item(self.me, 4, "payments", "payment.completed", read_at=timezone.now())
        item(self.me, 5, "promotions", "promo.offer", expires_at=timezone.now() - timedelta(seconds=1))
        item(self.other, 6)

        res = self.client.get(f"{BASE}/unread-count")

        self.assertEqual(res.json(), {"total": 3, "orders": 2, "updates": 1, "payments": 0, "promotions": 0})

    # Around it.

    def test_preferences_still_answer_beside_it(self):
        self.assertEqual(self.client.get(f"{BASE}/preferences").status_code, 200)

    def test_signed_out_is_401(self):
        self.client.force_authenticate(None)
        for path in (BASE, f"{BASE}/unread-count"):
            res = self.client.get(path)
            self.assertEqual(res.status_code, 401)
            self.assertEqual(res.json()["code"], "not_authenticated")


@override_settings(NOTIFICATION_CENTER_V1=False)
class SwitchedOffTests(APITestCase):
    def setUp(self):
        self.me = get_user_model().objects.create_user(phone="+971500000501", password="x")
        self.client.force_authenticate(self.me)

    def test_every_route_is_404(self):
        n = item(self.me, 1)
        for method, path in (
            ("get", BASE),
            ("get", f"{BASE}/unread-count"),
            ("get", f"{BASE}/{n.pk}"),
            ("post", f"{BASE}/{n.pk}/read"),
            ("post", f"{BASE}/read-all"),
        ):
            self.assertEqual(getattr(self.client, method)(path).status_code, 404, path)
        n.refresh_from_db()
        self.assertIsNone(n.read_at)

    def test_preferences_are_untouched_by_the_switch(self):
        self.assertEqual(self.client.get(f"{BASE}/preferences").status_code, 200)
