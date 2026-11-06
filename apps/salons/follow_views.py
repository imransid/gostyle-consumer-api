from drf_spectacular.types import OpenApiTypes
from drf_spectacular.utils import OpenApiParameter, OpenApiResponse, extend_schema
from rest_framework.exceptions import NotFound
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.throttling import ScopedRateThrottle
from rest_framework.views import APIView

from apps.accounts.models import SalonFollow

from .params import path_uuid
from .selectors import followers_count, salon_profile
from .serializers import SalonFollowStateSerializer
from .views import _OUR_ENVELOPE

NO_SALON = "This salon is not available."

_SALON_ID = OpenApiParameter(
    "salon_id", OpenApiTypes.UUID, OpenApiParameter.PATH,
    description="The salon (storefront) id, as GET /salon/{salon_id} gives it.",
)

_COMMON_ERRORS = {
    401: OpenApiResponse(response=_OUR_ENVELOPE, description="Not logged in, or a bad token."),
    429: OpenApiResponse(response=_OUR_ENVELOPE, description="Too many calls (60 a minute)."),
}


def _state(is_following, storefront_id):
    return Response(
        SalonFollowStateSerializer(
            {"is_following": is_following, "followers_count": followers_count(storefront_id)}
        ).data
    )


class SalonFollowView(APIView):
    """PUT follows, DELETE unfollows."""

    permission_classes = [IsAuthenticated]
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = "follow"

    @extend_schema(
        summary="Follow a salon",
        description=(
            "Safe to repeat: following twice is still one follow. Separate "
            "from the heart (POST /favourite). The answer has the new "
            "follower count."
        ),
        parameters=[_SALON_ID],
        responses={
            200: SalonFollowStateSerializer,
            404: OpenApiResponse(
                response=_OUR_ENVELOPE,
                description="The salon is not available, or the id is not a UUID.",
            ),
            **_COMMON_ERRORS,
        },
    )
    def put(self, request, salon_id):
        salon_uuid = path_uuid(salon_id)
        if salon_uuid is None or salon_profile(salon_uuid) is None:
            raise NotFound(NO_SALON)

        # get_or_create: a double tap makes one row, not an error.
        SalonFollow.objects.get_or_create(account=request.user, storefront_id=salon_uuid)
        return _state(True, salon_uuid)

    @extend_schema(
        summary="Unfollow a salon",
        description=(
            "Safe to repeat: unfollowing twice is still unfollowed. Works "
            "even if the salon has gone since."
        ),
        parameters=[_SALON_ID],
        responses={
            200: SalonFollowStateSerializer,
            404: OpenApiResponse(
                response=_OUR_ENVELOPE,
                description="The id is not a UUID.",
            ),
            **_COMMON_ERRORS,
        },
    )
    def delete(self, request, salon_id):
        salon_uuid = path_uuid(salon_id)
        if salon_uuid is None:
            raise NotFound(NO_SALON)

        # No look at the salon: unfollowing a salon that has gone must still work.
        SalonFollow.objects.filter(account=request.user, storefront_id=salon_uuid).delete()
        return _state(False, salon_uuid)

    @extend_schema(
        summary="Toggle follow on a salon",
        description=(
            "One tap follows, the next tap unfollows. The answer has the new "
            "is_following and the new follower count."
        ),
        parameters=[_SALON_ID],
        responses={
            200: SalonFollowStateSerializer,
            404: OpenApiResponse(
                response=_OUR_ENVELOPE,
                description="The salon is not available, or the id is not a UUID.",
            ),
            **_COMMON_ERRORS,
        },
    )
    def post(self, request, salon_id):
        salon_uuid = path_uuid(salon_id)
        if salon_uuid is None:
            raise NotFound(NO_SALON)

        existing = SalonFollow.objects.filter(account=request.user, storefront_id=salon_uuid)
        if existing.exists():
            existing.delete()
            return _state(False, salon_uuid)

        if salon_profile(salon_uuid) is None:
            raise NotFound(NO_SALON)
        SalonFollow.objects.get_or_create(account=request.user, storefront_id=salon_uuid)
        return _state(True, salon_uuid)
