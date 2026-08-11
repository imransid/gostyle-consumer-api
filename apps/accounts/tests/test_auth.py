"""
Tests for the register-then-verify auth flow.

THE ORDER MATTERS AND IT IS NOT THE OBVIOUS ONE. `POST /auth/register` creates
the account and returns tokens straight away; the phone or email is proved
afterwards, by an AUTHENTICATED caller, against the contact already stored on
that account. The OTP endpoints therefore require a token and ignore any
destination in the request body.

That inversion is a security fix, and the tests that pin it down are in
`OtpDestinationSecurityTests`: when the destination came from the client, a
logged-in user could make the server send mail to any address they liked.
Reading the caller's own contact instead is what closes it, so those tests
should fail loudly if anyone ever wires the request body back in.

A new account is unverified. `LoginView` refuses an unverified contact, so the
window between registering and verifying is the only time a member holds valid
tokens without being verified — see `apps/accounts/permissions.py` for what
that window means for endpoints added later.
"""

from datetime import timedelta

from django.core.cache import cache
from django.test import SimpleTestCase, TestCase
from django.utils import timezone
from rest_framework.test import APIClient

from apps.accounts.identifiers import InvalidDestination, normalize_destination
from apps.accounts.models import ConsumerAccount, OtpCode

PASSWORD = "Str0ng!Pass"
CODE = "654321"


class AuthTestCase(TestCase):
    """Shared helpers. Every request goes through the real URL conf."""

    def setUp(self):
        self.client = APIClient()
        self.phone = "+8801712345678"
        self.other_phone = "+8801722222222"
        self.email = "kevin@example.com"

        # Rate-limit counters live in the (locmem) cache and persist across
        # test methods; clear them so every test starts in a fresh window.
        cache.clear()
        self.addCleanup(cache.clear)

    # --- registration ----------------------------------------------------
    def register(self, destination_type="phone", destination=None, **overrides):
        payload = {
            "destination_type": destination_type,
            "destination": self.phone if destination is None else destination,
            "full_name": "Kevin Rogers",
            "password": PASSWORD,
            "confirm_password": PASSWORD,
            "gender": "male",
        }
        payload.update(overrides)
        return self.client.post("/api/v1/auth/register", payload, format="json")

    def authenticate(self, response):
        self.client.credentials(HTTP_AUTHORIZATION=f"Bearer {response.data['access']}")

    def anonymous(self):
        self.client.credentials()

    def register_and_authenticate(self, destination_type="phone", destination=None):
        resp = self.register(destination_type, destination)
        assert resp.status_code == 201, resp.data
        self.authenticate(resp)
        return resp

    # --- otp -------------------------------------------------------------
    def request_otp(self, destination_type="phone", destination=None, purpose="register"):
        return self.client.post(
            "/api/v1/auth/otp/request",
            {
                "destination_type": destination_type,
                # Required by the serializer and then DISCARDED by the service,
                # which reads the caller's own contact instead.
                "destination": self.phone if destination is None else destination,
                "purpose": purpose,
            },
            format="json",
        )

    def submit_code(self, code, destination_type="phone", destination=None, purpose="register"):
        return self.client.post(
            "/api/v1/auth/otp/verify",
            {
                "destination_type": destination_type,
                "destination": self.phone if destination is None else destination,
                "purpose": purpose,
                "code": code,
            },
            format="json",
        )

    def live_otp(self, destination, purpose="register"):
        return OtpCode.objects.filter(
            destination=destination, purpose=purpose, consumed_at__isnull=True
        ).latest("created_at")

    def force_code(self, destination, code=CODE, purpose="register"):
        """Overwrite the delivered code with a known one.

        The real code only ever exists in the sender call, so a test cannot
        read it back. Rewriting the hash is the smallest way to get a code the
        test knows without stubbing the OTP machinery it is trying to exercise.
        """
        otp = self.live_otp(destination, purpose)
        otp.set_code(code)
        otp.save()
        return otp

    def verify_own_contact(self, destination_type="phone", destination=None, purpose="register"):
        """Full request → force → verify cycle for the authenticated caller."""
        target = self.phone if destination is None else destination
        self.request_otp(destination_type, target, purpose)
        self.force_code(target, purpose=purpose)
        resp = self.submit_code(CODE, destination_type, target, purpose)
        assert resp.status_code == 200, resp.data
        return resp


class RegistrationTests(AuthTestCase):
    def test_register_creates_the_account_and_returns_tokens(self):
        resp = self.register()
        self.assertEqual(resp.status_code, 201)
        self.assertIn("access", resp.data)
        self.assertIn("refresh", resp.data)

        account = ConsumerAccount.objects.get(phone=self.phone)
        self.assertEqual(account.full_name, "Kevin Rogers")
        self.assertTrue(account.has_usable_password())
        self.assertIsNotNone(account.accepted_terms_at)
        self.assertTrue(account.is_active)
        self.assertIsNone(account.email)

    def test_a_new_account_starts_unverified(self):
        """Tokens now, proof later. The account exists but is not verified."""
        self.register()

        account = ConsumerAccount.objects.get(phone=self.phone)
        self.assertFalse(account.account_verified)
        self.assertIsNone(account.phone_verified_at)
        self.assertIsNone(account.email_verified_at)

    def test_register_sends_no_code(self):
        """Registering does not issue an OTP; the client asks for one after."""
        self.register()
        self.assertEqual(OtpCode.objects.count(), 0)

    def test_tokens_from_register_work_immediately(self):
        resp = self.register()
        self.authenticate(resp)

        me = self.client.get("/api/v1/auth/me")
        self.assertEqual(me.status_code, 200)
        self.assertEqual(me.data["phone"], self.phone)

    def test_register_with_email_only(self):
        resp = self.register("email", self.email)
        self.assertEqual(resp.status_code, 201)

        account = ConsumerAccount.objects.get(email=self.email)
        self.assertIsNone(account.phone)
        self.assertFalse(account.account_verified)

    def test_register_normalizes_the_destination(self):
        """A national number is stored in E.164, not as typed."""
        resp = self.register(destination="01712345678")
        self.assertEqual(resp.status_code, 201)
        self.assertTrue(ConsumerAccount.objects.filter(phone=self.phone).exists())

    def test_register_rejects_a_contact_that_already_has_an_account(self):
        self.assertEqual(self.register().status_code, 201)
        self.anonymous()

        second = self.register()
        self.assertEqual(second.status_code, 400)
        self.assertEqual(ConsumerAccount.objects.filter(phone=self.phone).count(), 1)

    def test_register_rejects_an_invalid_destination(self):
        resp = self.register(destination="not-a-number")
        self.assertEqual(resp.status_code, 400)
        self.assertIn("destination", resp.data)
        self.assertEqual(ConsumerAccount.objects.count(), 0)

    def test_register_rejects_a_weak_password(self):
        resp = self.register(password="weak", confirm_password="weak")
        self.assertEqual(resp.status_code, 400)
        self.assertIn("password", resp.data)
        self.assertEqual(ConsumerAccount.objects.count(), 0)

    def test_register_rejects_mismatched_passwords(self):
        resp = self.register(confirm_password="Different!1")
        self.assertEqual(resp.status_code, 400)
        self.assertIn("confirm_password", resp.data)
        self.assertEqual(ConsumerAccount.objects.count(), 0)

    def test_register_requires_gender(self):
        payload_without_gender = self.register(gender=None)
        self.assertEqual(payload_without_gender.status_code, 400)
        self.assertIn("gender", payload_without_gender.data)


class VerificationTests(AuthTestCase):
    def test_verifying_marks_the_account_and_the_channel(self):
        self.register_and_authenticate()
        resp = self.verify_own_contact()

        self.assertTrue(resp.data["account_exists"])
        account = ConsumerAccount.objects.get(phone=self.phone)
        self.assertTrue(account.account_verified)
        self.assertIsNotNone(account.phone_verified_at)
        # Verifying the phone says nothing about an email.
        self.assertIsNone(account.email_verified_at)

    def test_email_account_verifies_the_email_channel(self):
        self.register_and_authenticate("email", self.email)
        self.verify_own_contact("email", self.email)

        account = ConsumerAccount.objects.get(email=self.email)
        self.assertTrue(account.account_verified)
        self.assertIsNotNone(account.email_verified_at)
        self.assertIsNone(account.phone_verified_at)

    def test_otp_request_requires_authentication(self):
        """No token, no code: there is no account to act on yet."""
        resp = self.request_otp()
        self.assertEqual(resp.status_code, 401)
        self.assertEqual(OtpCode.objects.count(), 0)

    def test_otp_verify_requires_authentication(self):
        resp = self.submit_code(CODE)
        self.assertEqual(resp.status_code, 401)

    def test_requesting_a_channel_the_account_does_not_have_fails(self):
        """An email-only member has no phone on file to send a code to."""
        self.register_and_authenticate("email", self.email)

        resp = self.request_otp("phone", self.phone)
        self.assertEqual(resp.status_code, 400)
        self.assertEqual(OtpCode.objects.count(), 0)

    def test_resend_alias_issues_a_code(self):
        self.register_and_authenticate()

        resp = self.client.post(
            "/api/v1/auth/otp/resend",
            {
                "destination_type": "phone",
                "destination": self.phone,
                "purpose": "register",
            },
            format="json",
        )
        self.assertEqual(resp.status_code, 200)
        self.assertTrue(OtpCode.objects.filter(destination=self.phone).exists())

    def test_request_rejects_an_invalid_destination_shape(self):
        """The body is still validated even though its destination is ignored."""
        self.register_and_authenticate()

        resp = self.request_otp(destination="not-a-number")
        self.assertEqual(resp.status_code, 400)
        self.assertEqual(OtpCode.objects.count(), 0)


class OtpDestinationSecurityTests(AuthTestCase):
    """The property that commit 227d437 exists to create.

    Before it, `destination` came from the request body, so any authenticated
    caller could have the server deliver a code to an address they do not own.
    Now the service reads the caller's own contact and the body is discarded.
    """

    def test_request_ignores_a_destination_the_caller_does_not_own(self):
        self.register_and_authenticate()

        # Name someone else's number. It is a valid number, so the serializer
        # is happy; the service must still send to the caller's own contact.
        resp = self.request_otp(destination=self.other_phone)
        self.assertEqual(resp.status_code, 200)

        self.assertTrue(OtpCode.objects.filter(destination=self.phone).exists())
        self.assertFalse(OtpCode.objects.filter(destination=self.other_phone).exists())

    def test_verify_cannot_spend_another_accounts_code(self):
        """A code minted for one member must be useless to another."""
        # The victim registers and asks for a code.
        victim = self.register_and_authenticate(destination=self.other_phone)
        self.request_otp(destination=self.other_phone)
        victim_code = self.force_code(self.other_phone)

        # The attacker registers their own account and submits the victim's
        # destination together with the code that is live for it.
        self.anonymous()
        self.register_and_authenticate(destination=self.phone)
        resp = self.submit_code(CODE, destination=self.other_phone)

        # Rejected: the lookup used the attacker's own contact, which has no
        # live code at all.
        self.assertEqual(resp.status_code, 400)

        victim_code.refresh_from_db()
        self.assertIsNone(victim_code.consumed_at)

        attacker = ConsumerAccount.objects.get(phone=self.phone)
        victim_account = ConsumerAccount.objects.get(phone=self.other_phone)
        self.assertFalse(attacker.account_verified)
        self.assertFalse(victim_account.account_verified)
        self.assertIsNotNone(victim)  # registration response kept for clarity

    def test_verifying_marks_only_the_caller(self):
        """Two members, one code: the other account is untouched."""
        self.register_and_authenticate(destination=self.other_phone)
        self.anonymous()
        self.register_and_authenticate(destination=self.phone)
        self.verify_own_contact()

        other = ConsumerAccount.objects.get(phone=self.other_phone)
        self.assertFalse(other.account_verified)
        self.assertIsNone(other.phone_verified_at)


class OtpCodeLifecycleTests(AuthTestCase):
    def test_attempts_persist_after_a_wrong_code_and_then_lock_out(self):
        self.register_and_authenticate()
        self.request_otp()
        otp = self.force_code(self.phone)

        resp = self.submit_code("000000")
        self.assertEqual(resp.status_code, 400)

        # Assert on the committed value, not the stale in-memory object.
        otp.refresh_from_db()
        self.assertEqual(otp.attempts, 1)

        for _ in range(OtpCode.MAX_ATTEMPTS - 1):
            self.submit_code("000000")

        otp.refresh_from_db()
        self.assertEqual(otp.attempts, OtpCode.MAX_ATTEMPTS)
        self.assertFalse(otp.is_usable)
        self.assertEqual(self.submit_code("000000").status_code, 400)

    def test_the_right_code_is_refused_once_attempts_are_exhausted(self):
        self.register_and_authenticate()
        self.request_otp()
        self.force_code(self.phone)

        for _ in range(OtpCode.MAX_ATTEMPTS):
            self.submit_code("000000")

        # Locked, not merely wrong.
        self.assertEqual(self.submit_code(CODE).status_code, 400)
        self.assertFalse(
            ConsumerAccount.objects.get(phone=self.phone).account_verified
        )

    def test_a_code_works_once(self):
        self.register_and_authenticate()
        self.verify_own_contact()

        # The same code again: it was consumed, so nothing is live.
        self.assertEqual(self.submit_code(CODE).status_code, 400)

    def test_an_expired_code_is_refused(self):
        self.register_and_authenticate()
        self.request_otp()
        otp = self.force_code(self.phone)
        otp.expires_at = timezone.now() - timedelta(seconds=1)
        otp.save(update_fields=["expires_at"])

        self.assertEqual(self.submit_code(CODE).status_code, 400)
        self.assertFalse(
            ConsumerAccount.objects.get(phone=self.phone).account_verified
        )

    def test_requesting_a_new_code_retires_the_previous_one(self):
        self.register_and_authenticate()
        self.request_otp()
        first = self.live_otp(self.phone)

        cache.clear()  # step past the 60-second cooldown
        self.request_otp()

        first.refresh_from_db()
        self.assertIsNotNone(first.consumed_at)
        self.assertNotEqual(self.live_otp(self.phone).pk, first.pk)

    def test_verify_without_a_live_code_fails(self):
        self.register_and_authenticate()
        self.assertEqual(self.submit_code(CODE).status_code, 400)


class RateLimitTests(AuthTestCase):
    def test_a_second_code_within_the_cooldown_is_refused(self):
        self.register_and_authenticate()
        self.assertEqual(self.request_otp().status_code, 200)

        again = self.request_otp()
        self.assertEqual(again.status_code, 429)
        # The refusal did not mint a second code.
        self.assertEqual(OtpCode.objects.filter(destination=self.phone).count(), 1)


class LoginTests(AuthTestCase):
    def test_login_is_refused_until_the_contact_is_verified(self):
        self.register_and_authenticate()
        self.anonymous()

        resp = self.login()
        self.assertEqual(resp.status_code, 400)

    def test_login_succeeds_after_verification(self):
        self.register_and_authenticate()
        self.verify_own_contact()
        self.anonymous()

        resp = self.login()
        self.assertEqual(resp.status_code, 200)
        self.assertIn("access", resp.data)
        self.assertIn("refresh", resp.data)

    def test_wrong_password_and_unknown_account_look_the_same(self):
        """Neither answer may reveal whether the contact has an account."""
        self.register_and_authenticate()
        self.verify_own_contact()
        self.anonymous()

        wrong = self.login(password="Wr0ng!Pass")
        unknown = self.login(destination=self.other_phone)

        self.assertEqual(wrong.status_code, 400)
        self.assertEqual(unknown.status_code, 400)
        self.assertEqual(wrong.data, unknown.data)

    def login(self, destination=None, password=PASSWORD):
        return self.client.post(
            "/api/v1/auth/login",
            {
                "destination_type": "phone",
                "destination": self.phone if destination is None else destination,
                "password": password,
            },
            format="json",
        )


class ProfileAndLogoutTests(AuthTestCase):
    def test_me_requires_authentication(self):
        self.assertEqual(self.client.get("/api/v1/auth/me").status_code, 401)

    def test_me_returns_the_profile(self):
        self.register_and_authenticate()

        resp = self.client.get("/api/v1/auth/me")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.data["full_name"], "Kevin Rogers")
        self.assertEqual(resp.data["phone"], self.phone)

    def test_me_is_reachable_before_verification(self):
        """The app reads the profile on launch to decide which screen to show,
        so an unverified member must not be locked out of it."""
        self.register_and_authenticate()

        resp = self.client.get("/api/v1/auth/me")
        self.assertEqual(resp.status_code, 200)
        self.assertFalse(
            ConsumerAccount.objects.get(phone=self.phone).account_verified
        )

    def test_logout_blacklists_the_refresh_token(self):
        reg = self.register_and_authenticate()

        out = self.client.post(
            "/api/v1/auth/logout", {"refresh": reg.data["refresh"]}, format="json"
        )
        self.assertEqual(out.status_code, 205)

        refreshed = self.client.post(
            "/api/v1/auth/token/refresh",
            {"refresh": reg.data["refresh"]},
            format="json",
        )
        self.assertEqual(refreshed.status_code, 401)


class DestinationNormalizationTests(SimpleTestCase):
    """Pure: no database, no HTTP."""

    def test_bd_phone_numbers(self):
        self.assertEqual(
            normalize_destination("01712345678", "phone", region="BD"),
            "+8801712345678",
        )
        # A plus-less national number must resolve via the region, not by
        # prepending "+": it becomes +880..., never +1712345678.
        self.assertEqual(
            normalize_destination("1712345678", "phone", region="BD"),
            "+8801712345678",
        )

    def test_email(self):
        self.assertEqual(
            normalize_destination("  Kevin@Example.COM ", "email"),
            "kevin@example.com",
        )
        with self.assertRaises(InvalidDestination):
            normalize_destination("not-an-email", "email")
