from rest_framework_simplejwt.exceptions import TokenError
from rest_framework_simplejwt.tokens import AccessToken

from apps.accounts.models import ConsumerAccount

from . import auth_pb2, auth_pb2_grpc


class ConsumerAuthServicer(auth_pb2_grpc.ConsumerAuthServicer):
    def VerifyConsumer(self, request, context):
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