"""
Talking to gostyle-api (the platform): the story engagement calls.

Same rules as apps/salons/booking_api.py and apps/accounts/push_api.py:
stdlib urllib, URL and timeout in settings. One difference: these calls are
tracking, so NOTHING HERE RAISES. A failure is logged and dropped, and the
customer's own request goes on as if the call never happened.

The three routes are public on gostyle-api. It tells viewers apart, and rate
limits, by the caller's address and user agent, so the customer's own are
passed on (X-Forwarded-For, User-Agent). Without them every customer would
look like this server: one viewer, one shared rate limit.
"""

import json
import logging
import urllib.error
import urllib.request

from django.conf import settings

logger = logging.getLogger(__name__)


def record_view(story_id, *, session_id, max_frame, ip=None, user_agent=None):
    """POST /v2/storefront/stories/{id}/view. A retry only raises maxFrame."""
    _send(story_id, "view", {"sessionId": session_id, "maxFrame": max_frame}, ip, user_agent)


def record_booking(story_id, *, session_id, booking_id, service_id=None, ip=None, user_agent=None):
    """POST /v2/storefront/stories/{id}/booking. Once per booking, ever."""
    payload = {"bookingId": booking_id, "serviceId": service_id, "sessionId": session_id}
    _send(story_id, "booking", payload, ip, user_agent)


def record_profile_visit(story_id, *, session_id, ip=None, user_agent=None):
    """POST /v2/storefront/stories/{id}/profile-visit. Every visit counts."""
    _send(story_id, "profile-visit", {"sessionId": session_id}, ip, user_agent)


def _send(story_id, action, payload, ip, user_agent):
    headers = {"Content-Type": "application/json"}
    if ip:
        headers["X-Forwarded-For"] = ip
    if user_agent:
        headers["User-Agent"] = user_agent
    request = urllib.request.Request(
        f"{settings.GOSTYLE_API_URL.rstrip('/')}/v2/storefront/stories/{story_id}/{action}",
        data=json.dumps(payload).encode(),
        method="POST",
        headers=headers,
    )
    try:
        with urllib.request.urlopen(request, timeout=settings.GOSTYLE_API_TIMEOUT) as res:
            status = res.status
    except urllib.error.HTTPError as e:
        status = e.code
    except Exception as exc:  # unreachable, timed out, refused: never the customer's problem
        # One line, no traceback: while gostyle-api is down this runs on
        # every story request.
        logger.warning("story %s %s not recorded: gostyle-api unreachable (%s)", story_id, action, exc)
        return
    if status != 204:
        logger.warning("story %s %s not recorded: gostyle-api answered %s", story_id, action, status)
