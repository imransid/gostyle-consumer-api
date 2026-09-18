"""
Talking to gostyle-booking-api.

Bookings live in that service and in ITS database (`gostyle_booking`), which
this one has no connection to and no business writing. Everything here is one
HTTP call across the overlay network, and the answer comes back to the caller
very nearly untouched.

The rule that shapes this module:

    A REFUSAL IS AN ANSWER, NOT A FAILURE.

booking-api says 409 when the slot went to someone else between the customer
picking it and pressing Book, and 422 when the payload is wrong. Both are
things the app has a screen for. `urllib` raises `HTTPError` for either one,
so the whole point of `_send` is to catch that and hand the status and body
back as an ordinary return value. Only a booking-api that cannot be reached,
or that answers with something other than JSON, is an exception here.

Stdlib `urllib`, not `requests`, because that is what this project already
uses to call another service (apps/accounts/notifications.py) and because
`requests` is not in requirements.txt — the previous version of this module
imported it anyway, and would have raised ImportError the first time anything
called it.
"""

import json
import logging
import urllib.error
from datetime import datetime, timezone as dt_timezone
import urllib.parse
import urllib.request

from django.conf import settings

logger = logging.getLogger(__name__)

# How much of an unparseable response reaches the log. Enough to recognise an
# nginx error page or a stack trace, not enough to dump a payload into Loki.
LOG_BODY_LIMIT = 500


class BookingApiUnavailable(Exception):
    """booking-api could not be reached, or did not answer with JSON.

    NOT raised for 4xx or 5xx: those are answers, and they are returned. This
    means the network failed, the service is down, or something in front of it
    (nginx, a proxy) replied with HTML.
    """


def create_booking(body, *, authorization, idempotency_key=None, tenant_id=None):
    """
    POST /v1/booking, returning (status_code, parsed_body) exactly as given.

    `body` is the caller's RAW request bytes, forwarded verbatim. Re-encoding
    it here would round-trip every price through a Python float and reorder
    the keys, and booking-api hashes the request body to recognise a retry —
    so a re-serialised body is a different request to it, which is the one
    thing idempotency must not be.

    `authorization` is the customer's own bearer header, passed straight
    through: booking-api resolves who is booking from that token, and this
    service does not second-guess it.
    """
    headers = {
        "Content-Type": "application/json",
        "Authorization": authorization,
    }

    # Both are forwarded ONLY when the caller sent them. An invented
    # Idempotency-Key would make every retry a new booking; an invented tenant
    # would stamp someone else's rows.
    if idempotency_key:
        headers["Idempotency-Key"] = idempotency_key
    if tenant_id:
        headers["X-Tenant-Id"] = tenant_id

    return _send("POST", "/v1/mobile-booking", headers=headers, body=body)


def read_booking(booking_id, *, authorization, tenant_id=None):
    """
    GET /v1/mobile-booking/<id>, returning (status, parsed body) as given.

    A booking the caller may not see comes back 404, never 403 — an outsider
    should not learn that a booking id exists. That rule is booking-api's;
    this forwards its answer unchanged.
    """
    headers = {"Authorization": authorization}
    if tenant_id:
        headers["X-Tenant-Id"] = tenant_id

    return _send("GET", f"/v1/mobile-booking/{booking_id}", headers=headers)


def list_bookings(query, *, authorization, tenant_id=None):
    """
    GET /v1/mobile-booking?<query>, returning (status, parsed body) as given.

    `query` is the already-encoded query string. WHOSE BOOKINGS IS NOT IN IT:
    booking-api reads the customer off the bearer token, exactly as it does
    for create and read, and a customer id on this URL would be an
    enumeration of every booking in the system behind one valid login.

    The answer is a page of SUMMARIES without `salon`, `can_cancel` or
    `can_reschedule` — booking-api stores a branch id and cannot resolve any
    of the three (its booking-list.md §9). Each row carries `salon_id`, and
    filling those in is this service's job, in views.BookingListView.
    """
    headers = {"Authorization": authorization}
    if tenant_id:
        headers["X-Tenant-Id"] = tenant_id

    path = "/v1/mobile-booking"
    if query:
        path = f"{path}?{query}"

    return _send("GET", path, headers=headers)


def busy_intervals(
    branch_id, staff_ids, window_start, window_end, *, authorization, tenant_id=None
):
    """
    When these stylists are already occupied, from the service that knows.

    THE BUG THIS ENDS. The slot picker built its grid from `booking` in the
    PLATFORM database — a table booking-api has never written to, because
    bookings live in ITS database. So the picker computed free time against
    zero bookings and offered slots that were already sold; the customer
    picked one and the booking was refused, naming the stylist.

    Returns `(intervals, bookable_window)` — the occupied spans as
    `(staff_id, start, end)` with aware UTC datetimes, and the minutes-from-
    midnight window the engine will actually search (None if it did not say). NEVER an empty list on failure: an empty list means "everyone is
    free", which is precisely the wrong answer here — it would put the picker
    straight back to offering sold slots, silently.
    """
    query = urllib.parse.urlencode({
        "branchId": str(branch_id),
        "staffIds": ",".join(str(s) for s in staff_ids),
        "from": window_start.astimezone(dt_timezone.utc).isoformat(),
        "to": window_end.astimezone(dt_timezone.utc).isoformat(),
    })

    # THE CALLER'S OWN TOKEN. `/busy` lives on booking-api's mobile-booking
    # controller, which is behind its auth guard like every route on it — so
    # calling without one is a 401, which this turns into a 503 and the app
    # reads as "booking is down". It was exactly that for one deploy.
    headers = {"Authorization": authorization}
    if tenant_id:
        headers["X-Tenant-Id"] = str(tenant_id)

    status, body = _send("GET", f"/v1/mobile-booking/busy?{query}", headers=headers)
    if status != 200 or not isinstance(body, dict):
        # The status is IN THE MESSAGE. A 401 here means the token did not
        # travel, a 404 means the endpoint is not deployed, and a 500 is
        # booking-api's own problem — three different fixes that otherwise
        # all surface as one opaque 503.
        raise BookingApiUnavailable(
            f"booking-api answered {status} for the busy window"
        )

    intervals = [
        (
            row["staff_id"],
            datetime.fromisoformat(row["start_at"].replace("Z", "+00:00")),
            datetime.fromisoformat(row["end_at"].replace("Z", "+00:00")),
        )
        for row in body.get("busy") or []
    ]

    # THE ENGINE'S BOOKABLE DAY, read rather than copied. It searches a fixed
    # window and nothing outside it, whatever hours a branch keeps — so a
    # salon opening at 09:00 had its first hour offered here and refused
    # there. None when an older booking-api does not send it, and the caller
    # then clamps nothing, which is exactly the behaviour it had before.
    day = body.get("day") or {}
    window = (
        (day.get("from_min"), day.get("to_min"))
        if isinstance(day.get("from_min"), int) and isinstance(day.get("to_min"), int)
        else None
    )
    return intervals, window


def patch_booking(booking_id, body, *, authorization, idempotency_key=None, tenant_id=None):
    """
    PATCH /v1/mobile-booking/<id> — records the payment once the gateway answers.

    Same raw-body rule as create: the bytes go across as they arrived, because
    re-encoding rounds money through a float.
    """
    headers = {
        "Content-Type": "application/json",
        "Authorization": authorization,
    }
    if idempotency_key:
        headers["Idempotency-Key"] = idempotency_key
    if tenant_id:
        headers["X-Tenant-Id"] = tenant_id

    return _send("PATCH", f"/v1/mobile-booking/{booking_id}", headers=headers, body=body)


def get_branch_services(tenant_id, branch_id):
    """
    Services a branch offers, as booking-api reads them from the platform.

    Nothing in this service calls this today — the services tab reads the
    platform tables directly (selectors.salon_services). It is kept because it
    documents the one other endpoint we integrate with, and returns None on
    failure exactly as the version before it did.
    """
    query = urllib.parse.urlencode({"tenantId": tenant_id, "branchId": branch_id})
    try:
        status, body = _send("GET", f"/v1/services-directory/services?{query}")
    except BookingApiUnavailable:
        return None
    return body if status == 200 else None


def _send(method, path, *, headers=None, body=None):
    """One request to booking-api. Returns (status, parsed body or None)."""
    url = settings.BOOKING_API_URL.rstrip("/") + path
    request = urllib.request.Request(
        url, data=body, headers=headers or {}, method=method
    )

    try:
        with urllib.request.urlopen(
            request, timeout=settings.BOOKING_API_TIMEOUT
        ) as response:
            return response.status, _parse(response.read(), response.status, url)
    except urllib.error.HTTPError as exc:
        # 409 slot_taken, 422 validation_error, 401 from its own auth — every
        # one of them is the answer the customer needs to see.
        return exc.code, _parse(exc.read(), exc.code, url)
    except OSError as exc:
        # URLError (connection refused, DNS, TLS) and TimeoutError are both
        # OSError, and HTTPError is already handled above.
        logger.warning(
            "booking-api unreachable: %s %s (%s)", method, url, exc,
            extra={"booking_api_url": url},
        )
        raise BookingApiUnavailable(str(exc)) from exc


def _parse(raw, status, url):
    """The response body as JSON, or None when there is no body at all."""
    if not raw or not raw.strip():
        # 204, or a 5xx with nothing in it. A booking response always has a
        # body, so the caller decides what an empty one means.
        return None

    try:
        return json.loads(raw)
    except ValueError as exc:
        # An HTML error page from a proxy, most likely. Passing it through
        # would put markup where the app expects a booking, so this is a
        # failure rather than an answer.
        logger.warning(
            "booking-api answered %s with non-JSON at %s: %r",
            status, url, raw[:LOG_BODY_LIMIT],
            extra={"booking_api_status": status, "booking_api_url": url},
        )
        raise BookingApiUnavailable("booking-api did not answer with JSON") from exc
