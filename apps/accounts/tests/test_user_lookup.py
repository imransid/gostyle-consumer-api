"""
Tests for `GET /user/lookup?contact=`: find a member by phone or email.

Three properties matter more than the happy path, and each has tests that
should fail loudly if it breaks:

  * An unverified account and no account at all are the same `found: false`,
    byte for byte. Otherwise the endpoint tells anyone who has signed up.
  * The cap is 20 a day per CALLER ACCOUNT, charged after validation and
    before the query. Hits and misses cost the same; the IP plays no part.
  * The contact never comes back in the response, whatever the outcome.
"""

from django.core.cache import cache
from django.test import TestCase
from django.utils import timezone
from rest_framework.test import APIClient

from apps.accounts import ratelimit
from apps.accounts.models import ConsumerAccount
from apps.accounts.services import tokens_for

URL = "/api/v1/user/lookup"
LIMIT = ratelimit.LOOKUP_PER_ACCOUNT_DAILY

CALLER_PHONE = "+8801711111111"
TARGET_PHONE = "+8801712345678"
MISSING_PHONE = "+8801899999999"
IMAGE = "https://cdn.example.com/kevin.jpg"

IP_A = "203.0.113.10"
IP_B = "198.51.100.20"

NOT_FOUND = {"found": False, "user": None}


class UserLookupTestCase(TestCase):
    def setUp(self):
        self.client = APIClient()

        # The daily counters live in the (locmem) cache and persist across
        # test methods; clear them so every test starts with a full allowance.
        cache.clear()
        self.addCleanup(cache.clear)

        self.caller = self.make_account(phone=CALLER_PHONE)
        self.target = self.make_account(
            phone=TARGET_PHONE, full_name="Kevin Rogers", image=IMAGE
        )
        self.authenticate_as(self.caller)

    def make_account(self, phone=None, email=None, verified=True, **extra):
        """A member whose every contact on file is proved, or none of them."""
        now = timezone.now() if verified else None
        return ConsumerAccount.objects.create(
            phone=phone,
            email=email,
            account_verified=verified,
            phone_verified_at=now if phone else None,
            email_verified_at=now if email else None,
            **extra,
        )

    def authenticate_as(self, account):
        token = tokens_for(account)["access"]
        self.client.credentials(HTTP_AUTHORIZATION=f"Bearer {token}")

    def lookup(self, contact, ip=IP_A):
        # The test client URL-encodes the query, so a "+" travels as %2B.
        return self.client.get(
            URL,
            {"contact": contact},
            REMOTE_ADDR=ip,
            HTTP_X_FORWARDED_FOR=ip,
        )

    def spend_allowance(self, contact, ip=IP_A):
        # Hits and misses are both 200 now, so anything else is a failure.
        for i in range(LIMIT):
            resp = self.lookup(contact, ip=ip)
            self.assertEqual(resp.status_code, 200, f"call {i + 1}: {resp.content}")


class LookupAnswerTests(UserLookupTestCase):
    def test_a_hit_returns_id_name_and_image_only(self):
        resp = self.lookup(TARGET_PHONE)

        self.assertEqual(resp.status_code, 200)
        self.assertEqual(
            resp.json(),
            {
                "found": True,
                "user": {
                    "id": str(self.target.id),
                    "name": "Kevin Rogers",
                    "image": IMAGE,
                },
            },
        )

    def test_a_miss_is_200_with_found_false(self):
        """No match is not an error: the app offers "Add as Guest" instead."""
        resp = self.lookup(MISSING_PHONE)

        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json(), NOT_FOUND)

    def test_an_unverified_account_and_no_account_answer_identically(self):
        """The property the endpoint exists to keep. Same status, same bytes."""
        unverified_phone = "+8801722222222"
        self.make_account(phone=unverified_phone, verified=False)

        unverified = self.lookup(unverified_phone)
        missing = self.lookup(MISSING_PHONE)

        self.assertEqual(unverified.status_code, 200)
        self.assertEqual(missing.status_code, 200)
        self.assertEqual(unverified.content, missing.content)
        self.assertEqual(missing.json(), NOT_FOUND)

    def test_a_disabled_account_and_an_unproved_contact_are_the_same_miss(self):
        """The other exclusions in the WHERE fold into the same answer."""
        self.make_account(phone="+8801733333333", is_active=False)
        # Verified by email; the phone on file was never proved.
        ConsumerAccount.objects.create(
            phone="+8801744444444",
            email="kevin@example.com",
            account_verified=True,
            email_verified_at=timezone.now(),
        )

        missing = self.lookup(MISSING_PHONE)
        for phone in ("+8801733333333", "+8801744444444"):
            with self.subTest(phone=phone):
                resp = self.lookup(phone)
                self.assertEqual(resp.status_code, 200)
                self.assertEqual(resp.content, missing.content)

    def test_a_mixed_case_email_matches(self):
        member = self.make_account(email="kevin@example.com")

        resp = self.lookup("  Kevin@Example.COM ")

        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()["user"]["id"], str(member.id))

    def test_a_national_bd_number_matches_its_e164_row(self):
        resp = self.lookup("01712345678")

        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()["user"]["id"], str(self.target.id))

    def test_a_uae_number_keeps_its_plus(self):
        member = self.make_account(phone="+971501234567")

        resp = self.lookup("+971 50 123 4567")

        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()["user"]["id"], str(member.id))

    def test_an_unencoded_plus_is_refused_never_read_as_someone_else(self):
        """A bare "+" in a query string decodes to a space. Nothing guesses it
        back: the number is read as a national one and fails."""
        self.make_account(phone="+971501234567")

        resp = self.client.get(f"{URL}?contact=+971501234567")

        self.assertEqual(resp.status_code, 422)
        self.assertEqual(resp.json()["errors"][0]["code"], "invalid_contact")

    def test_a_malformed_contact_is_422_not_a_match(self):
        # A legacy row holding the malformed string verbatim. If the raw input
        # were ever compared with the column, this would be a hit.
        self.make_account(phone="12345678")

        resp = self.lookup("12345678")

        self.assertEqual(resp.status_code, 422)
        self.assertEqual(
            resp.json()["errors"][0],
            {
                "field": "contact",
                "code": "invalid_contact",
                "message": "Enter a valid email address or phone number.",
            },
        )

    def test_a_malformed_email_is_422(self):
        resp = self.lookup("kevin@")
        self.assertEqual(resp.status_code, 422)
        self.assertEqual(resp.json()["errors"][0]["code"], "invalid_contact")

    def test_no_contact_is_422(self):
        resp = self.client.get(URL)
        self.assertEqual(resp.status_code, 422)
        self.assertEqual(resp.json()["errors"][0]["field"], "contact")

    def test_the_contact_goes_in_the_query_not_a_body(self):
        resp = self.client.post(URL, {"contact": TARGET_PHONE}, format="json")
        self.assertEqual(resp.status_code, 405)


class LookupAccessTests(UserLookupTestCase):
    def test_anonymous_is_401(self):
        self.client.credentials()
        resp = self.lookup(TARGET_PHONE)
        self.assertEqual(resp.status_code, 401)

    def test_an_unverified_caller_is_403(self):
        """A fresh account is free, so it must not buy 20 lookups a day."""
        self.authenticate_as(self.make_account(phone="+8801755555555", verified=False))

        resp = self.lookup(TARGET_PHONE)

        self.assertEqual(resp.status_code, 403)
        self.assertEqual(resp.json()["code"], "permission_denied")


class LookupRateLimitTests(UserLookupTestCase):
    def test_the_21st_call_is_429_when_it_would_hit(self):
        # Twenty misses are charged exactly like twenty hits.
        self.spend_allowance(MISSING_PHONE)
        self.assertEqual(self.lookup(TARGET_PHONE).status_code, 429)

    def test_the_21st_call_is_429_when_it_would_miss(self):
        self.spend_allowance(TARGET_PHONE)
        self.assertEqual(self.lookup(MISSING_PHONE).status_code, 429)

    def test_a_refused_hit_and_a_refused_miss_look_the_same(self):
        self.spend_allowance(TARGET_PHONE)

        hit = self.lookup(TARGET_PHONE)
        miss = self.lookup(MISSING_PHONE)

        self.assertEqual(hit.status_code, 429)
        self.assertEqual(miss.status_code, 429)
        self.assertEqual(hit.content, miss.content)

    def test_two_accounts_on_one_ip_get_twenty_each(self):
        other = self.make_account(phone="+8801766666666")

        for caller in (self.caller, other):
            self.authenticate_as(caller)
            self.spend_allowance(TARGET_PHONE, ip=IP_A)

        for caller in (self.caller, other):
            self.authenticate_as(caller)
            self.assertEqual(self.lookup(TARGET_PHONE, ip=IP_A).status_code, 429)

    def test_one_account_on_two_ips_shares_twenty(self):
        for i in range(LIMIT):
            resp = self.lookup(TARGET_PHONE, ip=IP_A if i % 2 else IP_B)
            self.assertEqual(resp.status_code, 200, f"call {i + 1}")

        self.assertEqual(self.lookup(TARGET_PHONE, ip=IP_A).status_code, 429)
        self.assertEqual(self.lookup(TARGET_PHONE, ip=IP_B).status_code, 429)

    def test_a_malformed_contact_is_not_charged(self):
        """Validation runs before the counter, so bad input spends nothing."""
        for _ in range(LIMIT + 5):
            self.assertEqual(self.lookup("not-a-number").status_code, 422)

        self.assertEqual(self.lookup(TARGET_PHONE).status_code, 200)


class LookupEchoTests(UserLookupTestCase):
    def test_the_contact_is_never_echoed(self):
        self.make_account(email="kevin@example.com")

        responses = {
            "phone hit": (self.lookup("01712345678"), ["01712345678", "1712345678"]),
            "phone miss": (self.lookup(MISSING_PHONE), ["1899999999"]),
            "email hit": (self.lookup("Kevin@Example.com"), ["kevin@example.com"]),
            "email miss": (self.lookup("nobody@example.com"), ["nobody@example.com"]),
            "malformed": (self.lookup("kevin@"), ["kevin@"]),
        }
        # Spend what is left of the allowance to reach the 429 as well.
        for _ in range(LIMIT - 4):
            self.lookup(MISSING_PHONE)
        responses["rate limited"] = (
            self.lookup(TARGET_PHONE),
            ["1712345678"],
        )

        for name, (resp, fragments) in responses.items():
            body = resp.content.decode().lower()
            for fragment in fragments:
                with self.subTest(response=name, fragment=fragment):
                    self.assertNotIn(fragment, body)

        self.assertEqual(responses["rate limited"][0].status_code, 429)