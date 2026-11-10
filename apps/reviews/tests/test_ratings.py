"""
The app's reviews in the salon's and the stylists' rating and review_count
(docs/rate-your-visit-fe-contract.md, "What else a review changes", 2 and 3).
"""

import types
import uuid
from unittest import mock

from django.test import TestCase, override_settings

from apps.platform_data.models import Storefront
from apps.reviews.models import StylistRating
from apps.reviews.ratings import stylist_ratings
from apps.salons import expert_profile, selectors, views

from .fixtures import DARIUS, MARCO

SALON = types.SimpleNamespace(id=uuid.uuid4(), tenant_id=uuid.uuid4(), branch_id=uuid.uuid4())


def staff(staff_id, first):
    return types.SimpleNamespace(
        id=staff_id, tenant_id=SALON.tenant_id, branch_id=SALON.branch_id,
        first_name=first, last_name=None, position=None, job_title=None, avatar_url=None,
    )


@override_settings(RATE_VISIT_V1=True)
class StylistRatingTests(TestCase):
    def setUp(self):
        StylistRating.objects.create(stylist_id=DARIUS, rating_sum=17, review_count=4)
        StylistRating.objects.create(stylist_id=MARCO, rating_sum=0, review_count=0)

    def test_only_stylists_with_reviews(self):
        self.assertEqual(stylist_ratings([DARIUS, MARCO, uuid.uuid4()]), {DARIUS: (4.3, 4)})

    def test_the_stylists_tab_row_carries_them(self):
        row = views._stylist_row(staff(DARIUS, "Darius"), rating=(4.3, 4))
        self.assertEqual((row["rating"], row["review_count"]), (4.3, 4))
        row = views._stylist_row(staff(MARCO, "Marco"))
        self.assertEqual((row["rating"], row["review_count"]), (None, None))

    def test_the_stylists_tab_reads_and_sorts_by_them(self):
        roster = [staff(MARCO, "Marco"), staff(DARIUS, "Darius")]
        with mock.patch("apps.salons.views.salon_stylists", return_value=roster), mock.patch(
            "apps.salons.views.days_off_for", return_value={}
        ):
            rows = views.stylist_rows(SALON)
        self.assertEqual(
            [(row["name"], row["rating"], row["review_count"]) for row in rows],
            [("Darius", 4.3, 4), ("Marco", None, None)],
        )

    def test_the_expert_profile_shows_the_row_s_rating(self):
        row = views._stylist_row(staff(DARIUS, "Darius"), rating=(4.3, 4))
        fields = expert_profile.core_fields(SALON, row)
        self.assertEqual((fields["rating"], fields["review_count"]), (4.3, 4))

    def test_the_expert_profile_with_no_reviews_is_null_and_zero(self):
        fields = expert_profile.core_fields(SALON, views._stylist_row(staff(MARCO, "Marco")))
        self.assertEqual((fields["rating"], fields["review_count"]), (None, 0))

    @override_settings(RATE_VISIT_V1=False)
    def test_off_the_tab_is_as_before(self):
        roster = [staff(DARIUS, "Darius")]
        with mock.patch("apps.salons.views.salon_stylists", return_value=roster), mock.patch(
            "apps.salons.views.days_off_for", return_value={}
        ):
            rows = views.stylist_rows(SALON)
        self.assertEqual((rows[0]["rating"], rows[0]["review_count"]), (None, None))


class SalonRatingQueryTests(TestCase):
    """
    The platform's storefront tables are not in the test database, so this
    checks the SQL. The numbers are proven against the real tables locally.
    """

    def sql(self):
        return str(Storefront.objects.annotate(**selectors.salon_rating_fields()).query)

    @override_settings(RATE_VISIT_V1=False)
    def test_off_only_the_published_storefront_reviews(self):
        sql = self.sql()
        self.assertIn("storefront_review", sql)
        self.assertNotIn("salon_rating", sql)
        self.assertIn("AVG(", sql)

    @override_settings(RATE_VISIT_V1=True)
    def test_on_the_app_totals_are_added(self):
        sql = self.sql()
        self.assertIn("storefront_review", sql)
        self.assertIn('U0."rating_sum" AS "rating_sum" FROM "salon_rating"', sql)
        self.assertIn('U0."review_count" AS "review_count" FROM "salon_rating"', sql)
        self.assertIn("NULLIF(", sql)
