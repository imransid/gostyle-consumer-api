"""
gunicorn's access line never carries the user lookup's contact.

The lines are rendered with the format docker-compose.yml actually starts
gunicorn with, read from that file, so a change to the format is tested too.

Skipped where gunicorn is not installed (a dev venv that runs `runserver`);
CI installs requirements.txt, so it runs there.
"""

import importlib.util
import json
import types
import unittest
from datetime import timedelta
from pathlib import Path

import yaml
from django.test import SimpleTestCase

HAS_GUNICORN = importlib.util.find_spec("gunicorn") is not None
COMPOSE = Path(__file__).resolve().parents[2] / "docker-compose.yml"


def compose_command():
    return yaml.safe_load(COMPOSE.read_text())["services"]["api"]["command"]


def flag(command, name):
    return command[command.index(name) + 1]


@unittest.skipUnless(HAS_GUNICORN, "gunicorn is not installed")
class AccessLoggerTests(SimpleTestCase):
    def setUp(self):
        from gunicorn.config import Config
        from gunicorn.glogging import SafeAtoms

        from config.gunicorn_logging import AccessLogger

        self.format = flag(compose_command(), "--access-logformat")
        cfg = Config()
        cfg.set("access_log_format", self.format)
        self.logger = AccessLogger(cfg)
        self.safe_atoms = SafeAtoms

    def line(self, path, query):
        """The access line gunicorn would write for GET path?query."""
        environ = {
            "REQUEST_METHOD": "GET",
            "RAW_URI": f"{path}?{query}" if query else path,
            "PATH_INFO": path,
            "QUERY_STRING": query,
            "SERVER_PROTOCOL": "HTTP/1.1",
            "REMOTE_ADDR": "10.0.0.2",
        }
        resp = types.SimpleNamespace(status="404 Not Found", sent=58,
                                     headers=[("X-Request-ID", "abc123")])
        req = types.SimpleNamespace(headers=[])
        atoms = self.logger.atoms(resp, req, environ, timedelta(milliseconds=12))
        return self.format % self.safe_atoms(atoms), atoms

    def test_compose_starts_gunicorn_with_this_logger(self):
        self.assertEqual(flag(compose_command(), "--logger-class"),
                         "config.gunicorn_logging.AccessLogger")

    def test_the_lookup_logs_its_path_and_no_query(self):
        for path in ("/api/v1/user/lookup", "/api/v1/user/lookup/",
                     "/API/V1/USER/LOOKUP", "//api/v1//user/lookup"):
            with self.subTest(path):
                text, atoms = self.line(path, "contact=rana%40example.com")
                entry = json.loads(text)
                self.assertEqual((entry["path"], entry["query"]), (path, ""))
                self.assertEqual(entry["request_id"], "abc123")
                # Every atom that could carry it, not only the one in use.
                for key in ("q", "r", "{query_string}e", "{raw_uri}e"):
                    self.assertNotIn("rana", str(atoms[key]))

    def test_every_other_path_keeps_its_query(self):
        for path in ("/api/v1/salon/abc/stylists", "/api/v1/user/lookups"):
            with self.subTest(path):
                text, atoms = self.line(path, "keep=1")
                self.assertEqual(json.loads(text)["query"], "keep=1")
                self.assertEqual(atoms["r"], f"GET {path}?keep=1 HTTP/1.1")

    def test_the_phone_s_position_is_cut_out_and_the_rest_kept(self):
        # MAP_COUNTRY_V1: user_latitude and user_longitude only pick the country.
        query = ("latitude=25.2048&longitude=55.2708&latitudeDelta=0.12&longitudeDelta=0.08"
                 "&zoom=12&limit=100&user_latitude=25.20&user_longitude=55.27")
        kept = "latitude=25.2048&longitude=55.2708&latitudeDelta=0.12&longitudeDelta=0.08&zoom=12&limit=100"
        text, atoms = self.line("/api/v1/discover/map", query)
        self.assertEqual(json.loads(text)["query"], kept)
        self.assertEqual(atoms["r"], f"GET /api/v1/discover/map?{kept} HTTP/1.1")
        self.assertEqual(atoms["{query_string}e"], kept)
        self.assertEqual(atoms["{raw_uri}e"], f"/api/v1/discover/map?{kept}")
        for key in ("q", "r", "{query_string}e", "{raw_uri}e"):
            self.assertNotIn("user_", str(atoms[key]))

    def test_the_position_alone_leaves_no_query(self):
        text, atoms = self.line("/api/v1/discover/map", "user_longitude=55.27&user_latitude=25.20")
        self.assertEqual(json.loads(text)["query"], "")
        self.assertEqual(atoms["r"], "GET /api/v1/discover/map HTTP/1.1")

    def test_the_position_is_cut_out_on_any_path_and_in_any_spelling(self):
        text, _ = self.line("/api/v1/discover", "user%5Flatitude=25.2&user_longitude=55.27&page=2")
        self.assertEqual(json.loads(text)["query"], "page=2")
        # A name that only looks like it stays.
        text, _ = self.line("/api/v1/discover/map", "xuser_latitude=1&origin_latitude=25.19")
        self.assertEqual(json.loads(text)["query"], "xuser_latitude=1&origin_latitude=25.19")
