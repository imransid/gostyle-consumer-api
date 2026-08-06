from django.db.models import Avg, Count, Exists, FloatField, OuterRef, Subquery, TextField

from apps.platform_data.models import (
    Branch,
    Storefront,
    StorefrontCertification,
    StorefrontMedia,
    StorefrontReview,
)

def discoverable_salons():
    published_reviews = StorefrontReview.objects.filter(
        storefront_id=OuterRef("pk"),
        state="PUBLISHED",
    )

    branch = Branch.objects.filter(id=OuterRef("branch_id"))

    return (
        Storefront.objects.filter(
            visibility="PUBLIC",
            link_enabled=True,
            deleted_at__isnull=True,
        )
        .annotate(
            avg_rating=Subquery(
                published_reviews.values("storefront_id")
                .annotate(avg=Avg("rating"))
                .values("avg")[:1]
            ),
            review_count=Subquery(
                published_reviews.values("storefront_id")
                .annotate(c=Count("id"))
                .values("c")[:1]
            ),
            branch_name=Subquery(
                branch.values("name")[:1], output_field=TextField()
            ),
            branch_city=Subquery(
                branch.values("city")[:1], output_field=TextField()
            ),
            lat=Subquery(branch.values("lat")[:1], output_field=FloatField()),
            lng=Subquery(branch.values("lng")[:1], output_field=FloatField()),
            opening_hours=Subquery(branch.values("opening_hours")[:1]),
            branch_timezone=Subquery(
                branch.values("timezone")[:1], output_field=TextField()
            ),
            photo_url=Subquery(
                StorefrontMedia.objects.filter(
                    storefront_id=OuterRef("pk"),
                    deleted_at__isnull=True,
                    is_public=True,
                )
                .order_by("-is_featured", "sort_order")
                .values("url")[:1],
                output_field=TextField(),
            ),
            hijab_certified=Exists(
                StorefrontCertification.objects.filter(
                    storefront_id=OuterRef("pk"),
                    state="CERTIFIED",
                    revoked_at__isnull=True,
                    deleted_at__isnull=True,
                )
            ),
        )
    )