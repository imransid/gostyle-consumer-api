import hmac
import uuid
from datetime import datetime

import grpc
from django.conf import settings
from django.db import close_old_connections
from rest_framework_simplejwt.exceptions import TokenError
from rest_framework_simplejwt.tokens import AccessToken

from apps.accounts import sessions
from apps.accounts.models import ConsumerAccount, NotificationPreference
from apps.notifications import registry, wording
from apps.notifications.service import notify

from . import (
    auth_pb2,
    auth_pb2_grpc,
    consumer_directory_pb2,
    consumer_directory_pb2_grpc,
    customer_notifications_pb2,
    customer_notifications_pb2_grpc,
)


class ConsumerAuthServicer(auth_pb2_grpc.ConsumerAuthServicer):
    def VerifyConsumer(self, request, context):
        # THE CONNECTION IS REAPED BY HAND, because nothing else will.
        #
        # CONN_MAX_AGE is 60, so Django keeps a database connection open
        # between uses. In a web request that is safe: `request_started` and
        # `request_finished` both call close_old_connections(), which drops a
        # connection that has aged out AND one the server has already hung up
        # on. A gRPC call fires NEITHER signal.
        #
        # So this process opened one connection, Postgres closed its end
        # while it sat idle, and Django went on handing the dead socket back
        # for every call after that. The symptom in production was a first
        # error reading "server closed the connection unexpectedly" and then
        # "the connection is closed" forever, until the container was
        # restarted -- which is what made bookings fail with
        # DEPENDENCY_UNAVAILABLE / grpcStatus UNKNOWN.
        #
        # Called on the way IN and on the way OUT: in, so this call does not
        # inherit a corpse; out, so an idle process is not holding one open
        # for the next caller to find.
        close_old_connections()
        try:
            return self._verify(request)
        finally:
            close_old_connections()

    def _verify(self, request):
        # A bad token is an ORDINARY answer, not an error. The caller asked a
        # question and "no" is a valid reply, so this returns valid=False
        # rather than raising a gRPC status.
        try:
            token = AccessToken(request.token)
        except TokenError:
            return auth_pb2.VerifyConsumerResponse(valid=False)

        consumer_id = token.get("consumer_id")
        if not consumer_id:
            return auth_pb2.VerifyConsumerResponse(valid=False)

        # A signed-out device's token, or one with no session, is refused
        # here too, as by this service's own API
        # (apps/accounts/authentication.py).
        if sessions.enabled():
            sid = token.get(sessions.SID_CLAIM)
            if sid is None or not sessions.is_live(sid):
                return auth_pb2.VerifyConsumerResponse(valid=False)

        # The signature only proves the token was issued by us. It does NOT
        # prove the account still exists: access tokens last seven days, so a
        # deleted user's token stays signature-valid all week. Checking the
        # row is the whole reason this call goes over the network.
        account = ConsumerAccount.objects.filter(
            id=consumer_id, is_active=True
        ).first()

        if account is None:
            return auth_pb2.VerifyConsumerResponse(valid=False)

        return auth_pb2.VerifyConsumerResponse(
            valid=True,
            consumer_id=str(account.id),
            verified=account.account_verified,
        )


# The model's own defaults, read once, so a customer who never opened their
# notification settings is answered exactly as NotificationPreference would
# create them -- without this call creating the row.
_PREFERENCE_DEFAULTS = {
    name: NotificationPreference._meta.get_field(name).default
    for name in (
        "appointment_reminder",
        "push",
        "booking_confirmation",
        "booking_changes",
        "email",
        "reminder_lead_minutes",
    )
}


def _presented_key_ok(context):
    """
    The caller's x-internal-key against INTERNAL_GRPC_KEY.

    FAILS CLOSED. An empty INTERNAL_GRPC_KEY refuses every call rather than
    letting every call through: the answer is a person's email address, and
    "nobody set the key" must not mean "anyone on the network may ask".
    """
    expected = settings.INTERNAL_GRPC_KEY or ""
    if expected == "":
        return False
    presented = dict(context.invocation_metadata()).get("x-internal-key", "")
    return hmac.compare_digest(str(presented), expected)


class ConsumerDirectoryServicer(consumer_directory_pb2_grpc.ConsumerDirectoryServicer):
    """
    How to reach a customer, for the services that send them messages.

    booking-api asks this when a booking reminder is due: the email address,
    whether it is verified, the name to greet them by, and whether they want
    appointment reminders, booking confirmations and changes, and push at
    all. booking-api stores none of it, so
    a customer who changes their address or turns reminders off is honoured
    from the next reminder on.
    """

    def GetConsumerContact(self, request, context):
        if not _presented_key_ok(context):
            # abort() raises: nothing below runs for an unauthenticated call.
            context.abort(
                grpc.StatusCode.UNAUTHENTICATED,
                "missing or wrong x-internal-key",
            )

        # Reaped on the way in and out, for the reason VerifyConsumer gives:
        # a gRPC call fires neither request signal, so nothing else will.
        close_old_connections()
        try:
            return self._contact(request)
        finally:
            close_old_connections()

    def _contact(self, request):
        # An id that is not a UUID is no customer of ours. Answered, not
        # raised: Django would turn it into a ValidationError, and the caller
        # would see an UNKNOWN it could only retry.
        try:
            consumer_id = uuid.UUID(request.consumer_id)
        except ValueError:
            return consumer_directory_pb2.ConsumerContact(found=False)

        account = ConsumerAccount.objects.filter(
            id=consumer_id, is_active=True
        ).first()
        if account is None:
            return consumer_directory_pb2.ConsumerContact(found=False)

        # filter().first(), NOT for_account(): a lookup must not write.
        prefs = NotificationPreference.objects.filter(account=account).first()

        return consumer_directory_pb2.ConsumerContact(
            found=True,
            consumer_id=str(account.id),
            email=account.email or "",
            email_verified=account.email_verified_at is not None,
            full_name=account.full_name or "",
            language=account.language or "",
            appointment_reminder=(
                prefs.appointment_reminder
                if prefs is not None
                else _PREFERENCE_DEFAULTS["appointment_reminder"]
            ),
            push_enabled=(
                prefs.push if prefs is not None else _PREFERENCE_DEFAULTS["push"]
            ),
            # Always set, so their presence on the wire tells the caller this
            # customer-api knows them (consumer_directory.proto).
            booking_confirmation=(
                prefs.booking_confirmation
                if prefs is not None
                else _PREFERENCE_DEFAULTS["booking_confirmation"]
            ),
            booking_changes=(
                prefs.booking_changes
                if prefs is not None
                else _PREFERENCE_DEFAULTS["booking_changes"]
            ),
            email_enabled=(
                prefs.email if prefs is not None else _PREFERENCE_DEFAULTS["email"]
            ),
        )


# More ids than this in one call is a caller bug, refused before any query.
MAX_REMINDER_LEADS = 1000


class CustomerNotificationsServicer(customer_notifications_pb2_grpc.CustomerNotificationsServicer):
    """
    The Notification Center's calls for other services.

    GetReminderLeads: booking-api's reminder ladder asks, once a tick, for
    every customer with a visit coming up, whether they want a reminder and
    how long before. It keeps no copy, so a change in the app counts from
    its next tick.

    Notify: booking-api hands over one thing that happened to a customer
    (a booking confirmed, moved, cancelled). It is kept in their
    Notification Center and its alerts are queued by their switches
    (apps/notifications/service.notify); the worker sends them. Safe to
    repeat: booking-api retries until it hears back.
    """

    def GetReminderLeads(self, request, context):
        if not _presented_key_ok(context):
            context.abort(grpc.StatusCode.UNAUTHENTICATED, "missing or wrong x-internal-key")
        if len(request.consumer_ids) > MAX_REMINDER_LEADS:
            context.abort(
                grpc.StatusCode.INVALID_ARGUMENT,
                f"at most {MAX_REMINDER_LEADS} consumer_ids",
            )

        close_old_connections()
        try:
            return self._leads(request)
        finally:
            close_old_connections()

    def _leads(self, request):
        ids = set()
        for raw in request.consumer_ids:
            # Not an id is no customer of ours: left out, not an error.
            try:
                ids.add(uuid.UUID(raw))
            except ValueError:
                continue

        accounts = ConsumerAccount.objects.filter(id__in=ids, is_active=True).values_list(
            "id", flat=True
        )
        # filter(), never for_account(): a lookup must not write.
        prefs = {
            row.account_id: row
            for row in NotificationPreference.objects.filter(account_id__in=ids).only(
                "account_id", "appointment_reminder", "reminder_lead_minutes"
            )
        }
        reply = customer_notifications_pb2.GetReminderLeadsReply()
        for account_id in accounts:
            row = prefs.get(account_id)
            lead = reply.leads[str(account_id)]
            lead.enabled = (
                row.appointment_reminder
                if row is not None
                else _PREFERENCE_DEFAULTS["appointment_reminder"]
            )
            lead.lead_minutes = (
                row.reminder_lead_minutes
                if row is not None
                else _PREFERENCE_DEFAULTS["reminder_lead_minutes"]
            )
        return reply

    def Notify(self, request, context):
        if not _presented_key_ok(context):
            context.abort(grpc.StatusCode.UNAUTHENTICATED, "missing or wrong x-internal-key")

        close_old_connections()
        try:
            return self._notify(request, context)
        finally:
            close_old_connections()

    def _notify(self, request, context):
        Reply = customer_notifications_pb2.NotifyReply
        # A producer bug is INVALID_ARGUMENT: the caller gives up rather
        # than retrying something that can never work.
        if request.type not in registry.KINDS:
            context.abort(grpc.StatusCode.INVALID_ARGUMENT, f"unknown type {request.type!r}")
        if not request.dedupe_key:
            context.abort(grpc.StatusCode.INVALID_ARGUMENT, "dedupe_key is required")
        occurred_at = _instant(request.occurred_at, "occurred_at", context)
        deliver_by = _instant(request.deliver_by, "deliver_by", context)
        try:
            words = wording.words_for(request.type, dict(request.params))
        except wording.CannotWrite as e:
            context.abort(grpc.StatusCode.INVALID_ARGUMENT, str(e))

        # Answered, not raised, like GetConsumerContact: an id that is not
        # one, or no active account, is a customer we cannot tell.
        try:
            consumer_id = uuid.UUID(request.consumer_id)
        except ValueError:
            return Reply(outcome=Reply.NOT_FOUND)
        account = ConsumerAccount.objects.filter(id=consumer_id, is_active=True).first()
        if account is None:
            return Reply(outcome=Reply.NOT_FOUND)

        out = notify(
            account,
            request.type,
            dedupe_key=request.dedupe_key,
            title=words.title,
            body=words.body,
            data=words.data,
            target_type=request.target_type or None,
            target_id=request.target_id or None,
            occurred_at=occurred_at,
            deliver_by=deliver_by,
            source=request.source or "unknown",
        )
        if out.notification is None:
            return Reply(outcome=Reply.SUPPRESSED)
        return Reply(
            outcome=Reply.ACCEPTED if out.created else Reply.DUPLICATE,
            notification_id=str(out.notification.pk),
        )


def _instant(raw, field, context):
    """An RFC 3339 time with its offset, None when empty, else the call is
    refused."""
    if not raw:
        return None
    try:
        at = datetime.fromisoformat(raw)
    except ValueError:
        at = None
    if at is None or at.tzinfo is None:
        context.abort(grpc.StatusCode.INVALID_ARGUMENT, f"{field} {raw!r} is not an RFC 3339 time")
    return at
