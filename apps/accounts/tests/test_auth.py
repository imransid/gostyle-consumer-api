from datetime import timedelta

from django.core.cache import cache
from django.test import TestCase
from django.utils import timezone
from rest_framework.test import APIClient

from apps.accounts.identifiers import InvalidIdentifier, normalize_identifier
from apps.accounts.models import ConsumerAccount, OtpCode, VerificationToken

PASSWORD = "Str0ng!Pass"


class AuthFlowTests(TestCase):
    def setUp(self):
        self.client = APIClient()
        self.phone = "+8801712345678"
        self.email = "kevin@example.com"

        # Rate-limit / throttle counters live in the (locmem) cache and persist
        # across test methods; clear them so every test starts from a clean
        # window.
        cache.clear()
        self.addCleanup(cache.clear)

    # --- helpers ---------------------------------------------------------
    def _request(self, channel, identifier, purpose="register"):
        return self.client.post(
            "/api/v1/auth/otp/request",
            {"channel": channel, "identifier": identifier, "purpose": purpose},
            format="json",
        )

    def _live_otp(self, identifier, purpose="register"):
        return (
            OtpCode.objects.filter(
                identifier=identifier, purpose=purpose, consumed_at__isnull=True
            )
            .latest("created_at")
        )

    def _set_code(self, identifier, code, purpose="register"):
        otp = self._live_otp(identifier, purpose)
        otp.set_code(code)
        otp.save()
        return otp

    def _verify(self, channel, identifier, code, purpose="register"):
        return self.client.post(
            "/api/v1/auth/otp/verify",
            {
                "channel": channel,
                "identifier": identifier,
                "purpose": purpose,
                "code": code,
            },
            format="json",
        )

    def _register(self, token, full_name="Kevin Rogers", password=PASSWORD, accept_terms=True):
        return self.client.post(
            "/api/v1/auth/register",
            {
                "verification_token": token,
                "full_name": full_name,
                "password": password,
                "confirm_password": password,
                "accept_terms": accept_terms,
            },
            format="json",
        )

    def _issue_token(self, channel, identifier, code="654321", purpose="register"):
        """Run request + verify and return the raw verification token."""
        self._request(channel, identifier, purpose)
        self._set_code(identifier, code, purpose)
        resp = self._verify(channel, identifier, code, purpose)
        assert resp.status_code == 200, resp.data
        return resp.data["verification_token"]

    def _full_register(self, channel, identifier):
        token = self._issue_token(channel, identifier)
        return self._register(token)

    # --- required: attempts + lockout ------------------------------------
    def test_attempts_persist_after_wrong_code_and_lockout(self):
        self._request("phone", self.phone)
        otp = self._set_code(self.phone, "654321")

        resp = self._verify("phone", self.phone, "000000")
        self.assertEqual(resp.status_code, 400)

        # Assert on the committed DB value, not the stale in-memory object.
        otp.refresh_from_db()
        self.assertEqual(otp.attempts, 1)

        # Exhaust the remaining attempts.
        for _ in range(OtpCode.MAX_ATTEMPTS - 1):
            self._verify("phone", self.phone, "000000")

        otp.refresh_from_db()
        self.assertEqual(otp.attempts, OtpCode.MAX_ATTEMPTS)
        self.assertFalse(otp.is_usable)

        # A further attempt is now locked out.
        locked = self._verify("phone", self.phone, "000000")
        self.assertEqual(locked.status_code, 400)

    def test_correct_code_rejected_once_attempts_exhausted(self):
        self._request("phone", self.phone)
        self._set_code(self.phone, "654321")

        for _ in range(OtpCode.MAX_ATTEMPTS):
            self._verify("phone", self.phone, "000000")

        # The real code no longer works: the code is locked, not just wrong.
        resp = self._verify("phone", self.phone, "654321")
        self.assertEqual(resp.status_code, 400)
        self.assertFalse(VerificationToken.objects.filter(identifier=self.phone).exists())

    # --- required: verification token semantics --------------------------
    def test_verification_token_is_single_use(self):
        token = self._issue_token("phone", self.phone)

        first = self._register(token)
        self.assertEqual(first.status_code, 201)

        second = self._register(token)
        self.assertEqual(second.status_code, 400)
        self.assertEqual(ConsumerAccount.objects.filter(phone=self.phone).count(), 1)

    def test_password_reset_token_cannot_register(self):
        token = self._issue_token("phone", self.phone, purpose="password_reset")

        resp = self._register(token)
        self.assertEqual(resp.status_code, 400)
        self.assertFalse(ConsumerAccount.objects.filter(phone=self.phone).exists())

    def test_register_with_expired_token_fails(self):
        token = self._issue_token("phone", self.phone)

        vt = VerificationToken.objects.get(identifier=self.phone)
        vt.expires_at = timezone.now() - timedelta(seconds=1)
        vt.save(update_fields=["expires_at"])

        resp = self._register(token)
        self.assertEqual(resp.status_code, 400)
        self.assertFalse(ConsumerAccount.objects.filter(phone=self.phone).exists())

    # --- required: no account before verification ------------------------
    def test_no_account_created_by_request_alone(self):
        resp = self._request("phone", self.phone)
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(ConsumerAccount.objects.count(), 0)
        self.assertTrue(
            OtpCode.objects.filter(identifier=self.phone, purpose="register").exists()
        )

    # --- required: identifier normalization ------------------------------
    def test_normalize_identifier_bd_phone(self):
        self.assertEqual(
            normalize_identifier("01712345678", "phone", region="BD"),
            "+8801712345678",
        )
        # A plus-less national number must resolve via the region, not by
        # prepending "+": it becomes +880..., never +1712345678.
        self.assertEqual(
            normalize_identifier("1712345678", "phone", region="BD"),
            "+8801712345678",
        )

    def test_normalize_identifier_email(self):
        self.assertEqual(
            normalize_identifier("  Kevin@Example.COM ", "email"),
            "kevin@example.com",
        )
        with self.assertRaises(InvalidIdentifier):
            normalize_identifier("not-an-email", "email")

    # --- required: no user enumeration -----------------------------------
    def test_request_response_identical_for_known_and_unknown(self):
        known = "+8801711111111"
        unknown = "+8801722222222"
        ConsumerAccount.objects.create(
            phone=known, phone_verified_at=timezone.now()
        )

        r_known = self._request("phone", known)
        r_unknown = self._request("phone", unknown)

        self.assertEqual(r_known.status_code, r_unknown.status_code)
        self.assertEqual(r_known.status_code, 200)
        # Byte-identical: the body reveals nothing about account existence.
        self.assertEqual(r_known.content, r_unknown.content)

    # --- register / verify happy paths -----------------------------------
    def test_full_phone_registration_returns_tokens_and_marks_verified(self):
        resp = self._full_register("phone", self.phone)
        self.assertEqual(resp.status_code, 201)
        self.assertIn("access", resp.data)
        self.assertIn("refresh", resp.data)

        account = ConsumerAccount.objects.get(phone=self.phone)
        self.assertEqual(account.full_name, "Kevin Rogers")
        self.assertTrue(account.has_usable_password())
        self.assertIsNotNone(account.accepted_terms_at)
        self.assertIsNotNone(account.phone_verified_at)
        self.assertIsNone(account.email)

    def test_full_email_registration(self):
        resp = self._full_register("email", self.email)
        self.assertEqual(resp.status_code, 201)

        account = ConsumerAccount.objects.get(email=self.email)
        self.assertIsNone(account.phone)
        self.assertIsNotNone(account.email_verified_at)
        self.assertIsNone(account.phone_verified_at)

    def test_verify_returns_account_exists_flag(self):
        self._full_register("phone", self.phone)
        # A second request/verify for the now-registered contact reports it.
        cache.clear()  # clear the cooldown from the first request
        self._request("phone", self.phone)
        self._set_code(self.phone, "654321")
        resp = self._verify("phone", self.phone, "654321")
        self.assertEqual(resp.status_code, 200)
        self.assertTrue(resp.data["account_exists"])

    def test_register_rejects_weak_password(self):
        token = self._issue_token("phone", self.phone)
        resp = self._register(token, password="weak")
        self.assertEqual(resp.status_code, 400)
        self.assertIn("password", resp.data)
        self.assertFalse(ConsumerAccount.objects.filter(phone=self.phone).exists())

    def test_register_requires_accepted_terms(self):
        token = self._issue_token("phone", self.phone)
        resp = self._register(token, accept_terms=False)
        self.assertEqual(resp.status_code, 400)
        self.assertFalse(ConsumerAccount.objects.filter(phone=self.phone).exists())

    def test_verify_without_active_code_fails(self):
        resp = self._verify("phone", self.phone, "654321")
        self.assertEqual(resp.status_code, 400)

    def test_request_rejects_invalid_identifier(self):
        resp = self._request("phone", "not-a-number")
        self.assertEqual(resp.status_code, 400)
        self.assertEqual(OtpCode.objects.count(), 0)

    def test_resend_alias_issues_a_code(self):
        resp = self.client.post(
            "/api/v1/auth/otp/resend",
            {"channel": "phone", "identifier": self.phone, "purpose": "register"},
            format="json",
        )
        self.assertEqual(resp.status_code, 200)
        self.assertTrue(OtpCode.objects.filter(identifier=self.phone).exists())

    # --- login (password) kept -------------------------------------------
    def test_login_after_register_succeeds(self):
        self._full_register("phone", self.phone)
        resp = self.client.post(
            "/api/v1/auth/login",
            {"channel": "phone", "identifier": self.phone, "password": PASSWORD},
            format="json",
        )
        self.assertEqual(resp.status_code, 200)
        self.assertIn("access", resp.data)

    def test_login_wrong_password_is_generic_error(self):
        self._full_register("phone", self.phone)
        resp = self.client.post(
            "/api/v1/auth/login",
            {"channel": "phone", "identifier": self.phone, "password": "Wr0ng!Pass"},
            format="json",
        )
        self.assertEqual(resp.status_code, 400)

    # --- profile / logout kept -------------------------------------------
    def test_me_requires_authentication(self):
        resp = self.client.get("/api/v1/auth/me")
        self.assertEqual(resp.status_code, 401)

    def test_me_returns_profile_after_register(self):
        reg = self._full_register("phone", self.phone)
        self.client.credentials(HTTP_AUTHORIZATION=f"Bearer {reg.data['access']}")
        resp = self.client.get("/api/v1/auth/me")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.data["full_name"], "Kevin Rogers")
        self.assertEqual(resp.data["phone"], self.phone)

    def test_logout_blacklists_refresh_token(self):
        reg = self._full_register("phone", self.phone)
        self.client.credentials(HTTP_AUTHORIZATION=f"Bearer {reg.data['access']}")
        resp = self.client.post(
            "/api/v1/auth/logout", {"refresh": reg.data["refresh"]}, format="json"
        )
        self.assertEqual(resp.status_code, 205)

        refreshed = self.client.post(
            "/api/v1/auth/token/refresh",
            {"refresh": reg.data["refresh"]},
            format="json",
        )
        self.assertEqual(refreshed.status_code, 401)
