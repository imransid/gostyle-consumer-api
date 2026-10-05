"""
Talking to the push notification service.

Same rules as apps/salons/booking_api.py:
  - stdlib urllib, no extra library
  - a refusal (4xx) is an answer, returned as (status, body)
  - only "could not reach it" is an exception: PushApiUnavailable
"""

import json
import logging
import urllib.error
import urllib.request

from django.conf import settings

logger = logging.getLogger(__name__)


class PushApiUnavailable(Exception):
    """The push service could not be reached, or did not answer with JSON."""


def register_device(*, user_id, token, platform, app="customer"):
    """PUT /devices. Creates the device, or refreshes it if the token exists."""
    return _send("PUT", "/devices", {
        "userId": str(user_id),
        "app": app,
        "token": token,
        "platform": platform,
    })


def unregister_device(*, token):
    """POST /devices/unregister. Removes the device with this token."""
    return _send("POST", "/devices/unregister", {"token": token})


def _send(method, path, payload):
    request = urllib.request.Request(
        settings.PUSH_API_URL.rstrip("/") + path,
        data=json.dumps(payload).encode(),
        method=method,
        headers={
            "Content-Type": "application/json",
            "x-api-key": settings.PUSH_API_KEY,
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=settings.PUSH_API_TIMEOUT) as res:
            return res.status, _json(res.read())
    except urllib.error.HTTPError as e:
        return e.code, _json(e.read())
    except (urllib.error.URLError, TimeoutError) as e:
        raise PushApiUnavailable(str(e)) from e


def _json(raw):
    try:
        return json.loads(raw or b"{}")
    except ValueError as e:
        logger.warning("push service answered without JSON: %s", raw[:500])
        raise PushApiUnavailable("not JSON") from e