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
