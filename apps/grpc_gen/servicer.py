from django.db import close_old_connections
from rest_framework_simplejwt.exceptions import TokenError
from rest_framework_simplejwt.tokens import AccessToken

from apps.accounts.models import ConsumerAccount

from . import auth_pb2, auth_pb2_grpc


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
