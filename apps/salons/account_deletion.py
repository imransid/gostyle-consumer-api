"""
Day 30 of an account deletion: the account's bookings still to come are
cancelled, then the account is purged (apps/accounts/deletion.py,
docs/ACCOUNT_DELETION_FE.md). On the purge day and not when the deletion is
asked, so a customer who cancels the deletion keeps their bookings.

CANCELLED AS THE CUSTOMER, through the very booking-api routes the app's
Cancel buttons use, with a token this server makes for the account
(sessions.open_for_server). So a deletion follows the rules a customer's own
cancel does: the refund bands, the 24 hour late cancel, a party only by its
booker, a routine as a whole. Nothing new in booking-api, and nothing the
business web uses is touched.

What the customer could not cancel in the app is not cancelled here either,
and is logged: a place in someone else's party (it is the booker's), a
routine visit whose routine would not end, anything booking-api refuses.
The purge goes ahead anyway: a deletion is not held up by a booking.

booking-api not reachable, a 5xx, or a list it would not give: Unreachable,
and the caller decides (the purge of that account waits for a later run).
"""

import logging
import urllib.parse
from dataclasses import dataclass, field

from apps.accounts import sessions
from apps.accounts.models import UserSession

from .booking_api import (
    BookingApiUnavailable,
    cancel_booking,
    cancel_group_booking,
    cancel_series_booking,
    list_bookings,
)

logger = logging.getLogger(__name__)

# booking-api's own cap on a page.
PAGE_SIZE = 50
# 50 x 40 = 2,000 rows a shelf, far past any real customer. Only a guard.
MAX_PAGES = 40

# The device name of the session the purge acts in. Nobody sees it: it ends
# in seconds, and the purge deletes every session right after.
SESSION_NAME = "Account deletion"

# What a single booking's history says. booking-api requires a reason.
CANCEL_TEXT = "Cancelled because the customer deleted their account."
# A routine's reason is one of the app's words (booking-api's series cancel).
ROUTINE_REASON = "OTHER"
# The routines that can still be cancelled (booking-api's RoutineStatus).
ROUTINE_LIVE = {"ACTIVE", "PAUSED"}


class Unreachable(Exception):
    """booking-api could not be asked, or failed (5xx). Nothing to decide on."""


@dataclass
class Report:
    """What one account's cancel did."""

    cancelled: int = 0
    # (kind, id, why) for each one left as it was.
    left: list = field(default_factory=list)

    def leave(self, kind, row_id, why):
        self.left.append((kind, row_id, why))


def cancel_bookings_to_come(account):
    """Cancel `account`'s routines and bookings to come. Returns a Report;
    raises Unreachable when booking-api could not be asked."""
    session, access = sessions.open_for_server(account, SESSION_NAME)
    authorization = f"Bearer {access}"
    report = Report()
    try:
        # Routines first: ending one cancels its visits, which the Upcoming
        # list would otherwise show one by one.
        for row in _shelf("recurring", authorization):
            _cancel_routine(row, authorization, report)
        for row in _shelf("upcoming", authorization):
            _cancel_upcoming(row, authorization, report)
    finally:
        sessions.end(UserSession.objects.filter(pk=session.pk))

    for kind, row_id, why in report.left:
        logger.warning(
            "account deletion: %s %s of account %s left as it is: %s",
            kind, row_id, account.pk, why,
        )
    return report


def _shelf(name, authorization):
    """Every row on one shelf of the customer's list, page by page. Read in
    full before anything is cancelled, so a cancel cannot move a row onto a
    page already read."""
    rows, page = [], 1
    while True:
        query = urllib.parse.urlencode({"filter": name, "page": page, "pageSize": PAGE_SIZE})
        code, body = _ask(list_bookings, query, authorization=authorization)
        if code != 200 or not isinstance(body, dict):
            # Not a refusal to decide on: without the list nothing is known.
            raise Unreachable(f"the {name} list answered {code}")
        found = [row for row in body.get("results") or [] if isinstance(row, dict)]
        rows.extend(found)
        count = body.get("count")
        if not found or not isinstance(count, int) or page * PAGE_SIZE >= count:
            return rows
        if page >= MAX_PAGES:
            logger.warning("account deletion: the %s list has more than %d pages", name, page)
            return rows
        page += 1


def _cancel_routine(row, authorization, report):
    routine_id = row.get("id")
    if not routine_id or str(row.get("status", "")).upper() not in ROUTINE_LIVE:
        return
    code, body = _ask(
        cancel_series_booking, routine_id, {"reason": ROUTINE_REASON},
        authorization=authorization,
    )
    _settle("routine", routine_id, code, body, report)


def _cancel_upcoming(row, authorization, report):
    row_id = row.get("id")
    if not row_id:
        return
    if row.get("booking_type") == "GROUP":
        # The booker's whole party, as the app's party Cancel. A member's
        # place is the booker's booking: booking-api answers 404 for anyone
        # else, and it stays.
        if row.get("is_booker") is False:
            report.leave("party", row_id, "a place in someone else's party")
            return
        code, body = _ask(cancel_group_booking, row_id, authorization=authorization)
        _settle("party", row_id, code, body, report)
        return
    if row.get("series_id"):
        # A visit of a routine is cancelled with its routine, above. Still
        # here means the routine would not end; the app has no cancel for a
        # visit by its own id either.
        report.leave("routine visit", row_id, "its routine was not cancelled")
        return
    code, body = _ask(
        cancel_booking, row_id, {"reason": CANCEL_TEXT}, authorization=authorization,
    )
    _settle("booking", row_id, code, body, report)


def _ask(call, *args, **kwargs):
    try:
        return call(*args, **kwargs)
    except BookingApiUnavailable as exc:
        raise Unreachable(str(exc)) from exc


def _settle(kind, row_id, code, body, report):
    """A 2xx is cancelled; a 5xx is Unreachable; any other answer is a
    refusal, and the row stays as it is."""
    if 200 <= code < 300:
        report.cancelled += 1
        return
    if code >= 500:
        raise Unreachable(f"cancelling {kind} {row_id} answered {code}")
    report.leave(kind, row_id, f"{code} {_code_of(body)}")


def _code_of(body):
    """booking-api's error code, or its message, for the log."""
    if isinstance(body, dict):
        return body.get("code") or body.get("message") or ""
    return ""
