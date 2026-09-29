"""
The app team's routine contract (docs/routine-booking-fe-contract.md, and
our draft of the rest, docs/routine-booking-fe-contract-draft.md):

    POST  /api/v1/booking/routine-preview                 the dates a plan would take
    POST  /api/v1/booking/routine                         book the whole plan
    PATCH /api/v1/booking/<id>/sessions/<session_id>      move one session

Next to the /booking/series routes, which do not change: both sets reach the
same booking-api routes, and this one speaks the contract's words
(routine_contract.py, pure).

Behind ROUTINE_CONTRACT_V1: off, every route here is 404 and booking-api is
never called. Step C1 only draws the routes (docs/ROUTINE_FE_CONTRACT_AUDIT.md,
4.2); C3 to C5 build them. Until then, with the flag on, they answer 501, so
a switch turned on too early is not mistaken for a missing route.
"""

from django.conf import settings
from django.http import Http404
from drf_spectacular.utils import OpenApiResponse, extend_schema
from rest_framework import status
from rest_framework.exceptions import APIException
from rest_framework.permissions import IsAuthenticated
from rest_framework.views import APIView

from .views import _OUR_ENVELOPE


class RouteNotBuilt(APIException):
    """C1 only: the flag is on, and the route is built in a later step."""

    status_code = status.HTTP_501_NOT_IMPLEMENTED
    default_detail = "This route is not built yet."
    default_code = "not_built"


def _gate():
    """404 with the flag off, before anything else is looked at."""
    if not settings.ROUTINE_CONTRACT_V1:
        raise Http404("Not found")


_FLAG = (
    "Behind ROUTINE_CONTRACT_V1 (off: 404, booking-api is not called). "
    "Not built yet: with the flag on it answers 501 until its step lands "
    "(docs/ROUTINE_FE_CONTRACT_AUDIT.md, 4.2)."
)
_RESPONSES = {
    404: OpenApiResponse(response=_OUR_ENVELOPE, description="The flag is off."),
    501: OpenApiResponse(response=_OUR_ENVELOPE, description="Not built yet."),
}


class RoutinePreviewView(APIView):
    """POST /api/v1/booking/routine-preview (built in C3)."""

    permission_classes = [IsAuthenticated]

    @extend_schema(
        summary="Preview a routine (app contract)",
        description="The dates a plan would take, each available or not. " + _FLAG,
        request=None,
        responses=_RESPONSES,
    )
    def post(self, request):
        _gate()
        raise RouteNotBuilt()


class RoutineCreateView(APIView):
    """POST /api/v1/booking/routine (built in C4)."""

    permission_classes = [IsAuthenticated]

    @extend_schema(
        summary="Book a routine (app contract)",
        description="Every session booked in one call, or none. " + _FLAG,
        request=None,
        responses=_RESPONSES,
    )
    def post(self, request):
        _gate()
        raise RouteNotBuilt()


class RoutineSessionMoveView(APIView):
    """PATCH /api/v1/booking/<id>/sessions/<session_id> (built in C5)."""

    permission_classes = [IsAuthenticated]

    @extend_schema(
        summary="Move one session of a routine (app contract)",
        description="One session to a new start; the rest of the routine stays. " + _FLAG,
        request=None,
        responses=_RESPONSES,
    )
    def patch(self, request, booking_id, session_id):
        _gate()
        raise RouteNotBuilt()
