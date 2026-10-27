"""
2-step verification and the authenticator app (docs/TWO_FACTOR_API.md).
"""

from datetime import timedelta

from django.db import IntegrityError, connection, transaction
from django.test import TestCase
from django.utils import timezone

from apps.accounts.models import (
    ConsumerAccount,
    LoginChallenge,
    TwoFactorPurpose,
    UserAuthenticator,
    UserSession,
    VerificationCode,
    VerificationToken,
)

PHONE = "+971501234567"
TABLES = ("user_authenticators", "verification_codes", "verification_tokens", "login_challenges")


class StorageTests(TestCase):
    """The brief's section 8, as the database has it."""

    def setUp(self):
        self.account = ConsumerAccount.objects.create_user(phone=PHONE)

    def later(self, minutes=10):
        return timezone.now() + timedelta(minutes=minutes)

    def make_rows(self):
        session = UserSession.objects.create(user=self.account, refresh_token_hash="h")
        UserAuthenticator.objects.create(
            user=self.account, secret_encrypted=b"x", status=UserAuthenticator.Status.PENDING
        )
        VerificationCode.objects.create(
            user=self.account,
            method="email",
            purpose=TwoFactorPurpose.TWO_STEP_ON,
            code_hash="h",
            expires_at=self.later(),
        )
        VerificationToken.objects.create(
            token_hash="t",
            user=self.account,
            session_id=session.pk,
            purpose=TwoFactorPurpose.TWO_STEP_ON,
            expires_at=self.later(5),
        )
        LoginChallenge.objects.create(token_hash="c", user=self.account, expires_at=self.later())

    def column_default(self, table, column):
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT column_default FROM information_schema.columns"
                " WHERE table_name = %s AND column_name = %s",
                [table, column],
            )
            return cursor.fetchone()[0]

    def test_2_step_is_off_for_a_new_account_and_in_the_table(self):
        self.assertFalse(self.account.two_factor_enabled)
        self.assertIsNone(self.account.two_factor_changed_at)
        # Also for a row written by code from before the column.
        self.assertEqual(self.column_default("consumer_account", "two_factor_enabled"), "false")

    def test_a_session_has_not_passed_2_step_until_it_does(self):
        session = UserSession.objects.create(user=self.account, refresh_token_hash="h")
        session.refresh_from_db()
        self.assertIsNone(session.two_factor_passed_at)

    def test_the_counters_start_at_zero_in_the_table(self):
        for table in ("verification_codes", "login_challenges"):
            self.assertEqual(self.column_default(table, "attempts"), "0")

    def test_the_tables_refuse_what_the_brief_refuses(self):
        self.make_rows()
        refused = (
            (UserAuthenticator, {"status": "gone"}),
            (VerificationCode, {"method": "authenticator"}),
        )
        for model, change in refused:
            with self.subTest(model=model.__name__), self.assertRaises(IntegrityError):
                with transaction.atomic():
                    model.objects.update(**change)

    def test_one_authenticator_per_account(self):
        self.make_rows()
        with self.assertRaises(IntegrityError), transaction.atomic():
            UserAuthenticator.objects.create(
                user=self.account, secret_encrypted=b"y", status=UserAuthenticator.Status.ACTIVE
            )

    def test_deleting_the_account_row_deletes_its_rows_in_the_database(self):
        self.make_rows()
        with connection.cursor() as cursor:
            cursor.execute("DELETE FROM consumer_account WHERE id = %s", [self.account.pk])
        for model in (UserAuthenticator, VerificationCode, VerificationToken, LoginChallenge):
            with self.subTest(model=model.__name__):
                self.assertFalse(model.objects.exists())

    def test_each_table_references_the_account_on_delete_cascade(self):
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT conrelid::regclass::text, confdeltype FROM pg_constraint"
                " WHERE contype = 'f' AND conrelid::regclass::text = ANY(%s)",
                [list(TABLES)],
            )
            found = dict(cursor.fetchall())
        self.assertEqual(found, {table: "c" for table in TABLES})

    def test_token_hashes_have_only_their_primary_key_index(self):
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT indexname FROM pg_indexes"
                " WHERE tablename IN ('verification_tokens', 'login_challenges')"
                " AND indexdef LIKE '%%token_hash%%'"
            )
            names = sorted(row[0] for row in cursor.fetchall())
        self.assertEqual(names, ["login_challenges_pkey", "verification_tokens_pkey"])
