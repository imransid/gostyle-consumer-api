"""
The app team's Service Detail screen (docs/service-detail-fe-contract.md):

    GET /api/v1/salon/<salon_id>/service/<service_id>

Everything the screen draws, in one call. Built step by step
(docs/SERVICE_DETAIL_AUDIT.md, section 7): the route, the 404s and auth in S1,
the fields in S2 to S6.

PUBLIC, like every other /salon/<id>/... route (Rafa, Q4): a guest can open a
salon, its services and its stylists, so the detail opens too. A token is
accepted, and a bad or expired one is still a 401, because the JWT check runs
on every route whatever its permission.

EVERY 404 IS OURS. The ids come in as strings and are checked here. With
Django's <uuid:> converter a bad id never reaches a view: Django answers it
with its own HTML page, not our JSON envelope.
"""

import re
import uuid

from drf_spectacular.types import OpenApiTypes
from drf_spectacular.utils import OpenApiParameter, OpenApiResponse, extend_schema
from rest_framework.exceptions import NotFound
from rest_framework.permissions import AllowAny
from rest_framework.response import Response
from rest_framework.views import APIView

from .selectors import salon_profile, service_for_salon
from .views import _OUR_ENVELOPE

# Written for the customer: the app shows `detail` on its empty state.
NO_SALON = "This salon is not available."
NO_SERVICE = "This service is no longer on the menu."

# The dashed form only, any case: what a UUID in a path looks like. Braces,
# "urn:uuid:" and the bare 32 hex digits are refused, as <uuid:> refuses them.
_UUID = re.compile(
    r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}"
)


def path_uuid(value):
    """The id from the path as a UUID, or None when it is not one."""
    if not isinstance(value, str) or not _UUID.fullmatch(value):
        return None
    return uuid.UUID(value)


@extend_schema(
    summary="One service of a salon, for its detail screen",
    description=(
        "Everything the Service Detail screen draws, in one call. Public, like "
        "the other salon routes: a token is accepted, not needed, and a bad or "
        "expired one is a 401.\n\n"
        "A service of this salon that was ever on sale answers 200, even when "
        "it has since been pulled. Another tenant's service, one never on "
        "sale, and an id that is not a UUID answer 404."
    ),
    parameters=[
        OpenApiParameter(
            "salon_id", OpenApiTypes.UUID, OpenApiParameter.PATH,
            description="The salon (storefront) id, as the other salon routes give it.",
        ),
        OpenApiParameter(
            "service_id", OpenApiTypes.UUID, OpenApiParameter.PATH,
            description="The service id, as GET /salon/{salon_id}/services gives it.",
        ),
    ],
    responses={
        200: OpenApiResponse(description="The service. Fields are added in S2 to S6."),
        401: OpenApiResponse(response=_OUR_ENVELOPE, description="A bad or expired token."),
        404: OpenApiResponse(
            response=_OUR_ENVELOPE,
            description=(
                "No such salon, no such service at this salon, a service never "
                "on sale, or an id that is not a UUID. `detail` is a sentence "
                "for the customer."
            ),
        ),
    },
)
class SalonServiceDetailView(APIView):
    """GET /api/v1/salon/<salon_id>/service/<service_id>"""

    permission_classes = [AllowAny]

    def get(self, request, salon_id, service_id):
        salon_uuid = path_uuid(salon_id)
        salon = salon_profile(salon_uuid) if salon_uuid else None
        if salon is None:
            raise NotFound(NO_SALON)

        service_uuid = path_uuid(service_id)
        service = service_for_salon(salon, service_uuid) if service_uuid else None
        if service is None:
            raise NotFound(NO_SERVICE)

        return Response({
            "id": str(service.id),
            "salon_id": str(salon.id),
        })
