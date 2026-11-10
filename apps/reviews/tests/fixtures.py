"""Shared test data: one visit to the Iron Razor with Darius and Marco, as in the doc."""

import uuid
from datetime import timedelta

from django.utils import timezone

from apps.reviews.visits import Line, Visit

SALON = "5d6a110f-5633-4bfe-942f-19be062a4373"
BRANCH = uuid.UUID("22222222-2222-2222-2222-222222222222")
TENANT = uuid.UUID("11111111-1111-1111-1111-111111111111")
BOOKING = uuid.UUID("b7f1c2e4-1d2a-4c3b-9f8e-0a1b2c3d4e5f")
DARIUS = uuid.UUID("c1a2b3c4-0000-4000-8000-000000000001")
MARCO = uuid.UUID("c1a2b3c4-0000-4000-8000-000000000002")
NOBODY = uuid.UUID("c1a2b3c4-0000-4000-8000-0000000000ff")
FADE = "6f1d2e3a-0000-4000-8000-000000000001"
BEARD = "6f1d2e3a-0000-4000-8000-000000000002"
GREY = "6f1d2e3a-0000-4000-8000-000000000003"

SALON_CARD = {
    "id": SALON,
    "name": "The Iron Razor Barbershop",
    "logo_url": "https://cdn.example.com/salons/iron-razor/logo.png",
    "city": "Dubai",
    "timezone": "Asia/Dubai",
}

STYLIST_CARDS = {
    DARIUS: {"name": "Darius Stone", "avatar_url": "https://cdn.example.com/stylists/darius.jpg"},
    MARCO: {"name": "Marco Bell", "avatar_url": None},
}


def make_visit(customer_id, status="completed", completed_ago=timedelta(hours=2), lines=None, **over):
    """A visit that started three hours ago: Darius did the fade, then Marco the beard and the grey."""
    now = timezone.now()
    start = now - timedelta(hours=3)
    if lines is None:
        lines = (
            Line(FADE, "Modern Fade", DARIUS, 0, start),
            Line(BEARD, "Beard trim", MARCO, 1, start + timedelta(minutes=30)),
            Line(GREY, "Grey blending", MARCO, 2, start + timedelta(minutes=50)),
        )
    fields = {
        "booking_id": BOOKING,
        "customer_id": customer_id,
        "status": status,
        "branch_id": BRANCH,
        "tenant_id": TENANT,
        "start_at": start,
        "completed_at": None if completed_ago is None else now - completed_ago,
        "lines": tuple(lines),
        **over,
    }
    return Visit(**fields)
