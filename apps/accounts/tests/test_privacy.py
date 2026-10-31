import uuid
from unittest import mock

from django.contrib.auth import get_user_model
from django.db import DatabaseError
from django.test import SimpleTestCase, TestCase, override_settings
from rest_framework.test import APITestCase

from apps.accounts.privacy import name_for_salons, short_name
from apps.salons import customer_sync
from apps.salons.group_views import SELF, REGISTERED, _name_members

URL = "/api/v1/me/privacy"
REFRESH = "apps.accounts.privacy_views.refresh_salon_name"


class ShortNameTests(SimpleTestCase):
    def test_first_name_and_the_last_names_initial(self):
        self.assertEqual(short_name("Sara Ahmed"), "Sara A.")
        self.assertEqual(short_name("sara bin ahmed"), "sara A.")
        self.assertEqual(short_name("  Sara   Ahmed  "), "Sara A.")

    def test_one_word_or_none_stays_as_it_is(self):
        self.assertEqual(short_name("Sara"), "Sara")
        self.assertEqual(short_name(""), "")
        self.assertEqual(short_name(None), "")

    def test_shown_is_the_whole_name(self):
        self.assertEqual(name_for_salons("Sara  Ahmed", True), "Sara Ahmed")
        self.assertEqual(name_for_salons("Sara Ahmed", False), "Sara A.")


@override_settings(PRIVACY_SETTINGS_V1=True)
class PrivacyApiTests(APITestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user(
            phone="+971500001001", password="x", full_name="Sara Ahmed"
        )
        self.client.force_authenticate(self.user)

    def patch(self, body):
        return self.client.patch(URL, body, format="json")

    def test_get_answers_the_defaults(self):
        res = self.client.get(URL)
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.json(), {"show_name_to_salons": True, "show_photo_in_moments": False})

    def test_name_off_is_kept_and_the_salons_record_shortened(self):
        with mock.patch(REFRESH, return_value=1) as refresh:
            res = self.patch({"show_name_to_salons": False})

        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.json()["show_name_to_salons"], False)
        self.user.refresh_from_db()
        self.assertFalse(self.user.show_name_to_salons)
        refresh.assert_called_once()
        self.assertFalse(refresh.call_args.args[0].show_name_to_salons)

    def test_the_photo_alone_does_not_touch_the_salons_record(self):
        with mock.patch(REFRESH) as refresh:
            res = self.patch({"show_photo_in_moments": True})
        self.assertEqual(res.json(), {"show_name_to_salons": True, "show_photo_in_moments": True})
        refresh.assert_not_called()

    def test_the_same_value_again_changes_nothing(self):
        with mock.patch(REFRESH) as refresh:
            res = self.patch({"show_name_to_salons": True})
        self.assertEqual(res.status_code, 200)
        refresh.assert_not_called()

    def test_not_a_boolean_or_a_typo_is_422_and_saves_nothing(self):
        with mock.patch(REFRESH) as refresh:
            for body, code in (
                ({"show_name_to_salons": "false"}, "invalid"),
                ({"show_name_to_salon": False}, "unknown_field"),
            ):
                res = self.patch(body)
                self.assertEqual(res.status_code, 422, body)
                self.assertEqual(res.json()["errors"][0]["code"], code)
        refresh.assert_not_called()
        self.user.refresh_from_db()
        self.assertTrue(self.user.show_name_to_salons)

    def test_the_salons_record_failing_saves_nothing(self):
        with mock.patch(REFRESH, side_effect=DatabaseError("platform db down")):
            res = self.patch({"show_name_to_salons": False, "show_photo_in_moments": True})
        self.assertEqual(res.status_code, 503)
        self.assertEqual(res.json()["code"], "service_unavailable")
        self.user.refresh_from_db()
        self.assertTrue(self.user.show_name_to_salons)
        self.assertFalse(self.user.show_photo_in_moments)

    def test_signed_out_is_401(self):
        self.client.force_authenticate(None)
        self.assertEqual(self.client.get(URL).status_code, 401)


@override_settings(PRIVACY_SETTINGS_V1=False)
class PrivacySwitchedOffTests(APITestCase):
    def test_both_are_404(self):
        user = get_user_model().objects.create_user(phone="+971500001002", password="x")
        self.client.force_authenticate(user)
        self.assertEqual(self.client.get(URL).status_code, 404)
        self.assertEqual(self.client.patch(URL, {"show_name_to_salons": False}, format="json").status_code, 404)
        user.refresh_from_db()
        self.assertTrue(user.show_name_to_salons)


class SalonRecordTests(SimpleTestCase):
    def account(self, show):
        return mock.Mock(
            pk=uuid.UUID(int=5), full_name="Sara Ahmed", show_name_to_salons=show
        )

    def test_a_new_record_takes_the_name_the_switch_allows(self):
        self.assertEqual(customer_sync._name_of(self.account(True)), ("Sara", "Ahmed"))
        self.assertEqual(customer_sync._name_of(self.account(False)), ("Sara", "A."))

    def test_refresh_writes_only_the_row_this_app_made(self):
        cursor = mock.MagicMock(rowcount=1)
        with mock.patch.object(customer_sync, "connection") as conn:
            conn.cursor.return_value.__enter__.return_value = cursor
            self.assertEqual(customer_sync.refresh_salon_name(self.account(False)), 1)
        sql, params = cursor.execute.call_args.args
        self.assertIn("WHERE id = %(id)s::uuid", sql)
        self.assertEqual(
            params, {"id": uuid.UUID(int=5), "first_name": "Sara", "last_name": "A."}
        )


class GroupLaneNameTests(TestCase):
    def setUp(self):
        users = get_user_model().objects
        self.booker = users.create_user(
            phone="+971500001003", password="x", full_name="Sara Ahmed", show_name_to_salons=False
        )
        self.member = users.create_user(
            phone="+971500001004", password="x", full_name="Omar Saleh", account_verified=True,
            show_name_to_salons=False,
        )
        self.shown = users.create_user(
            phone="+971500001005", password="x", full_name="Nour Khalil", account_verified=True,
        )

    def test_lanes_carry_the_names_each_account_allows(self):
        members = [
            {"kind": SELF, "name": "typed"},
            {"kind": REGISTERED, "id": str(self.member.pk), "name": "typed"},
            {"kind": REGISTERED, "id": str(self.shown.pk), "name": "typed"},
        ]
        _name_members(self.booker, members)
        self.assertEqual([m["name"] for m in members], ["Sara A.", "Omar S.", "Nour Khalil"])


class GrpcContactNameTests(SimpleTestCase):
    @override_settings(INTERNAL_GRPC_KEY="k-123")
    def test_the_check_in_desk_gets_the_short_name(self):
        from apps.grpc_gen.servicer import ConsumerDirectoryServicer

        account = mock.Mock(
            id=uuid.UUID(int=6),
            email="",
            email_verified_at=None,
            full_name="Sara Ahmed",
            language="en",
            show_name_to_salons=False,
        )
        context = mock.Mock()
        context.invocation_metadata.return_value = [("x-internal-key", "k-123")]
        with mock.patch("apps.grpc_gen.servicer.ConsumerAccount") as accounts, mock.patch(
            "apps.grpc_gen.servicer.NotificationPreference"
        ) as prefs, mock.patch("apps.grpc_gen.servicer.close_old_connections"):
            accounts.objects.filter.return_value.first.return_value = account
            prefs.objects.filter.return_value.first.return_value = None
            reply = ConsumerDirectoryServicer().GetConsumerContact(
                mock.Mock(consumer_id=str(account.id)), context
            )
        self.assertEqual(reply.full_name, "Sara A.")


class PurgeTests(TestCase):
    def test_purge_puts_the_settings_back_to_their_defaults(self):
        user = get_user_model().objects.create_user(
            phone="+971500001006", password="x", show_name_to_salons=False, show_photo_in_moments=True
        )
        user.purge()
        user.refresh_from_db()
        self.assertTrue(user.show_name_to_salons)
        self.assertFalse(user.show_photo_in_moments)
