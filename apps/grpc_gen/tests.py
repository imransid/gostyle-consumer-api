"""
The gRPC servicer, and the one thing about it that is not like a view.

THE BUG THIS PINS. `CONN_MAX_AGE` is 60, so Django holds a database
connection open between uses and relies on `request_started` /
`request_finished` to reap one that has aged out or been hung up on. A gRPC
call fires neither signal. The servicer therefore opened one connection,
Postgres closed its end while the process sat idle, and every call after that
got the same dead socket back -- in production, for hours, until someone
restarted the container.

It surfaced as `503 DEPENDENCY_UNAVAILABLE / grpcStatus UNKNOWN` on
`POST /booking`, which reads as "auth is down" and sent everyone looking at
the network instead of at a connection nobody was closing.
"""

from unittest import mock

from django.test import SimpleTestCase

from apps.grpc_gen.servicer import ConsumerAuthServicer


class ConnectionReapingTests(SimpleTestCase):
    def call(self, **verify):
        servicer = ConsumerAuthServicer()
        with mock.patch.object(
            ConsumerAuthServicer, "_verify", **verify
        ), mock.patch(
            "apps.grpc_gen.servicer.close_old_connections"
        ) as reap:
            try:
                servicer.VerifyConsumer(mock.Mock(token="t"), mock.Mock())
            except RuntimeError:
                pass
            return reap

    def test_stale_connections_are_reaped_around_every_call(self):
        # Before AND after: before so this call does not inherit a dead
        # socket, after so an idle process is not holding one for the next
        # caller to find.
        self.assertEqual(self.call(return_value="ok").call_count, 2)

    def test_the_connection_is_reaped_even_when_the_lookup_blows_up(self):
        """
        THE HALF THAT MATTERS. A database error is exactly when the
        connection is broken, so a reap that only ran on success would skip
        the one case it exists for -- and the process would hand the same
        dead socket to every caller after it, which is what happened.
        """
        reap = self.call(side_effect=RuntimeError("connection is closed"))
        self.assertEqual(reap.call_count, 2)


# ---------------------------------------------------------------------------
# ConsumerDirectory.GetConsumerContact -- booking-api's reminders ask this.
# ---------------------------------------------------------------------------

import uuid as _uuid
from datetime import datetime, timezone

import grpc
from django.test import override_settings

from apps.grpc_gen.servicer import ConsumerDirectoryServicer

CONSUMER_ID = "22222222-2222-4222-8222-222222222222"


class _Aborted(Exception):
    pass


def _context(key=None):
    context = mock.Mock()
    context.invocation_metadata.return_value = (
        [] if key is None else [("x-internal-key", key)]
    )
    context.abort.side_effect = _Aborted
    return context


def _account(**over):
    fields = dict(
        id=_uuid.UUID(CONSUMER_ID),
        email="sara@example.com",
        email_verified_at=datetime(2026, 9, 1, tzinfo=timezone.utc),
        full_name="Sara Ahmed",
        language="en",
    )
    fields.update(over)
    return mock.Mock(**fields)


@override_settings(INTERNAL_GRPC_KEY="k-123")
class ConsumerContactTests(SimpleTestCase):
    def ask(self, consumer_id=CONSUMER_ID, key="k-123", account=None, prefs=None):
        with mock.patch(
            "apps.grpc_gen.servicer.ConsumerAccount"
        ) as accounts, mock.patch(
            "apps.grpc_gen.servicer.NotificationPreference"
        ) as preferences, mock.patch(
            "apps.grpc_gen.servicer.close_old_connections"
        ) as reap:
            accounts.objects.filter.return_value.first.return_value = account
            preferences.objects.filter.return_value.first.return_value = prefs
            request = mock.Mock(consumer_id=consumer_id)
            reply = ConsumerDirectoryServicer().GetConsumerContact(
                request, _context(key)
            )
            return reply, accounts, preferences, reap

    def test_an_account_with_a_verified_email_and_preferences(self):
        prefs = mock.Mock(appointment_reminder=True, push=False)
        reply, accounts, _, _ = self.ask(account=_account(), prefs=prefs)

        self.assertTrue(reply.found)
        self.assertEqual(reply.consumer_id, CONSUMER_ID)
        self.assertEqual(reply.email, "sara@example.com")
        self.assertTrue(reply.email_verified)
        self.assertEqual(reply.full_name, "Sara Ahmed")
        self.assertTrue(reply.appointment_reminder)
        self.assertFalse(reply.push_enabled)
        # Only an active account answers, the same rule VerifyConsumer uses.
        accounts.objects.filter.assert_called_with(
            id=_uuid.UUID(CONSUMER_ID), is_active=True
        )

    def test_no_email_and_an_unverified_one_are_said_plainly(self):
        reply, *_ = self.ask(account=_account(email=None, email_verified_at=None))
        self.assertTrue(reply.found)
        self.assertEqual(reply.email, "")
        self.assertFalse(reply.email_verified)

    def test_never_opened_settings_reads_the_model_defaults_without_writing(self):
        reply, _, preferences, _ = self.ask(account=_account(), prefs=None)
        self.assertTrue(reply.appointment_reminder)
        self.assertTrue(reply.push_enabled)
        # filter().first(), never for_account()/get_or_create: no row written.
        preferences.for_account.assert_not_called()
        preferences.objects.get_or_create.assert_not_called()

    def test_no_such_customer_is_an_answer_not_an_error(self):
        reply, *_ = self.ask(account=None)
        self.assertFalse(reply.found)

    def test_an_id_that_is_not_a_uuid_is_not_found(self):
        reply, accounts, _, _ = self.ask(consumer_id="anonymous")
        self.assertFalse(reply.found)
        accounts.objects.filter.assert_not_called()

    def test_connections_are_reaped_around_the_call(self):
        *_, reap = self.ask(account=_account())
        self.assertEqual(reap.call_count, 2)


class ConsumerContactKeyTests(SimpleTestCase):
    """The answer is a person's email address: no key, no answer."""

    def call(self, key):
        with mock.patch("apps.grpc_gen.servicer.ConsumerAccount") as accounts:
            context = _context(key)
            with self.assertRaises(_Aborted):
                ConsumerDirectoryServicer().GetConsumerContact(
                    mock.Mock(consumer_id=CONSUMER_ID), context
                )
            accounts.objects.filter.assert_not_called()
            return context

    @override_settings(INTERNAL_GRPC_KEY="k-123")
    def test_a_caller_without_the_key_is_refused(self):
        context = self.call(key=None)
        self.assertEqual(
            context.abort.call_args[0][0], grpc.StatusCode.UNAUTHENTICATED
        )

    @override_settings(INTERNAL_GRPC_KEY="k-123")
    def test_a_wrong_key_is_refused(self):
        self.call(key="guess")

    @override_settings(INTERNAL_GRPC_KEY="")
    def test_an_unset_server_key_refuses_everyone_rather_than_no_one(self):
        self.call(key="")
