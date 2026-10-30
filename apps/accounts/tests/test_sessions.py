"""
Sessions: GET /api/v1/me/sessions, session creation on login/register,
and location lookup from GeoIP.

Each test uses the real URL conf and the APIClient like the other test files.
"""

from unittest.mock import patch

from django.test import SimpleTestCase, TestCase
from rest_framework.test import APIClient

from apps.accounts.models import ConsumerAccount, Session
from apps.accounts.session_views import _device_name_from_ua, _trusted_ip
from apps.accounts.tests.test_auth import PASSWORD, AuthTestCase

SESSIONS = "/api/v1/me/sessions"


class SessionListTests(AuthTestCase):
    """GET /api/v1/me/sessions."""

    def setUp(self):
        super().setUp()
        self.register_and_authenticate()
        self.account = ConsumerAccount.objects.get(phone=self.phone)

    def test_lists_the_session_created_at_register(self):
        resp = self.client.get(SESSIONS)

        self.assertEqual(resp.status_code, 200)
        # Registration creates one session.
        self.assertEqual(len(resp.json()), 1)
        session = resp.json()[0]
        self.assertIn("id", session)
        self.assertIn("device_name", session)
        self.assertIn("location", session)
        self.assertIn("full_location", session)
        self.assertIn("ip_address", session)
        self.assertIn("last_seen_at", session)
        self.assertIn("created_at", session)

    def test_login_adds_a_second_session(self):
        # Verify the phone so login works.
        self.verify_own_contact()
        self.anonymous()

        self.client.post(
            "/api/v1/auth/login",
            {"destination_type": "phone", "destination": self.phone, "password": PASSWORD},
            format="json",
        )
        # Authenticate again to call /me/sessions.
        token = self.client.post(
            "/api/v1/auth/login",
            {"destination_type": "phone", "destination": self.phone, "password": PASSWORD},
            format="json",
        ).data["access"]
        self.client.credentials(HTTP_AUTHORIZATION=f"Bearer {token}")

        resp = self.client.get(SESSIONS)

        self.assertEqual(resp.status_code, 200)
        # register (1) + two logins (2) = 3 sessions
        self.assertEqual(len(resp.json()), 3)

    def test_401_without_a_token(self):
        self.anonymous()

        self.assertEqual(self.client.get(SESSIONS).status_code, 401)

    def test_sessions_are_ordered_most_recent_first(self):
        # Create a second session.
        Session.objects.create(account=self.account, device_name="Older")

        resp = self.client.get(SESSIONS)

        dates = [s["last_seen_at"] for s in resp.json()]
        self.assertEqual(dates, sorted(dates, reverse=True))


class SessionOnLoginTests(AuthTestCase):
    """A session row is created on each login."""

    def test_login_creates_a_session_with_ip_and_ua(self):
        self.register_and_authenticate()
        self.verify_own_contact()
        self.anonymous()

        self.client.post(
            "/api/v1/auth/login",
            {"destination_type": "phone", "destination": self.phone, "password": PASSWORD},
            format="json",
            HTTP_USER_AGENT="GoStyle/3.1.0 (iPhone15,4; iOS 18.1)",
            REMOTE_ADDR="203.0.113.10",
        )

        account = ConsumerAccount.objects.get(phone=self.phone)
        session = Session.objects.filter(account=account).order_by("-created_at").first()
        self.assertIsNotNone(session)
        self.assertEqual(session.ip_address, "203.0.113.10")
        self.assertEqual(session.device_name, "iPhone15,4")

    def test_login_prefers_x_real_ip_over_remote_addr(self):
        self.register_and_authenticate()
        self.verify_own_contact()
        self.anonymous()

        self.client.post(
            "/api/v1/auth/login",
            {"destination_type": "phone", "destination": self.phone, "password": PASSWORD},
            format="json",
            REMOTE_ADDR="10.0.0.1",
            HTTP_X_REAL_IP="203.0.113.20",
        )

        account = ConsumerAccount.objects.get(phone=self.phone)
        session = Session.objects.filter(account=account).order_by("-created_at").first()
        self.assertEqual(session.ip_address, "203.0.113.20")


class DeviceNameTests(SimpleTestCase):
    """User-Agent → device_name extraction."""
    databases = []

    def test_ios_gostyle_app(self):
        ua = "GoStyle/3.1.0 (iPhone15,4; iOS 18.1)"
        self.assertEqual(_device_name_from_ua(ua), "iPhone15,4")

    def test_android_gostyle_app(self):
        ua = "GoStyle/3.1.0 (Samsung SM-S928B; Android 15)"
        self.assertEqual(_device_name_from_ua(ua), "Samsung SM-S928B")

    def test_generic_iphone(self):
        ua = "Mozilla/5.0 (iPhone; CPU iPhone OS 18_1 like Mac OS X)"
        self.assertEqual(_device_name_from_ua(ua), "iPhone")

    def test_generic_android(self):
        ua = "Mozilla/5.0 (Linux; Android 15; Pixel 9 Pro Build/AP4A) AppleWebKit/537.36"
        self.assertEqual(_device_name_from_ua(ua), "Pixel 9 Pro")

    def test_mac_browser(self):
        ua = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36"
        self.assertEqual(_device_name_from_ua(ua), "Mac")

    def test_windows_browser(self):
        ua = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
        self.assertEqual(_device_name_from_ua(ua), "Windows")

    def test_empty_ua(self):
        self.assertEqual(_device_name_from_ua(""), "")

    def test_unknown_ua(self):
        self.assertEqual(_device_name_from_ua("curl/8.4.0"), "")


class TrustedIpTests(SimpleTestCase):
    """_trusted_ip reads CF-Connecting-IP, X-Real-IP, right-most X-Forwarded-For, then REMOTE_ADDR."""
    databases = []

    def _fake_request(self, **meta):
        class FakeRequest:
            META = meta
        return FakeRequest()

    def test_cf_connecting_ip_wins(self):
        req = self._fake_request(
            HTTP_CF_CONNECTING_IP="198.51.100.5",
            HTTP_X_REAL_IP="203.0.113.10",
            REMOTE_ADDR="10.0.0.1",
        )
        self.assertEqual(_trusted_ip(req), "198.51.100.5")

    def test_x_real_ip_wins_over_remote_addr(self):
        req = self._fake_request(
            HTTP_X_REAL_IP="203.0.113.10",
            REMOTE_ADDR="10.0.0.1",
        )
        self.assertEqual(_trusted_ip(req), "203.0.113.10")

    def test_x_forwarded_for_takes_right_most(self):
        req = self._fake_request(
            HTTP_X_FORWARDED_FOR="1.1.1.1, 2.2.2.2, 203.0.113.50",
            REMOTE_ADDR="10.0.0.1",
        )
        self.assertEqual(_trusted_ip(req), "203.0.113.50")

    def test_falls_back_to_remote_addr(self):
        req = self._fake_request(REMOTE_ADDR="192.168.1.1")
        self.assertEqual(_trusted_ip(req), "192.168.1.1")

    def test_strips_whitespace(self):
        req = self._fake_request(HTTP_X_REAL_IP="  203.0.113.10  ")
        self.assertEqual(_trusted_ip(req), "203.0.113.10")


class GeoIpTests(SimpleTestCase):
    """city_for_ip with private addresses, missing database, and mock reader."""
    databases = []

    def test_private_ips_return_none(self):
        from apps.accounts.geoip import city_for_ip

        for ip in ["127.0.0.1", "10.0.0.1", "192.168.1.1", "::1", "fc00::1", "169.254.1.1"]:
            with self.subTest(ip=ip):
                self.assertIsNone(city_for_ip(ip))

    def test_none_ip_returns_none(self):
        from apps.accounts.geoip import city_for_ip

        self.assertIsNone(city_for_ip(None))
        self.assertIsNone(city_for_ip(""))

    @patch("apps.accounts.geoip._get_reader", return_value=None)
    def test_no_database_returns_none(self, _mock):
        from apps.accounts.geoip import city_for_ip

        self.assertIsNone(city_for_ip("203.0.113.10"))

    def test_city_and_country_code_format_dhaka_bd(self):
        from unittest.mock import MagicMock
        from apps.accounts.geoip import city_for_ip, location_details_for_ip

        mock_resp = MagicMock()
        mock_resp.city.names = {"en": "Dhaka"}
        mock_resp.country.names = {"en": "Bangladesh"}
        mock_resp.country.iso_code = "BD"
        mock_resp.subdivisions.most_specific.names = {"en": "Dhaka Division"}
        mock_reader = MagicMock()
        mock_reader.city.return_value = mock_resp

        with patch("apps.accounts.geoip._get_reader", return_value=mock_reader):
            self.assertEqual(city_for_ip("8.8.8.8"), "Dhaka, BD")
            self.assertEqual(
                location_details_for_ip("8.8.8.8"),
                ("Dhaka, BD", "Dhaka, Dhaka Division, Bangladesh"),
            )

    def test_city_and_country_code_format_new_york_usa(self):
        from unittest.mock import MagicMock
        from apps.accounts.geoip import city_for_ip, location_details_for_ip

        mock_resp = MagicMock()
        mock_resp.city.names = {"en": "New York"}
        mock_resp.country.names = {"en": "United States"}
        mock_resp.country.iso_code = "US"
        mock_resp.subdivisions.most_specific.names = {"en": "New York"}
        mock_reader = MagicMock()
        mock_reader.city.return_value = mock_resp

        with patch("apps.accounts.geoip._get_reader", return_value=mock_reader):
            self.assertEqual(city_for_ip("8.8.8.8"), "New York, USA")
            self.assertEqual(
                location_details_for_ip("8.8.8.8"),
                ("New York, USA", "New York, United States"),
            )

    def test_city_and_country_code_format_london_uk(self):
        from unittest.mock import MagicMock
        from apps.accounts.geoip import city_for_ip, location_details_for_ip

        mock_resp = MagicMock()
        mock_resp.city.names = {"en": "London"}
        mock_resp.country.names = {"en": "United Kingdom"}
        mock_resp.country.iso_code = "GB"
        mock_resp.subdivisions.most_specific.names = {"en": "England"}
        mock_reader = MagicMock()
        mock_reader.city.return_value = mock_resp

        with patch("apps.accounts.geoip._get_reader", return_value=mock_reader):
            self.assertEqual(city_for_ip("8.8.8.8"), "London, UK")
            self.assertEqual(
                location_details_for_ip("8.8.8.8"),
                ("London, UK", "London, England, United Kingdom"),
            )

    def test_full_location_with_distinct_subdivision(self):
        from unittest.mock import MagicMock
        from apps.accounts.geoip import location_details_for_ip

        mock_resp = MagicMock()
        mock_resp.city.names = {"en": "Austin"}
        mock_resp.country.names = {"en": "United States"}
        mock_resp.country.iso_code = "US"
        mock_resp.subdivisions.most_specific.names = {"en": "Texas"}
        mock_reader = MagicMock()
        mock_reader.city.return_value = mock_resp

        with patch("apps.accounts.geoip._get_reader", return_value=mock_reader):
            self.assertEqual(
                location_details_for_ip("8.8.8.8"),
                ("Austin, USA", "Austin, Texas, United States"),
            )

    def test_country_returned_when_city_missing(self):
        from unittest.mock import MagicMock
        from apps.accounts.geoip import city_for_ip, location_details_for_ip

        mock_resp = MagicMock()
        mock_resp.city.names = {}
        mock_resp.country.names = {"en": "United Arab Emirates"}
        mock_resp.country.iso_code = "AE"
        mock_resp.subdivisions = None
        mock_reader = MagicMock()
        mock_reader.city.return_value = mock_resp

        with patch("apps.accounts.geoip._get_reader", return_value=mock_reader):
            self.assertEqual(city_for_ip("8.8.8.8"), "United Arab Emirates")
            self.assertEqual(
                location_details_for_ip("8.8.8.8"),
                ("United Arab Emirates", "United Arab Emirates"),
            )

    def test_none_returned_when_both_city_and_country_missing(self):
        from unittest.mock import MagicMock
        from apps.accounts.geoip import city_for_ip

        mock_resp = MagicMock()
        mock_resp.city.names = {}
        mock_resp.country.names = {}
        mock_reader = MagicMock()
        mock_reader.city.return_value = mock_resp

        with patch("apps.accounts.geoip._get_reader", return_value=mock_reader):
            self.assertIsNone(city_for_ip("8.8.8.8"))


class SessionPurgeTests(AuthTestCase):
    """Sessions are cleaned up when an account is purged."""

    def test_purge_removes_sessions(self):
        self.register_and_authenticate()
        account = ConsumerAccount.objects.get(phone=self.phone)
        self.assertTrue(Session.objects.filter(account=account).exists())

        # Schedule and purge.
        from datetime import timedelta
        from io import StringIO

        from django.core.management import call_command
        from django.utils import timezone

        account.request_deletion()
        ConsumerAccount.all_objects.filter(pk=account.pk).update(
            purge_at=timezone.now() - timedelta(minutes=1)
        )
        call_command("purge_due_accounts", stdout=StringIO())

        self.assertFalse(Session.objects.filter(account=account).exists())

