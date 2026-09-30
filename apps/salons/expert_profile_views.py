"""
The app team's Expert Profile screen (docs/expert-profile-fe-contract.md):

    GET /api/v1/salon/<salon_id>/stylist/<stylist_id>

One stylist, read at one salon: everything the screen draws, in one call.
Built step by step (docs/EXPERT_PROFILE_AUDIT.md, section 8): the route, the
404s and auth in E1, the fields in E2 to E8. This module reads;
expert_profile.py (pure) writes the fields.

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

from .expert_profile import core_fields
from .params import path_uuid
from .selectors import salon_profile, stylist_for_salon
from .views import _OUR_ENVELOPE, _stylist_row

# Written for the customer: the app shows `detail` on its empty state. The
# salon's is the service detail's own sentence; the stylist's is the
# contract's, word for word.
NO_SALON = "This salon is not available."
NO_STYLIST = "This stylist is no longer at this salon."

# The 200 so far: the person (E2). E8 replaces this with the full shape.
_ANSWER_SO_FAR = {
    "type": "object",
    "properties": {
        "id": {"type": "string", "format": "uuid"},
        "salon_id": {"type": "string", "format": "uuid"},
        "name": {"type": "string", "nullable": True},
        "title": {"type": "string", "nullable": True},
        "role": {"type": "string", "nullable": True},
        "avatar_url": {"type": "string", "nullable": True},
        "rating": {"type": "number", "nullable": True},
        "review_count": {"type": "integer"},
    },
    "required": [
        "id", "salon_id", "name", "title", "role", "avatar_url", "rating", "review_count",
    ],
}


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
            response=_ANSWER_SO_FAR,
            description=(
                "The stylist, at this salon: the same name, title, role and "
                "avatar as in GET /salon/{salon_id}/stylists. No stylist "
                "reviews exist yet, so `rating` is null and `review_count` "
                "is 0. The rest of the screen's fields are being added."
            ),
            examples=[
                OpenApiExample(
                    "A stylist of this salon",
                    value={
                        "id": "04e58d74-04db-4088-bd97-e3ff765cc322",
                        "salon_id": "c6c248ab-f2cd-4f12-a31e-243c6e64b3b5",
                        "name": "Darius Stone",
                        "title": "Master Barber",
                        "role": "Haircut and Styling Expert",
                        "avatar_url": None,
                        "rating": None,
                        "review_count": 0,
                    },
                ),
            ],
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
        # title, role and avatar as GET /salon/<id>/stylists gives this stylist.
        return Response(core_fields(salon, _stylist_row(stylist)))
