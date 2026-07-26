from unittest.mock import patch

from django.core.cache import cache
from django.test import TestCase
from rest_framework.test import APIClient

from apps.accounts.models import ConsumerAccount, OtpCode

PASSWORD = "Str0ng!Pass"


class AuthFlowTests(TestCase):
    def setUp(self):
        self.client = APIClient()
        self.phone = "+8801712345678"
        self.email = "kevin@example.com"

        # Throttle history lives in the (locmem) cache and persists across test
        # methods; clear it so per-destination/IP limits start fresh each test.
        cache.clear()
        self.addCleanup(cache.clear)

        patcher = patch("apps.accounts.services.get_sender")
        self.mock_get_sender = patcher.start()
        self.addCleanup(patcher.stop)

    # --- helpers ---------------------------------------------------------
    def _register(self, identifier):
        resp = self.client.post(
            "/api/v1/auth/register",
            {
                "full_name": "Kevin Rogers",
                "identifier": identifier,
                "password": PASSWORD,
                "confirm_password": PASSWORD,
                "accept_terms": True,
            },
            format="json",
        )
        return resp

    def _set_known_code(self, destination, code):
        otp = OtpCode.objects.filter(destination=destination).latest("created_at")
        otp.set_code(code)
        otp.save()

    def _verify(self, identifier, destination, code):
        self._set_known_code(destination, code)
        return self.client.post(
            "/api/v1/auth/otp/verify",
            {"identifier": identifier, "code": code},
            format="json",
        )

    def _register_and_verify(self, identifier, destination, code="123456"):
        self._register(identifier)
        return self._verify(identifier, destination, code)

    # --- tests -----------------------------------------------------------
    def test_register_creates_account_and_sends_code(self):
        resp = self._register(self.phone)
        self.assertEqual(resp.status_code, 201)
        account = ConsumerAccount.objects.get(phone=self.phone)
        self.assertEqual(account.full_name, "Kevin Rogers")
        self.assertTrue(account.has_usable_password())
        self.assertIsNotNone(account.accepted_terms_at)
        self.assertIsNone(account.phone_verified_at)
        self.assertTrue(OtpCode.objects.filter(destination=self.phone).exists())

    def test_register_rejects_weak_password(self):
        resp = self.client.post(
            "/api/v1/auth/register",
            {
                "full_name": "Kevin",
                "identifier": self.phone,
                "password": "weak",
                "confirm_password": "weak",
                "accept_terms": True,
            },
            format="json",
        )
        self.assertEqual(resp.status_code, 400)
        self.assertIn("password", resp.data)

    def test_register_requires_terms(self):
        resp = self.client.post(
            "/api/v1/auth/register",
            {
                "full_name": "Kevin",
                "identifier": self.phone,
                "password": PASSWORD,
                "confirm_password": PASSWORD,
                "accept_terms": False,
            },
            format="json",
        )
        self.assertEqual(resp.status_code, 400)

    def test_verify_marks_verified_and_returns_tokens(self):
        self._register(self.phone)
        resp = self._verify(self.phone, self.phone, "111111")
        self.assertEqual(resp.status_code, 200)
        self.assertIn("access", resp.data)
        self.assertIn("refresh", resp.data)
        account = ConsumerAccount.objects.get(phone=self.phone)
        self.assertIsNotNone(account.phone_verified_at)

    def test_email_registration_flow(self):
        resp = self._register(self.email)
        self.assertEqual(resp.status_code, 201)
        account = ConsumerAccount.objects.get(email=self.email)
        self.assertIsNone(account.phone)
        verify = self._verify(self.email, self.email, "222222")
        self.assertEqual(verify.status_code, 200)
        account.refresh_from_db()
        self.assertIsNotNone(account.email_verified_at)

    def test_login_requires_verification(self):
        self._register(self.phone)
        resp = self.client.post(
            "/api/v1/auth/login",
            {"identifier": self.phone, "password": PASSWORD},
            format="json",
        )
        self.assertEqual(resp.status_code, 400)

    def test_login_after_verify_succeeds(self):
        self._register(self.phone)
        self._verify(self.phone, self.phone, "333333")
        resp = self.client.post(
            "/api/v1/auth/login",
            {"identifier": self.phone, "password": PASSWORD},
            format="json",
        )
        self.assertEqual(resp.status_code, 200)
        self.assertIn("access", resp.data)

    def test_login_wrong_password_is_generic_error(self):
        self._register(self.phone)
        self._verify(self.phone, self.phone, "444444")
        resp = self.client.post(
            "/api/v1/auth/login",
            {"identifier": self.phone, "password": "Wr0ng!Pass"},
            format="json",
        )
        self.assertEqual(resp.status_code, 400)

    def test_duplicate_verified_registration_rejected(self):
        self._register(self.phone)
        self._verify(self.phone, self.phone, "555555")
        resp = self._register(self.phone)
        self.assertEqual(resp.status_code, 400)

    def test_code_cannot_be_reused(self):
        self._register(self.phone)
        first = self._verify(self.phone, self.phone, "666666")
        self.assertEqual(first.status_code, 200)
        second = self.client.post(
            "/api/v1/auth/otp/verify",
            {"identifier": self.phone, "code": "666666"},
            format="json",
        )
        self.assertEqual(second.status_code, 400)

    def test_wrong_code_locks_after_five_attempts(self):
        self._register(self.phone)
        self._set_known_code(self.phone, "777777")
        for _ in range(5):
            self.client.post(
                "/api/v1/auth/otp/verify",
                {"identifier": self.phone, "code": "000000"},
                format="json",
            )
        resp = self.client.post(
            "/api/v1/auth/otp/verify",
            {"identifier": self.phone, "code": "777777"},
            format="json",
        )
        self.assertEqual(resp.status_code, 400)

    # --- password reset --------------------------------------------------
    def _forgot(self, identifier):
        return self.client.post(
            "/api/v1/auth/password/forgot", {"identifier": identifier}, format="json"
        )

    def _reset(self, identifier, destination, code, new_password):
        otp = OtpCode.objects.filter(destination=destination, purpose="reset").latest(
            "created_at"
        )
        otp.set_code(code)
        otp.save()
        return self.client.post(
            "/api/v1/auth/password/reset",
            {
                "identifier": identifier,
                "code": code,
                "new_password": new_password,
                "confirm_password": new_password,
            },
            format="json",
        )

    def test_forgot_sends_reset_code_for_existing_account(self):
        self._register_and_verify(self.phone, self.phone)
        resp = self._forgot(self.phone)
        self.assertEqual(resp.status_code, 200)
        self.assertTrue(
            OtpCode.objects.filter(destination=self.phone, purpose="reset").exists()
        )

    def test_forgot_unknown_account_is_generic_and_sends_nothing(self):
        resp = self._forgot("+8801999999999")
        self.assertEqual(resp.status_code, 200)
        self.assertFalse(OtpCode.objects.filter(purpose="reset").exists())

    def test_reset_updates_password_and_allows_login(self):
        self._register_and_verify(self.phone, self.phone)
        self._forgot(self.phone)
        new_password = "N3w!Passw0rd"
        resp = self._reset(self.phone, self.phone, "424242", new_password)
        self.assertEqual(resp.status_code, 200)

        old = self.client.post(
            "/api/v1/auth/login",
            {"identifier": self.phone, "password": PASSWORD},
            format="json",
        )
        self.assertEqual(old.status_code, 400)
        new = self.client.post(
            "/api/v1/auth/login",
            {"identifier": self.phone, "password": new_password},
            format="json",
        )
        self.assertEqual(new.status_code, 200)

    def test_reset_rejects_wrong_code(self):
        self._register_and_verify(self.phone, self.phone)
        self._forgot(self.phone)
        # Set the real code but submit a different one.
        otp = OtpCode.objects.filter(destination=self.phone, purpose="reset").latest(
            "created_at"
        )
        otp.set_code("111111")
        otp.save()
        resp = self.client.post(
            "/api/v1/auth/password/reset",
            {
                "identifier": self.phone,
                "code": "999999",
                "new_password": "N3w!Passw0rd",
                "confirm_password": "N3w!Passw0rd",
            },
            format="json",
        )
        self.assertEqual(resp.status_code, 400)

    def test_reset_enforces_password_policy(self):
        self._register_and_verify(self.phone, self.phone)
        self._forgot(self.phone)
        resp = self._reset(self.phone, self.phone, "424242", "weak")
        self.assertEqual(resp.status_code, 400)

    def test_reset_revokes_existing_refresh_tokens(self):
        verify = self._register_and_verify(self.phone, self.phone)
        old_refresh = verify.data["refresh"]
        self._forgot(self.phone)
        self._reset(self.phone, self.phone, "424242", "N3w!Passw0rd")

        resp = self.client.post(
            "/api/v1/auth/token/refresh", {"refresh": old_refresh}, format="json"
        )
        self.assertEqual(resp.status_code, 401)

    # --- profile & logout ------------------------------------------------
    def _auth(self, access):
        self.client.credentials(HTTP_AUTHORIZATION=f"Bearer {access}")

    def test_register_response_includes_resend_metadata(self):
        resp = self._register(self.phone)
        self.assertEqual(resp.data["retry_after"], 60)
        self.assertEqual(resp.data["expires_in"], 300)
        self.assertEqual(resp.data["destination"], "+88********678")

    def test_me_requires_authentication(self):
        resp = self.client.get("/api/v1/auth/me")
        self.assertEqual(resp.status_code, 401)

    def test_me_returns_profile(self):
        verify = self._register_and_verify(self.phone, self.phone)
        self._auth(verify.data["access"])
        resp = self.client.get("/api/v1/auth/me")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.data["full_name"], "Kevin Rogers")
        self.assertEqual(resp.data["phone"], self.phone)

    def test_me_updates_full_name_but_not_phone(self):
        verify = self._register_and_verify(self.phone, self.phone)
        self._auth(verify.data["access"])
        resp = self.client.patch(
            "/api/v1/auth/me",
            {"full_name": "Kevin B. Rogers", "phone": "+8801700000000"},
            format="json",
        )
        self.assertEqual(resp.status_code, 200)
        account = ConsumerAccount.objects.get(phone=self.phone)
        self.assertEqual(account.full_name, "Kevin B. Rogers")
        self.assertEqual(account.phone, self.phone)  # read-only, unchanged

    def test_logout_blacklists_refresh_token(self):
        verify = self._register_and_verify(self.phone, self.phone)
        self._auth(verify.data["access"])
        resp = self.client.post(
            "/api/v1/auth/logout", {"refresh": verify.data["refresh"]}, format="json"
        )
        self.assertEqual(resp.status_code, 205)

        refreshed = self.client.post(
            "/api/v1/auth/token/refresh",
            {"refresh": verify.data["refresh"]},
            format="json",
        )
        self.assertEqual(refreshed.status_code, 401)
