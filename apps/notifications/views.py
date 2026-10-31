"""
The Notification Center (docs/NOTIFICATION_CENTER_API.md), behind
NOTIFICATION_CENTER_V1. Off, every route here is the 404 it was before.

The customer always comes from the token: every query starts from
request.user, and another account's id answers 404, never 403, so ids
cannot be probed.
"""

import uuid

from django.conf import settings
from django.db.models import Count, Q
from django.http import Http404
from django.utils import timezone
from drf_spectacular.types import OpenApiTypes
from drf_spectacular.utils import OpenApiParameter, extend_schema
from rest_framework import serializers
from rest_framework.exceptions import NotFound
from rest_framework.pagination import CursorPagination
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.accounts.ratelimit import enforce_notifications_read_all

from .models import Category, Notification
from .serializers import (
    ListParamsSerializer,
    NotificationSerializer,
    ReadAllResultSerializer,
    ReadAllSerializer,
    UnreadCountSerializer,
)


def _require_switch():
    if not settings.NOTIFICATION_CENTER_V1:
        raise Http404("Not found")


def _visible(account):
    """The account's items, less offers past their end."""
    return Notification.objects.filter(account=account).filter(
        Q(expires_at__isnull=True) | Q(expires_at__gt=timezone.now())
    )


def _category(queryset, category):
    return queryset if category == "all" else queryset.filter(category=category)


def _unread_counts(account):
    counts = dict.fromkeys(Category.values, 0)
    rows = (
        _visible(account)
        .filter(read_at__isnull=True)
        .values("category")
        .annotate(n=Count("id"))
    )
    for row in rows:
        counts[row["category"]] = row["n"]
    return {"total": sum(counts.values()), **counts}


def _own(request, notification_id):
    """The caller's notification with this id, or 404 (unknown, someone
    else's, or not a uuid at all: the route takes any string so a bad id
    gets this JSON 404, not Django's HTML one)."""
    try:
        pk = uuid.UUID(str(notification_id))
    except ValueError:
        raise Http404("Not found") from None
    row = _visible(request.user).filter(pk=pk).first()
    if row is None:
        raise Http404("Not found")
    return row


class NotificationPagination(CursorPagination):
    """Newest first, by cursor, not page number: new items arrive at the top,
    and page numbers would then show one twice or skip one."""

    ordering = ("-created_at", "-id")
    page_size = 20
    page_size_query_param = "page_size"
    max_page_size = 50

    def decode_cursor(self, request):
        try:
            return super().decode_cursor(request)
        except NotFound:
            raise serializers.ValidationError(
                {"cursor": [serializers.ErrorDetail("Invalid cursor.", code="invalid_cursor")]}
            ) from None


class NotificationListView(APIView):
    """GET /api/v1/notifications"""

    @extend_schema(
        summary="My notifications, newest first",
        description=(
            "Behind NOTIFICATION_CENTER_V1 (off: 404). `category` is a tab: "
            "`all` (default), `orders`, `updates`, `payments`, `promotions`. "
            "`unread=true` for unread only. Paged by cursor: follow `next`. "
            "No items is `200` with an empty `results`. Group by Today, "
            "Yesterday and so on in the app, from `created_at` in the "
            "phone's own time zone."
        ),
        parameters=[
            OpenApiParameter("category", OpenApiTypes.STR, enum=["all", *Category.values]),
            OpenApiParameter("unread", OpenApiTypes.BOOL),
            OpenApiParameter("cursor", OpenApiTypes.STR),
            OpenApiParameter("page_size", OpenApiTypes.INT, description="1 to 50, default 20."),
        ],
        responses={200: NotificationSerializer(many=True)},
    )
    def get(self, request):
        _require_switch()
        params = ListParamsSerializer(data=request.query_params)
        params.is_valid(raise_exception=True)
        rows = _category(_visible(request.user), params.validated_data["category"])
        unread = params.validated_data["unread"]
        if unread is not None:
            rows = rows.filter(read_at__isnull=unread)

        paginator = NotificationPagination()
        page = paginator.paginate_queryset(rows, request, view=self)
        return paginator.get_paginated_response(NotificationSerializer(page, many=True).data)


class NotificationDetailView(APIView):
    """GET /api/v1/notifications/<id>"""

    @extend_schema(
        summary="One notification",
        description=(
            "Behind NOTIFICATION_CENTER_V1 (off: 404). Does not mark it read: "
            "call POST .../read when the customer opens it."
        ),
        responses={200: NotificationSerializer},
    )
    def get(self, request, notification_id):
        _require_switch()
        return Response(NotificationSerializer(_own(request, notification_id)).data)


class NotificationReadView(APIView):
    """POST /api/v1/notifications/<id>/read"""

    @extend_schema(
        summary="Mark one notification read",
        description=(
            "Behind NOTIFICATION_CENTER_V1 (off: 404). No body. Safe to repeat: "
            "an item already read keeps its first read_at."
        ),
        request=None,
        responses={200: NotificationSerializer},
    )
    def post(self, request, notification_id):
        _require_switch()
        row = _own(request, notification_id)
        if row.read_at is None:
            # Only an unread row is written, so two taps keep the first time.
            Notification.objects.filter(pk=row.pk, read_at__isnull=True).update(
                read_at=timezone.now()
            )
            row.refresh_from_db(fields=["read_at"])
        return Response(NotificationSerializer(row).data)


class NotificationReadAllView(APIView):
    """POST /api/v1/notifications/read-all"""

    @extend_schema(
        summary="Mark all notifications read",
        description=(
            "Behind NOTIFICATION_CENTER_V1 (off: 404). Both fields optional. "
            "`category` limits it to one tab. `before` limits it to items "
            "created at or before that time: send the newest one on screen, "
            "so one arriving meanwhile stays unread. Answers how many changed "
            "and the unread counts after."
        ),
        request=ReadAllSerializer,
        responses={200: ReadAllResultSerializer},
    )
    def post(self, request):
        _require_switch()
        enforce_notifications_read_all(request.user.pk)
        body = ReadAllSerializer(data=request.data)
        body.is_valid(raise_exception=True)
        rows = _category(_visible(request.user), body.validated_data["category"]).filter(
            read_at__isnull=True
        )
        if body.validated_data["before"] is not None:
            rows = rows.filter(created_at__lte=body.validated_data["before"])
        updated = rows.update(read_at=timezone.now())
        return Response({"updated": updated, "unread": _unread_counts(request.user)})


class NotificationUnreadCountView(APIView):
    """GET /api/v1/notifications/unread-count"""

    @extend_schema(
        summary="How many notifications are unread, in all and per tab",
        description="Behind NOTIFICATION_CENTER_V1 (off: 404).",
        responses={200: UnreadCountSerializer},
    )
    def get(self, request):
        _require_switch()
        return Response(_unread_counts(request.user))
