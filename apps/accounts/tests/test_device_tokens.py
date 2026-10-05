from unittest.mock import patch

from django.contrib.auth import get_user_model
from rest_framework.test import APITestCase

from apps.accounts.push_api import PushApiUnavailable

URL = "/api/v1/me/device-tokens"
TOKEN = "fcm-token-1234567890"


class DeviceTokensTests(APITestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user(
            phone="+971500000002", password="x"
        )
        self.user.account_verified = True
        self.user.save()
        self.client.force_authenticate(self.user)

    def test_rejects_a_call_without_login(self):
        self.client.force_authenticate(None)
        res = self.client.post(URL, {"token": TOKEN, "platform": "ios"}, format="json")
        self.assertEqual(res.status_code, 401)

    def test_rejects_an_unverified_account(self):
        self.user.account_verified = False
        self.user.save()
        res = self.client.post(URL, {"token": TOKEN, "platform": "ios"}, format="json")
        self.assertEqual(res.status_code, 403)

    @patch("apps.accounts.push_api.register_device", return_value=(200, {"ok": True}))
    def test_registers_under_the_logged_in_user(self, register):
        res = self.client.post(URL, {"token": TOKEN, "platform": "ios"}, format="json")
        self.assertEqual(res.status_code, 204)
        register.assert_called_once_with(
            user_id=self.user.id, token=TOKEN, platform="ios"
        )

    @patch("apps.accounts.push_api.register_device", return_value=(200, {"ok": True}))
    def test_ignores_a_user_id_in_the_body(self, register):
        body = {"token": TOKEN, "platform": "ios", "userId": "someone-else"}
        self.client.post(URL, body, format="json")
        self.assertEqual(register.call_args.kwargs["user_id"], self.user.id)

    def test_rejects_an_unknown_platform(self):
        res = self.client.post(URL, {"token": TOKEN, "platform": "web"}, format="json")
        self.assertEqual(res.status_code, 422)
        self.assertEqual(res.json()["code"], "validation_error")

    @patch("apps.accounts.push_api.register_device", side_effect=PushApiUnavailable("down"))
    def test_says_503_when_the_push_service_is_down(self, _register):
        res = self.client.post(URL, {"token": TOKEN, "platform": "ios"}, format="json")
        self.assertEqual(res.status_code, 503)
        self.assertEqual(res.json()["code"], "push_unavailable")

    @patch("apps.accounts.push_api.unregister_device", return_value=(200, {"ok": True}))
    def test_delete_unregisters_the_token(self, unregister):
        res = self.client.delete(URL, {"token": TOKEN}, format="json")
        self.assertEqual(res.status_code, 204)
        unregister.assert_called_once_with(token=TOKEN)