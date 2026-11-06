"""
PUT / DELETE /api/v1/salon/<salon_id>/follow.

The follow table is our own (consumer schema), so these tests use the real
test database and a real token. The platform's salon table is not in the test
database, so the one lookup "can the app show this salon?" is mocked.
"""

import uuid
from unittest import mock

from django.test import TestCase
from django.utils import timezone
from rest_framework.test import APIClient

from apps.accounts.models import ConsumerAccount, Favourite, SalonFollow
from apps.accounts.services import tokens_for

SALON = uuid.UUID("33333333-3333-3333-3333-333333333333")
OTHER_SALON = uuid.UUID("33333333-3333-3333-3333-3333333333ff")


def url(salon_id=SALON):
    return f"/api/v1/salon/{salon_id}/follow"


class FollowTestCase(TestCase):
    def setUp(self):
        self.client = APIClient()
        self.sara = ConsumerAccount.objects.create(phone="+971500000001")
        self.omar = ConsumerAccount.objects.create(phone="+971500000002")
        self.sign_in(self.sara)
        # "The app can show this salon": yes, for every test unless we say no.
        patcher = mock.patch(
            "apps.salons.follow_views.salon_profile", return_value=object()
        )
        self.salon_lookup = patcher.start()
        self.addCleanup(patcher.stop)

    def sign_in(self, account):
        token = tokens_for(account)["access"]
        self.client.credentials(HTTP_AUTHORIZATION=f"Bearer {token}")

    def rows(self, account=None, salon=SALON):
        return SalonFollow.objects.filter(
            account=account or self.sara, storefront_id=salon
        ).count()


class FollowTests(FollowTestCase):
    def test_guest_gets_401(self):
        self.client.credentials()
        self.assertEqual(self.client.put(url()).status_code, 401)
        self.assertEqual(self.client.delete(url()).status_code, 401)

    def test_put_follows_and_counts(self):
        response = self.client.put(url())
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"is_following": True, "followers_count": 1})
        self.assertEqual(self.rows(), 1)

    def test_put_twice_is_still_one_row(self):
        self.client.put(url())
        response = self.client.put(url())
        self.assertEqual(response.json(), {"is_following": True, "followers_count": 1})
        self.assertEqual(self.rows(), 1)

    def test_delete_unfollows(self):
        self.client.put(url())
        response = self.client.delete(url())
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"is_following": False, "followers_count": 0})
        self.assertEqual(self.rows(), 0)

    def test_delete_twice_is_fine(self):
        self.client.put(url())
        self.client.delete(url())
        self.assertEqual(self.client.delete(url()).status_code, 200)

    def test_bad_id_is_404(self):
        self.assertEqual(self.client.put("/api/v1/salon/abc/follow").status_code, 404)
        self.assertEqual(self.client.delete("/api/v1/salon/abc/follow").status_code, 404)

    def test_put_on_a_salon_the_app_cannot_show_is_404(self):
        self.salon_lookup.return_value = None
        response = self.client.put(url())
        self.assertEqual(response.status_code, 404)
        self.assertEqual(self.rows(), 0)

    def test_delete_works_even_if_the_salon_has_gone(self):
        self.client.put(url())
        self.salon_lookup.return_value = None  # the salon is gone now
        response = self.client.delete(url())
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.rows(), 0)

    def test_post_toggles_follow_and_unfollow(self):
        first = self.client.post(url())
        self.assertEqual(first.status_code, 200)
        self.assertEqual(first.json(), {"is_following": True, "followers_count": 1})
        self.assertEqual(self.rows(), 1)

        second = self.client.post(url())
        self.assertEqual(second.status_code, 200)
        self.assertEqual(second.json(), {"is_following": False, "followers_count": 0})
        self.assertEqual(self.rows(), 0)

    def test_post_guest_gets_401(self):
        self.client.credentials()
        self.assertEqual(self.client.post(url()).status_code, 401)

    def test_post_bad_id_or_missing_salon_is_404(self):
        self.assertEqual(self.client.post("/api/v1/salon/abc/follow").status_code, 404)
        self.salon_lookup.return_value = None
        self.assertEqual(self.client.post(url()).status_code, 404)
        self.assertEqual(self.rows(), 0)


class FollowCountTests(FollowTestCase):
    def test_two_customers_make_two_followers(self):
        self.client.put(url())
        self.sign_in(self.omar)
        response = self.client.put(url())
        self.assertEqual(response.json()["followers_count"], 2)

    def test_other_salons_are_not_counted(self):
        self.client.put(url(OTHER_SALON))
        response = self.client.put(url())
        self.assertEqual(response.json()["followers_count"], 1)

    def test_account_waiting_for_deletion_is_not_counted(self):
        self.client.put(url())
        self.sign_in(self.omar)
        self.client.put(url())
        ConsumerAccount.objects.filter(pk=self.omar.pk).update(purge_at=timezone.now())
        self.sign_in(self.sara)
        response = self.client.put(url())
        self.assertEqual(response.json()["followers_count"], 1)


class FollowIsSeparateFromTheHeartTests(FollowTestCase):
    def test_following_does_not_make_a_heart(self):
        self.client.put(url())
        self.assertEqual(Favourite.objects.filter(account=self.sara).count(), 0)

    def test_a_heart_does_not_make_a_follow(self):
        self.client.post("/api/v1/favourite", {"salon_id": str(SALON)}, format="json")
        self.assertEqual(self.rows(), 0)