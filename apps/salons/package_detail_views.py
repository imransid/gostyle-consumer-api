"""
The mobile team's Package Details screen (docs/package-detail-fe-contract.md):

    GET /api/v1/salon/<salon_id>/package/<package_id>

Everything the screen draws, in one call (docs/PACKAGE_DETAIL_AUDIT.md). This
module reads; packages.py (pure) writes the fields, with the same rules the
Packages tab follows when PACKAGES_LIVE_RULES_V1 is on.

PUBLIC, like every other /salon/<id>/... route: a guest may read this screen
(their §4). A token is accepted, and a bad or expired one is still a 401,
because the JWT check runs on every route whatever its permission.

EVERY 404 IS OURS. The ids come in as text and are checked here
(params.path_uuid), as on the service detail: with Django's <uuid:> converter
a bad id would get Django's HTML page, not our JSON envelope.

PACKAGE_BOOKING_V1 on: the answer also carries `can_book`, last
(packages.can_book, the Packages tab's rule). Off: no key. Whatever
PACKAGES_LIVE_RULES_V1 says: this screen always shows the published content,
the price booking-api charges, so its `can_book` is always the rule's. (The
tab on its old rules shows the draft's price, and answers false.)
"""

from django.conf import settings
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

from . import packages
from .package_detail_serializers import EXAMPLE, SalonPackageDetailResponseSerializer
from .params import path_uuid
from .selectors import package_for_salon, package_lines, salon_profile
from .views import _OUR_ENVELOPE

# Written for the customer: the app shows `detail` on its empty state.
NO_SALON = "This salon is not available."
NO_PACKAGE = "This package is no longer available."


@extend_schema(
    summary="One package of a salon, for its details screen",
    description=(
        "Everything the Package Details screen draws, in one call. Public, "
        "like the other salon routes: a token is accepted, not needed, and a "
        "bad or expired one is a 401.\n\n"
        "A package of this salon's business that was ever on sale answers "
        "200, even when it has since been pulled: `is_active` says whether "
        "it can be booked here now. Another business's package, one never "
        "on sale, and an id that is not a UUID answer 404."
    ),
    parameters=[
        OpenApiParameter(
            "salon_id", OpenApiTypes.UUID, OpenApiParameter.PATH,
            description="The salon (storefront) id, as the other salon routes give it.",
        ),
        OpenApiParameter(
            "package_id", OpenApiTypes.UUID, OpenApiParameter.PATH,
            description="The package id, as GET /salon/{salon_id}/packages gives it (bundles[].id).",
        ),
    ],
    responses={
        200: OpenApiResponse(
            response=SalonPackageDetailResponseSerializer,
            description=(
                "Everything the screen draws. docs/PACKAGE_DETAIL_API.md has "
                "every field and rule."
            ),
            examples=[OpenApiExample("A package on sale", value=EXAMPLE)],
        ),
        401: OpenApiResponse(response=_OUR_ENVELOPE, description="A bad or expired token."),
        404: OpenApiResponse(
            response=_OUR_ENVELOPE,
            description=(
                "No such salon, no such package at this salon's business, a "
                "package never on sale, or an id that is not a UUID. `detail` "
                "is a sentence for the customer."
            ),
            examples=[
                OpenApiExample(
                    "No such package",
                    value={
                        "detail": NO_PACKAGE,
                        "code": "not_found",
                        "errors": [
                            {"field": None, "code": "not_found", "message": NO_PACKAGE}
                        ],
                    },
                ),
            ],
        ),
    },
)
class SalonPackageDetailView(APIView):
    """GET /api/v1/salon/<salon_id>/package/<package_id>"""

    permission_classes = [AllowAny]

    def get(self, request, salon_id, package_id):
        salon_uuid = path_uuid(salon_id)
        salon = salon_profile(salon_uuid) if salon_uuid else None
        if salon is None:
            raise NotFound(NO_SALON)

        package_uuid = path_uuid(package_id)
        package = package_for_salon(salon, package_uuid) if package_uuid else None
        if package is None:
            raise NotFound(NO_PACKAGE)

        # The tab's own reads (selectors.package_lines), for one package.
        content, lines, complete = package_lines(salon, [package])[package.id]
        answer = packages.detail(salon, package, content, lines, complete)
        if settings.PACKAGE_BOOKING_V1:
            answer["can_book"] = packages.can_book(package, content, lines, complete)
        return Response(answer)
