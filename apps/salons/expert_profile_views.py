"""
The app team's Expert Profile screen (docs/expert-profile-fe-contract.md):

    GET /api/v1/salon/<salon_id>/stylist/<stylist_id>

One stylist, read at one salon: everything the screen draws, in one call.
Built step by step (docs/EXPERT_PROFILE_AUDIT.md, section 8): the route, the
404s and auth in E1, the fields in E2 to E8. This module reads;
expert_profile.py (pure) writes the fields, in the contract's order. The app
team's guide is docs/EXPERT_PROFILE_API.md.

PUBLIC, like every other /salon/<id>/... route (Rafa, Q1): a guest can open a
salon and its stylists, so the profile opens too. A token is accepted, not
needed, and a bad or expired one is still a 401, because the JWT check runs
on every route whatever its permission.

EVERY 404 IS OURS. The ids come in as strings and are checked here
(params.path_uuid). With Django's <uuid:> converter a bad id never reaches a
view: Django answers it with its own HTML page, not our JSON envelope.

WHO OPENS is the Expert step's own list (selectors.stylist_for_salon): a
stylist of another salon, one who left, and an id nobody has are all the same
404. Never a cross-salon read.
"""

from drf_spectacular.types import OpenApiTypes
from drf_spectacular.utils import (
    OpenApiExample,
    OpenApiParameter,
    OpenApiResponse,
    extend_schema,
)
from rest_framework.exceptions import NotFound
from rest_framework.permissions import AllowAny
from rest_framework.response import Response
from rest_framework.views import APIView

from . import menu
from .expert_profile import core_fields, media, salon_block, service_groups
from .expert_profile_serializers import EXAMPLE, SalonExpertProfileResponseSerializer
from .params import path_uuid
from .selectors import (
    is_favourite_stylist,
    salon_categories,
    salon_profile,
    salon_services,
    service_stage_rows,
    stylist_for_salon,
    stylist_media_rows,
    stylist_service_coverage,
)
from .snapshot import read_snapshot
from .views import _OUR_ENVELOPE, _stylist_row, days_off_for, salon_profile_data

# Written for the customer: the app shows `detail` on its empty state. The
# salon's is the service detail's own sentence; the stylist's is the
# contract's, word for word.
NO_SALON = "This salon is not available."
NO_STYLIST = "This stylist is no longer at this salon."

@extend_schema(
    summary="One stylist of a salon, for the expert profile screen",
    description=(
        "Everything the Expert Profile screen draws, in one call. Public, "
        "like the other salon routes: a token is accepted, not needed, and a "
        "bad or expired one is a 401.\n\n"
        "Only a stylist of THIS salon answers 200: the people "
        "GET /salon/{salon_id}/stylists lists. A stylist of another salon, "
        "one who left, and an id that is not a UUID answer 404."
    ),
    parameters=[
        OpenApiParameter(
            "salon_id", OpenApiTypes.UUID, OpenApiParameter.PATH,
            description="The salon (storefront) id, as the other salon routes give it.",
        ),
        OpenApiParameter(
            "stylist_id", OpenApiTypes.UUID, OpenApiParameter.PATH,
            description="The stylist id, as GET /salon/{salon_id}/stylists gives it.",
        ),
    ],
    responses={
        200: OpenApiResponse(
            response=SalonExpertProfileResponseSerializer,
            description=(
                "Everything the screen draws. docs/EXPERT_PROFILE_API.md has "
                "every field and rule."
            ),
            examples=[OpenApiExample("A stylist of this salon", value=EXAMPLE)],
        ),
        401: OpenApiResponse(response=_OUR_ENVELOPE, description="A bad or expired token."),
        404: OpenApiResponse(
            response=_OUR_ENVELOPE,
            description=(
                "No such salon, no such stylist at this salon, or an id that "
                "is not a UUID. `detail` is a sentence for the customer."
            ),
            examples=[
                OpenApiExample(
                    "No such stylist",
                    value={
                        "detail": NO_STYLIST,
                        "code": "not_found",
                        "errors": [
                            {"field": None, "code": "not_found", "message": NO_STYLIST}
                        ],
                    },
                ),
            ],
        ),
    },
)
class SalonExpertProfileView(APIView):
    """GET /api/v1/salon/<salon_id>/stylist/<stylist_id>"""

    permission_classes = [AllowAny]

    def get(self, request, salon_id, stylist_id):
        salon_uuid = path_uuid(salon_id)
        salon = salon_profile(salon_uuid) if salon_uuid else None
        if salon is None:
            raise NotFound(NO_SALON)

        stylist_uuid = path_uuid(stylist_id)
        stylist = stylist_for_salon(salon, stylist_uuid) if stylist_uuid else None
        if stylist is None:
            raise NotFound(NO_STYLIST)

        # The person, from the Expert step's own row code: the same name,
        # title, role, avatar and day off as GET /salon/<id>/stylists gives
        # this stylist.
        # The salon's published snapshot, read once: its open days for the
        # day off, and its own page's answer for the salon block.
        snapshot = read_snapshot(salon)

        day_off = days_off_for(salon, [stylist.id], snapshot).get(stylist.id)
        body = core_fields(salon, _stylist_row(stylist, day_off=day_off))

        # The caller's own heart on this stylist: read with their token when
        # they sent one, false for a guest (the contract's section 3).
        body["is_favorite"] = is_favourite_stylist(request.user, stylist.id)

        # Their posted work: this salon's photos tagged with them.
        body.update(media(stylist_media_rows(salon, stylist.id)))

        # The salon, from its own page's answer (GET /salon/<id>): the same
        # name, open now and pin, by the same code.
        body["salon"] = salon_block(salon_profile_data(salon, snapshot))
        body["service_groups"] = self._service_groups(salon, stylist)
        return Response(body)

    @staticmethod
    def _service_groups(salon, stylist):
        """What this stylist does, grouped as the Services tab."""
        services = list(salon_services(salon))
        if not services:
            return []

        # The Expert step's own question (GET /salon/<id>/stylists
        # ?service_ids=), asked the other way round: not "who can do this
        # service" but "which services can this one do". The same code, fed
        # the whole menu and its stages read once, so a service is here
        # exactly when this stylist is in that route's list for it. A service
        # with no stages is in nobody's list (that route's 422).
        ids = [service.id for service in services]
        covered = stylist_service_coverage(
            salon, ids, [stylist.id], stages=service_stage_rows(ids),
        ).get(stylist.id)
        if not covered:
            return []

        # The whole menu as the tab groups it, then narrowed: the tab's
        # groups, order, rows and branch prices.
        _, groups = menu.service_groups(services, salon_categories(salon.tenant_id))
        return service_groups(groups, covered)
