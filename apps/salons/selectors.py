from django.db.models import Avg, Count, OuterRef, Subquery

from apps.platform_data.models import Storefront, StorefrontReview


def discoverable_salons():
    published_reviews = StorefrontReview.objects.filter(
        storefront_id=OuterRef("pk"),
        state="PUBLISHED",
    )

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
        )
    )