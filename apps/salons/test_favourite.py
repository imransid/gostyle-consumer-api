"""
POST /api/v1/favourite, the heart (docs/EXPERT_PROFILE_AUDIT.md section 4 and
steps E3a, E3b).

The favourite table is our own (the `consumer` schema, apps/accounts), so
these tests use the real test database and a real token: what is saved is
read back from the table.

E3a: the salon heart. Its answers as they were, pinned first (save, unsave,
no id, no token), then the two fixes: an id that is not a UUID is a 422, not a
500 (audit F3), and a double tap never answers 500 (audit F4).

E3b: the stylist heart, `{"stylist_id": "..."}` on the same route, saved in
its own table (favourite_stylist), and `is_favorite` on the expert profile.
Whether a stylist is one the app can show is read from the platform's tables,
which the test database does not have, so that one lookup is mocked.
"""

import importlib
import types
import uuid
from contextlib import ExitStack
from unittest import mock

from django.db import IntegrityError, migrations, transaction
from django.db.models.query import QuerySet
from django.test import SimpleTestCase, TestCase
from drf_spectacular.generators import SchemaGenerator
from rest_framework.test import APIClient

from apps.accounts.models import ConsumerAccount, Favourite, FavouriteStylist
from apps.accounts.services import tokens_for
from apps.salons import views
from apps.salons.test_expert_profile import mock_platform_reads

URL = "/api/v1/favourite"

SALON = uuid.UUID("33333333-3333-3333-3333-333333333333")
OTHER_SALON = uuid.UUID("33333333-3333-3333-3333-3333333333ff")
TENANT = uuid.UUID("11111111-1111-1111-1111-111111111111")
BRANCH = uuid.UUID("22222222-2222-2222-2222-222222222222")
STYLIST = uuid.UUID("99999999-9999-9999-9999-999999999990")
OTHER_STYLIST = uuid.UUID("99999999-9999-9999-9999-999999999991")

GONE = "This stylist is no longer available."


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


# ---------------------------------------------------------------------------
# E3b: the stylist heart
# ---------------------------------------------------------------------------

class StylistHeartTestCase(FavouriteTestCase):
    """
    The heart on a stylist. `showable` stands in for the one platform read
    (is this a stylist the app can show?): yes, unless a test says otherwise.
    """

    def setUp(self):
        super().setUp()
        self.client.raise_request_exception = False
        patcher = mock.patch.object(views, "stylist_is_showable", return_value=True)
        self.showable = patcher.start()
        self.addCleanup(patcher.stop)

    def saved_stylists(self, account=None):
        rows = FavouriteStylist.objects.filter(account=account or self.customer)
        return sorted(rows.values_list("staff_id", flat=True))


class StylistHeartTests(StylistHeartTestCase):
    """Their section 5: the same toggle, the same answer."""

    def test_the_first_tap_saves(self):
        response = self.tap(stylist_id=str(STYLIST))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"is_favorite": True})
        self.assertEqual(self.saved_stylists(), [STYLIST])

    def test_the_second_tap_unsaves(self):
        self.tap(stylist_id=str(STYLIST))
        response = self.tap(stylist_id=str(STYLIST))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"is_favorite": False})
        self.assertEqual(self.saved_stylists(), [])

    def test_a_third_tap_saves_again(self):
        for _ in range(2):
            self.tap(stylist_id=str(STYLIST))
        self.assertEqual(self.tap(stylist_id=str(STYLIST)).json(), {"is_favorite": True})
        self.assertEqual(self.saved_stylists(), [STYLIST])

    def test_each_stylist_has_their_own_heart(self):
        self.tap(stylist_id=str(STYLIST))
        self.tap(stylist_id=str(OTHER_STYLIST))
        self.assertEqual(self.saved_stylists(), [STYLIST, OTHER_STYLIST])
        self.tap(stylist_id=str(STYLIST))
        self.assertEqual(self.saved_stylists(), [OTHER_STYLIST])

    def test_each_customer_has_their_own_hearts(self):
        self.tap(stylist_id=str(STYLIST))
        other = ConsumerAccount.objects.create(phone="+971500000002")
        self.sign_in(other)
        # Not "unsave": this customer never saved them.
        self.assertEqual(self.tap(stylist_id=str(STYLIST)).json(), {"is_favorite": True})
        self.assertEqual(self.saved_stylists(), [STYLIST])
        self.assertEqual(self.saved_stylists(other), [STYLIST])

    def test_every_uuid_spelling_is_the_same_heart(self):
        for spelling in (str(STYLIST).upper(), STYLIST.hex, "{%s}" % STYLIST):
            with self.subTest(spelling=spelling):
                self.assertEqual(self.tap(stylist_id=spelling).json(), {"is_favorite": True})
                self.assertEqual(self.saved_stylists(), [STYLIST])
                self.assertEqual(self.tap(stylist_id=str(STYLIST)).json(), {"is_favorite": False})

    def test_a_stylist_heart_is_not_a_salon_heart(self):
        # Two tables. The same id under the other key is another heart.
        self.tap(stylist_id=str(STYLIST))
        self.assertEqual(self.saved(), [])
        self.assertEqual(self.tap(salon_id=str(STYLIST)).json(), {"is_favorite": True})
        self.assertEqual(self.saved(), [STYLIST])
        self.assertEqual(self.saved_stylists(), [STYLIST])
        self.assertEqual(self.tap(salon_id=str(STYLIST)).json(), {"is_favorite": False})
        self.assertEqual(self.saved_stylists(), [STYLIST])

    def test_an_empty_salon_id_beside_a_stylist_id_is_the_stylist_heart(self):
        response = self.tap({"salon_id": "", "stylist_id": str(STYLIST)})
        self.assertEqual(response.json(), {"is_favorite": True})
        self.assertEqual(self.saved_stylists(), [STYLIST])

    def test_no_token_is_401_and_nothing_is_saved(self):
        self.sign_out()
        response = self.tap(stylist_id=str(STYLIST))
        self.assertEqual(response.status_code, 401)
        self.assertEqual(response.json()["code"], "not_authenticated")
        self.assertEqual(FavouriteStylist.objects.count(), 0)
        self.showable.assert_not_called()


class StylistHeartBodyTests(StylistHeartTestCase):
    """Exactly one of `salon_id` and `stylist_id`, and a real UUID."""

    def assert_nothing_saved(self):
        self.assertEqual(Favourite.objects.count(), 0)
        self.assertEqual(FavouriteStylist.objects.count(), 0)

    def test_both_ids_is_422(self):
        response = self.tap(salon_id=str(SALON), stylist_id=str(STYLIST))
        self.assertEqual(response.status_code, 422)
        self.assertEqual(response.json(), {
            "detail": "Send salon_id or stylist_id, not both.",
            "code": "validation_error",
            "errors": [{
                "field": None,
                "code": "one_id_only",
                "message": "Send salon_id or stylist_id, not both.",
            }],
        })
        self.assert_nothing_saved()
        self.showable.assert_not_called()

    def test_neither_id_keeps_the_old_answer(self):
        for body in ({}, {"stylist_id": ""}, {"stylist_id": None}, {"salon_id": "", "stylist_id": ""}):
            with self.subTest(body=body):
                self.assert_field_error(
                    self.tap(body), "salon_id", "invalid", "This field is required.",
                )
        self.assert_nothing_saved()

    def test_a_stylist_id_that_is_not_a_uuid_is_422(self):
        for value in ("not-a-uuid", "darius", "123", str(STYLIST)[:-1],
                      [str(STYLIST)], {"id": str(STYLIST)}, 123, 1.5, True):
            with self.subTest(value=value):
                self.assert_field_error(
                    self.tap(stylist_id=value), "stylist_id", "invalid", "Must be a valid UUID.",
                )
        self.assert_nothing_saved()
        self.showable.assert_not_called()


class StylistHeartGoneTests(StylistHeartTestCase):
    """
    Saving looks the stylist up: a heart on someone the app cannot show could
    never be seen again. Unsaving never does: a heart on a stylist who has
    left since must still come off.
    """

    def test_saving_a_stylist_the_app_cannot_show_is_404(self):
        self.showable.return_value = False
        response = self.tap(stylist_id=str(STYLIST))
        self.assertEqual(response.status_code, 404)
        self.assertEqual(response.json(), {
            "detail": GONE,
            "code": "not_found",
            "errors": [{"field": None, "code": "not_found", "message": GONE}],
        })
        self.assertEqual(self.saved_stylists(), [])

    def test_the_lookup_gets_the_parsed_id_once(self):
        self.tap(stylist_id=str(STYLIST).upper())
        self.showable.assert_called_once_with(STYLIST)

    def test_unsaving_never_looks_the_stylist_up(self):
        self.tap(stylist_id=str(STYLIST))                  # saved while they were here
        self.showable.reset_mock()
        self.showable.return_value = False                 # they left
        response = self.tap(stylist_id=str(STYLIST))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"is_favorite": False})
        self.assertEqual(self.saved_stylists(), [])
        self.showable.assert_not_called()

    def test_once_unsaved_a_stylist_who_left_cannot_be_saved_again(self):
        self.tap(stylist_id=str(STYLIST))
        self.showable.return_value = False
        self.tap(stylist_id=str(STYLIST))
        self.assertEqual(self.tap(stylist_id=str(STYLIST)).status_code, 404)
        self.assertEqual(self.saved_stylists(), [])

    def test_the_salon_heart_never_looks_a_stylist_up(self):
        self.tap(salon_id=str(SALON))
        self.tap(salon_id=str(SALON))
        self.showable.assert_not_called()


class StylistHeartDoubleTapTests(StylistHeartTestCase):
    """get_or_create from the start: two taps at once never answer 500."""

    def second_tap(self):
        """The slower of two taps: see SalonHeartDoubleTapTests.second_tap."""
        FavouriteStylist.objects.create(account=self.customer, staff_id=STYLIST)
        with mock.patch.object(QuerySet, "delete", return_value=(0, {})):
            return self.tap(stylist_id=str(STYLIST))

    def test_the_second_save_answers_saved_not_500(self):
        response = self.second_tap()
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"is_favorite": True})
        self.assertEqual(self.saved_stylists(), [STYLIST])

    def test_the_next_tap_unsaves_as_usual(self):
        self.second_tap()
        self.assertEqual(self.tap(stylist_id=str(STYLIST)).json(), {"is_favorite": False})
        self.assertEqual(self.saved_stylists(), [])


class FavouriteStylistTableTests(TestCase):
    """The table's own rules (migration accounts/0012)."""

    def setUp(self):
        self.customer = ConsumerAccount.objects.create(phone="+971500000001")

    def test_one_row_per_customer_and_stylist(self):
        FavouriteStylist.objects.create(account=self.customer, staff_id=STYLIST)
        with self.assertRaises(IntegrityError), transaction.atomic():
            FavouriteStylist.objects.create(account=self.customer, staff_id=STYLIST)

    def test_two_customers_can_save_the_same_stylist(self):
        other = ConsumerAccount.objects.create(phone="+971500000002")
        FavouriteStylist.objects.create(account=self.customer, staff_id=STYLIST)
        FavouriteStylist.objects.create(account=other, staff_id=STYLIST)
        self.assertEqual(FavouriteStylist.objects.count(), 2)

    def test_a_deleted_account_takes_its_hearts_with_it(self):
        FavouriteStylist.objects.create(account=self.customer, staff_id=STYLIST)
        self.customer.delete()
        self.assertEqual(FavouriteStylist.objects.count(), 0)

    def test_the_table_and_its_columns(self):
        self.assertEqual(FavouriteStylist._meta.db_table, "favourite_stylist")
        self.assertEqual(
            [f.column for f in FavouriteStylist._meta.fields],
            ["id", "account_id", "staff_id", "created_at"],
        )

    def test_the_migration_only_creates_the_table(self):
        # Additive: an older image that does not know the table never sees it.
        module = importlib.import_module("apps.accounts.migrations.0012_favourite_stylist")
        (operation,) = module.Migration.operations
        self.assertIsInstance(operation, migrations.CreateModel)
        self.assertEqual(operation.name, "FavouriteStylist")
        self.assertEqual(
            module.Migration.dependencies, [("accounts", "0011_backfill_account_verified")]
        )


class ProfileIsFavoriteTests(StylistHeartTestCase):
    """
    `is_favorite` on GET /salon/<id>/stylist/<id>: the customer's own heart,
    read from the real table with a real token. The salon, the stylist and
    the menu are platform rows, so those reads are mocked.
    """

    def profile(self, stylist_id=STYLIST):
        person = types.SimpleNamespace(
            id=stylist_id, tenant_id=TENANT, branch_id=BRANCH, first_name="Liam",
            last_name="Johnson", position="Senior Barber", job_title=None, avatar_url=None,
        )
        with ExitStack() as stack:
            # Every platform read mocked; the favourite table is the real one.
            mock_platform_reads(stack, person=person)
            response = self.client.get(f"/api/v1/salon/{SALON}/stylist/{stylist_id}")
        self.assertEqual(response.status_code, 200)
        return response.json()

    def test_false_for_a_guest(self):
        self.tap(stylist_id=str(STYLIST))          # someone saved them
        self.sign_out()
        self.assertIs(self.profile()["is_favorite"], False)

    def test_false_until_the_customer_saves_them(self):
        self.assertIs(self.profile()["is_favorite"], False)

    def test_true_after_the_heart_and_false_again_after_the_next_tap(self):
        self.tap(stylist_id=str(STYLIST))
        self.assertIs(self.profile()["is_favorite"], True)
        self.tap(stylist_id=str(STYLIST))
        self.assertIs(self.profile()["is_favorite"], False)

    def test_another_customer_does_not_see_this_heart(self):
        self.tap(stylist_id=str(STYLIST))
        self.sign_in(ConsumerAccount.objects.create(phone="+971500000002"))
        self.assertIs(self.profile()["is_favorite"], False)

    def test_a_heart_on_another_stylist_does_not_light_this_one(self):
        self.tap(stylist_id=str(OTHER_STYLIST))
        self.assertIs(self.profile()["is_favorite"], False)
        self.assertIs(self.profile(OTHER_STYLIST)["is_favorite"], True)

    def test_a_salon_heart_with_the_same_id_does_not_light_the_stylist(self):
        self.tap(salon_id=str(STYLIST))
        self.assertIs(self.profile()["is_favorite"], False)


class HeartOpenApiTests(SimpleTestCase):
    """POST /favourite in Swagger shows both bodies and every answer."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        schema = SchemaGenerator().get_schema(request=None, public=True)
        cls.operation = schema["paths"]["/api/v1/favourite"]["post"]
        cls.listing = schema["paths"]["/api/v1/favourite"]["get"]

    def body(self):
        return self.operation["requestBody"]["content"]["application/json"]

    def test_the_request_is_one_of_two_bodies(self):
        bodies = self.body()["schema"]["oneOf"]
        self.assertEqual(
            [(list(b["properties"]), b["required"]) for b in bodies],
            [(["salon_id"], ["salon_id"]), (["stylist_id"], ["stylist_id"])],
        )
        for one in bodies:
            (field,) = one["properties"].values()
            self.assertEqual(field, {"type": "string", "format": "uuid"})

    def test_there_is_an_example_of_each_body(self):
        values = [example["value"] for example in self.body()["examples"].values()]
        self.assertEqual([list(value) for value in values], [["salon_id"], ["stylist_id"]])

    def test_the_answers_are_200_401_404_and_422(self):
        self.assertEqual(set(self.operation["responses"]), {"200", "401", "404", "422"})

    def test_the_200_is_is_favorite(self):
        schema = self.operation["responses"]["200"]["content"]["application/json"]["schema"]
        self.assertEqual(schema["properties"], {"is_favorite": {"type": "boolean"}})

    def test_the_404_example_is_the_stylist_sentence(self):
        content = self.operation["responses"]["404"]["content"]["application/json"]
        (example,) = content["examples"].values()
        self.assertEqual(example["value"], {
            "detail": GONE,
            "code": "not_found",
            "errors": [{"field": None, "code": "not_found", "message": GONE}],
        })

    def test_a_token_is_needed(self):
        self.assertEqual(self.operation["security"], [{"jwtAuth": []}])

    def test_the_step_title_names_both(self):
        self.assertRegex(self.operation["summary"], r"a salon or a stylist")

    def test_the_list_of_saved_salons_is_untouched(self):
        self.assertEqual(
            [p["name"] for p in self.listing["parameters"]],
            ["category", "is_open_now", "is_top_rated", "page", "page_size", "search"],
        )
        self.assertRegex(self.listing["summary"], r"My saved salons")
