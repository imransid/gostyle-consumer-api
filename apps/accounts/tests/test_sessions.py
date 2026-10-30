"""
Where you're signed in (apps/accounts/sessions.py, docs/DEVICE_SESSIONS_API.md).

Every request goes through the real URL conf with real tokens from
/auth/login, because the session lives in the token (`sid`). The GeoIP file
is replaced by FakeReader, so a place comes from PLACES.
"""

import hashlib
import json
import re
from datetime import datetime, timedelta
from datetime import timezone as dt_timezone
from io import StringIO
from types import SimpleNamespace
from unittest.mock import patch

from django.conf import settings
from django.core.cache import cache
from django.core.management import call_command
from django.test import RequestFactory, SimpleTestCase, TestCase, override_settings
from django.utils import timezone
from rest_framework.test import APIClient
from rest_framework_simplejwt.token_blacklist.models import BlacklistedToken, OutstandingToken
from rest_framework_simplejwt.tokens import AccessToken, RefreshToken

from apps.accounts import geoip, services, sessions
from apps.accounts.models import ConsumerAccount, Purpose, UserSession, Verification
from apps.accounts.push_api import PushApiUnavailable
from apps.grpc_gen import auth_pb2
from apps.grpc_gen.servicer import ConsumerAuthServicer

PASSWORD = "Str0ng!Pass"
PHONE = "+971501234567"
SESSIONS = "/api/v1/me/sessions"
REFRESH = "/api/v1/auth/token/refresh"
PUSH_TOKEN = "fcm-token-1234567890"
UAE_TIME = re.compile(r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d\+04:00$")

IPHONE = {
    "HTTP_X_DEVICE_ID": "9F1C2B7E-3D4A-4C1F-8E2B-6A0D5C9B1E47",
    "HTTP_X_DEVICE_NAME": "iPhone 15",
    "HTTP_X_DEVICE_TYPE": "phone",
    "HTTP_X_PLATFORM": "ios",
    "HTTP_X_OS_VERSION": "18.1",
    "HTTP_X_APP_VERSION": "1.0.0",
    "HTTP_X_APP_BUILD": "42",
    "REMOTE_ADDR": "94.200.14.31",
}
IPAD = {
    **IPHONE,
    "HTTP_X_DEVICE_ID": "1B7E4D2A-0C6F-4A93-8E15-7D9B3F2C6A40",
    "HTTP_X_DEVICE_NAME": "iPad",
    "HTTP_X_DEVICE_TYPE": "tablet",
    "HTTP_X_OS_VERSION": "17.6",
    "REMOTE_ADDR": "94.200.18.7",
}
GALAXY = {
    "HTTP_X_DEVICE_ID": "a1b2c3d4e5f60718",
    "HTTP_X_DEVICE_NAME": "Galaxy S23 Ultra",
    "HTTP_X_DEVICE_TYPE": "phone",
    "HTTP_X_PLATFORM": "android",
    "REMOTE_ADDR": "86.98.44.120",
}
CHROME = {
    "HTTP_USER_AGENT": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36",
    "REMOTE_ADDR": "86.98.44.120",
}

EMIRATES = {"iso_code": "AE", "names": {"en": "United Arab Emirates"}}
PLACES = {
    "94.200.14.31": {
        "city": {"names": {"en": "Dubai"}},
        "country": EMIRATES,
        "location": {"latitude": 25.0657, "longitude": 55.17128},
    },
    "94.200.18.7": {"city": {"names": {"en": "Dubai"}}, "country": EMIRATES},
    "86.98.44.120": {"city": {"names": {"en": "Abu Dhabi"}}, "country": EMIRATES},
    "5.195.0.1": {"country": EMIRATES},
    "2001:8f8:1::1": {"city": {"names": {"en": "Sharjah"}}, "country": EMIRATES},
    "103.4.145.2": {
        "city": {"names": {"en": "Dhaka"}},
        "country": {"iso_code": "BD", "names": {"en": "Bangladesh"}},
    },
    "23.20.0.1": {
        "city": {"names": {"en": "New York"}},
        "country": {"iso_code": "US", "names": {"en": "United States"}},
    },
    "81.2.69.142": {
        "city": {"names": {"en": "London"}},
        "country": {"iso_code": "GB", "names": {"en": "United Kingdom"}},
    },
    "41.33.0.1": {
        "city": {"names": {"en": "Cairo"}},
        "registered_country": {"iso_code": "EG", "names": {"en": "Egypt"}},
    },
    "185.93.1.7": {"city": {"names": {"en": "Fujairah"}}},
}

# simplejwt's own 401 for a token it cannot use, as this API sends it.
INVALID_TOKEN_CODE = "not_authenticated"


def sha256(text):
    return hashlib.sha256(text.encode()).hexdigest()


class FakeReader:
    def __init__(self, built=None):
        self.asked = []
        self.built = built or timezone.now()

    def get(self, ip):
        self.asked.append(ip)
        return PLACES.get(ip)

    def metadata(self):
        return SimpleNamespace(
            database_type="GeoLite2-City", build_epoch=int(self.built.timestamp())
        )


@override_settings(DEVICE_SESSIONS_V1=True)
class SessionTestCase(TestCase):
    def setUp(self):
        cache.clear()
        self.addCleanup(cache.clear)
        self.reader = FakeReader()
        patcher = patch.object(geoip, "_current_reader", return_value=self.reader)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.account = self.make_account(PHONE)

    def make_account(self, phone):
        account = ConsumerAccount.objects.create_user(phone=phone)
        account.set_password(PASSWORD)
        account.phone_verified_at = timezone.now()
        account.account_verified = True
        account.save()
        return account

    def login(self, device=IPHONE, phone=PHONE):
        client = APIClient()
        body = {"destination_type": "phone", "destination": phone, "password": PASSWORD}
        resp = client.post("/api/v1/auth/login", body, format="json", **device)
        self.assertEqual(resp.status_code, 200, resp.content)
        client.tokens = resp.json()
        client.device = device
        self.reconnect(client)
        return client

    def reconnect(self, client, **meta):
        """The client's headers from now on: its device's, changed by `meta`.
        (APIClient's credentials win over a single request's own headers.)"""
        client.credentials(
            HTTP_AUTHORIZATION=f"Bearer {client.tokens['access']}", **{**client.device, **meta}
        )

    def older_client(self):
        """A client with a token pair from before sessions: no sid."""
        client = APIClient()
        client.tokens = services.tokens_for(self.account)
        client.credentials(HTTP_AUTHORIZATION=f"Bearer {client.tokens['access']}")
        return client

    def sid(self, client):
        return AccessToken(client.tokens["access"])["sid"]

    def session(self, client):
        return UserSession.objects.get(pk=self.sid(client))

    def refresh(self, client, **headers):
        return APIClient().post(
            REFRESH, {"refresh": client.tokens["refresh"]}, format="json", **headers
        )

    def assertRevoked(self, resp):
        self.assertEqual(resp.status_code, 401)
        self.assertEqual(
            resp.json(),
            {
                "detail": "You’ve been signed out on this device.",
                "code": "session_revoked",
                "errors": [],
            },
        )


class SwitchOffTests(SessionTestCase):
    @override_settings(DEVICE_SESSIONS_V1=False)
    def test_off_nothing_changes(self):
        phone = self.login()
        self.assertNotIn("sid", AccessToken(phone.tokens["access"]).payload)
        self.assertNotIn("sid", RefreshToken(phone.tokens["refresh"]).payload)
        self.assertFalse(UserSession.objects.exists())
        self.assertEqual(phone.get("/api/v1/auth/me").status_code, 200)

        self.assertEqual(phone.get(SESSIONS).status_code, 404)
        self.assertEqual(phone.delete(SESSIONS).status_code, 404)
        self.assertEqual(phone.delete(f"{SESSIONS}/{self.account.pk}").status_code, 404)

        resp = self.refresh(phone, **IPHONE)
        self.assertEqual(resp.status_code, 200)
        self.assertNotIn("sid", RefreshToken(resp.json()["refresh"]).payload)
        self.assertFalse(UserSession.objects.exists())

    def test_a_sid_token_is_not_checked_while_off(self):
        phone = self.login()
        other = self.login(IPAD)
        other.delete(f"{SESSIONS}/{self.sid(phone)}")
        with override_settings(DEVICE_SESSIONS_V1=False):
            self.assertEqual(phone.get("/api/v1/auth/me").status_code, 200)


class OpenTests(SessionTestCase):
    def test_login_opens_a_session_from_the_headers(self):
        phone = self.login()
        refresh = RefreshToken(phone.tokens["refresh"])
        self.assertEqual(refresh["sid"], self.sid(phone))

        s = self.session(phone)
        self.assertEqual(s.user, self.account)
        self.assertEqual(
            (s.device_id, s.device_name, s.device_type, s.platform),
            ("9F1C2B7E-3D4A-4C1F-8E2B-6A0D5C9B1E47", "iPhone 15", "phone", "ios"),
        )
        self.assertEqual((s.os_version, s.app_version, s.app_build), ("18.1", "1.0.0", "42"))
        self.assertEqual((s.ip_address, s.location), ("94.200.14.31", "Dubai, UAE"))
        self.assertEqual(s.refresh_token_hash, sha256(phone.tokens["refresh"]))
        self.assertIsNone(s.ended_at)
        # The outstanding token is the very string the app holds.
        self.assertTrue(OutstandingToken.objects.filter(token=phone.tokens["refresh"]).exists())

    def test_register_opens_a_session(self):
        body = {
            "destination_type": "phone",
            "destination": "+971509999999",
            "full_name": "Kevin Rogers",
            "password": PASSWORD,
            "confirm_password": PASSWORD,
            "gender": "male",
        }
        resp = APIClient().post("/api/v1/auth/register", body, format="json", **GALAXY)
        self.assertEqual(resp.status_code, 201, resp.content)
        s = UserSession.objects.get(pk=AccessToken(resp.json()["access"])["sid"])
        self.assertEqual(
            (s.device_name, s.platform, s.location),
            ("Galaxy S23 Ultra", "android", "Abu Dhabi, UAE"),
        )

    def test_a_new_sign_in_on_the_same_device_ends_the_old_one(self):
        first = self.login()
        second = self.login()
        old, new = self.session(first), self.session(second)

        self.assertIsNotNone(old.ended_at)
        self.assertIsNone(new.ended_at)
        self.assertEqual(UserSession.objects.filter(ended_at__isnull=True).count(), 1)
        self.assertRevoked(first.get("/api/v1/auth/me"))
        self.assertEqual(second.get("/api/v1/auth/me").status_code, 200)
        self.assertTrue(BlacklistedToken.objects.filter(token__token=first.tokens["refresh"]).exists())

    def test_without_a_device_id_each_sign_in_is_its_own(self):
        bare = {"HTTP_X_PLATFORM": "ios", "HTTP_X_OS_VERSION": "18.1"}
        self.login(bare)
        self.login(bare)
        self.assertEqual(UserSession.objects.filter(ended_at__isnull=True).count(), 2)

    def test_the_same_device_on_another_account_is_another_session(self):
        self.make_account("+971502222222")
        mine = self.login()
        theirs = self.login(phone="+971502222222")
        self.assertIsNone(self.session(mine).ended_at)
        self.assertIsNone(self.session(theirs).ended_at)

    def test_old_builds_send_no_headers_and_nothing_fails(self):
        phone = self.login({})
        s = self.session(phone)
        self.assertEqual(
            (s.device_id, s.device_name, s.device_type, s.platform, s.ip_address, s.location),
            (None, None, "unknown", None, "127.0.0.1", None),
        )
        self.assertEqual(phone.get("/api/v1/auth/me").status_code, 200)

    def test_a_browser_is_named_from_its_user_agent(self):
        s = self.session(self.login(CHROME))
        self.assertEqual(
            (s.device_name, s.device_type, s.platform, s.app_version),
            ("Chrome on macOS", "browser", "web", None),
        )


class TokenWithoutSessionTests(SessionTestCase):
    def test_is_refused_like_an_invalid_token_so_the_app_refreshes(self):
        older = self.older_client()
        resp = older.get("/api/v1/auth/me")
        self.assertEqual(resp.status_code, 401)
        self.assertEqual(resp.json()["code"], INVALID_TOKEN_CODE)
        # Exactly the body of a token simplejwt cannot read.
        junk = APIClient()
        junk.credentials(HTTP_AUTHORIZATION="Bearer not.a.token")
        self.assertEqual(
            set(resp.json()), set(junk.get("/api/v1/auth/me").json())
        )

    def test_its_refresh_opens_the_session(self):
        older = self.older_client()
        resp = APIClient().post(REFRESH, {"refresh": older.tokens["refresh"]}, format="json", **IPAD)
        self.assertEqual(resp.status_code, 200)
        tokens = resp.json()
        sid = AccessToken(tokens["access"])["sid"]
        self.assertEqual(RefreshToken(tokens["refresh"])["sid"], sid)
        s = UserSession.objects.get(pk=sid)
        self.assertEqual((s.device_name, s.location), ("iPad", "Dubai, UAE"))
        self.assertEqual(s.refresh_token_hash, sha256(tokens["refresh"]))

        client = APIClient()
        client.credentials(HTTP_AUTHORIZATION=f"Bearer {tokens['access']}")
        rows = client.get(SESSIONS).json()["results"]
        self.assertEqual([r["is_current"] for r in rows], [True])

    def test_on_the_sessions_routes_it_is_please_sign_in_again(self):
        resp = self.older_client().get(SESSIONS)
        self.assertEqual(
            (resp.status_code, resp.json()),
            (401, {"detail": "Please sign in again.", "code": "not_authenticated", "errors": []}),
        )


class ReadDeviceTests(SimpleTestCase):
    def read(self, **meta):
        return sessions.read_device(RequestFactory().get("/", **meta))

    def test_keeps_printable_ascii_only_and_drops_empty_values(self):
        fields = self.read(
            HTTP_X_DEVICE_NAME="Kevin\u2019s Galaxy",
            HTTP_X_OS_VERSION="   ",
            HTTP_X_APP_VERSION="",
        )
        self.assertEqual(fields, {"device_name": "Kevins Galaxy"})

    def test_an_unknown_type_or_platform_is_no_header(self):
        fields = self.read(HTTP_X_DEVICE_TYPE="watch", HTTP_X_PLATFORM="Symbian")
        self.assertEqual(fields, {})
        fields = self.read(HTTP_X_DEVICE_TYPE="Tablet", HTTP_X_PLATFORM="IOS")
        self.assertEqual(fields, {"device_type": "tablet", "platform": "ios"})

    def test_long_values_are_cut(self):
        self.assertEqual(len(self.read(HTTP_X_DEVICE_NAME="x" * 500)["device_name"]), 100)

    def test_browser_names(self):
        for agent, name in (
            ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
             "Chrome/130.0.0.0 Safari/537.36 Edg/130.0.0.0", "Edge on Windows"),
            ("Mozilla/5.0 (iPhone; CPU iPhone OS 18_1 like Mac OS X) AppleWebKit/605.1.15 "
             "(KHTML, like Gecko) Version/18.1 Mobile/15E148 Safari/604.1", "Safari on iOS"),
            ("Mozilla/5.0 (Linux; Android 14; SM-S918B) AppleWebKit/537.36 (KHTML, like Gecko) "
             "Chrome/130.0.0.0 Mobile Safari/537.36", "Chrome on Android"),
            ("Mozilla/5.0 (X11; Linux x86_64; rv:131.0) Gecko/20100101 Firefox/131.0", "Firefox on Linux"),
            ("GoStyle/42 CFNetwork/1568.100.1 Darwin/24.0.0", None),
            ("okhttp/4.9.2", None),
        ):
            self.assertEqual(sessions._browser_name(agent), name, agent)


BEHIND_NGINX = {**settings.REST_FRAMEWORK, "NUM_PROXIES": 1}


class ClientIpTests(SimpleTestCase):
    def ip(self, **meta):
        return sessions.client_ip(RequestFactory().get("/", **meta))

    @override_settings(REST_FRAMEWORK=BEHIND_NGINX, PROXY_SECRET="s3cret")
    def test_through_nginx_the_right_most_forwarded_address(self):
        # The client wrote the first one; nginx appended the one it saw.
        meta = {
            "HTTP_X_FORWARDED_FOR": "1.2.3.4, 94.200.14.31",
            "HTTP_X_PROXY_SECRET": "s3cret",
            "REMOTE_ADDR": "10.0.0.5",
        }
        self.assertEqual(self.ip(**meta), "94.200.14.31")

    @override_settings(REST_FRAMEWORK=BEHIND_NGINX, PROXY_SECRET="s3cret")
    def test_past_nginx_the_forwarded_address_is_not_trusted(self):
        meta = {"HTTP_X_FORWARDED_FOR": "94.200.14.31", "REMOTE_ADDR": "10.0.0.5"}
        self.assertEqual(self.ip(**meta), "10.0.0.5")
        self.assertEqual(self.ip(HTTP_X_PROXY_SECRET="guess", **meta), "10.0.0.5")

    @override_settings(REST_FRAMEWORK=BEHIND_NGINX, PROXY_SECRET="")
    def test_without_a_secret_it_is_never_trusted(self):
        meta = {"HTTP_X_FORWARDED_FOR": "94.200.14.31", "HTTP_X_PROXY_SECRET": "", "REMOTE_ADDR": "10.0.0.5"}
        self.assertEqual(self.ip(**meta), "10.0.0.5")

    def test_without_proxies_the_socket_address(self):
        meta = {"HTTP_X_FORWARDED_FOR": "1.2.3.4", "REMOTE_ADDR": "94.200.14.31"}
        self.assertEqual(self.ip(**meta), "94.200.14.31")

    @override_settings(REST_FRAMEWORK=BEHIND_NGINX, PROXY_SECRET="s3cret")
    def test_garbage_is_none(self):
        meta = {"HTTP_X_FORWARDED_FOR": "not-an-ip", "HTTP_X_PROXY_SECRET": "s3cret"}
        self.assertIsNone(self.ip(**meta))


class LocationTests(SimpleTestCase):
    def setUp(self):
        patcher = patch.object(geoip, "_current_reader", return_value=FakeReader())
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_city_and_country_code_then_city_then_country_then_none(self):
        self.assertEqual(geoip.location("103.4.145.2"), "Dhaka, BD")
        self.assertEqual(geoip.location("185.93.1.7"), "Fujairah")
        self.assertEqual(geoip.location("5.195.0.1"), "United Arab Emirates")
        self.assertIsNone(geoip.location("8.8.8.8"))

    def test_usa_uk_and_uae_by_the_names_people_know(self):
        self.assertEqual(geoip.location("23.20.0.1"), "New York, USA")
        self.assertEqual(geoip.location("81.2.69.142"), "London, UK")
        self.assertEqual(geoip.location("94.200.14.31"), "Dubai, UAE")

    def test_the_registered_country_when_there_is_no_other(self):
        self.assertEqual(geoip.location("41.33.0.1"), "Cairo, EG")

    def test_ipv6_and_mapped_ipv4(self):
        self.assertEqual(geoip.location("2001:8f8:1::1"), "Sharjah, UAE")
        self.assertEqual(geoip.location("::ffff:94.200.14.31"), "Dubai, UAE")

    def test_private_and_reserved_addresses_are_not_looked_up(self):
        for ip in ("10.1.2.3", "192.168.1.9", "127.0.0.1", "::1", "fc00::1", "100.64.0.1", "junk"):
            self.assertIs(geoip.location(ip), geoip.KEEP, ip)


class NoGeoDatabaseTests(SimpleTestCase):
    def test_no_database_keeps_the_location(self):
        with override_settings(GEOIP_CITY_DB=""):
            self.assertIs(geoip.location("94.200.14.31"), geoip.KEEP)
        with override_settings(GEOIP_CITY_DB="/nonexistent/GeoLite2-City.mmdb"):
            self.assertIs(geoip.location("94.200.14.31"), geoip.KEEP)


@override_settings(PROXY_SECRET="s3cret", GEOIP_CITY_DB="/geoip/GeoLite2-City.mmdb")
class SessionLocationsCommandTests(SessionTestCase):
    """python manage.py session_locations: why there is no place, and
    --apply for the live sessions."""

    def run_command(self, *args):
        out = StringIO()
        call_command("session_locations", *args, stdout=out)
        return out.getvalue()

    def sessions_at(self, *ips):
        for ip in ips:
            UserSession.objects.create(
                user=self.account, refresh_token_hash=ip, ip_address=ip, location="Dubai"
            )

    def test_says_what_is_set_and_changes_nothing_by_default(self):
        self.sessions_at("94.200.14.31", "103.4.145.2")
        out = self.run_command()
        self.assertIn("PROXY_SECRET: set", out)
        self.assertIn("GEOIP_CITY_DB: /geoip/GeoLite2-City.mmdb (GeoLite2-City, built ", out)
        self.assertIn("2 with a public IP (2 with a place), 0 with a private IP, 0 with none", out)
        self.assertIn("Would change (run with --apply): 2 location(s).", out)
        self.assertEqual(set(UserSession.objects.values_list("location", flat=True)), {"Dubai"})

    def test_apply_writes_todays_words_to_live_sessions_only(self):
        self.sessions_at("94.200.14.31", "103.4.145.2", "8.8.8.8", "10.0.0.7")
        UserSession.objects.create(
            user=self.account,
            refresh_token_hash="ended",
            ip_address="23.20.0.1",
            location="New York",
            ended_at=timezone.now(),
        )
        out = self.run_command("--apply")
        self.assertIn("Changed: 3 location(s).", out)
        self.assertEqual(
            dict(UserSession.objects.values_list("ip_address", "location")),
            {
                "94.200.14.31": "Dubai, UAE",
                "103.4.145.2": "Dhaka, BD",
                "8.8.8.8": None,  # no place for it
                "10.0.0.7": "Dubai",  # private: kept
                "23.20.0.1": "New York",  # ended: not listed, left alone
            },
        )

    def test_a_private_ip_points_at_the_proxy_secret(self):
        self.sessions_at("10.0.0.7")
        out = self.run_command()
        self.assertIn("1 with a private IP", out)
        self.assertIn("the app is not getting the client's real IP", out)

    @override_settings(PROXY_SECRET="", GEOIP_CITY_DB="")
    def test_nothing_set_says_so_and_looks_nothing_up(self):
        with patch.object(geoip, "_current_reader", return_value=None):
            self.sessions_at("94.200.14.31")
            out = self.run_command("--apply")
        self.assertIn("PROXY_SECRET: not set", out)
        self.assertIn("GEOIP_CITY_DB: not set. No session gets a place", out)
        self.assertNotIn("Changed", out)
        self.assertEqual(UserSession.objects.get().location, "Dubai")

    def test_an_old_file_is_pointed_out(self):
        self.reader.built = timezone.now() - timedelta(days=60)
        self.assertIn("The GeoIP file is old", self.run_command())


class ListTests(SessionTestCase):
    def test_the_current_device_first_then_by_last_seen(self):
        tablet = self.login(IPAD)
        android = self.login(GALAXY)
        phone = self.login()
        UserSession.objects.filter(pk=self.sid(tablet)).update(
            last_seen_at=timezone.now() - timedelta(days=3)
        )
        UserSession.objects.filter(pk=self.sid(android)).update(
            last_seen_at=timezone.now() - timedelta(days=1)
        )

        resp = phone.get(SESSIONS)
        self.assertEqual(resp.status_code, 200)
        body = resp.json()
        self.assertEqual((body["count"], body["next"], body["previous"]), (3, None, None))
        rows = body["results"]
        self.assertEqual(
            [r["id"] for r in rows], [self.sid(phone), self.sid(android), self.sid(tablet)]
        )
        self.assertEqual([r["is_current"] for r in rows], [True, False, False])
        self.assertEqual(
            list(rows[0]),
            [
                "id", "device_name", "device_type", "platform", "os_version",
                "app_version", "ip_address", "location", "is_current",
                "created_at", "last_seen_at",
            ],
        )
        for row in rows:
            self.assertRegex(row["created_at"], UAE_TIME)
            self.assertRegex(row["last_seen_at"], UAE_TIME)

    def test_ended_expired_and_other_accounts_sessions_are_not_listed(self):
        phone = self.login()
        gone = self.login(IPAD)
        stale = self.login(GALAXY)
        self.make_account("+971502222222")
        self.login(phone="+971502222222")
        phone.delete(f"{SESSIONS}/{self.sid(gone)}")
        # Its refresh token has expired: the session has ended too.
        OutstandingToken.objects.filter(token=stale.tokens["refresh"]).update(
            expires_at=timezone.now() - timedelta(seconds=1)
        )

        rows = phone.get(SESSIONS).json()["results"]
        self.assertEqual([r["id"] for r in rows], [self.sid(phone)])


class SignOutOneTests(SessionTestCase):
    def test_the_other_device_is_out_at_once(self):
        phone = self.login()
        tablet = self.login(IPAD)
        self.assertEqual(tablet.get("/api/v1/auth/me").status_code, 200)  # caches "live"

        resp = phone.delete(f"{SESSIONS}/{self.sid(tablet)}")
        self.assertEqual((resp.status_code, resp.content), (204, b""))

        self.assertIsNotNone(self.session(tablet).ended_at)
        self.assertTrue(BlacklistedToken.objects.filter(token__token=tablet.tokens["refresh"]).exists())
        self.assertRevoked(tablet.get("/api/v1/auth/me"))
        self.assertRevoked(self.refresh(tablet, **IPAD))
        self.assertEqual(phone.get("/api/v1/auth/me").status_code, 200)

    def test_an_already_ended_or_expired_session_is_204(self):
        phone = self.login()
        tablet = self.login(IPAD)
        android = self.login(GALAXY)
        phone.delete(f"{SESSIONS}/{self.sid(tablet)}")
        self.assertEqual(phone.delete(f"{SESSIONS}/{self.sid(tablet)}").status_code, 204)
        OutstandingToken.objects.filter(token=android.tokens["refresh"]).update(
            expires_at=timezone.now() - timedelta(seconds=1)
        )
        self.assertEqual(phone.delete(f"{SESSIONS}/{self.sid(android)}").status_code, 204)

    def test_the_callers_own_session_is_409(self):
        phone = self.login()
        resp = phone.delete(f"{SESSIONS}/{self.sid(phone)}")
        self.assertEqual(resp.status_code, 409)
        self.assertEqual(
            resp.json(),
            {
                "detail": "Use Sign Out to leave this device.",
                "code": "current_session",
                "errors": [],
            },
        )
        self.assertIsNone(self.session(phone).ended_at)

    def test_another_accounts_session_or_no_session_is_404(self):
        self.make_account("+971502222222")
        theirs = self.login(phone="+971502222222")
        phone = self.login()
        not_found = {
            "detail": "That device isn’t signed in anymore.",
            "code": "not_found",
            "errors": [],
        }
        for target in (self.sid(theirs), "6f2a1c9e-8b3d-4e57-9a10-2c4d6e8f0a1b", "nope"):
            resp = phone.delete(f"{SESSIONS}/{target}")
            self.assertEqual((resp.status_code, resp.json()), (404, not_found), target)
        self.assertIsNone(self.session(theirs).ended_at)

    def test_401_in_the_sessions_api_words(self):
        resp = APIClient().get(SESSIONS)
        self.assertEqual(resp.status_code, 401)
        self.assertEqual(
            resp.json(),
            {"detail": "Please sign in again.", "code": "not_authenticated", "errors": []},
        )
        client = APIClient()
        client.credentials(HTTP_AUTHORIZATION="Bearer not.a.token")
        self.assertEqual(client.delete(SESSIONS).json()["detail"], "Please sign in again.")


class SignOutOthersTests(SessionTestCase):
    def test_every_other_device_but_never_this_one(self):
        tablet = self.login(IPAD)
        android = self.login(GALAXY)
        phone = self.login()

        resp = phone.delete(SESSIONS)
        self.assertEqual((resp.status_code, resp.json()), (200, {"revoked": 2}))
        self.assertRevoked(tablet.get("/api/v1/auth/me"))
        self.assertRevoked(android.get("/api/v1/auth/me"))
        self.assertEqual(phone.get("/api/v1/auth/me").status_code, 200)
        self.assertEqual(self.refresh(phone, **IPHONE).status_code, 200)
        self.assertEqual(phone.delete(SESSIONS).json(), {"revoked": 0})

    def test_tokens_older_than_sessions_are_signed_out_too(self):
        older = self.older_client()
        phone = self.login()
        phone.delete(SESSIONS)

        resp = APIClient().post(REFRESH, {"refresh": older.tokens["refresh"]}, format="json")
        self.assertEqual(resp.status_code, 401)
        self.assertEqual(older.get("/api/v1/auth/me").status_code, 401)
        self.assertEqual(self.refresh(phone, **IPHONE).status_code, 200)


class PushTokenTests(SessionTestCase):
    """The push service keeps the tokens. It records each with the session
    it was registered under, and deletes a session's tokens when asked."""

    def setUp(self):
        super().setUp()
        for name in ("register_device", "unregister_session"):
            patcher = patch(f"apps.accounts.push_api.{name}", return_value=(200, {"ok": True}))
            setattr(self, name, patcher.start())
            self.addCleanup(patcher.stop)

    def register(self, client, token=PUSH_TOKEN):
        body = {"token": token, "platform": "ios"}
        self.assertEqual(client.post("/api/v1/me/device-tokens", body, format="json").status_code, 204)

    def test_a_token_is_registered_with_the_bearers_session(self):
        tablet = self.login(IPAD)
        self.register(tablet)
        self.register_device.assert_called_once_with(
            user_id=self.account.id,
            token=PUSH_TOKEN,
            platform="ios",
            session_id=self.session(tablet).pk,
        )

    def test_sign_out_deletes_that_sessions_push_tokens(self):
        phone = self.login()
        tablet = self.login(IPAD)
        phone.delete(f"{SESSIONS}/{self.sid(tablet)}")
        self.unregister_session.assert_called_once_with(session_id=str(self.session(tablet).pk))

    def test_sign_out_of_all_deletes_every_other_sessions_push_tokens(self):
        tablet = self.login(IPAD)
        android = self.login(GALAXY)
        phone = self.login()
        phone.delete(SESSIONS)
        self.assertEqual(
            {c.kwargs["session_id"] for c in self.unregister_session.call_args_list},
            {self.sid(tablet), self.sid(android)},
        )

    def test_logout_a_new_sign_in_and_a_reset_leave_push_tokens_alone(self):
        phone = self.login()
        tablet = self.login(IPAD)
        phone.post("/api/v1/auth/logout", {"refresh": phone.tokens["refresh"]}, format="json")
        self.login(IPAD)  # ends the tablet's old session
        reset = Verification.issue(PHONE, "phone", Purpose.PASSWORD_RESET)
        services.reset_password(reset_token=str(reset.id), new_password="N3w!Passw0rd")
        self.assertIsNotNone(self.session(tablet).ended_at)
        self.unregister_session.assert_not_called()

    def test_push_service_down_signs_out_and_deletes_the_tokens_later(self):
        phone = self.login()
        tablet = self.login(IPAD)
        self.unregister_session.side_effect = PushApiUnavailable("down")
        resp = phone.delete(f"{SESSIONS}/{self.sid(tablet)}")
        self.assertEqual(resp.status_code, 204)
        self.assertRevoked(tablet.get("/api/v1/auth/me"))

        # Back up: the next push call tells it.
        self.unregister_session.side_effect = None
        self.unregister_session.reset_mock()
        self.register(phone)
        self.unregister_session.assert_called_once_with(session_id=self.sid(tablet))
        self.unregister_session.reset_mock()
        self.register(phone)
        self.unregister_session.assert_not_called()

    @override_settings(DEVICE_SESSIONS_V1=False)
    def test_off_the_push_call_is_what_it_was(self):
        client = APIClient()
        client.credentials(HTTP_AUTHORIZATION=f"Bearer {services.tokens_for(self.account)['access']}")
        self.register(client)
        self.register_device.assert_called_once_with(
            user_id=self.account.id, token=PUSH_TOKEN, platform="ios"
        )


class RefreshTests(SessionTestCase):
    def test_keeps_the_session_and_takes_the_new_details(self):
        phone = self.login()
        before = self.session(phone)
        UserSession.objects.filter(pk=before.pk).update(
            last_seen_at=timezone.now() - timedelta(days=2)
        )

        updated = {**IPHONE, "HTTP_X_APP_VERSION": "1.1.0", "HTTP_X_APP_BUILD": "45",
                   "REMOTE_ADDR": "86.98.44.120"}
        resp = self.refresh(phone, **updated)
        self.assertEqual(resp.status_code, 200)
        tokens = resp.json()
        self.assertEqual(AccessToken(tokens["access"])["sid"], str(before.pk))
        self.assertEqual(RefreshToken(tokens["refresh"])["sid"], str(before.pk))

        after = UserSession.objects.get(pk=before.pk)
        self.assertEqual((after.app_version, after.app_build), ("1.1.0", "45"))
        self.assertEqual((after.ip_address, after.location), ("86.98.44.120", "Abu Dhabi, UAE"))
        self.assertEqual(after.refresh_token_hash, sha256(tokens["refresh"]))
        self.assertGreater(after.last_seen_at, timezone.now() - timedelta(minutes=1))
        self.assertEqual(UserSession.objects.count(), 1)

        # The rotated token finds the same session again.
        phone.tokens = tokens
        self.assertEqual(self.refresh(phone, **updated).status_code, 200)
        self.assertEqual(UserSession.objects.count(), 1)

    def test_last_seen_moves_at_most_every_five_minutes_on_refresh_too(self):
        phone = self.login()
        recent = timezone.now() - timedelta(minutes=1)
        UserSession.objects.filter(pk=self.sid(phone)).update(last_seen_at=recent)
        self.refresh(phone, **IPHONE)
        self.assertEqual(self.session(phone).last_seen_at, recent)

    def test_a_missing_header_keeps_the_stored_value(self):
        phone = self.login()
        self.refresh(phone, REMOTE_ADDR="94.200.14.31")
        s = self.session(phone)
        self.assertEqual((s.device_name, s.app_version), ("iPhone 15", "1.0.0"))

    def test_a_new_device_id_takes_over_that_devices_session(self):
        bare = {"HTTP_X_PLATFORM": "ios", "REMOTE_ADDR": "94.200.14.31"}
        old_build = self.login(bare)
        new_build = self.login()  # the same phone, rebuilt app, signed in too
        resp = self.refresh(old_build, **IPHONE)
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(self.session(old_build).device_id, IPHONE["HTTP_X_DEVICE_ID"])
        self.assertIsNotNone(self.session(new_build).ended_at)

    def test_a_rotated_away_token_is_simplejwts_answer(self):
        phone = self.login()
        spent = phone.tokens["refresh"]
        self.refresh(phone, **IPHONE)
        resp = APIClient().post(REFRESH, {"refresh": spent}, format="json")
        self.assertEqual((resp.status_code, resp.json()["code"]), (401, INVALID_TOKEN_CODE))

    def test_an_invalid_token_is_simplejwts_answer(self):
        resp = APIClient().post(REFRESH, {"refresh": "junk"}, format="json")
        self.assertEqual(resp.status_code, 401)
        self.assertEqual(resp.json()["code"], INVALID_TOKEN_CODE)


class LastSeenTests(SessionTestCase):
    def test_moves_at_most_every_five_minutes(self):
        phone = self.login()
        sid = self.sid(phone)
        long_ago = timezone.now() - timedelta(hours=1)
        UserSession.objects.filter(pk=sid).update(last_seen_at=long_ago)

        phone.get("/api/v1/auth/me")
        self.assertGreater(UserSession.objects.get(pk=sid).last_seen_at, long_ago)

        UserSession.objects.filter(pk=sid).update(last_seen_at=long_ago)
        phone.get("/api/v1/auth/me")  # within the cache's five minutes
        self.assertEqual(UserSession.objects.get(pk=sid).last_seen_at, long_ago)

        cache.clear()  # five minutes later
        phone.get("/api/v1/auth/me")
        self.assertGreater(UserSession.objects.get(pk=sid).last_seen_at, long_ago)

    def test_a_recent_visit_is_not_written_again(self):
        phone = self.login()
        sid = self.sid(phone)
        recent = timezone.now() - timedelta(minutes=1)
        UserSession.objects.filter(pk=sid).update(last_seen_at=recent)
        phone.get("/api/v1/auth/me")
        self.assertEqual(UserSession.objects.get(pk=sid).last_seen_at, recent)

    def test_a_request_does_not_change_the_device_fields(self):
        phone = self.login()
        UserSession.objects.filter(pk=self.sid(phone)).update(
            last_seen_at=timezone.now() - timedelta(hours=1)
        )
        self.reconnect(phone, HTTP_X_APP_VERSION="9.9.9")
        phone.get("/api/v1/auth/me")
        self.assertEqual(self.session(phone).app_version, "1.0.0")

    def five_minutes_later(self, sid):
        cache.delete(sessions._CACHE_KEY.format(sid))
        UserSession.objects.filter(pk=sid).update(last_seen_at=timezone.now() - timedelta(minutes=6))

    def test_a_place_in_the_old_words_is_redone_on_the_same_ip(self):
        phone = self.login()
        sid = self.sid(phone)
        # As a session from before "Dubai, UAE": its place and its lookup
        # remembered under the old cache name.
        UserSession.objects.filter(pk=sid).update(location="Dubai")
        cache.delete(sessions._GEO_KEY.format(sid))
        cache.set(f"consumer-session-geo:{sid}", "94.200.14.31")
        self.five_minutes_later(sid)
        phone.get("/api/v1/auth/me")
        self.assertEqual(UserSession.objects.get(pk=sid).location, "Dubai, UAE")

    def test_ip_address_is_the_last_requests_ip(self):
        phone = self.login()
        phone.get("/api/v1/auth/me")  # caches "live"
        self.reconnect(phone, REMOTE_ADDR="86.98.44.120")
        phone.get("/api/v1/auth/me")
        self.assertEqual(self.session(phone).ip_address, "86.98.44.120")

    def test_the_place_is_looked_up_only_on_a_last_seen_update_when_the_ip_changed(self):
        phone = self.login()
        sid = self.sid(phone)
        phone.get("/api/v1/auth/me")
        self.reader.asked.clear()

        # A new IP: stored at once, looked up at the next last_seen_at update.
        self.reconnect(phone, REMOTE_ADDR="86.98.44.120")
        phone.get("/api/v1/auth/me")
        s = UserSession.objects.get(pk=sid)
        self.assertEqual(
            (s.ip_address, s.location, self.reader.asked), ("86.98.44.120", "Dubai, UAE", [])
        )

        self.five_minutes_later(sid)
        phone.get("/api/v1/auth/me")
        self.assertEqual(self.reader.asked, ["86.98.44.120"])
        self.assertEqual(UserSession.objects.get(pk=sid).location, "Abu Dhabi, UAE")

        # The same network again: no lookup.
        self.five_minutes_later(sid)
        phone.get("/api/v1/auth/me")
        self.assertEqual(self.reader.asked, ["86.98.44.120"])

    def test_a_private_address_keeps_the_place_and_is_no_lookup(self):
        phone = self.login()
        sid = self.sid(phone)
        self.reconnect(phone, REMOTE_ADDR="192.168.1.20")
        self.five_minutes_later(sid)
        phone.get("/api/v1/auth/me")
        s = UserSession.objects.get(pk=sid)
        self.assertEqual((s.ip_address, s.location), ("192.168.1.20", "Dubai, UAE"))

        # Back on the first network: the IP of the last lookup, so none.
        self.reader.asked.clear()
        self.reconnect(phone, REMOTE_ADDR="94.200.14.31")
        self.five_minutes_later(sid)
        phone.get("/api/v1/auth/me")
        self.assertEqual(self.reader.asked, [])

    def test_a_cache_that_fails_does_not_fail_the_request(self):
        phone = self.login()
        down = ConnectionError("redis down")
        with patch.object(sessions.cache, "get_many", side_effect=down), \
             patch.object(sessions.cache, "get", side_effect=down), \
             patch.object(sessions.cache, "set", side_effect=down), \
             patch.object(sessions.cache, "add", side_effect=down):
            self.assertEqual(phone.get("/api/v1/auth/me").status_code, 200)


class LogoutAndResetTests(SessionTestCase):
    def test_logout_ends_this_session(self):
        phone = self.login()
        tablet = self.login(IPAD)
        resp = phone.post("/api/v1/auth/logout", {"refresh": phone.tokens["refresh"]}, format="json")
        self.assertEqual(resp.status_code, 205)
        self.assertIsNotNone(self.session(phone).ended_at)
        self.assertRevoked(phone.get("/api/v1/auth/me"))
        self.assertEqual(tablet.get("/api/v1/auth/me").status_code, 200)
        self.assertEqual([r["id"] for r in tablet.get(SESSIONS).json()["results"]], [self.sid(tablet)])

    def test_logout_with_a_spent_refresh_token_still_ends_the_session(self):
        phone = self.login()
        resp = phone.post("/api/v1/auth/logout", {"refresh": "junk"}, format="json")
        self.assertEqual(resp.status_code, 422)
        self.assertIsNotNone(self.session(phone).ended_at)

    def test_a_password_reset_signs_every_device_out(self):
        phone = self.login()
        tablet = self.login(IPAD)
        reset = Verification.issue(PHONE, "phone", Purpose.PASSWORD_RESET)
        services.reset_password(reset_token=str(reset.id), new_password="N3w!Passw0rd")

        self.assertRevoked(phone.get("/api/v1/auth/me"))
        self.assertRevoked(tablet.get("/api/v1/auth/me"))
        self.assertFalse(UserSession.objects.filter(ended_at__isnull=True).exists())

    def test_purge_deletes_the_sessions(self):
        self.login()
        self.account.purge()
        self.assertFalse(UserSession.objects.exists())


class StorageTests(SessionTestCase):
    def test_the_table_refuses_what_the_brief_refuses(self):
        from django.db import IntegrityError, transaction

        phone = self.login()
        for field, value in (("device_type", "watch"), ("platform", "symbian")):
            with self.assertRaises(IntegrityError), transaction.atomic():
                UserSession.objects.filter(pk=self.sid(phone)).update(**{field: value})

    def test_deleting_the_account_row_deletes_its_sessions_in_the_database(self):
        from django.db import connection

        self.login()
        OutstandingToken.objects.filter(user=self.account).delete()  # its own FK
        with connection.cursor() as cursor:
            cursor.execute("DELETE FROM consumer_account WHERE id = %s", [self.account.pk])
        self.assertFalse(UserSession.objects.exists())


class VerifyConsumerTests(SessionTestCase):
    def verify(self, access):
        return ConsumerAuthServicer()._verify(auth_pb2.VerifyConsumerRequest(token=access))

    def test_a_signed_out_devices_token_is_not_valid(self):
        phone = self.login()
        tablet = self.login(IPAD)
        self.assertTrue(self.verify(tablet.tokens["access"]).valid)
        phone.delete(f"{SESSIONS}/{self.sid(tablet)}")
        self.assertFalse(self.verify(tablet.tokens["access"]).valid)
        self.assertTrue(self.verify(phone.tokens["access"]).valid)

    def test_a_token_without_a_session_is_not_valid(self):
        self.assertFalse(self.verify(self.older_client().tokens["access"]).valid)


FE_DOC = settings.BASE_DIR / "docs" / "DEVICE_SESSIONS_API.md"


class SessionsDocTests(SessionTestCase):
    """docs/DEVICE_SESSIONS_API.md promises every JSON block is a real answer.
    This builds the same sessions and must get the same bodies."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        text = FE_DOC.read_text(encoding="utf-8")
        cls.blocks = [json.loads(b) for b in re.findall(r"```json\n(.*?)\n```", text, re.S)]

    def at(self, day, *hms):
        return datetime(2026, 10, day, *hms, tzinfo=dt_timezone.utc)

    def token_for(self, sid):
        refresh = RefreshToken.for_user(self.account)
        refresh["sid"] = sid
        return str(refresh.access_token)

    def test_the_list_example(self):
        phone = self.login()
        tablet = self.login(IPAD)
        browser = self.login(CHROME)
        example = self.blocks[0]["results"]
        for client, row, created, seen in (
            (phone, example[0], self.at(20, 5, 12, 44), self.at(27, 7, 58, 3)),
            (tablet, example[1], self.at(2, 14, 40, 10), self.at(24, 17, 15, 37)),
            (browser, example[2], self.at(8, 8, 3, 51), self.at(13, 12, 22, 9)),
        ):
            UserSession.objects.filter(pk=self.sid(client)).update(
                id=row["id"], created_at=created, last_seen_at=seen
            )
        phone.credentials(HTTP_AUTHORIZATION=f"Bearer {self.token_for(example[0]['id'])}", **IPHONE)
        cache.set(sessions._CACHE_KEY.format(example[0]["id"]), "live")  # no visit noted

        resp = phone.get(SESSIONS)
        self.assertEqual((resp.status_code, resp.json()), (200, self.blocks[0]))

    def test_the_other_bodies(self):
        phone = self.login()
        self.login(IPAD)
        self.login(GALAXY)
        self.assertEqual(phone.delete(SESSIONS).json(), self.blocks[1])

        tablet = APIClient()
        tablet_sid = UserSession.objects.get(device_name="iPad").pk
        tablet.credentials(HTTP_AUTHORIZATION=f"Bearer {self.token_for(str(tablet_sid))}")
        self.assertEqual(tablet.get("/api/v1/auth/me").json(), self.blocks[2])

        self.assertEqual(phone.delete(f"{SESSIONS}/{self.sid(phone)}").json(), self.blocks[3])
        self.assertEqual(phone.delete(f"{SESSIONS}/nope").json(), self.blocks[4])
        self.assertEqual(APIClient().get(SESSIONS).json(), self.blocks[5])

    def test_no_other_json_blocks(self):
        self.assertEqual(len(self.blocks), 6)
