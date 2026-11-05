"""
Sign in with Google, and one account for both ways in (GOOGLE_SIGN_IN_V1,
apps/accounts/google.py, docs/GOOGLE_SIGN_IN_API.md).

Google is never called: the tokens are signed here with a test RSA key,
and google._keys answers that key, as Google's key list would.
"""

import json
import re
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import jwt
from cryptography.hazmat.primitives.asymmetric import rsa
from django.conf import settings
from django.core import mail
from django.core.cache import cache
from django.test import SimpleTestCase, TestCase, override_settings
from django.utils import timezone
from rest_framework.test import APIClient
from rest_framework_simplejwt.token_blacklist.models import BlacklistedToken, OutstandingToken
from rest_framework_simplejwt.tokens import AccessToken, RefreshToken

from apps.accounts import google, push_api, two_factor
from apps.accounts.models import (
    ConsumerAccount,
    SocialIdentity,
    TwoFactorPurpose,
    UserSession,
    VerificationToken,
)

from .test_two_factor import IPAD, IPHONE, FakeSender

WEB = "1002425773215-g4grmqc4as5dut3cncd19lkd7ai5qk0d.apps.googleusercontent.com"
ANDROID = "1002425773215-llauqr34tk3qdnld4ucn56vlqt2tsfpb.apps.googleusercontent.com"
IOS = "1002425773215-ks6giaqd84uoiuljlqc12qr971fiqedr.apps.googleusercontent.com"
SUB = "109876543210987654321"
OTHER_SUB = "100000000000000000001"
EMAIL = "kevin.new@gmail.com"
PHONE = "+971501234567"
PASSWORD = "Str0ng!Pass"
NEW_PASSWORD = "An0ther-Str0ng-Pass!"

GOOGLE_URL = "/api/v1/auth/google"
LOGIN = "/api/v1/auth/login"
ME = "/api/v1/auth/me"
SET = "/api/v1/auth/password/set"
DELETE = "/api/v1/auth/me/delete"
VERIFY_SEND = "/api/v1/me/security/verify/send"
VERIFY = "/api/v1/me/security/verify"
REFRESH = "/api/v1/auth/token/refresh"

FE_GUIDE = Path(settings.BASE_DIR) / "docs" / "GOOGLE_SIGN_IN_API.md"

KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)
OTHER_KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)


def mint(key=KEY, algorithm="RS256", **claims):
    """A Google ID token. A claim given as None is left out."""
    now = int(time.time())
    payload = {
        "iss": "https://accounts.google.com",
        "azp": WEB,
        "aud": WEB,
        "sub": SUB,
        "email": EMAIL,
        "email_verified": True,
        "name": "Kevin New",
        "iat": now,
        "exp": now + 3600,
        **claims,
    }
    payload = {k: v for k, v in payload.items() if v is not None}
    return jwt.encode(payload, key, algorithm=algorithm, headers={"kid": "test-key"})


class FakeKeys:
    """Google's key list, holding only KEY."""

    def get_signing_key_from_jwt(self, token):
        jwt.get_unverified_header(token)  # a token that is no JWT fails here, as at Google's
        return SimpleNamespace(key=KEY.public_key())


class KeysDown:
    def get_signing_key_from_jwt(self, token):
        raise jwt.PyJWKClientConnectionError("Fail to fetch data from the url")


def with_fake_keys(test_case):
    patcher = patch.object(google, "_keys", return_value=FakeKeys())
    patcher.start()
    test_case.addCleanup(patcher.stop)


# --- the ID token ---------------------------------------------------------------------


class VerifyTests(SimpleTestCase):
    def setUp(self):
        with_fake_keys(self)

    def test_a_good_token_gives_who_it_is(self):
        user = google.verify(mint(email="Kevin.New@Gmail.com"))
        self.assertEqual(
            (user.subject, user.email, user.email_verified, user.name),
            (SUB, EMAIL, True, "Kevin New"),
        )

    def test_both_client_ids_and_both_issuers_pass(self):
        for claims in (
            {"aud": WEB},
            {"aud": IOS},
            {"iss": "accounts.google.com"},
            {"iss": "https://accounts.google.com"},
        ):
            with self.subTest(claims=claims):
                self.assertEqual(google.verify(mint(**claims)).subject, SUB)

    def test_anything_off_is_invalid_token(self):
        now = int(time.time())
        bad = {
            "another key": mint(key=OTHER_KEY),
            "expired": mint(iat=now - 7200, exp=now - 60),
            "expired a second ago": mint(iat=now - 3601, exp=now - 1),
            "another app": mint(aud="1234-other.apps.googleusercontent.com"),
            "the android client": mint(aud=ANDROID),
            "not Google": mint(iss="https://evil.example.com"),
            "no sub": mint(sub=None),
            "an empty sub": mint(sub=""),
            "no expiry": mint(exp=None),
            "no audience": mint(aud=None),
            "HS256": mint(key="a shared secret of thirty-two bytes!!", algorithm="HS256"),
            "not a JWT": "not-a-token",
        }
        for why, token in bad.items():
            with self.subTest(why=why), self.assertRaises(google.InvalidToken):
                google.verify(token)

    def test_googles_keys_out_of_reach_is_a_503_not_the_customers_fault(self):
        with patch.object(google, "_keys", return_value=KeysDown()):
            with self.assertRaises(google.GoogleUnavailable):
                google.verify(mint())

    def test_email_verified_false_and_no_email(self):
        user = google.verify(mint(email_verified=False))
        self.assertFalse(user.email_verified)
        user = google.verify(mint(email=None, email_verified=None, name=None))
        self.assertEqual((user.email, user.email_verified, user.name), ("", False, ""))

    def test_the_audiences_are_exactly_the_two_from_the_brief(self):
        self.assertEqual(google.client_ids(), [WEB, IOS])

    def test_only_the_briefs_checks_iat_is_not_one_of_them(self):
        now = int(time.time())
        for claims in ({"iat": now + 600}, {"iat": None}):
            with self.subTest(claims=claims):
                self.assertEqual(google.verify(mint(**claims)).subject, SUB)

    def test_a_fresh_token_is_one_from_the_last_10_minutes(self):
        now = int(time.time())
        self.assertEqual(google.verify_fresh(mint(iat=now - 590)).subject, SUB)
        for token in (mint(iat=now - 610), mint(iat=None), mint(key=OTHER_KEY)):
            with self.assertRaises(google.StaleIdToken):
                google.verify_fresh(token)


# --- POST /auth/google ----------------------------------------------------------------


@override_settings(GOOGLE_SIGN_IN_V1=True, DEVICE_SESSIONS_V1=True, OTP_SENDER="live")
class GoogleTestCase(TestCase):
    def setUp(self):
        cache.clear()
        self.addCleanup(cache.clear)
        with_fake_keys(self)
        # Ending a session asks the push service to forget its phone.
        push = patch.object(push_api, "unregister_session", return_value=(200, {}))
        push.start()
        self.addCleanup(push.stop)

    def post(self, path, body, device=IPHONE, client=None):
        with self.captureOnCommitCallbacks(execute=True):
            return (client or APIClient()).post(path, body, format="json", **device)

    def google(self, token=None, device=IPHONE):
        return self.post(GOOGLE_URL, {"id_token": token or mint()}, device)

    def signed_in(self, tokens, device=IPHONE):
        client = APIClient()
        client.credentials(HTTP_AUTHORIZATION=f"Bearer {tokens['access']}", **device)
        return client

    def me(self, client):
        resp = client.get(ME)
        self.assertEqual(resp.status_code, 200, resp.content)
        return resp.json()

    def account(self, email=EMAIL, phone=None, verified=True, password=PASSWORD, **fields):
        fields.setdefault("is_active", True)
        account = ConsumerAccount(email=email, phone=phone, full_name="Kevin Owner", **fields)
        if verified:
            account.email_verified_at = timezone.now()
            account.account_verified = True
        if password:
            account.set_password(password)
            account.password_changed_at = timezone.now()
        else:
            account.set_unusable_password()
        account.save()
        return account

    def link(self, account, sub=SUB, email=EMAIL):
        return SocialIdentity.objects.create(
            account=account, provider="google", subject=sub, email=email
        )

    def subjects(self):
        return [m.subject for m in mail.outbox]

    def assertRefusal(self, resp, status_code, code):
        """An error of the whole request: its code at the top, errors empty."""
        self.assertEqual(resp.status_code, status_code, resp.content)
        body = resp.json()
        self.assertEqual((body["code"], body["errors"]), (code, []), body)
        self.assertIsInstance(body["detail"], str)


class SwitchTests(GoogleTestCase):
    def test_off_both_routes_are_404_and_nothing_else_changes(self):
        account = self.account(password=None)
        self.link(account)
        with override_settings(GOOGLE_SIGN_IN_V1=False):
            self.assertEqual(self.google().status_code, 404)
            self.assertEqual(self.post(SET, {}).status_code, 404)
            # Login answers what it answers today for an account with no password.
            body = {"destination_type": "email", "destination": EMAIL, "password": "x"}
            resp = self.post(LOGIN, body)
            self.assertEqual(resp.status_code, 422, resp.content)
            self.assertEqual(resp.json()["detail"], "Invalid credentials.")
        self.assertEqual(SocialIdentity.objects.count(), 1)


class NewAccountTests(GoogleTestCase):
    def test_a_new_google_user_gets_a_new_account(self):
        resp = self.google()
        self.assertEqual(resp.status_code, 200, resp.content)
        body = resp.json()
        self.assertEqual(set(body), {"access", "refresh", "is_new_user"})
        self.assertIs(body["is_new_user"], True)

        account = ConsumerAccount.objects.get()
        self.assertEqual(
            (account.full_name, account.email, account.phone), ("Kevin New", EMAIL, None)
        )
        self.assertIsNotNone(account.email_verified_at)
        self.assertTrue(account.account_verified)
        self.assertFalse(account.has_usable_password())
        self.assertIsNone(account.password_changed_at)
        self.assertEqual(account.gender, "")
        self.assertIsNotNone(account.accepted_terms_at)
        link = SocialIdentity.objects.get()
        self.assertEqual((link.account, link.subject, link.email), (account, SUB, EMAIL))

        # A real session, as a password sign-in has.
        session = UserSession.objects.get(pk=AccessToken(body["access"])["sid"])
        self.assertEqual((session.user, session.device_name), (account, "iPhone 15"))
        # As a password sign-in: the new sign-in alert. No other email.
        self.assertEqual(self.subjects(), ["Go Style: new sign-in to your account"])

    def test_the_name_falls_back_to_the_email_before_the_at(self):
        self.google(mint(name=None))
        self.assertEqual(ConsumerAccount.objects.get().full_name, "kevin.new")

    def test_a_phone_only_account_is_nothing_to_match(self):
        phone_only = self.account(email=None, phone=PHONE, verified=False)
        phone_only.phone_verified_at = timezone.now()
        phone_only.save()
        self.assertIs(self.google().json()["is_new_user"], True)
        self.assertEqual(ConsumerAccount.objects.count(), 2)

    def test_the_same_google_account_then_signs_in(self):
        first = self.google().json()
        mail.outbox.clear()
        resp = self.google(device=IPAD)
        self.assertEqual(resp.status_code, 200, resp.content)
        self.assertIs(resp.json()["is_new_user"], False)
        self.assertEqual(ConsumerAccount.objects.count(), 1)
        same = AccessToken(first["access"])["consumer_id"]
        self.assertEqual(AccessToken(resp.json()["access"])["consumer_id"], same)
        # A sign-in from a new device: the new sign-in alert, as after a password.
        self.assertEqual(self.subjects(), ["Go Style: new sign-in to your account"])

    def test_google_did_not_verify_the_email_and_nobody_has_it(self):
        resp = self.google(mint(email_verified=False))
        self.assertIs(resp.json()["is_new_user"], True)
        account = ConsumerAccount.objects.get()
        self.assertEqual(account.email, EMAIL)
        self.assertIsNone(account.email_verified_at)
        self.assertEqual(self.subjects(), [])  # no verified address to welcome


class LinkedTests(GoogleTestCase):
    def test_found_by_its_sub_even_after_the_email_changed(self):
        account = self.account(email="kevin@work.example", password=None)
        self.link(account, email="old@gmail.com")
        resp = self.google(mint(email="kevin.renamed@gmail.com"))
        self.assertEqual(resp.status_code, 200, resp.content)
        self.assertIs(resp.json()["is_new_user"], False)
        self.assertEqual(AccessToken(resp.json()["access"])["consumer_id"], str(account.pk))
        # google_email follows the Google account; the account's email stays.
        link = SocialIdentity.objects.get()
        self.assertEqual(link.email, "kevin.renamed@gmail.com")
        self.assertIsNotNone(link.last_used_at)
        account.refresh_from_db()
        self.assertEqual(account.email, "kevin@work.example")

    def test_a_scheduled_deletion_signs_in_as_normal(self):
        account = self.account(password=None)
        self.link(account)
        account.request_deletion("privacy")
        resp = self.google()
        self.assertEqual(resp.status_code, 200, resp.content)
        self.assertIsNotNone(self.me(self.signed_in(resp.json()))["purge_at"])

    def test_a_disabled_account_is_refused_as_login_refuses_it(self):
        account = self.account(password=None, is_active=False)
        self.link(account)
        resp = self.google()
        self.assertEqual(resp.status_code, 422, resp.content)
        self.assertEqual(resp.json()["detail"], "Account disabled.")

    def test_after_the_purge_the_same_google_account_starts_again(self):
        account = self.account(password=None)
        self.link(account)
        account.purge()
        self.assertFalse(SocialIdentity.objects.exists())
        resp = self.google()
        self.assertIs(resp.json()["is_new_user"], True)
        self.assertNotEqual(AccessToken(resp.json()["access"])["consumer_id"], str(account.pk))


class SameEmailTests(GoogleTestCase):
    def test_a_verified_email_is_linked_and_its_password_keeps_working(self):
        account = self.account(email="Kevin.New@gmail.com")
        resp = self.google()
        self.assertEqual(resp.status_code, 200, resp.content)
        self.assertIs(resp.json()["is_new_user"], False)
        self.assertEqual(AccessToken(resp.json()["access"])["consumer_id"], str(account.pk))
        self.assertEqual(SocialIdentity.objects.get().account, account)
        account.refresh_from_db()
        self.assertTrue(account.check_password(PASSWORD))

        # The owner hears of the new way in.
        (linked,) = [m for m in mail.outbox if "Google" in m.subject]
        self.assertEqual(linked.to, ["Kevin.New@gmail.com"])
        self.assertEqual(linked.subject, "Go Style: Google sign-in was added to your account")
        self.assertTrue(
            linked.body.startswith(
                "Google sign-in was added to your Go Style account on iPhone 15. "
                "From now on, Continue with Google signs you in to it.\n\n"
                "If this was not you, contact support straight away."
            ),
            linked.body,
        )

    def test_after_the_link_email_and_password_still_sign_in(self):
        self.account()
        self.assertIs(self.google().json()["is_new_user"], False)
        body = {"destination_type": "email", "destination": EMAIL, "password": PASSWORD}
        self.assertEqual(self.post(LOGIN, body, IPAD).status_code, 200)

    def test_an_email_never_verified_is_taken_over(self):
        # Someone signed up with this address before its owner did.
        body = {
            "destination_type": "email",
            "destination": EMAIL,
            "full_name": "Not Kevin",
            "password": PASSWORD,
            "confirm_password": PASSWORD,
            "gender": "male",
        }
        squatter = self.post("/api/v1/auth/register", body, IPAD)
        self.assertEqual(squatter.status_code, 201, squatter.content)
        account = ConsumerAccount.objects.get()

        resp = self.google()
        self.assertEqual(resp.status_code, 200, resp.content)
        self.assertIs(resp.json()["is_new_user"], False)
        account.refresh_from_db()
        self.assertIsNotNone(account.email_verified_at)
        self.assertFalse(account.has_usable_password())
        self.assertIsNone(account.password_changed_at)

        # The squatter's session is over, and its tokens no longer work.
        old = UserSession.objects.get(device_id=IPAD["HTTP_X_DEVICE_ID"])
        self.assertIsNotNone(old.ended_at)
        self.assertEqual(self.signed_in(squatter.json(), IPAD).get(ME).status_code, 401)
        refresh = self.post(REFRESH, {"refresh": squatter.json()["refresh"]}, IPAD)
        self.assertEqual(refresh.status_code, 401, refresh.content)
        # The new sign-in's is not.
        self.assertEqual(self.signed_in(resp.json()).get(ME).status_code, 200)
        # Its password never chose no longer opens it.
        login = {"destination_type": "email", "destination": EMAIL, "password": PASSWORD}
        self.assertRefusal(self.post(LOGIN, login), 401, "password_not_set")

    def test_the_takeover_revokes_refresh_tokens_with_no_session_too(self):
        account = self.account(verified=False)
        with override_settings(DEVICE_SESSIONS_V1=False):
            RefreshToken.for_user(account)
        self.google()
        outstanding = OutstandingToken.objects.filter(user=account)
        self.assertEqual(
            BlacklistedToken.objects.filter(token__in=outstanding).count(),
            outstanding.count() - 1,  # all but the Google sign-in's own
        )

    def test_an_unverified_email_is_taken_over_even_with_a_verified_phone(self):
        account = self.account(phone=PHONE, verified=False)
        account.phone_verified_at = timezone.now()
        account.save()
        resp = self.google()
        self.assertEqual(resp.status_code, 200, resp.content)
        self.assertIs(resp.json()["is_new_user"], False)
        account.refresh_from_db()
        self.assertIsNotNone(account.email_verified_at)
        self.assertFalse(account.has_usable_password())
        self.assertEqual(SocialIdentity.objects.get().account, account)

    def test_google_did_not_verify_the_email_and_an_account_has_it(self):
        self.account()
        self.assertRefusal(self.google(mint(email_verified=False)), 409, "email_in_use")
        self.assertFalse(SocialIdentity.objects.exists())

    def test_one_google_account_per_account(self):
        account = self.account()
        self.link(account, sub=OTHER_SUB)
        self.assertRefusal(self.google(), 409, "google_already_linked")
        self.assertEqual(SocialIdentity.objects.get().subject, OTHER_SUB)

    def test_a_disabled_account_is_not_linked(self):
        self.account(is_active=False)
        self.assertEqual(self.google().status_code, 422)
        self.assertFalse(SocialIdentity.objects.exists())


class GoogleErrorTests(GoogleTestCase):
    def test_a_bad_token(self):
        resp = self.google(mint(key=OTHER_KEY))
        self.assertRefusal(resp, 401, "invalid_token")
        self.assertFalse(ConsumerAccount.objects.exists())

    def test_no_id_token(self):
        resp = self.post(GOOGLE_URL, {})
        self.assertEqual(resp.status_code, 422, resp.content)
        self.assertEqual(
            [(e["field"], e["code"]) for e in resp.json()["errors"]], [("id_token", "required")]
        )

    def test_a_stale_bearer_token_does_not_matter(self):
        client = APIClient()
        client.credentials(HTTP_AUTHORIZATION="Bearer not-a-token")
        resp = self.post(GOOGLE_URL, {"id_token": mint()}, client=client)
        self.assertEqual(resp.status_code, 200)

    def test_googles_keys_out_of_reach(self):
        with patch.object(google, "_keys", return_value=KeysDown()):
            self.assertRefusal(self.google(), 503, "service_unavailable")


# --- 2-step verification ---------------------------------------------------------------


@override_settings(TWO_FACTOR_V1=True)
class TwoStepTests(GoogleTestCase):
    def setUp(self):
        super().setUp()
        self.outbox = []
        sender = patch.object(
            two_factor, "get_sender", side_effect=lambda m: FakeSender(self.outbox, [])
        )
        sender.start()
        self.addCleanup(sender.stop)
        self.owner = self.account(two_factor_enabled=True)
        self.link(self.owner)

    def test_google_does_not_skip_it(self):
        resp = self.google()
        self.assertEqual(resp.status_code, 200, resp.content)
        body = resp.json()
        self.assertEqual(
            set(body), {"two_factor_required", "challenge_token", "expires_in", "methods"}
        )
        self.assertEqual(body["methods"], [{"method": "email", "hint": "k•••@gmail.com"}])

        # The 2-step screen the app has, then the tokens, as after a password.
        send = {"challenge_token": body["challenge_token"], "method": "email"}
        self.assertEqual(self.post("/api/v1/auth/2fa/send", send).status_code, 200)
        finish = {**send, "code": self.outbox[-1][1]}
        tokens = self.post("/api/v1/auth/2fa/verify", finish)
        self.assertEqual(tokens.status_code, 200, tokens.content)
        self.assertEqual(AccessToken(tokens.json()["access"])["consumer_id"], str(self.owner.pk))

        # This phone has passed 2-step now: the tokens at once next time.
        again = self.google()
        self.assertEqual(set(again.json()), {"access", "refresh", "is_new_user"})
        self.assertIs(again.json()["is_new_user"], False)

    def test_switched_off_2_step_is_not_asked(self):
        with override_settings(TWO_FACTOR_V1=False):
            self.assertEqual(set(self.google().json()), {"access", "refresh", "is_new_user"})


# --- /auth/me --------------------------------------------------------------------------


class MeTests(GoogleTestCase):
    def fields(self, body):
        names = ("has_password", "sign_in_methods", "google_email")
        return {k: body.get(k, "absent") for k in names}

    def test_an_account_made_with_google(self):
        client = self.signed_in(self.google(mint(email="kevin.new@gmail.com")).json())
        self.assertEqual(
            self.fields(self.me(client)),
            {"has_password": False, "sign_in_methods": ["google"], "google_email": EMAIL},
        )

    def test_a_password_account_then_linked(self):
        account = self.account()
        login = {"destination_type": "email", "destination": EMAIL, "password": PASSWORD}
        client = self.signed_in(self.post(LOGIN, login).json())
        self.assertEqual(
            self.fields(self.me(client)),
            {"has_password": True, "sign_in_methods": ["password"], "google_email": None},
        )
        self.link(account, email="kevin.google@gmail.com")
        self.assertEqual(
            self.fields(self.me(client)),
            {
                "has_password": True,
                "sign_in_methods": ["password", "google"],
                "google_email": "kevin.google@gmail.com",
            },
        )

    def test_switched_off_the_fields_are_not_sent(self):
        client = self.signed_in(self.google().json())
        with override_settings(GOOGLE_SIGN_IN_V1=False):
            body = self.me(client)
        self.assertEqual(
            self.fields(body),
            {"has_password": "absent", "sign_in_methods": "absent", "google_email": "absent"},
        )
        # PATCH answers the same profile, without them too.
        with override_settings(GOOGLE_SIGN_IN_V1=False):
            patched = client.patch(ME, {"display_name": "Kev"}, format="json").json()
        self.assertNotIn("has_password", patched)


# --- POST /auth/login --------------------------------------------------------------------


class LoginTests(GoogleTestCase):
    def login(self, destination=EMAIL, kind="email", password="anything"):
        body = {"destination_type": kind, "destination": destination, "password": password}
        return self.post(LOGIN, body)

    def test_an_account_made_with_google_says_so(self):
        self.google()
        resp = self.login()
        self.assertEqual(resp.status_code, 401, resp.content)
        self.assertEqual(
            resp.json(),
            {
                "detail": "This account signs in with Google. Continue with Google, or set a "
                "password in Settings.",
                "code": "password_not_set",
                "errors": [],
            },
        )

    def test_a_password_account_signs_in_as_before(self):
        self.account()
        self.assertEqual(self.login(password=PASSWORD).status_code, 200)
        resp = self.login(password="wrong")
        self.assertEqual((resp.status_code, resp.json()["detail"]), (422, "Invalid credentials."))

    def test_any_account_with_no_password_says_so_by_its_email(self):
        self.account(password=None)
        self.assertRefusal(self.login(), 401, "password_not_set")

    def test_by_phone_it_is_invalid_credentials_as_today(self):
        account = self.account(phone=PHONE, password=None)
        account.phone_verified_at = timezone.now()
        account.save()
        resp = self.login(destination=PHONE, kind="phone")
        self.assertEqual((resp.status_code, resp.json()["detail"]), (422, "Invalid credentials."))

    def test_forgot_password_works_for_an_account_made_with_google(self):
        self.google()
        with patch("apps.accounts.services.get_sender") as get_sender:
            sent = []
            get_sender.return_value.send.side_effect = lambda dest, code, *a, **k: sent.append(code)
            forgot = {"destination_type": "email", "destination": EMAIL}
            self.post("/api/v1/auth/password/forgot", forgot)
            verify = {"destination_type": "email", "destination": EMAIL, "code": sent[-1]}
            reset_token = self.post("/api/v1/auth/password/verify", verify).json()["reset_token"]
        body = {"reset_token": reset_token, "new_password": NEW_PASSWORD}
        reset = self.post("/api/v1/auth/password/reset", body)
        self.assertEqual(reset.status_code, 200, reset.content)
        self.assertEqual(self.login(password=NEW_PASSWORD).status_code, 200)


# --- POST /auth/password/set -----------------------------------------------------------


@override_settings(TWO_FACTOR_V1=True)
class PasswordSetTests(GoogleTestCase):
    def setUp(self):
        super().setUp()
        self.outbox = []
        sender = patch.object(
            two_factor, "get_sender", side_effect=lambda m: FakeSender(self.outbox, [])
        )
        sender.start()
        self.addCleanup(sender.stop)
        self.phone = self.signed_in(self.google().json())
        self.owner = ConsumerAccount.objects.get()
        mail.outbox.clear()

    def token(self, purpose="password_set"):
        cache.delete(f"2fa:resend:{self.owner.pk}:{purpose}")
        sent = self.post(VERIFY_SEND, {"method": "email", "purpose": purpose}, client=self.phone)
        self.assertEqual(sent.status_code, 200, sent.content)
        body = {"method": "email", "purpose": purpose, "code": self.outbox[-1][1]}
        resp = self.post(VERIFY, body, client=self.phone)
        self.assertEqual(resp.status_code, 200, resp.content)
        expected = 600 if purpose == "password_set" else 300
        self.assertEqual(resp.json()["expires_in"], expected)
        return resp.json()["verification_token"]

    def set(self, token, new=NEW_PASSWORD, confirm=None):
        body = {
            "verification_token": token,
            "new_password": new,
            "confirm_password": confirm or new,
        }
        return self.post(SET, body, client=self.phone)

    def test_sets_it_and_then_both_ways_work(self):
        resp = self.set(self.token())
        self.assertEqual(resp.status_code, 204, resp.content)
        self.owner.refresh_from_db()
        self.assertTrue(self.owner.check_password(NEW_PASSWORD))
        self.assertIsNotNone(self.owner.password_changed_at)

        me = self.me(self.phone)
        self.assertEqual(
            (me["has_password"], me["sign_in_methods"]), (True, ["password", "google"])
        )
        security = self.phone.get("/api/v1/me/security").json()
        self.assertIsNotNone(security["password_changed_at"])

        login = {"destination_type": "email", "destination": EMAIL, "password": NEW_PASSWORD}
        self.assertEqual(self.post(LOGIN, login, IPAD).status_code, 200)
        self.assertEqual(self.google().status_code, 200)

        (added,) = [m for m in mail.outbox if "password" in m.subject]
        self.assertEqual(added.subject, "Go Style: a password was added to your account")
        self.assertTrue(
            added.body.startswith("A password was added to your Go Style account on iPhone 15."),
            added.body,
        )

    def test_an_account_with_a_password_is_409(self):
        token = self.token()
        self.assertEqual(self.set(token).status_code, 204)
        self.assertRefusal(self.set(self.token()), 409, "password_already_set")

    def test_the_token_must_be_a_live_password_set_one(self):
        other = self.token(TwoFactorPurpose.TWO_STEP_ON)
        for token in (None, "", "vt_made_up", other):
            with self.subTest(token=token):
                resp = self.set(token)
                self.assertEqual(resp.status_code, 403, resp.content)
                self.assertEqual(
                    [(e["field"], e["code"]) for e in resp.json()["errors"]],
                    [("verification_token", "invalid")],
                )
        token = self.token()
        self.assertEqual(self.set(token).status_code, 204)
        # Used once.
        used = VerificationToken.objects.filter(used_at__isnull=False, purpose="password_set")
        self.assertTrue(used.exists())

    def test_the_sign_up_rules(self):
        token = self.token()
        weak = self.set(token, new="short")
        self.assertEqual(weak.status_code, 422, weak.content)
        self.assertEqual({e["field"] for e in weak.json()["errors"]}, {"new_password"})
        differ = self.set(token, new=NEW_PASSWORD, confirm=NEW_PASSWORD + "x")
        self.assertEqual(
            [(e["field"], e["message"]) for e in differ.json()["errors"]],
            [("new_password", "Passwords do not match.")],
        )
        # A refused password does not spend the token.
        self.assertEqual(self.set(token).status_code, 204)

    def test_password_set_is_a_purpose_only_while_google_sign_in_is_on(self):
        with override_settings(GOOGLE_SIGN_IN_V1=False):
            body = {"method": "email", "purpose": "password_set"}
            resp = self.post(VERIFY_SEND, body, client=self.phone)
        self.assertEqual(resp.status_code, 422, resp.content)
        self.assertEqual(resp.json()["errors"][0]["code"], "invalid_choice")


# --- POST /auth/me/delete --------------------------------------------------------------


class DeleteTests(GoogleTestCase):
    def setUp(self):
        super().setUp()
        self.phone = self.signed_in(self.google().json())
        self.owner = ConsumerAccount.objects.get()

    def delete(self, body):
        return self.post(DELETE, body, client=self.phone)

    def test_confirm_with_google(self):
        resp = self.delete({"id_token": mint(), "reason": "privacy"})
        self.assertEqual(resp.status_code, 200, resp.content)
        self.assertEqual(set(resp.json()), {"purge_at", "confirmation_sent_to"})
        self.owner.refresh_from_db()
        self.assertIsNotNone(self.owner.purge_at)
        self.assertEqual(self.owner.deletion_reason, "privacy")

    def test_a_token_older_than_10_minutes_or_bad(self):
        now = int(time.time())
        for token in (mint(iat=now - 660, exp=now + 2940), mint(key=OTHER_KEY)):
            with self.subTest(token=token[-8:]):
                resp = self.delete({"id_token": token})
                self.assertEqual(resp.status_code, 401, resp.content)
                body = resp.json()
                self.assertEqual(body["code"], "invalid_token")
                self.assertEqual(
                    [(e["field"], e["code"]) for e in body["errors"]],
                    [("id_token", "invalid_token")],
                )
        self.owner.refresh_from_db()
        self.assertIsNone(self.owner.purge_at)

    def test_the_same_two_client_ids_as_sign_in(self):
        for aud, status_code in ((ANDROID, 401), (IOS, 200)):
            with self.subTest(aud=aud):
                resp = self.delete({"id_token": mint(aud=aud)})
                self.assertEqual(resp.status_code, status_code, resp.content)

    def test_another_google_account(self):
        resp = self.delete({"id_token": mint(sub=OTHER_SUB)})
        self.assertEqual(resp.status_code, 403, resp.content)
        self.assertEqual(
            [(e["field"], e["code"]) for e in resp.json()["errors"]],
            [("id_token", "wrong_google_account")],
        )

    def test_exactly_one_of_password_and_id_token(self):
        for body in ({}, {"password": PASSWORD, "id_token": mint()}):
            with self.subTest(body=sorted(body)):
                resp = self.delete(body)
                self.assertEqual(resp.status_code, 422, resp.content)
                self.assertEqual(
                    [(e["field"], e["code"]) for e in resp.json()["errors"]],
                    [("password", "required")],
                )

    def test_the_password_still_works_for_an_account_with_one(self):
        account = self.account(email="pass@example.com")
        login = {
            "destination_type": "email",
            "destination": "pass@example.com",
            "password": PASSWORD,
        }
        client = self.signed_in(self.post(LOGIN, login, IPAD).json(), IPAD)
        wrong = self.post(DELETE, {"password": "nope"}, IPAD, client)
        self.assertEqual(wrong.json()["errors"][0]["code"], "wrong_password")
        # A password account with no Google link cannot confirm with Google.
        google_resp = self.post(DELETE, {"id_token": mint()}, IPAD, client)
        self.assertEqual(google_resp.json()["errors"][0]["code"], "wrong_google_account")
        self.assertEqual(self.post(DELETE, {"password": PASSWORD}, IPAD, client).status_code, 200)
        account.refresh_from_db()
        self.assertIsNotNone(account.purge_at)

    def test_switched_off_the_password_is_required_as_today(self):
        with override_settings(GOOGLE_SIGN_IN_V1=False):
            resp = self.delete({"id_token": mint()})
        self.assertEqual(resp.status_code, 422, resp.content)
        self.assertEqual(resp.json()["errors"][0]["field"], "password")


# --- the guide ---------------------------------------------------------------------------


class GoogleSignInDocTests(GoogleTestCase):
    """docs/GOOGLE_SIGN_IN_API.md: every error body on the page is what the
    code answers, word for word."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.text = FE_GUIDE.read_text(encoding="utf-8")
        cls.blocks = [json.loads(b) for b in re.findall(r"```json\n(.*?)\n```", cls.text, re.S)]

    def assertOnThePage(self, resp):
        self.assertIn(resp.json(), self.blocks, resp.content)

    def google_account(self):
        tokens = self.google().json()
        return self.signed_in(tokens)

    def test_the_sign_in_errors(self):
        self.assertOnThePage(self.google(mint(key=OTHER_KEY)))
        self.assertOnThePage(self.post(GOOGLE_URL, {}))
        account = self.account()
        self.assertOnThePage(self.google(mint(email_verified=False)))
        self.link(account, sub=OTHER_SUB)
        self.assertOnThePage(self.google())

    def test_the_me_fields(self):
        me = self.me(self.google_account())
        fields = {k: me[k] for k in ("has_password", "sign_in_methods", "google_email")}
        self.assertIn(fields, self.blocks)

    def test_password_not_set(self):
        self.google()
        body = {"destination_type": "email", "destination": EMAIL, "password": "anything"}
        self.assertOnThePage(self.post(LOGIN, body))

    def test_the_set_a_password_errors(self):
        phone = self.google_account()
        body = {"new_password": NEW_PASSWORD, "confirm_password": NEW_PASSWORD}
        self.assertOnThePage(self.post(SET, body, client=phone))
        mismatch = {**body, "verification_token": "vt_x", "confirm_password": NEW_PASSWORD + "x"}
        self.assertOnThePage(self.post(SET, mismatch, client=phone))
        owner = ConsumerAccount.objects.get()
        owner.set_password(PASSWORD)
        owner.save()
        self.assertOnThePage(self.post(SET, body, client=phone))

    def test_the_delete_errors(self):
        phone = self.google_account()
        now = int(time.time())
        self.assertOnThePage(self.post(DELETE, {"id_token": mint(iat=now - 660)}, client=phone))
        self.assertOnThePage(self.post(DELETE, {"id_token": mint(sub=OTHER_SUB)}, client=phone))
        self.assertOnThePage(self.post(DELETE, {}, client=phone))

    def test_every_message_and_code_is_on_the_page(self):
        for exc in (
            google.InvalidToken,
            google.EmailInUse,
            google.GoogleAlreadyLinked,
            google.PasswordNotSet,
            google.PasswordAlreadySet,
            google.GoogleUnavailable,
        ):
            with self.subTest(code=exc.default_code):
                self.assertIn(f"`{exc.default_code}`", self.text)
                self.assertIn(exc.default_detail, self.text)

    def test_the_emails_are_on_the_page(self):
        for subject in (
            "Go Style: Google sign-in was added to your account",
            "Go Style: a password was added to your account",
        ):
            self.assertIn(f"| {subject} |", self.text)

    def test_no_em_dash(self):
        self.assertNotIn("\u2014", self.text)
