"""Structured logging: one JSON object per line, tagged with a request ID.

Everything this service writes goes to stdout, the container runtime captures it,
and Grafana Alloy ships it to Loki (see docs/OBSERVABILITY.md). Loki does not
index the log *body* -- it indexes labels and then greps -- so a line is only as
queryable as its fields make it. Hence JSON rather than prose: `| json |
level="ERROR" | status >= 500` beats guessing at substrings.

The request ID is what stitches a single request back together across the three
places it gets logged:

  * Django application logs      -- read from the context variable below
  * gunicorn's access line       -- `%({X-Request-ID}o)s` reads it back off the
                                    response header this module sets
  * nginx's access line          -- `$sent_http_x_request_id`, same header

It is deliberately *not* a Loki label. Labels build a stream index, and a value
that is unique per request would make one stream per request; it travels as
structured metadata instead (see observability/alloy/config.alloy).
"""

from __future__ import annotations

import contextvars
import json
import logging
import re
import uuid
from datetime import datetime, timezone

# Set per request by RequestIDMiddleware. Context variables are per-thread and
# per-async-task, so gunicorn's sync workers and any future async view both see
# the value belonging to the request they are actually serving.
request_id_var: contextvars.ContextVar[str] = contextvars.ContextVar(
    "request_id", default=""
)

# An inbound X-Request-ID is attacker-controlled: it ends up in a response header
# and in every log line for the request. A newline in it would forge a log entry,
# and an unbounded one would bloat every line. Keep the safe alphabet only.
_UNSAFE = re.compile(r"[^A-Za-z0-9._-]")
_MAX_REQUEST_ID = 64


def sanitize_request_id(value: str) -> str:
    """Strip an inbound request ID down to something safe to echo and log."""
    return _UNSAFE.sub("", value)[:_MAX_REQUEST_ID]


# Attributes every LogRecord carries. Anything else on a record came from a
# caller's `extra=`, so it is worth emitting; these are either already mapped to
# a JSON field or too noisy to be.
#
# Two are here for specific reasons. `request` is excluded because
# django.request attaches the whole HttpRequest, which is not JSON, not useful
# serialized, and may carry credentials. `request_id` is excluded because
# RequestIDFilter puts a "-" placeholder there for the plain formatter's benefit
# -- the JSON formatter emits the field itself, or omits it entirely.
_STANDARD_ATTRS = frozenset(
    """
    args asctime created exc_info exc_text filename funcName levelname levelno
    lineno message module msecs msg name pathname process processName
    relativeCreated request request_id stack_info taskName thread threadName
    """.split()
)


class RequestIDFilter(logging.Filter):
    """Copy the current request ID onto the record.

    The JSON formatter could read the context variable itself, but a filter also
    makes `%(request_id)s` work for the plain text formatter used in local dev.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        if not getattr(record, "request_id", ""):
            record.request_id = request_id_var.get() or "-"
        return True


class JsonFormatter(logging.Formatter):
    """Render a record as a single-line JSON object.

    json.dumps escapes newlines inside strings, so a multi-line message or a
    traceback still occupies exactly one line -- which is what both the Docker
    log driver and Loki assume.
    """

    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "ts": datetime.fromtimestamp(record.created, timezone.utc)
            .isoformat(timespec="milliseconds")
            .replace("+00:00", "Z"),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
        }

        request_id = getattr(record, "request_id", "") or request_id_var.get()
        if request_id and request_id != "-":
            payload["request_id"] = request_id

        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)
        elif record.exc_text:
            payload["exc"] = record.exc_text
        if record.stack_info:
            payload["stack"] = self.formatStack(record.stack_info)

        for key, value in record.__dict__.items():
            if key not in _STANDARD_ATTRS and key not in payload:
                payload[key] = value

        # default=str keeps one unserializable `extra=` value from losing the
        # whole line; ensure_ascii=False keeps non-Latin text readable in Grafana.
        return json.dumps(payload, default=str, ensure_ascii=False)


class RequestIDMiddleware:
    """Give every request an ID, log under it, and echo it to the client.

    Sits first in MIDDLEWARE so that responses produced by the middleware below
    it -- SecurityMiddleware's HTTPS redirect, CommonMiddleware's 301s -- are
    tagged too.

    Note what this does *not* do: reset the context variable on the way out.
    That looks like a leak, and it is deliberate. Django logs every 4xx and 5xx
    from `BaseHandler.get_response`, *after* the middleware chain has returned:

        response = self._middleware_chain(request)      # <- we return here
        if response.status_code >= 400:
            log_response(...)                           # <- "Not Found: /x"

    Resetting on the way out would therefore strip the request ID from exactly
    the lines most worth correlating. Leaving the value in place costs a stale
    ID on anything a worker logs *between* requests, which for a WSGI app with
    no background threads means startup and shutdown -- and each request
    overwrites it before doing any work of its own.
    """

    request_header = "HTTP_X_REQUEST_ID"
    response_header = "X-Request-ID"

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        request_id = (
            sanitize_request_id(request.META.get(self.request_header, ""))
            or uuid.uuid4().hex
        )
        request.request_id = request_id
        request_id_var.set(request_id)
        response = self.get_response(request)
        response[self.response_header] = request_id
        return response
