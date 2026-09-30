"""
POST /api/v1/favourite, the heart (docs/EXPERT_PROFILE_AUDIT.md section 4 and
steps E3a, E3b).

The favourite table is our own (the `consumer` schema, apps/accounts), so
these tests use the real test database and a real token: what is saved is
read back from the table.

E3a: the salon heart. Its answers as they were, pinned first (save, unsave,
no id, no token), then the two fixes: an id that is not a UUID is a 422, not a
500 (audit F3), and a double tap never answers 500 (audit F4).
"""

import uuid
from unittest import mock

from django.db.models.query import QuerySet
from django.test import TestCase
from rest_framework.test import APIClient

from apps.accounts.models import ConsumerAccount, Favourite
from apps.accounts.services import tokens_for

URL = "/api/v1/favourite"

SALON = uuid.UUID("33333333-3333-3333-3333-333333333333")
OTHER_SALON = uuid.UUID("33333333-3333-3333-3333-3333333333ff")


class FavouriteTestCase(TestCase):
    def setUp(self):
        self.client = APIClient()
        self.customer = ConsumerAccount.objects.create(phone="+971500000001")
        self.sign_in(self.customer)

    def sign_in(self, account):
        token = tokens_for(account)["access"]
        self.client.credentials(HTTP_AUTHORIZATION=f"Bearer {token}")

    def sign_out(self):
        self.client.credentials()

    def tap(self, body=None, **fields):
        """POST the heart. `body` sends that JSON as it is."""
        return self.client.post(URL, fields if body is None else body, format="json")

    def saved(self, account=None):
        """The salons this customer has saved, as the table holds them."""
        rows = Favourite.objects.filter(account=account or self.customer)
        return sorted(rows.values_list("storefront_id", flat=True))

    def assert_field_error(self, response, field, code, message):
        self.assertEqual(response.status_code, 422)
        self.assertEqual(response.json(), {
            "detail": "Please correct the highlighted fields.",
            "code": "validation_error",
            "errors": [{"field": field, "code": code, "message": message}],
        })


class SalonHeartAsItWasTests(FavouriteTestCase):
    """Pinned BEFORE the E3a fixes: they must not change any of these."""

    def test_the_first_tap_saves(self):
        response = self.tap(salon_id=str(SALON))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"is_favorite": True})
        self.assertEqual(self.saved(), [SALON])

    def test_the_second_tap_unsaves(self):
        self.tap(salon_id=str(SALON))
        response = self.tap(salon_id=str(SALON))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"is_favorite": False})
        self.assertEqual(self.saved(), [])

    def test_a_third_tap_saves_again(self):
        for _ in range(2):
            self.tap(salon_id=str(SALON))
        self.assertEqual(self.tap(salon_id=str(SALON)).json(), {"is_favorite": True})
        self.assertEqual(self.saved(), [SALON])

    def test_each_salon_has_its_own_heart(self):
        self.tap(salon_id=str(SALON))
        self.tap(salon_id=str(OTHER_SALON))
        self.assertEqual(self.saved(), [SALON, OTHER_SALON])
        self.tap(salon_id=str(SALON))
        self.assertEqual(self.saved(), [OTHER_SALON])

    def test_each_customer_has_their_own_hearts(self):
        self.tap(salon_id=str(SALON))
        other = ConsumerAccount.objects.create(phone="+971500000002")
        self.sign_in(other)
        # Not "unsave": this customer never saved it.
        self.assertEqual(self.tap(salon_id=str(SALON)).json(), {"is_favorite": True})
        self.assertEqual(self.saved(), [SALON])
        self.assertEqual(self.saved(other), [SALON])

    def test_the_salon_is_not_looked_up(self):
        # On purpose (the view says why): any well-formed id saves.
        nobody = uuid.uuid4()
        self.assertEqual(self.tap(salon_id=str(nobody)).json(), {"is_favorite": True})
        self.assertEqual(self.saved(), [nobody])

    def test_an_uppercase_id_is_the_same_heart(self):
        self.tap(salon_id=str(SALON))
        self.assertEqual(self.tap(salon_id=str(SALON).upper()).json(), {"is_favorite": False})
        self.assertEqual(self.saved(), [])

    def test_the_other_uuid_spellings_are_the_same_heart(self):
        # What the table's UUID column has always read: no dashes, braces, a
        # URN. The E3a check must keep taking them.
        for spelling in (SALON.hex, "{%s}" % SALON, "urn:uuid:%s" % SALON):
            with self.subTest(spelling=spelling):
                self.assertEqual(self.tap(salon_id=spelling).json(), {"is_favorite": True})
                self.assertEqual(self.saved(), [SALON])
                self.assertEqual(self.tap(salon_id=str(SALON)).json(), {"is_favorite": False})

    def test_no_id_is_422(self):
        for body in ({}, {"salon_id": ""}, {"salon_id": None}):
            with self.subTest(body=body):
                self.assert_field_error(
                    self.tap(body), "salon_id", "invalid", "This field is required.",
                )
        self.assertEqual(self.saved(), [])

    def test_no_token_is_401_and_nothing_is_saved(self):
        self.sign_out()
        response = self.tap(salon_id=str(SALON))
        self.assertEqual(response.status_code, 401)
        self.assertEqual(response.json(), {
            "detail": "Authentication credentials were not provided.",
            "code": "not_authenticated",
            "errors": [{
                "field": None,
                "code": "not_authenticated",
                "message": "Authentication credentials were not provided.",
            }],
        })
        self.assertEqual(Favourite.objects.count(), 0)

    def test_a_bad_token_is_401(self):
        self.client.credentials(HTTP_AUTHORIZATION="Bearer not.a.token")
        response = self.tap(salon_id=str(SALON))
        self.assertEqual(response.status_code, 401)
        self.assertEqual(response.json()["code"], "not_authenticated")
        self.assertEqual(Favourite.objects.count(), 0)


class SalonHeartBadIdTests(FavouriteTestCase):
    """
    E3a, audit F3: an id that is not a UUID is a 422 on `salon_id`. It used
    to be a 500: the table's UUID column raised Django's own ValidationError,
    which the API's error handler does not know.
    """

    def setUp(self):
        super().setUp()
        # A 500 comes back as a response, so a test can say "not 500".
        self.client.raise_request_exception = False

    def assert_not_a_uuid(self, response):
        self.assert_field_error(response, "salon_id", "invalid", "Must be a valid UUID.")
        self.assertEqual(Favourite.objects.count(), 0)

    def test_text_that_is_not_a_uuid(self):
        for value in ("not-a-uuid", "green-wave", "123", str(SALON)[:-1], str(SALON) + "0"):
            with self.subTest(value=value):
                self.assert_not_a_uuid(self.tap(salon_id=value))

    def test_a_list_or_an_object_is_not_a_uuid(self):
        for value in ([str(SALON)], {"id": str(SALON)}):
            with self.subTest(value=value):
                self.assert_not_a_uuid(self.tap(salon_id=value))

    def test_a_number_is_not_a_uuid(self):
        # It used to SAVE: the column read 123 as the UUID numbered 123.
        for value in (123, 1.5, True):
            with self.subTest(value=value):
                self.assert_not_a_uuid(self.tap(salon_id=value))

    def test_a_body_that_is_not_an_object_has_no_id(self):
        # A JSON list or a bare string has no `salon_id` to read. Was a 500.
        for body in ([str(SALON)], "salon"):
            with self.subTest(body=body):
                self.assert_field_error(
                    self.tap(body), "salon_id", "invalid", "This field is required.",
                )
        self.assertEqual(Favourite.objects.count(), 0)

    def test_a_good_id_after_a_bad_one_still_saves(self):
        self.tap(salon_id="not-a-uuid")
        self.assertEqual(self.tap(salon_id=str(SALON)).json(), {"is_favorite": True})
        self.assertEqual(self.saved(), [SALON])


class SalonHeartDoubleTapTests(FavouriteTestCase):
    """
    E3a, audit F4: two taps at once. Both find nothing to unsave, both save,
    and the second save used to hit the table's unique rule: a 500. Now it
    answers what is true, the salon is saved.
    """

    def setUp(self):
        super().setUp()
        self.client.raise_request_exception = False

    def second_tap(self):
        """
        The slower of two taps: the other one's row is already in the table,
        but this one's unsave ran before it landed and found nothing.
        """
        Favourite.objects.create(account=self.customer, storefront_id=SALON)
        with mock.patch.object(QuerySet, "delete", return_value=(0, {})):
            return self.tap(salon_id=str(SALON))

    def test_the_second_save_answers_saved_not_500(self):
        response = self.second_tap()
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"is_favorite": True})

    def test_the_salon_is_saved_once(self):
        self.second_tap()
        self.assertEqual(self.saved(), [SALON])

    def test_the_next_tap_unsaves_as_usual(self):
        self.second_tap()
        self.assertEqual(self.tap(salon_id=str(SALON)).json(), {"is_favorite": False})
        self.assertEqual(self.saved(), [])
