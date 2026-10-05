"""
POST / DELETE /api/v1/me/device-tokens

The app hands its FCM token to us, and we pass it on to the push service.
The user always comes from the login token (request.user), never from the
body, so nobody can register a phone under someone else's account.
"""

import logging

from drf_spectacular.utils import extend_schema
from rest_framework import serializers, status
from rest_framework.response import Response
from rest_framework.views import APIView

from . import push_api
from .permissions import IsVerified

logger = logging.getLogger(__name__)


class DeviceTokenSerializer(serializers.Serializer):
    token = serializers.CharField(min_length=10, max_length=4096)
    platform = serializers.ChoiceField(choices=["android", "ios"])


class DeviceTokenDeleteSerializer(serializers.Serializer):
    token = serializers.CharField(min_length=10, max_length=4096)


class DeviceTokensView(APIView):
    permission_classes = [IsVerified]

    @extend_schema(request=DeviceTokenSerializer, responses={204: None})
    def post(self, request):
        s = DeviceTokenSerializer(data=request.data)
        s.is_valid(raise_exception=True)
        return _forward(
            lambda: push_api.register_device(
                user_id=request.user.id,
                token=s.validated_data["token"],
                platform=s.validated_data["platform"],
            )
        )

    @extend_schema(request=DeviceTokenDeleteSerializer, responses={204: None})
    def delete(self, request):
        s = DeviceTokenDeleteSerializer(data=request.data)
        s.is_valid(raise_exception=True)
        return _forward(
            lambda: push_api.unregister_device(token=s.validated_data["token"])
        )


def _forward(call):
    """Run one push_api call and turn its outcome into our answer."""
    try:
        status_code, body = call()
    except push_api.PushApiUnavailable as e:
        logger.warning("push service unavailable: %s", e)
        return Response(
            {"detail": "Notifications are unavailable right now.", "code": "push_unavailable"},
            status=status.HTTP_503_SERVICE_UNAVAILABLE,
        )
    if status_code >= 400:
        logger.warning("push service refused (%s): %s", status_code, body)
        return Response(
            {"detail": "Could not update this device.", "code": "push_refused"},
            status=status.HTTP_502_BAD_GATEWAY,
        )
    return Response(status=status.HTTP_204_NO_CONTENT)
