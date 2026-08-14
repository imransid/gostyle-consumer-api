"""
Tests for the logging pipeline's two contracts.

Both matter to something outside this repo, which is why they are pinned here:

  1. **One line, valid JSON.** Loki and the Docker log driver are line-based. A
     traceback that spans lines, or a value that is not serializable, would
     otherwise silently split or drop a log entry.

  2. **A safe, echoed request ID.** X-Request-ID is client-supplied, so it is
     sanitized before it reaches a response header or a log line. If it were
     not, a newline in it would let a caller forge log entries. The echo itself
     is what lets nginx (`$sent_http_x_request_id`) and gunicorn
     (`%({X-Request-ID}o)s`) tag their own lines with the same ID.
"""

import json
import logging

from django.test import SimpleTestCase

from config.observability import (
    JsonFormatter,
    RequestIDFilter,
    RequestIDMiddleware,
    request_id_var,
    sanitize_request_id,
)


def make_record(**kwargs):
    defaults = {
        "name": "apps.accounts",
        "level": logging.INFO,
        "pathname": __file__,
        "lineno": 1,
        "msg": "hello",
        "args": (),
        "exc_info": None,
    }
    defaults.update(kwargs)
    return logging.LogRecord(**defaults)


class JsonFormatterTests(SimpleTestCase):
    def setUp(self):
        self.formatter = JsonFormatter()

    def test_emits_the_core_fields(self):
        payload = json.loads(self.formatter.format(make_record()))

        self.assertEqual(payload["level"], "INFO")
        self.assertEqual(payload["logger"], "apps.accounts")
        self.assertEqual(payload["msg"], "hello")
        self.assertTrue(payload["ts"].endswith("Z"))

    def test_interpolates_message_args(self):
        record = make_record(msg="salon %s not found", args=("abc",))
        payload = json.loads(self.formatter.format(record))

        self.assertEqual(payload["msg"], "salon abc not found")

    def test_extras_become_fields(self):
        record = make_record()
        record.salon_id = 7

        payload = json.loads(self.formatter.format(record))

        self.assertEqual(payload["salon_id"], 7)

    def test_the_http_request_is_not_serialized(self):
        # django.request logs with extra={"request": <HttpRequest>, ...}. It is
        # neither JSON nor useful stringified, and it can hold credentials.
        record = make_record()
        record.request = object()
        record.status_code = 500

        payload = json.loads(self.formatter.format(record))

        self.assertNotIn("request", payload)
        self.assertEqual(payload["status_code"], 500)

    def test_a_traceback_stays_on_one_line(self):
        try:
            raise ValueError("boom")
        except ValueError:
            import sys

            record = make_record(
                level=logging.ERROR, msg="failed", exc_info=sys.exc_info()
            )

        line = self.formatter.format(record)

        self.assertNotIn("\n", line)
        payload = json.loads(line)
        self.assertIn("ValueError: boom", payload["exc"])

    def test_a_multiline_message_stays_on_one_line(self):
        line = self.formatter.format(make_record(msg="first\nsecond"))

        self.assertNotIn("\n", line)
        self.assertEqual(json.loads(line)["msg"], "first\nsecond")

    def test_an_unserializable_extra_does_not_lose_the_line(self):
        record = make_record()
        record.weird = object()

        payload = json.loads(self.formatter.format(record))

        self.assertEqual(payload["msg"], "hello")
        self.assertIsInstance(payload["weird"], str)

    def test_the_request_id_rides_along(self):
        token = request_id_var.set("abc123")
        try:
            payload = json.loads(self.formatter.format(make_record()))
        finally:
            request_id_var.reset(token)

        self.assertEqual(payload["request_id"], "abc123")

    def test_no_request_id_field_outside_a_request(self):
        # Management commands and startup logging have no request.
        payload = json.loads(self.formatter.format(make_record()))

        self.assertNotIn("request_id", payload)

    def test_the_filters_placeholder_never_reaches_the_json(self):
        # The real handler runs RequestIDFilter before the formatter, and the
        # filter writes "-" so that the plain formatter's %(request_id)s has
        # something to print. That placeholder is not a request ID.
        record = make_record()
        RequestIDFilter().filter(record)

        payload = json.loads(self.formatter.format(record))

        self.assertNotIn("request_id", payload)

    def test_the_filters_real_value_does_reach_the_json_once(self):
        record = make_record()
        token = request_id_var.set("abc123")
        try:
            RequestIDFilter().filter(record)
            line = self.formatter.format(record)
        finally:
            request_id_var.reset(token)

        self.assertEqual(line.count("abc123"), 1)
        self.assertEqual(json.loads(line)["request_id"], "abc123")


class RequestIDFilterTests(SimpleTestCase):
    def test_sets_a_placeholder_so_the_plain_format_never_breaks(self):
        record = make_record()

        RequestIDFilter().filter(record)

        self.assertEqual(record.request_id, "-")

    def test_prefers_the_current_request(self):
        record = make_record()
        token = request_id_var.set("deadbeef")
        try:
            RequestIDFilter().filter(record)
        finally:
            request_id_var.reset(token)

        self.assertEqual(record.request_id, "deadbeef")


class SanitizeRequestIDTests(SimpleTestCase):
    def test_strips_anything_that_could_forge_a_log_entry(self):
        self.assertEqual(
            sanitize_request_id('abc\n{"level":"ERROR"}'), "abclevelERROR"
        )

    def test_truncates(self):
        self.assertEqual(len(sanitize_request_id("a" * 500)), 64)

    def test_keeps_a_normal_uuid_hex_intact(self):
        self.assertEqual(sanitize_request_id("9f8c2e1a4b"), "9f8c2e1a4b")


class RequestIDMiddlewareTests(SimpleTestCase):
    def call(self, meta=None, get_response=None):
        class Request:
            pass

        request = Request()
        request.META = meta or {}
        middleware = RequestIDMiddleware(get_response or (lambda r: {}))
        return request, middleware(request)

    def test_generates_an_id_when_the_client_sends_none(self):
        request, response = self.call()

        self.assertEqual(len(request.request_id), 32)
        self.assertEqual(response["X-Request-ID"], request.request_id)

    def test_honours_a_client_supplied_id(self):
        request, response = self.call({"HTTP_X_REQUEST_ID": "trace-42"})

        self.assertEqual(request.request_id, "trace-42")
        self.assertEqual(response["X-Request-ID"], "trace-42")

    def test_sanitizes_a_hostile_id_before_echoing_it(self):
        _, response = self.call({"HTTP_X_REQUEST_ID": "bad\r\nSet-Cookie: x=1"})

        self.assertEqual(response["X-Request-ID"], "badSet-Cookiex1")

    def test_falls_back_when_nothing_survives_sanitizing(self):
        request, _ = self.call({"HTTP_X_REQUEST_ID": "!!!!"})

        self.assertEqual(len(request.request_id), 32)

    def test_the_id_is_visible_to_logging_during_the_view(self):
        seen = {}

        def view(request):
            seen["value"] = request_id_var.get()
            return {}

        self.call({"HTTP_X_REQUEST_ID": "abc"}, get_response=view)

        self.assertEqual(seen["value"], "abc")

    def test_the_id_outlives_the_middleware_chain(self):
        # Django logs "Not Found: /x" and every other 4xx/5xx from
        # BaseHandler.get_response, which runs AFTER the middleware chain
        # returns. If the middleware reset the context variable on the way out,
        # those lines -- the ones most worth correlating -- would lose their
        # request ID. See the docstring on RequestIDMiddleware.
        self.call({"HTTP_X_REQUEST_ID": "still-here"})

        self.assertEqual(request_id_var.get(), "still-here")

    def test_each_request_overwrites_the_previous_id(self):
        self.call({"HTTP_X_REQUEST_ID": "first"})
        self.call({"HTTP_X_REQUEST_ID": "second"})

        self.assertEqual(request_id_var.get(), "second")
