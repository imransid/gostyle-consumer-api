"""
2-step verification and the authenticator app (apps/accounts/two_factor.py,
docs/TWO_FACTOR_API.md).

Codes never leave the process: FakeSender keeps what would have been sent.
The authenticator's tests stop the clock, so a code made in the test is
checked in the same 30-second step.
"""

import base64
import hashlib
import json
import re
import uuid
from datetime import datetime, timedelta
from datetime import timezone as dt_timezone
from unittest.mock import patch

import pyotp
from django.conf import settings
from django.core import mail
from django.core.cache import cache
from django.db import IntegrityError, connection, transaction
from django.test import SimpleTestCase, TestCase, override_settings
from django.utils import timezone
from rest_framework.exceptions import ErrorDetail, ValidationError
from rest_framework.test import APIClient
from rest_framework_simplejwt.tokens import AccessToken

from apps.accounts import sessions, two_factor, two_factor_views
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
ADD = TwoFactorPurpose.AUTHENTICATOR_ADD
REMOVE = TwoFactorPurpose.AUTHENTICATOR_REMOVE
PASSWORD = "Str0ng!Pass"
SECURITY = "/api/v1/me/security"
UAE_TIME = re.compile(r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d\+04:00$")

IPHONE = {
    "HTTP_X_DEVICE_ID": "9F1C2B7E-3D4A-4C1F-8E2B-6A0D5C9B1E47",
    "HTTP_X_DEVICE_NAME": "iPhone 15",
    "HTTP_X_DEVICE_TYPE": "phone",
    "HTTP_X_PLATFORM": "ios",
    "HTTP_X_APP_VERSION": "1.4.0",
    "REMOTE_ADDR": IP,
}
IPAD = {
    **IPHONE,
    "HTTP_X_DEVICE_ID": "1B7E4D2A-0C6F-4A93-8E15-7D9B3F2C6A40",
    "HTTP_X_DEVICE_NAME": "iPad",
}
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

    # Section 7 word for word, and our own one at the end.
    BRIEF = {
        "verification_required": (403, "Please verify it’s you first."),
        "already_added": (409, "Remove your current authenticator app first."),
        "no_setup": (409, "Start setting up your authenticator app again."),
        "too_many_attempts": (429, "Too many tries. Wait a few minutes and try again."),
        "challenge_expired": (401, "Your sign-in timed out. Please sign in again."),
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
        sent = re.search(r"Your code: (\d{6})\nIt expires in 10 minutes\.", msg.body)
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


@override_settings(OTP_SENDER="live")
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
            "If this was not you, reset your password and contact support straight away."
            "\n\nAutomated message from GoStyle. Replies to this address are not read.",
        )
        html, mimetype = msg.alternatives[0]
        self.assertEqual(mimetype, "text/html")
        self.assertIn("2-step verification turned off", html)  # the headline
        self.assertIn("on iPhone 15 in Dubai.", html)

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


# --- the /me/security routes ----------------------------------------------------------

ROUTES = (
    ("get", ""),
    ("patch", ""),
    ("post", "/verify/send"),
    ("post", "/verify"),
    ("patch", "/two-factor"),
    ("post", "/authenticator"),
    ("post", "/authenticator/confirm"),
    ("delete", "/authenticator"),
)


@override_settings(
    DEVICE_SESSIONS_V1=True,
    TWO_FACTOR_V1=True,
    TWO_FACTOR_MIN_APP_VERSION="1.4.0",
    OTP_SENDER="live",
)
class SecurityApiTestCase(AuthenticatorTestCase):
    """Through the real URLs, signed in with /auth/login, so every token has
    its session. The clock stands still (AuthenticatorTestCase)."""

    def setUp(self):
        super().setUp()
        self.account.set_password(PASSWORD)
        self.account.save()
        self.phone = self.login(IPHONE)

    def login(self, device):
        client = APIClient()
        body = {"destination_type": "phone", "destination": PHONE, "password": PASSWORD}
        resp = client.post("/api/v1/auth/login", body, format="json", **device)
        self.assertEqual(resp.status_code, 200, resp.content)
        client.credentials(HTTP_AUTHORIZATION=f"Bearer {resp.json()['access']}", **device)
        client.sid = uuid.UUID(AccessToken(resp.json()["access"])["sid"])
        return client

    def call(self, method, path, body=None, client=None):
        with self.captureOnCommitCallbacks(execute=True):
            return getattr(client or self.phone, method)(SECURITY + path, body, format="json")

    def token(self, purpose, method="email", client=None):
        """A verification token for `purpose`, from Verify it's you."""
        if method == "authenticator":
            code = self.code(self.secret)
            self.now += timedelta(seconds=30)  # the next code is a new one
        else:
            cache.delete(f"2fa:resend:{self.account.pk}:{purpose}")
            resp = self.call("post", "/verify/send", {"method": method, "purpose": purpose}, client)
            self.assertEqual(resp.status_code, 200, resp.content)
            code = self.sent_code()
        body = {"method": method, "purpose": purpose, "code": code}
        resp = self.call("post", "/verify", body, client)
        self.assertEqual(resp.status_code, 200, resp.content)
        return resp.json()["verification_token"]

    def add_authenticator(self, client=None):
        """Adds one through the routes. Keeps its secret in self.secret."""
        body = {"verification_token": self.token(ADD, client=client), "purpose": ADD}
        resp = self.call("post", "/authenticator", body, client)
        self.assertEqual(resp.status_code, 201, resp.content)
        self.secret = resp.json()["secret"]
        resp = self.call("post", "/authenticator/confirm", {"code": self.code(self.secret)}, client)
        self.assertEqual(resp.status_code, 200, resp.content)
        self.now += timedelta(seconds=30)
        return resp

    def assertRequestError(self, resp, status_code, code):
        """An error of no field, as auth-error-response.md section 4 has it."""
        self.assertEqual(resp.status_code, status_code, resp.content)
        body = resp.json()
        self.assertEqual(body["code"], code)
        self.assertEqual(body["errors"], [{"field": None, "code": code, "message": body["detail"]}])

    def assertFieldError(self, resp, field, code):
        self.assertEqual(resp.status_code, 422, resp.content)
        self.assertEqual(
            [(e["field"], e["code"]) for e in resp.json()["errors"]], [(field, code)]
        )


class SecuritySwitchTests(SecurityApiTestCase):
    def test_every_route_is_404_while_either_switch_is_off(self):
        for switches in ({"TWO_FACTOR_V1": False}, {"DEVICE_SESSIONS_V1": False}):
            with override_settings(**switches):
                for method, path in ROUTES:
                    with self.subTest(switches=switches, route=f"{method} {path}"):
                        self.assertEqual(self.call(method, path, {}).status_code, 404)

    def test_no_token_is_401(self):
        stranger = APIClient()
        stranger.credentials(**IPHONE)
        for method, path in ROUTES:
            with self.subTest(route=f"{method} {path}"):
                self.assertEqual(self.call(method, path, {}, stranger).status_code, 401)

    def test_no_call_looks_at_the_app_version(self):
        for version in ("1.0.0", "v1.4.0-beta", None):
            device = {**IPAD, "HTTP_X_APP_VERSION": version}
            if version is None:
                del device["HTTP_X_APP_VERSION"]
            with self.subTest(version=version):
                client = self.login(device)
                self.assertEqual(self.call("get", "", client=client).status_code, 200)
                self.add_authenticator(client=client)
                body = {"verification_token": self.token(REMOVE, client=client)}
                resp = self.call("delete", "/authenticator", body, client)
                self.assertEqual(resp.status_code, 204, resp.content)


class SecurityReadTests(SecurityApiTestCase):
    def test_a_new_account_as_the_brief_has_it(self):
        resp = self.call("get", "")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(
            resp.json(),
            {
                "two_factor_enabled": False,
                "methods": {
                    "email": {"available": True, "hint": "k•••@gmail.com"},
                    "phone": {"available": True, "hint": "+971 •• ••• 4567"},
                    "authenticator": {"available": False, "added_at": None},
                },
                "password_changed_at": two_factor_views._uae(self.account.created_at),
                "sign_in_alerts": True,
            },
        )

    def test_a_contact_not_verified_is_not_available(self):
        ConsumerAccount.objects.filter(pk=self.account.pk).update(email_verified_at=None)
        self.assertEqual(
            self.call("get", "").json()["methods"]["email"], {"available": False, "hint": None}
        )

    def test_a_confirmed_authenticator_with_its_date_and_never_a_pending_one(self):
        body = {"verification_token": self.token(ADD)}
        self.assertEqual(self.call("post", "/authenticator", body).status_code, 201)
        pending = self.call("get", "").json()["methods"]["authenticator"]
        self.assertEqual(pending, {"available": False, "added_at": None})
        self.call("delete", "/authenticator", {"verification_token": self.token(REMOVE)})
        added_at = self.add_authenticator().json()["added_at"]
        self.assertRegex(added_at, UAE_TIME)
        self.assertEqual(
            self.call("get", "").json()["methods"]["authenticator"],
            {"available": True, "added_at": added_at},
        )


class VerifyItsYouApiTests(SecurityApiTestCase):
    def test_send_then_check_answers_a_token(self):
        resp = self.call("post", "/verify/send", {"method": "phone", "purpose": ON})
        self.assertEqual(
            (resp.status_code, resp.json()),
            (200, {"sent_to": "+971 •• ••• 4567", "expires_in": 600, "resend_in": 30}),
        )
        self.assertEqual(self.outbox[-1][0], PHONE)
        body = {"method": "phone", "purpose": ON, "code": self.sent_code()}
        resp = self.call("post", "/verify", body)
        self.assertEqual(resp.status_code, 200, resp.content)
        self.assertEqual(set(resp.json()), {"verification_token", "expires_in"})
        self.assertEqual(VerificationToken.objects.get().session_id, self.phone.sid)

    def test_nothing_to_send_for_an_authenticator_or_a_sign_in(self):
        for body, field in (
            ({"method": "authenticator", "purpose": ON}, "method"),
            ({"method": "email", "purpose": "sign_in"}, "purpose"),
        ):
            with self.subTest(body=body):
                resp = self.call("post", "/verify/send", body)
                self.assertFieldError(resp, field, "invalid_choice")
        self.assertEqual(self.outbox, [])

    def test_a_code_that_is_not_six_digits_is_wrong_and_costs_no_try(self):
        self.call("post", "/verify/send", {"method": "email", "purpose": ON})
        for code in ("12ab56", "12345", "1234567"):
            with self.subTest(code=code):
                body = {"method": "email", "purpose": ON, "code": code}
                resp = self.call("post", "/verify", body)
                self.assertFieldError(resp, "code", "invalid")
        self.assertEqual(
            resp.json()["errors"][0]["message"], "That code isn’t right. Check it and try again."
        )
        self.assertIsNone(cache.get(f"2fa:tries:{self.account.pk}"))

    def test_5_wrong_codes_then_429(self):
        self.call("post", "/verify/send", {"method": "email", "purpose": ON})
        body = {"method": "email", "purpose": ON, "code": self.wrong(self.sent_code())}
        for _ in range(5):
            self.assertFieldError(self.call("post", "/verify", body), "code", "invalid")
        body["code"] = self.sent_code()
        self.assertRequestError(self.call("post", "/verify", body), 429, "too_many_attempts")


class TwoFactorSwitchApiTests(SecurityApiTestCase):
    def switch(self, enabled, token, client=None):
        body = {"enabled": enabled, "verification_token": token}
        return self.call("patch", "/two-factor", body, client)

    def test_turning_it_on(self):
        UserSession.objects.update(location="Dubai")
        resp = self.switch(True, self.token(ON))
        self.assertEqual((resp.status_code, resp.json()), (200, {"two_factor_enabled": True}))
        self.account.refresh_from_db()
        self.assertTrue(self.account.two_factor_enabled)
        self.assertEqual(self.account.two_factor_changed_at, self.now)
        # This phone just passed Verify it's you: it is not asked at its next sign-in.
        session = UserSession.objects.get(pk=self.phone.sid)
        self.assertEqual(session.two_factor_passed_at, self.now)
        (notice,) = mail.outbox
        self.assertEqual(notice.subject, "Go Style: 2-step verification turned on")
        self.assertIn("for your Go Style account on iPhone 15 in Dubai.", notice.body)

    def test_turning_it_off_leaves_the_authenticator(self):
        self.add_authenticator()
        self.switch(True, self.token(ON))
        resp = self.switch(False, self.token(OFF, method="authenticator"))
        self.assertEqual(resp.json(), {"two_factor_enabled": False})
        self.assertIsNotNone(two_factor.active_authenticator(self.account))
        self.assertEqual(mail.outbox[-1].subject, "Go Style: 2-step verification turned off")

    def test_needs_a_token_for_its_own_direction_this_session_and_once(self):
        ipad = self.login(IPAD)
        on = self.token(ON)
        for token, client in (
            (None, None),
            ("", None),
            (self.token(OFF), None),
            (on, ipad),
        ):
            with self.subTest(token=token, client=client):
                resp = self.switch(True, token, client)
                self.assertRequestError(resp, 403, "verification_required")
                self.assertEqual(resp.json()["detail"], "Please verify it’s you first.")
        self.assertEqual(self.switch(True, on).status_code, 200)
        self.assertRequestError(self.switch(False, on), 403, "verification_required")
        self.assertRequestError(self.switch(True, on), 403, "verification_required")

    def test_the_value_it_has_is_200_uses_the_token_and_tells_nobody(self):
        off = self.token(OFF)
        resp = self.switch(False, off)
        self.assertEqual((resp.status_code, resp.json()), (200, {"two_factor_enabled": False}))
        self.assertRequestError(self.switch(False, off), 403, "verification_required")
        self.assertEqual(mail.outbox, [])
        self.account.refresh_from_db()
        self.assertIsNone(self.account.two_factor_changed_at)


class AuthenticatorApiTests(SecurityApiTestCase):
    def test_adding_one(self):
        UserSession.objects.update(location="Dubai")
        token = self.token(ADD)
        resp = self.call("post", "/authenticator", {"verification_token": token, "purpose": ADD})
        self.assertEqual(resp.status_code, 201, resp.content)
        answer = resp.json()
        self.assertEqual(set(answer), {"secret", "otpauth_url", "expires_in"})
        self.assertEqual(answer["expires_in"], 600)
        self.assertEqual(
            answer["otpauth_url"], two_factor.otpauth_url(self.account, answer["secret"])
        )
        self.assertEqual(mail.outbox, [])  # not added yet

        resp = self.call("post", "/authenticator/confirm", {"code": self.code(answer["secret"])})
        self.assertEqual(resp.status_code, 200, resp.content)
        self.assertEqual(resp.json(), {"added_at": two_factor_views._uae(self.now)})
        (notice,) = mail.outbox
        self.assertEqual(notice.subject, "Go Style: authenticator app added")
        self.assertEqual(
            notice.body.split("\n")[0],
            "An authenticator app was added to your Go Style account on iPhone 15 in Dubai.",
        )

    def test_starting_needs_a_token_for_adding(self):
        for token in (None, self.token(REMOVE)):
            with self.subTest(token=token):
                body = {"verification_token": token} if token else {}
                resp = self.call("post", "/authenticator", body)
                self.assertRequestError(resp, 403, "verification_required")
        self.assertFalse(UserAuthenticator.objects.exists())

    def test_one_already_on_is_409_and_the_token_is_kept(self):
        self.add_authenticator()
        token = self.token(ADD)
        resp = self.call("post", "/authenticator", {"verification_token": token})
        self.assertRequestError(resp, 409, "already_added")
        remove = self.token(REMOVE, method="authenticator")
        resp = self.call("delete", "/authenticator", {"verification_token": remove})
        self.assertEqual(resp.status_code, 204)
        resp = self.call("post", "/authenticator", {"verification_token": token})
        self.assertEqual(resp.status_code, 201, resp.content)

    def test_no_key_is_503_and_the_token_is_kept(self):
        token = self.token(ADD)
        with override_settings(TWO_FACTOR_ENCRYPTION_KEY=""):
            with self.assertLogs("apps.accounts.two_factor", "ERROR"):
                resp = self.call("post", "/authenticator", {"verification_token": token})
        self.assertRequestError(resp, 503, "authenticator_unavailable")
        resp = self.call("post", "/authenticator", {"verification_token": token})
        self.assertEqual(resp.status_code, 201, resp.content)

    def test_confirming_is_this_sessions_and_a_wrong_code_keeps_it_pending(self):
        resp = self.call("post", "/authenticator", {"verification_token": self.token(ADD)})
        secret = resp.json()["secret"]
        ipad = self.login(IPAD)
        resp = self.call("post", "/authenticator/confirm", {"code": self.code(secret)}, ipad)
        self.assertRequestError(resp, 409, "no_setup")
        resp = self.call("post", "/authenticator/confirm", {"code": self.wrong(self.code(secret))})
        self.assertFieldError(resp, "code", "invalid")
        resp = self.call("post", "/authenticator/confirm", {"code": self.code(secret)})
        self.assertEqual(resp.status_code, 200, resp.content)

    def test_removing_it_with_its_own_code(self):
        self.add_authenticator()
        self.call("patch", "/two-factor", {"enabled": True, "verification_token": self.token(ON)})
        mail.outbox.clear()
        token = self.token(REMOVE, method="authenticator")
        body = {"verification_token": token, "purpose": REMOVE}
        resp = self.call("delete", "/authenticator", body)
        self.assertEqual(resp.status_code, 204, resp.content)
        self.assertFalse(UserAuthenticator.objects.exists())
        self.account.refresh_from_db()
        self.assertTrue(self.account.two_factor_enabled)  # email and phone still work
        (notice,) = mail.outbox
        self.assertEqual(notice.subject, "Go Style: authenticator app removed")

    def test_removing_none_is_204_but_still_needs_a_token(self):
        resp = self.call("delete", "/authenticator", {})
        self.assertRequestError(resp, 403, "verification_required")
        resp = self.call("delete", "/authenticator", {"verification_token": self.token(REMOVE)})
        self.assertEqual(resp.status_code, 204)
        self.assertEqual(mail.outbox, [])


# --- the sign-in challenge ----------------------------------------------------------------

LOGIN = "/api/v1/auth/login"
SEND = "/api/v1/auth/2fa/send"
FINISH = "/api/v1/auth/2fa/verify"
SIGN_IN = TwoFactorPurpose.SIGN_IN
NO_DEVICE_ID = {k: v for k, v in IPAD.items() if k != "HTTP_X_DEVICE_ID"}
BROWSER = {"REMOTE_ADDR": IP}  # no device headers at all
GALAXY = {**IPAD, "HTTP_X_DEVICE_ID": "a1b2c3d4e5f60718", "HTTP_X_DEVICE_NAME": "Galaxy"}


class SignInTestCase(SecurityApiTestCase):
    """The account has 2-step on. The iPhone signed in before it was; the
    iPad signs in now."""

    def setUp(self):
        super().setUp()
        ConsumerAccount.objects.filter(pk=self.account.pk).update(two_factor_enabled=True)

    def anonymous(self, path, body, device=IPAD):
        with self.captureOnCommitCallbacks(execute=True):
            return APIClient().post(path, body, format="json", **device)

    def sign_in(self, device=IPAD):
        body = {"destination_type": "phone", "destination": PHONE, "password": PASSWORD}
        return self.anonymous(LOGIN, body, device)

    def challenge(self, device=IPAD):
        resp = self.sign_in(device)
        self.assertEqual(resp.status_code, 200, resp.content)
        self.assertTrue(resp.json().get("two_factor_required"), resp.content)
        return resp.json()["challenge_token"]

    def sign_in_code(self, challenge, method="email", device=IPAD):
        cache.delete(f"2fa:resend:{self.account.pk}:{SIGN_IN}")
        resp = self.anonymous(SEND, {"challenge_token": challenge, "method": method}, device)
        self.assertEqual(resp.status_code, 200, resp.content)
        return self.sent_code()

    def finish(self, challenge, code, method="email", device=IPAD):
        body = {"challenge_token": challenge, "method": method, "code": code}
        return self.anonymous(FINISH, body, device)

    def signed_in(self, device=IPAD):
        """Through the whole challenge. Returns the token pair."""
        challenge = self.challenge(device)
        resp = self.finish(challenge, self.sign_in_code(challenge, device=device), device=device)
        self.assertEqual(resp.status_code, 200, resp.content)
        return resp.json()

    def session_of(self, tokens):
        return UserSession.objects.get(pk=AccessToken(tokens["access"])["sid"])


class SignInSwitchTests(SignInTestCase):
    def test_switched_off_login_answers_tokens_as_before(self):
        for switches in ({"TWO_FACTOR_V1": False}, {"DEVICE_SESSIONS_V1": False}):
            with self.subTest(switches=switches), override_settings(**switches):
                resp = self.sign_in({**IPAD, "HTTP_X_APP_VERSION": "1.0.0"})
                self.assertEqual(resp.status_code, 200, resp.content)
                self.assertEqual(set(resp.json()), {"access", "refresh"})
                for path in (SEND, FINISH):
                    self.assertEqual(self.anonymous(path, {}).status_code, 404)
        self.assertFalse(LoginChallenge.objects.exists())

    def test_an_account_with_2_step_off_signs_in_as_before_from_any_app(self):
        ConsumerAccount.objects.filter(pk=self.account.pk).update(two_factor_enabled=False)
        for device in ({**IPAD, "HTTP_X_APP_VERSION": "1.0.0"}, NO_DEVICE_ID):
            with self.subTest(device=device):
                resp = self.sign_in(device)
                self.assertEqual(set(resp.json()), {"access", "refresh"})


class ChallengeTests(SignInTestCase):
    def test_login_answers_the_briefs_challenge_and_no_tokens(self):
        resp = self.sign_in()
        self.assertEqual(resp.status_code, 200)
        body = resp.json()
        raw = body.pop("challenge_token")
        self.assertRegex(raw, r"^ch_[A-Za-z0-9_-]{43}$")
        self.assertEqual(
            body,
            {
                "two_factor_required": True,
                "expires_in": 600,
                "methods": [
                    {"method": "email", "hint": "k•••@gmail.com"},
                    {"method": "phone", "hint": "+971 •• ••• 4567"},
                ],
            },
        )
        challenge = LoginChallenge.objects.get()
        self.assertEqual(challenge.token_hash, hashlib.sha256(raw.encode()).hexdigest())
        self.assertEqual(challenge.device_id, IPAD["HTTP_X_DEVICE_ID"])
        self.assertEqual(challenge.expires_at, self.now + timedelta(minutes=10))
        self.assertFalse(UserSession.objects.filter(device_id=IPAD["HTTP_X_DEVICE_ID"]).exists())

    def test_the_methods_are_only_what_the_account_has(self):
        self.add_authenticator(client=self.phone)
        ConsumerAccount.objects.filter(pk=self.account.pk).update(email_verified_at=None)
        self.assertEqual(
            self.sign_in().json()["methods"],
            [
                {"method": "phone", "hint": "+971 •• ••• 4567"},
                {"method": "authenticator", "hint": None},
            ],
        )

    def test_a_wrong_password_is_only_a_wrong_password(self):
        body = {"destination_type": "phone", "destination": PHONE, "password": "wrong"}
        resp = self.anonymous(LOGIN, body, {**IPAD, "HTTP_X_APP_VERSION": "1.0.0"})
        self.assertEqual(resp.status_code, 422)
        self.assertEqual(resp.json()["detail"], "Invalid credentials.")
        self.assertFalse(LoginChallenge.objects.exists())

    def test_any_app_or_browser_gets_the_challenge_whatever_its_version(self):
        no_version = {k: v for k, v in IPAD.items() if k != "HTTP_X_APP_VERSION"}
        for device in ({**IPAD, "HTTP_X_APP_VERSION": "1.0.0"}, no_version, BROWSER):
            with self.subTest(device=device):
                resp = self.sign_in(device)
                self.assertEqual(resp.status_code, 200, resp.content)
                self.assertTrue(resp.json()["two_factor_required"])
                self.assertNotIn("access", resp.json())
        self.assertFalse(UserSession.objects.filter(device_id=IPAD["HTTP_X_DEVICE_ID"]).exists())

    def test_a_browser_can_finish_the_challenge(self):
        tokens = self.signed_in(BROWSER)
        self.assertEqual(set(tokens), {"access", "refresh"})
        self.assertIsNone(self.session_of(tokens).app_version)

    def test_a_device_that_passed_gets_the_tokens_while_it_stays_signed_in(self):
        first = self.session_of(self.signed_in())
        passed_at = first.two_factor_passed_at
        self.now += timedelta(minutes=1)
        resp = self.sign_in()
        self.assertEqual(set(resp.json()), {"access", "refresh"})
        again = self.session_of(resp.json())
        self.assertEqual(again.two_factor_passed_at, passed_at)
        first.refresh_from_db()
        self.assertIsNotNone(first.ended_at)  # one session per device, as ever

    def test_a_device_is_asked_again_once_it_signs_out(self):
        tokens = self.signed_in()
        client = APIClient()
        client.credentials(HTTP_AUTHORIZATION=f"Bearer {tokens['access']}", **IPAD)
        resp = client.post("/api/v1/auth/logout", {"refresh": tokens["refresh"]}, format="json")
        self.assertEqual(resp.status_code, 205)
        self.assertTrue(self.sign_in().json()["two_factor_required"])

    def test_a_device_signed_out_from_another_is_asked_again(self):
        sid = self.session_of(self.signed_in()).pk
        self.assertEqual(self.phone.delete(f"/api/v1/me/sessions/{sid}").status_code, 204)
        self.assertTrue(self.sign_in().json()["two_factor_required"])

    def test_without_an_x_device_id_it_always_asks(self):
        self.signed_in(NO_DEVICE_ID)
        self.assertTrue(self.sign_in(NO_DEVICE_ID).json()["two_factor_required"])

    def test_another_device_is_asked(self):
        self.signed_in()
        self.assertTrue(self.sign_in(GALAXY).json()["two_factor_required"])

    def test_the_phone_that_turned_it_on_is_not_asked(self):
        ConsumerAccount.objects.filter(pk=self.account.pk).update(two_factor_enabled=False)
        body = {"enabled": True, "verification_token": self.token(ON)}
        self.assertEqual(self.call("patch", "/two-factor", body).status_code, 200)
        self.assertEqual(set(self.sign_in(IPHONE).json()), {"access", "refresh"})
        self.assertTrue(self.sign_in(IPAD).json()["two_factor_required"])


class SignInSendTests(SignInTestCase):
    def test_sends_the_sign_in_code_to_the_accounts_own_contact(self):
        challenge = self.challenge()
        resp = self.anonymous(SEND, {"challenge_token": challenge, "method": "phone"})
        self.assertEqual(
            (resp.status_code, resp.json()),
            (200, {"sent_to": "+971 •• ••• 4567", "expires_in": 600, "resend_in": 30}),
        )
        self.assertEqual(self.outbox[-1][0], PHONE)
        self.assertEqual(VerificationCode.objects.get().purpose, SIGN_IN)
        resp = self.anonymous(SEND, {"challenge_token": challenge, "method": "email"})
        self.assertRequestError(resp, 429, "too_many_attempts")

    def test_nothing_to_send_for_an_authenticator(self):
        body = {"challenge_token": self.challenge(), "method": "authenticator"}
        self.assertFieldError(self.anonymous(SEND, body), "method", "invalid_choice")

    def test_only_for_a_live_challenge(self):
        used = self.challenge()
        self.finish(used, self.sign_in_code(used))
        expired = self.challenge(GALAXY)  # the iPad is remembered now
        self.now += timedelta(minutes=10, seconds=1)
        for raw in ("ch_made-up", used, expired):
            with self.subTest(raw=raw):
                resp = self.anonymous(SEND, {"challenge_token": raw, "method": "email"})
                self.assertRequestError(resp, 401, "challenge_expired")
                self.assertEqual(
                    resp.json()["detail"], "Your sign-in timed out. Please sign in again."
                )


class SignInVerifyTests(SignInTestCase):
    def test_the_code_answers_the_token_pair_and_starts_the_session(self):
        challenge = self.challenge()
        resp = self.finish(challenge, self.sign_in_code(challenge))
        self.assertEqual(resp.status_code, 200, resp.content)
        tokens = resp.json()
        self.assertEqual(set(tokens), {"access", "refresh"})
        session = self.session_of(tokens)
        self.assertEqual(
            (session.device_id, session.device_name, session.two_factor_passed_at),
            (IPAD["HTTP_X_DEVICE_ID"], "iPad", self.now),
        )
        client = APIClient()
        client.credentials(HTTP_AUTHORIZATION=f"Bearer {tokens['access']}", **IPAD)
        self.assertEqual(client.get("/api/v1/auth/me").status_code, 200)

    def test_by_authenticator(self):
        self.add_authenticator(client=self.phone)
        resp = self.finish(self.challenge(), self.code(self.secret), method="authenticator")
        self.assertEqual(resp.status_code, 200, resp.content)

    def test_a_challenge_finishes_once(self):
        challenge = self.challenge()
        self.assertEqual(self.finish(challenge, self.sign_in_code(challenge)).status_code, 200)
        self.add_authenticator(client=self.phone)
        resp = self.finish(challenge, self.code(self.secret), method="authenticator")
        self.assertRequestError(resp, 401, "challenge_expired")

    def test_only_from_the_device_that_signed_in(self):
        challenge = self.challenge()
        code = self.sign_in_code(challenge)
        for device in (IPHONE, NO_DEVICE_ID):
            with self.subTest(device=device["HTTP_X_DEVICE_NAME"]):
                resp = self.finish(challenge, code, device=device)
                self.assertRequestError(resp, 401, "challenge_expired")
        self.assertEqual(self.finish(challenge, code).status_code, 200)

    def test_5_wrong_codes_use_the_challenge_up(self):
        challenge = self.challenge()
        code = self.sign_in_code(challenge)
        for _ in range(5):
            self.assertFieldError(self.finish(challenge, self.wrong(code)), "code", "invalid")
        self.assertEqual(LoginChallenge.objects.get().attempts, 5)
        self.assertRequestError(self.finish(challenge, code), 401, "challenge_expired")

    def test_a_code_not_sent_is_expired_and_does_not_count(self):
        challenge = self.challenge()
        self.assertFieldError(self.finish(challenge, "123456"), "code", "expired")
        self.assertEqual(LoginChallenge.objects.get().attempts, 0)

    def test_after_10_minutes_it_has_expired(self):
        challenge = self.challenge()
        code = self.sign_in_code(challenge)
        self.now += timedelta(minutes=10, seconds=1)
        self.assertRequestError(self.finish(challenge, code), 401, "challenge_expired")

    def test_a_verify_its_you_code_does_not_sign_in(self):
        self.call("post", "/verify/send", {"method": "email", "purpose": ON})
        resp = self.finish(self.challenge(), self.sent_code())
        self.assertFieldError(resp, "code", "expired")

    def test_an_account_disabled_meanwhile_cannot_finish(self):
        challenge = self.challenge()
        code = self.sign_in_code(challenge)
        ConsumerAccount.objects.filter(pk=self.account.pk).update(is_active=False)
        self.assertRequestError(self.finish(challenge, code), 401, "challenge_expired")


# --- the FE guide ---------------------------------------------------------------------

FE_DOC = settings.BASE_DIR / "docs" / "TWO_FACTOR_API.md"
UAE = dt_timezone(timedelta(hours=4))
EXAMPLE_SECRET = "JBSWY3DPEHPK3PXPJBSWY3DPEHPK3PXP"
EXAMPLE_TOKEN = "4f8b2c9e1a7d5e3b9c0f6a2d8e4b1c7f3a9d5e2b0c6"
EXAMPLE_CHALLENGE = "6a1f9e3c7b2d8f4a0e5c1b9d7f3a6e2c8b4d0f5a1e7"


class TwoFactorDocTests(SignInTestCase):
    """docs/TWO_FACTOR_API.md promises every JSON block is a real answer,
    and every authenticator code in its requests a real code. This builds
    the brief's account (2-step on, verified email and phone), sends the
    same requests at the same times and must get the same bodies."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.text = FE_DOC.read_text(encoding="utf-8")
        cls.blocks = [json.loads(b) for b in re.findall(r"```json\n(.*?)\n```", cls.text, re.S)]

    def at(self, hour, minute, second):
        self.now = datetime(2026, 10, 27, hour, minute, second, tzinfo=UAE)

    def example_code(self, expected):
        """The example secret's code now, which the page's request shows."""
        code = self.code(EXAMPLE_SECRET)
        self.assertEqual(code, expected)
        self.assertIn(f'"code": "{code}"', self.text)
        return code

    def add_example_authenticator(self):
        self.at(14, 5, 12)
        body = {"verification_token": self.token(ADD), "purpose": "authenticator_add"}
        with patch.object(two_factor.pyotp, "random_base32", return_value=EXAMPLE_SECRET):
            resp = self.call("post", "/authenticator", body)
        self.assertEqual((resp.status_code, resp.json()), (201, self.blocks[4]))
        resp = self.call("post", "/authenticator/confirm", {"code": self.example_code("377576")})
        self.assertEqual((resp.status_code, resp.json()), (200, self.blocks[5]))

    def test_get(self):
        changed = datetime(2026, 6, 14, 10, 22, 5, tzinfo=UAE)
        ConsumerAccount.objects.filter(pk=self.account.pk).update(password_changed_at=changed)
        self.assertEqual(self.call("get", "").json(), self.blocks[0])

    def test_verify_its_you_and_the_switch(self):
        resp = self.call("post", "/verify/send", {"method": "email", "purpose": "two_step_on"})
        self.assertEqual(resp.json(), self.blocks[1])
        self.add_example_authenticator()
        self.at(14, 6, 40)
        body = {
            "method": "authenticator",
            "purpose": "two_step_off",
            "code": self.example_code("057138"),
        }
        with patch.object(two_factor.secrets, "token_urlsafe", return_value=EXAMPLE_TOKEN):
            resp = self.call("post", "/verify", body)
        self.assertEqual(resp.json(), self.blocks[2])
        body = {"enabled": False, "verification_token": "vt_" + EXAMPLE_TOKEN}
        self.assertEqual(self.call("patch", "/two-factor", body).json(), self.blocks[3])

    def test_signing_in(self):
        self.add_example_authenticator()
        self.at(14, 10, 5)
        with patch.object(two_factor.secrets, "token_urlsafe", return_value=EXAMPLE_CHALLENGE):
            self.assertEqual(self.sign_in().json(), self.blocks[6])
        challenge = "ch_" + EXAMPLE_CHALLENGE
        resp = self.anonymous(SEND, {"challenge_token": challenge, "method": "phone"})
        self.assertEqual(resp.json(), self.blocks[7])
        resp = self.finish(challenge, self.example_code("417231"), method="authenticator")
        self.assertEqual((resp.status_code, set(resp.json())), (200, {"access", "refresh"}))

    def test_the_error_examples(self):
        resp = self.call("patch", "/two-factor", {"enabled": True})
        self.assertEqual(resp.json(), self.blocks[8])
        self.call("post", "/verify/send", {"method": "email", "purpose": "two_step_on"})
        body = {"method": "email", "purpose": "two_step_on", "code": self.wrong(self.sent_code())}
        self.assertEqual(self.call("post", "/verify", body).json(), self.blocks[9])

    def test_no_other_json_blocks(self):
        self.assertEqual(len(self.blocks), 10)

    def test_every_error_message_is_on_the_page(self):
        messages = [
            two_factor.WRONG_CODE[2],
            two_factor.EXPIRED_CODE[2],
            two_factor.METHOD_UNAVAILABLE[2],
        ] + [
            exc.default_detail
            for exc in (
                two_factor.VerificationRequired,
                two_factor.AlreadyAdded,
                two_factor.NoSetup,
                two_factor.TooManyAttempts,
                two_factor.ChallengeExpired,
                two_factor.AuthenticatorUnavailable,
            )
        ]
        for message in messages:
            with self.subTest(message=message):
                self.assertIn(message, self.text)

    def test_every_email_is_on_the_page(self):
        for subject, what in two_factor._NOTICES.values():
            with self.subTest(subject=subject):
                self.assertIn(f"| {subject} | {what} on iPhone 15 in Dubai. |", self.text)
        for subject in (two_factor.SECURITY_SUBJECT, two_factor.SIGN_IN_SUBJECT):
            self.assertIn(f'"{subject}"', self.text)
