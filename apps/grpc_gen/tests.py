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
