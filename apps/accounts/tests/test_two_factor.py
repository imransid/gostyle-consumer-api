"""
2-step verification and the authenticator app (apps/accounts/two_factor.py,
docs/TWO_FACTOR_API.md).

Codes never leave the process: FakeSender keeps what would have been sent.
The authenticator's tests stop the clock, so a code made in the test is
checked in the same 30-second step.
"""

import base64
import hashlib
import re
import uuid
from datetime import timedelta
from unittest.mock import patch

import pyotp
from django.core import mail
from django.core.cache import cache
from django.db import IntegrityError, connection, transaction
from django.test import RequestFactory, SimpleTestCase, TestCase, override_settings
from django.utils import timezone
from rest_framework.exceptions import ErrorDetail, ValidationError

from apps.accounts import sessions, two_factor
from apps.accounts.exceptions import api_exception_handler
from apps.accounts.notifications import EmailSender
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
EMAIL = "kevin.new@gmail.com"
IP = "94.200.14.31"
KEY = base64.b64encode(bytes(range(32))).decode()
ON = TwoFactorPurpose.TWO_STEP_ON
OFF = TwoFactorPurpose.TWO_STEP_OFF
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


class FakeSender:
    def __init__(self, outbox, extras):
        self.outbox, self.extras = outbox, extras

    def send(self, destination, code, destination_type, **extra):
        self.outbox.append((destination, code, destination_type))
        self.extras.append(extra)


def error_of(caught):
    """(field, code) of a raised field error."""
    ((field, details),) = caught.exception.detail.items()
    return field, details[0].code


class TwoFactorTestCase(TestCase):
    def setUp(self):
        cache.clear()
        self.addCleanup(cache.clear)
        self.account = self.make_account(PHONE, EMAIL)
        self.sid = uuid.uuid4()
        self.outbox, self.extras = [], []
        patcher = patch.object(
            two_factor,
            "get_sender",
            side_effect=lambda method: FakeSender(self.outbox, self.extras),
        )
        patcher.start()
        self.addCleanup(patcher.stop)

    def make_account(self, phone, email=None):
        account = ConsumerAccount.objects.create_user(phone=phone)
        account.email = email
        account.phone_verified_at = timezone.now()
        account.email_verified_at = timezone.now() if email else None
        account.save()
        return account

    def send(self, method="email", purpose=ON, account=None, ip=IP):
        with self.captureOnCommitCallbacks(execute=True):
            return two_factor.send_code(account or self.account, method, purpose, ip)

    def sent_code(self):
        return self.outbox[-1][1]

    def check(self, code, method="email", purpose=ON):
        two_factor.check_code(self.account, method, purpose, code)

    def wrong(self, code):
        """A code that is not `code`."""
        return f"{(int(code) + 1) % 1_000_000:06d}"


# --- errors -------------------------------------------------------------------


class ErrorShapeTests(SimpleTestCase):
    """Section 7, in the shape of auth-error-response.md section 4."""

    def answer(self, exc):
        response = api_exception_handler(exc, {})
        return response.status_code, response.data

    # Section 7 word for word, and our own two at the end.
    BRIEF = {
        "verification_required": (403, "Please verify it’s you first."),
        "already_added": (409, "Remove your current authenticator app first."),
        "no_setup": (409, "Start setting up your authenticator app again."),
        "too_many_attempts": (429, "Too many tries. Wait a few minutes and try again."),
        "challenge_expired": (401, "Your sign-in timed out. Please sign in again."),
        "app_update_required": (426, "Update the Go Style app to use 2-step verification."),
        "authenticator_unavailable": (
            503,
            "The authenticator app can’t be used right now. Please try again later.",
        ),
    }

    def test_errors_of_no_field_have_their_code_at_the_top_and_in_errors(self):
        classes = (
            two_factor.VerificationRequired,
            two_factor.AlreadyAdded,
            two_factor.NoSetup,
            two_factor.TooManyAttempts,
            two_factor.ChallengeExpired,
            two_factor.AppUpdateRequired,
            two_factor.AuthenticatorUnavailable,
        )
        self.assertEqual({c.default_code for c in classes}, set(self.BRIEF))
        for exc_class in classes:
            code = exc_class.default_code
            status_code, message = self.BRIEF[code]
            with self.subTest(code=code):
                self.assertEqual(
                    self.answer(exc_class()),
                    (
                        status_code,
                        {
                            "detail": message,
                            "code": code,
                            "errors": [{"field": None, "code": code, "message": message}],
                        },
                    ),
                )

    def test_a_message_of_its_own_keeps_the_code(self):
        _, data = self.answer(two_factor.AppUpdateRequired("Update the Go Style app to sign in."))
        self.assertEqual(data["detail"], "Update the Go Style app to sign in.")
        self.assertEqual(data["code"], "app_update_required")

    def test_the_422s_are_on_their_field(self):
        cases = (
            (two_factor.WRONG_CODE, "That code isn’t right. Check it and try again."),
            (two_factor.EXPIRED_CODE, "This code has expired. Send a new one."),
            (two_factor.METHOD_UNAVAILABLE, "That way to verify isn’t set up on your account."),
        )
        for (field, code, message), brief_message in cases:
            with self.subTest(code=code):
                self.assertEqual(message, brief_message)
                exc = ValidationError({field: [ErrorDetail(message, code=code)]})
                self.assertEqual(
                    self.answer(exc),
                    (
                        422,
                        {
                            "detail": "Please correct the highlighted fields.",
                            "code": "validation_error",
                            "errors": [{"field": field, "code": code, "message": message}],
                        },
                    ),
                )

    def test_a_send_too_soon_says_when_to_try_again(self):
        response = api_exception_handler(two_factor.TooManyAttempts(wait=30), {})
        self.assertEqual(response["Retry-After"], "30")

    def test_the_shipped_sessions_errors_are_unchanged(self):
        self.assertEqual(
            self.answer(sessions.SessionRevoked()),
            (
                401,
                {
                    "detail": "You’ve been signed out on this device.",
                    "code": "session_revoked",
                    "errors": [],
                },
            ),
        )


# --- hints and switches -----------------------------------------------------------


class HintTests(SimpleTestCase):
    def test_email_shows_its_first_letter_and_domain(self):
        self.assertEqual(two_factor.email_hint("kevin.new@gmail.com"), "k•••@gmail.com")

    def test_phone_shows_its_country_code_and_last_4_digits(self):
        self.assertEqual(two_factor.phone_hint("+971501234567"), "+971 •• ••• 4567")
        self.assertEqual(two_factor.phone_hint("+447911123456"), "+44 •• ••• 3456")


class ContactHintTests(TwoFactorTestCase):
    def test_only_verified_contacts_count(self):
        self.assertEqual(
            two_factor.contact_hints(self.account),
            {"email": "k•••@gmail.com", "phone": "+971 •• ••• 4567"},
        )
        self.account.email_verified_at = None
        self.assertEqual(
            two_factor.contact_hints(self.account), {"phone": "+971 •• ••• 4567"}
        )


class SwitchTests(SimpleTestCase):
    def test_on_only_with_sessions_on_too(self):
        for two_step, device_sessions, expected in (
            (True, True, True),
            (True, False, False),
            (False, True, False),
        ):
            with self.subTest(two_step=two_step, device_sessions=device_sessions):
                with override_settings(
                    TWO_FACTOR_V1=two_step, DEVICE_SESSIONS_V1=device_sessions
                ):
                    self.assertEqual(bool(two_factor.enabled()), expected)


# --- codes by email and phone --------------------------------------------------------


class SendCodeTests(TwoFactorTestCase):
    def test_answers_where_it_went_and_for_how_long(self):
        self.assertEqual(
            self.send("email"),
            {"sent_to": "k•••@gmail.com", "expires_in": 600, "resend_in": 30},
        )

    def test_sends_six_digits_to_the_accounts_own_email_or_phone(self):
        self.send("email", ON)
        self.send("phone", OFF)
        self.assertEqual([(d, t) for d, _, t in self.outbox], [(EMAIL, "email"), (PHONE, "phone")])
        for _, code, _ in self.outbox:
            self.assertRegex(code, r"^\d{6}$")

    def test_stores_only_a_hash_for_10_minutes(self):
        before = timezone.now()
        self.send()
        row = VerificationCode.objects.get()
        self.assertNotIn(self.sent_code(), row.code_hash)
        self.assertEqual(
            (row.method, row.purpose, row.attempts, row.used_at), ("email", ON, 0, None)
        )
        self.assertAlmostEqual(
            row.expires_at, before + timedelta(minutes=10), delta=timedelta(seconds=5)
        )

    def test_a_contact_not_on_the_account_or_not_verified_is_unavailable(self):
        self.account.email_verified_at = None
        self.account.save()
        for method in ("email", "authenticator"):
            with self.subTest(method=method), self.assertRaises(ValidationError) as caught:
                self.send(method)
            self.assertEqual(error_of(caught), ("method", "unavailable"))
        self.assertEqual(self.outbox, [])
        self.assertFalse(VerificationCode.objects.exists())

    def test_sending_again_within_30_seconds_is_429(self):
        self.send()
        with self.assertRaises(two_factor.TooManyAttempts) as caught:
            self.send()
        self.assertEqual(caught.exception.wait, 30)
        self.send(purpose=OFF)  # another purpose is its own

    def test_10_sends_an_hour_per_account(self):
        for _ in range(10):
            self.send()
            cache.delete(f"2fa:resend:{self.account.pk}:{ON}")
        with self.assertRaises(two_factor.TooManyAttempts):
            self.send()

    def test_20_sends_an_hour_per_ip(self):
        for n in range(20):
            other = self.make_account(f"+97150123{n:04d}")
            self.send("phone", account=other)
        other = self.make_account("+971509999999")
        with self.assertRaises(two_factor.TooManyAttempts):
            self.send("phone", account=other)
        self.send("phone", account=other, ip="86.98.44.120")


class CodeEmailTests(TwoFactorTestCase):
    def test_says_what_the_code_is_for_and_its_10_minutes(self):
        self.send("email", ON)
        self.send("phone", TwoFactorPurpose.SIGN_IN)
        self.assertEqual(
            self.extras,
            [
                {"subject": "Your Go Style security code", "minutes": 10},
                {"subject": "Your Go Style sign-in code", "minutes": 10},
            ],
        )

    def test_the_email_itself(self):
        with patch.object(two_factor, "get_sender", return_value=EmailSender()):
            self.send("email", OFF)
        (msg,) = mail.outbox
        self.assertEqual((msg.to, msg.subject), ([EMAIL], "Your Go Style security code"))
        code = VerificationCode.objects.get()
        sent = re.match(r"Your verification code is (\d{6})\. It expires in 10 minutes\.", msg.body)
        self.assertIsNotNone(sent, msg.body)
        self.check(sent.group(1), "email", OFF)
        code.refresh_from_db()
        self.assertIsNotNone(code.used_at)


class CheckCodeTests(TwoFactorTestCase):
    def test_a_right_code_works_once(self):
        self.send()
        code = self.sent_code()
        self.check(code)
        self.assertIsNotNone(VerificationCode.objects.get().used_at)
        with self.assertRaises(ValidationError) as caught:
            self.check(code)
        self.assertEqual(error_of(caught), ("code", "expired"))

    def test_a_wrong_code_is_invalid_and_counted_and_the_code_stays(self):
        self.send()
        with self.assertRaises(ValidationError) as caught:
            self.check(self.wrong(self.sent_code()))
        self.assertEqual(error_of(caught), ("code", "invalid"))
        self.assertEqual(VerificationCode.objects.get().attempts, 1)
        self.check(self.sent_code())

    def test_a_new_send_voids_the_code_before(self):
        self.send()
        first = self.sent_code()
        cache.delete(f"2fa:resend:{self.account.pk}:{ON}")
        self.send("phone")
        with self.assertRaises(ValidationError) as caught:
            self.check(first, "email")
        self.assertEqual(error_of(caught), ("code", "expired"))
        self.check(self.sent_code(), "phone")

    def test_a_code_past_its_10_minutes_is_expired(self):
        self.send()
        VerificationCode.objects.update(expires_at=timezone.now() - timedelta(seconds=1))
        with self.assertRaises(ValidationError) as caught:
            self.check(self.sent_code())
        self.assertEqual(error_of(caught), ("code", "expired"))

    def test_a_code_is_for_its_own_purpose_and_method(self):
        self.send("email", ON)
        for method, purpose in (("email", OFF), ("phone", ON)):
            with self.subTest(method=method, purpose=purpose):
                with self.assertRaises(ValidationError) as caught:
                    self.check(self.sent_code(), method, purpose)
                self.assertEqual(error_of(caught), ("code", "expired"))

    def test_no_code_sent_is_expired(self):
        with self.assertRaises(ValidationError) as caught:
            self.check("123456")
        self.assertEqual(error_of(caught), ("code", "expired"))


class WrongCodeLimitTests(TwoFactorTestCase):
    """5 wrong codes per 15 minutes per account, across every call."""

    @override_settings(TWO_FACTOR_ENCRYPTION_KEY=KEY)
    def test_after_5_wrong_codes_even_the_right_one_is_429(self):
        self.send()
        code = self.sent_code()
        two_factor.start_setup(self.account, self.sid)
        for _ in range(2):  # tries are shared with the authenticator
            with self.assertRaises(ValidationError):
                two_factor.confirm_setup(self.account, self.sid, "000000x")
        for _ in range(3):
            with self.assertRaises(ValidationError):
                self.check(self.wrong(code))
        with self.assertRaises(two_factor.TooManyAttempts):
            self.check(code)

    def test_a_right_code_does_not_use_a_try(self):
        self.send()
        code = self.sent_code()
        for _ in range(4):
            with self.assertRaises(ValidationError):
                self.check(self.wrong(code))
        self.check(code)
        cache.delete(f"2fa:resend:{self.account.pk}:{ON}")
        self.send()
        with self.assertRaises(ValidationError):  # the fifth
            self.check(self.wrong(self.sent_code()))
        with self.assertRaises(two_factor.TooManyAttempts):
            self.check(self.sent_code())

    def test_the_15_minutes_end(self):
        self.send()
        for _ in range(5):
            with self.assertRaises(ValidationError):
                self.check(self.wrong(self.sent_code()))
        cache.delete(f"2fa:tries:{self.account.pk}")  # what the window's end does
        cache.delete(f"2fa:resend:{self.account.pk}:{ON}")
        self.send()
        self.check(self.sent_code())


# --- verification tokens ------------------------------------------------------------


class VerificationTokenTests(TwoFactorTestCase):
    def verified(self, purpose=ON):
        self.send(purpose=purpose)
        return two_factor.verify(self.account, self.sid, "email", purpose, self.sent_code())

    def test_a_passed_code_answers_a_token_for_5_minutes(self):
        answer = self.verified()
        self.assertRegex(answer["verification_token"], r"^vt_[A-Za-z0-9_-]{43}$")
        self.assertEqual(answer["expires_in"], 300)
        row = VerificationToken.objects.get()
        self.assertEqual(
            row.token_hash, hashlib.sha256(answer["verification_token"].encode()).hexdigest()
        )
        self.assertEqual(
            (row.user_id, row.session_id, row.purpose), (self.account.pk, self.sid, ON)
        )
        self.assertAlmostEqual(
            row.expires_at, timezone.now() + timedelta(minutes=5), delta=timedelta(seconds=5)
        )

    def test_a_wrong_code_answers_no_token(self):
        self.send()
        with self.assertRaises(ValidationError):
            two_factor.verify(self.account, self.sid, "email", ON, self.wrong(self.sent_code()))
        self.assertFalse(VerificationToken.objects.exists())

    def test_a_token_is_used_once(self):
        token = self.verified()["verification_token"]
        two_factor.spend_token(self.account, self.sid, ON, token)
        with self.assertRaises(two_factor.VerificationRequired):
            two_factor.spend_token(self.account, self.sid, ON, token)

    def test_a_token_is_for_its_own_user_session_and_purpose(self):
        token = self.verified()["verification_token"]
        other = self.make_account("+971509999999")
        for user, sid, purpose in (
            (self.account, self.sid, OFF),
            (self.account, uuid.uuid4(), ON),
            (other, self.sid, ON),
        ):
            with self.subTest(purpose=purpose), self.assertRaises(two_factor.VerificationRequired):
                two_factor.spend_token(user, sid, purpose, token)
        two_factor.spend_token(self.account, self.sid, ON, token)  # still unspent

    def test_an_expired_missing_or_made_up_token_is_refused(self):
        token = self.verified()["verification_token"]
        VerificationToken.objects.update(expires_at=timezone.now() - timedelta(seconds=1))
        for raw in (token, None, "", "vt_made-up"):
            with self.subTest(raw=raw), self.assertRaises(two_factor.VerificationRequired):
                two_factor.spend_token(self.account, self.sid, ON, raw)


# --- notices ----------------------------------------------------------------------


@override_settings(OTP_SENDER="smtp")
class NoticeTests(TwoFactorTestCase):
    def session(self, **fields):
        return UserSession.objects.create(user=self.account, refresh_token_hash="h", **fields)

    def notify(self, sid, change=OFF):
        with self.captureOnCommitCallbacks(execute=True):
            two_factor.notify(self.account, sid, change)

    def test_names_the_device_and_place_it_was_done_from(self):
        self.notify(self.session(device_name="iPhone 15", location="Dubai").pk)
        (msg,) = mail.outbox
        self.assertEqual(msg.to, [EMAIL])
        self.assertEqual(msg.subject, "Go Style: 2-step verification turned off")
        self.assertEqual(
            msg.body,
            "2-step verification was turned off for your Go Style account"
            " on iPhone 15 in Dubai.\n\n"
            "If this was not you, reset your password and contact support straight away.",
        )

    def test_each_change_says_what_it_was(self):
        sid = self.session(device_name="iPhone 15").pk
        for change in (ON, OFF, TwoFactorPurpose.AUTHENTICATOR_REMOVE):
            self.notify(sid, change)
        self.assertEqual(
            [(m.subject, m.body.split(" on iPhone 15.")[0]) for m in mail.outbox],
            [
                (
                    "Go Style: 2-step verification turned on",
                    "2-step verification was turned on for your Go Style account",
                ),
                (
                    "Go Style: 2-step verification turned off",
                    "2-step verification was turned off for your Go Style account",
                ),
                (
                    "Go Style: authenticator app removed",
                    "The authenticator app was removed from your Go Style account",
                ),
            ],
        )

    def test_without_a_place_or_a_device_name(self):
        self.notify(self.session(device_name="Galaxy S23 Ultra").pk)
        self.notify(self.session().pk)
        self.notify(uuid.uuid4())
        self.assertEqual(
            [m.body.split("\n")[0] for m in mail.outbox],
            [
                "2-step verification was turned off for your Go Style account"
                " on Galaxy S23 Ultra.",
                "2-step verification was turned off for your Go Style account"
                " on an unknown device.",
                "2-step verification was turned off for your Go Style account"
                " on an unknown device.",
            ],
        )

    def test_sent_only_once_the_change_is_committed(self):
        with self.captureOnCommitCallbacks(execute=False) as callbacks:
            two_factor.notify(self.account, self.session().pk, OFF)
        self.assertEqual((len(callbacks), mail.outbox), (1, []))

    def test_only_to_a_verified_email(self):
        sid = self.session().pk
        self.account.email_verified_at = None
        self.notify(sid)
        self.account.email = None  # phone only: WhatsApp has no template for it
        self.notify(sid)
        self.assertEqual(mail.outbox, [])


# --- the authenticator app ---------------------------------------------------------


@override_settings(TWO_FACTOR_ENCRYPTION_KEY=KEY)
class AuthenticatorTestCase(TwoFactorTestCase):
    def setUp(self):
        super().setUp()
        self.now = timezone.now()
        clock = patch("django.utils.timezone.now", side_effect=lambda: self.now)
        clock.start()
        self.addCleanup(clock.stop)

    def code(self, secret, steps=0):
        return pyotp.TOTP(secret).at(self.now, steps)

    def added(self):
        """A confirmed authenticator. Returns its secret."""
        secret = two_factor.start_setup(self.account, self.sid)["secret"]
        two_factor.confirm_setup(self.account, self.sid, self.code(secret))
        self.now += timedelta(seconds=30)  # its code is used: the next step's is new
        return secret


class OtpauthUrlTests(AuthenticatorTestCase):
    def test_is_the_briefs_link(self):
        self.assertEqual(
            two_factor.otpauth_url(self.account, "JBSWY3DPEHPK3PXPJBSWY3DPEHPK3PXP"),
            "otpauth://totp/Go%20Style:kevin.new%40gmail.com"
            "?secret=JBSWY3DPEHPK3PXPJBSWY3DPEHPK3PXP&issuer=Go%20Style"
            "&algorithm=SHA1&digits=6&period=30",
        )

    def test_is_labelled_with_the_phone_without_an_email(self):
        self.account.email = None
        self.assertIn(
            "Go%20Style:%2B971501234567?",
            two_factor.otpauth_url(self.account, "JBSWY3DPEHPK3PXPJBSWY3DPEHPK3PXP"),
        )


class SetupTests(AuthenticatorTestCase):
    def test_answers_a_new_160_bit_secret_and_its_link(self):
        answer = two_factor.start_setup(self.account, self.sid)
        self.assertRegex(answer["secret"], r"^[A-Z2-7]{32}$")
        self.assertEqual(
            answer["otpauth_url"], two_factor.otpauth_url(self.account, answer["secret"])
        )
        self.assertEqual(answer["expires_in"], 600)

    def test_keeps_the_secret_encrypted_and_pending_for_this_session(self):
        secret = two_factor.start_setup(self.account, self.sid)["secret"]
        row = UserAuthenticator.objects.get()
        self.assertEqual(
            (row.status, row.session_id, row.confirmed_at), ("pending", self.sid, None)
        )
        self.assertNotIn(secret.encode(), bytes(row.secret_encrypted))
        self.assertEqual(two_factor._decrypt(row), secret)
        self.assertIsNone(two_factor.active_authenticator(self.account))

    def test_the_first_code_turns_it_on(self):
        secret = two_factor.start_setup(self.account, self.sid)["secret"]
        added_at = two_factor.confirm_setup(self.account, self.sid, self.code(secret))
        row = UserAuthenticator.objects.get()
        self.assertEqual((row.status, row.confirmed_at, row.session_id), ("active", added_at, None))
        self.assertEqual(added_at, self.now)
        self.assertEqual(two_factor.active_authenticator(self.account), row)

    def test_a_wrong_first_code_leaves_it_pending(self):
        secret = two_factor.start_setup(self.account, self.sid)["secret"]
        with self.assertRaises(ValidationError) as caught:
            two_factor.confirm_setup(self.account, self.sid, self.wrong(self.code(secret)))
        self.assertEqual(error_of(caught), ("code", "invalid"))
        self.assertEqual(UserAuthenticator.objects.get().status, "pending")
        two_factor.confirm_setup(self.account, self.sid, self.code(secret))

    def test_no_setup_without_one_for_this_session_within_10_minutes(self):
        with self.assertRaises(two_factor.NoSetup):
            two_factor.confirm_setup(self.account, self.sid, "123456")
        secret = two_factor.start_setup(self.account, self.sid)["secret"]
        with self.assertRaises(two_factor.NoSetup):
            two_factor.confirm_setup(self.account, uuid.uuid4(), self.code(secret))
        self.now += timedelta(minutes=10, seconds=1)
        with self.assertRaises(two_factor.NoSetup):
            two_factor.confirm_setup(self.account, self.sid, self.code(secret))

    def test_starting_again_replaces_a_pending_setup(self):
        old = two_factor.start_setup(self.account, self.sid)["secret"]
        new = two_factor.start_setup(self.account, self.sid)["secret"]
        self.assertNotEqual(old, new)
        self.assertEqual(UserAuthenticator.objects.count(), 1)
        with self.assertRaises(ValidationError):
            two_factor.confirm_setup(self.account, self.sid, self.code(old))
        two_factor.confirm_setup(self.account, self.sid, self.code(new))

    def test_a_confirmed_one_must_be_removed_first(self):
        self.added()
        before = UserAuthenticator.objects.values().get()
        with self.assertRaises(two_factor.AlreadyAdded):
            two_factor.start_setup(self.account, self.sid)
        self.assertEqual(UserAuthenticator.objects.values().get(), before)

    def test_remove_takes_a_pending_or_confirmed_one_and_none_is_fine(self):
        self.added()
        two_factor.remove(self.account)
        two_factor.start_setup(self.account, self.sid)
        two_factor.remove(self.account)
        self.assertFalse(UserAuthenticator.objects.exists())
        two_factor.remove(self.account)


class AuthenticatorCodeTests(AuthenticatorTestCase):
    def test_the_current_step_and_one_either_side_pass(self):
        secret = self.added()
        self.now += timedelta(seconds=30)  # so the step before is not the setup's
        for steps in (-1, 1):  # the later step last: an earlier one never passes after it
            with self.subTest(steps=steps):
                two_factor.check_authenticator_code(self.account, self.code(secret, steps))
        self.now += timedelta(seconds=60)
        two_factor.check_authenticator_code(self.account, self.code(secret))

    def test_two_steps_away_is_wrong(self):
        secret = self.added()
        for steps in (-2, 2):
            with self.subTest(steps=steps), self.assertRaises(ValidationError) as caught:
                two_factor.check_authenticator_code(self.account, self.code(secret, steps))
            self.assertEqual(error_of(caught), ("code", "invalid"))

    def test_a_code_passes_once_and_never_an_older_one_after_it(self):
        secret = self.added()
        current, earlier = self.code(secret), self.code(secret, -1)
        two_factor.check_authenticator_code(self.account, current)
        for code in (current, earlier):
            with self.assertRaises(ValidationError):
                two_factor.check_authenticator_code(self.account, code)

    def test_the_setup_code_does_not_pass_again(self):
        secret = two_factor.start_setup(self.account, self.sid)["secret"]
        two_factor.confirm_setup(self.account, self.sid, self.code(secret))
        with self.assertRaises(ValidationError):
            two_factor.check_authenticator_code(self.account, self.code(secret))

    def test_without_a_confirmed_one_it_is_unavailable(self):
        secret = two_factor.start_setup(self.account, self.sid)["secret"]
        with self.assertRaises(ValidationError) as caught:
            two_factor.check_authenticator_code(self.account, self.code(secret))
        self.assertEqual(error_of(caught), ("method", "unavailable"))

    def test_verify_by_authenticator_answers_a_token(self):
        secret = self.added()
        answer = two_factor.verify(
            self.account, self.sid, "authenticator", OFF, self.code(secret)
        )
        two_factor.spend_token(self.account, self.sid, OFF, answer["verification_token"])

    def test_the_briefs_python_works(self):
        secret = pyotp.random_base32(32)  # 160 bits
        otpauth_url = pyotp.TOTP(secret).provisioning_uri(
            name="kevin.new@gmail.com", issuer_name="Go Style"
        )
        self.assertTrue(otpauth_url.startswith("otpauth://totp/Go%20Style:kevin.new%40gmail.com?"))
        totp = pyotp.TOTP(secret)
        self.assertTrue(totp.verify(totp.at(self.now), for_time=self.now, valid_window=1))


class EncryptionKeyTests(AuthenticatorTestCase):
    @override_settings(TWO_FACTOR_ENCRYPTION_KEY="")
    def test_no_key_is_503_logged_and_nothing_written(self):
        with self.assertLogs("apps.accounts.two_factor", "ERROR") as logs:
            with self.assertRaises(two_factor.AuthenticatorUnavailable):
                two_factor.start_setup(self.account, self.sid)
        self.assertIn("TWO_FACTOR_ENCRYPTION_KEY", logs.output[0])
        self.assertFalse(UserAuthenticator.objects.exists())

    def test_a_key_that_is_not_32_bytes_of_base64_is_no_key(self):
        for key in ("not base64!", base64.b64encode(bytes(16)).decode()):
            with self.subTest(key=key), override_settings(TWO_FACTOR_ENCRYPTION_KEY=key):
                with self.assertLogs("apps.accounts.two_factor", "ERROR"):
                    with self.assertRaises(two_factor.AuthenticatorUnavailable):
                        two_factor.start_setup(self.account, self.sid)

    def test_a_url_safe_key_works(self):
        key = base64.urlsafe_b64encode(bytes([251] * 32)).decode()
        self.assertIn("_", key)
        with override_settings(TWO_FACTOR_ENCRYPTION_KEY=key):
            self.added()

    def test_a_changed_key_is_503_and_costs_no_try(self):
        secret = self.added()
        tries = cache.get(f"2fa:tries:{self.account.pk}")
        other = base64.b64encode(bytes(32)).decode()
        with override_settings(TWO_FACTOR_ENCRYPTION_KEY=other):
            with self.assertLogs("apps.accounts.two_factor", "ERROR") as logs:
                with self.assertRaises(two_factor.AuthenticatorUnavailable):
                    two_factor.check_authenticator_code(self.account, self.code(secret))
        self.assertIn("does not open", logs.output[0])
        self.assertEqual(cache.get(f"2fa:tries:{self.account.pk}"), tries)

    def test_a_secret_copied_onto_another_account_does_not_open(self):
        self.added()
        other = self.make_account("+971509999999")
        row = UserAuthenticator.objects.get()
        copy = UserAuthenticator(user=other, secret_encrypted=row.secret_encrypted, status="active")
        with self.assertLogs("apps.accounts.two_factor", "ERROR"):
            with self.assertRaises(two_factor.AuthenticatorUnavailable):
                two_factor._decrypt(copy)


# --- the app version gate ---------------------------------------------------------------


class VersionGateTests(SimpleTestCase):
    def request(self, version=None):
        headers = {"HTTP_X_APP_VERSION": version} if version is not None else {}
        return RequestFactory().post("/", **headers)

    @override_settings(TWO_FACTOR_MIN_APP_VERSION="1.4.0")
    def test_at_least_the_minimum_passes(self):
        for version, passes in (
            ("1.4.0", True),
            ("1.4", True),
            ("1.4.1", True),
            ("1.10.0", True),
            ("2", True),
            (" 1.4.0 ", True),
            ("1.3.9", False),
            ("1.3", False),
            ("1.4.0-beta", False),
            ("v1.4.0", False),
            ("", False),
            (None, False),
        ):
            with self.subTest(version=version):
                self.assertEqual(two_factor.app_is_new_enough(self.request(version)), passes)

    def test_while_the_minimum_is_empty_no_app_is_new_enough(self):
        for minimum in ("", "not a version"):
            with self.subTest(minimum=minimum):
                with override_settings(TWO_FACTOR_MIN_APP_VERSION=minimum):
                    self.assertFalse(two_factor.app_is_new_enough(self.request("99.0.0")))

    @override_settings(TWO_FACTOR_MIN_APP_VERSION="1.4.0")
    def test_an_old_app_is_told_to_update(self):
        two_factor.require_new_app(self.request("1.4.0"))
        with self.assertRaises(two_factor.AppUpdateRequired) as caught:
            two_factor.require_new_app(self.request("1.3.0"), "Update the Go Style app to sign in.")
        self.assertEqual(caught.exception.status_code, 426)
        self.assertEqual(
            str(caught.exception.detail["detail"]), "Update the Go Style app to sign in."
        )
