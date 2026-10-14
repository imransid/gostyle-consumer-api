"""
Group self check-in (GROUP_SELF_CHECK_IN_V1): a member of an app party checks
in on their own lane with the single check-in route,
POST /api/v1/booking/{id}/check-in, and the lane's booking_id from the party
read. Off by default.
"""

import os
from pathlib import Path
from unittest import mock

from django.test import SimpleTestCase

from config.settings.base import env

FLAG = "GROUP_SELF_CHECK_IN_V1"


class SwitchTests(SimpleTestCase):

    def test_the_flag_is_off_by_default(self):
        self.assertIn(f"{FLAG}=false", Path(".env.example").read_text())
        self.assertIn(f"{FLAG}: ${{{FLAG}:-False}}", Path("docker-compose.yml").read_text())
        source = Path("config/settings/base.py").read_text()
        self.assertIn(f'{FLAG} = env.bool("{FLAG}", default=False)', source)

    def test_unset_means_off(self):
        without = {k: v for k, v in os.environ.items() if k != FLAG}
        with mock.patch.dict(os.environ, without, clear=True):
            self.assertIs(env.bool(FLAG, default=False), False)

    def test_true_turns_it_on_and_a_typo_does_not(self):
        with mock.patch.dict(os.environ, {FLAG: "true"}):
            self.assertIs(env.bool(FLAG, default=False), True)
        with mock.patch.dict(os.environ, {FLAG: "ture"}):
            self.assertIs(env.bool(FLAG, default=False), False)
