"""
GET /api/v1/salon/<id>/reviews and PUT, DELETE /api/v1/review/<id>/helpful,
against docs/salon-reviews-fe-contract.md.

Our tables (reviews, marks, photos, accounts) are real. The platform's
storefront tables and booking-api's database are not in the test database,
so the salon profile row, the published storefront counts and the services
are faked at the module's seams.
"""

import types
import uuid
from datetime import UTC, datetime, timedelta
from unittest import mock

from django.test import TestCase, override_settings
from django.utils import timezone
from rest_framework.test import APIClient

from apps.accounts.models import ConsumerAccount
from apps.accounts.services import tokens_for
from apps.reviews import salon_reviews
from apps.reviews.models import Review, ReviewHelpful, ReviewPhoto
from apps.uploads.models import FileType, UploadedFile

SALON = uuid.UUID("5d6a110f-5633-4bfe-942f-19be062a4373")
URL = f"/api/v1/salon/{SALON}/reviews"
FADE = {"id": "55a98876-efdc-4d04-b4bc-f783ced6a8fb", "name": "Modern Fade"}
BEARD = {"id": "7a7fafa5-bc8d-485e-b1ef-15a8f1634906", "name": "Beard Trim"}

RESULT_KEYS = [
    "id", "stars", "comment", "submitted_at", "author", "services",
    "helpful_count", "is_helpful", "is_mine", "photos", "tags",
]


@override_settings(SALON_REVIEWS_V1=True, RATE_VISIT_V1=True)
class SalonReviewsTestCase(TestCase):
    def setUp(self):
        self.client = APIClient()
        self.omar = ConsumerAccount.objects.create(phone="+971500000201", full_name="Omar Al Fahim")
        self.layla = ConsumerAccount.objects.create(
            phone="+971500000202", full_name="Layla Hassan", image="https://cdn.example.com/customers/layla.jpg"
        )
        self.sara = ConsumerAccount.objects.create(phone="+971500000203", full_name="Sara")

        # The profile's own row: rating and review_count as GET /salon/{id} has them.
        self.salon = types.SimpleNamespace(id=SALON, avg_rating=None, review_count=0)
        self.published = []
        self.services = {}
        seams = {
            "apps.reviews.salon_views.salon_profile": lambda salon_id, user=None: (
                self.salon if salon_id == SALON else None
            ),
            "apps.reviews.salon_reviews.published_star_counts": lambda salon_id: self.published,
            "apps.reviews.salon_reviews.services_of": lambda ids: {i: self.services[i] for i in ids if i in self.services},
        }
        for target, fake in seams.items():
            patcher = mock.patch(target, side_effect=fake)
            patcher.start()
            self.addCleanup(patcher.stop)

    def sign_in(self, account):
        self.client.credentials(HTTP_AUTHORIZATION=f"Bearer {tokens_for(account)['access']}")

    def review(self, customer, stars, comment=None, ago=timedelta(hours=1), helpful=0, salon_id=SALON, tags=()):
        review = Review.objects.create(
            booking_id=uuid.uuid4(),
            customer=customer,
            salon_id=salon_id,
            salon_stars=stars,
            comment=comment,
            submitted_at=timezone.now() - ago,
            helpful_count=helpful,
            salon_tags=list(tags),
        )
        self.salon.review_count = Review.objects.filter(salon_id=SALON).count()
        return review

    def photo(self, review, position=0):
        file = UploadedFile.objects.create(
            file=f"uploads/2026/11/{uuid.uuid4().hex}-cut.jpg",
            file_type=FileType.IMAGE,
            original_name="cut.jpg",
            uploaded_by=review.customer,
        )
        return ReviewPhoto.objects.create(review=review, file=file, position=position)

    def get(self, **query):
        return self.client.get(URL, query)

    def errors(self, response):
        return [(e["field"], e["code"]) for e in response.json()["errors"]]

    def ids(self, response):
        return [row["id"] for row in response.json()["results"]]


class SwitchTests(SalonReviewsTestCase):
    @override_settings(SALON_REVIEWS_V1=False)
    def test_off_all_three_are_404(self):
        review = self.review(self.omar, 5)
        self.assertEqual(self.get().status_code, 404)
        self.sign_in(self.layla)
        self.assertEqual(self.client.put(f"/api/v1/review/{review.id}/helpful").status_code, 404)
        self.assertEqual(self.client.delete(f"/api/v1/review/{review.id}/helpful").status_code, 404)
        self.assertFalse(ReviewHelpful.objects.exists())


class ListTests(SalonReviewsTestCase):
    def test_public_without_a_token(self):
        self.review(self.omar, 5, "Best fade")
        response = self.get()
        self.assertEqual(response.status_code, 200)
        row = response.json()["results"][0]
        self.assertEqual((row["is_helpful"], row["is_mine"]), (False, False))

    def test_the_doc_shape(self):
        review = self.review(self.omar, 5, "Best fade I have had in Dubai.", helpful=13, tags=["cleanliness"])
        review.submitted_at = datetime(2026, 11, 12, 9, 41, 7, 512000, tzinfo=UTC)
        review.save()
        self.services = {review.booking_id: [FADE, BEARD]}
        photo = self.photo(review)
        self.salon.avg_rating, self.salon.review_count = 5.0, 1

        body = self.get().json()
        self.assertEqual(list(body), ["summary", "count", "next", "previous", "results"])
        self.assertEqual(list(body["summary"]), ["rating", "review_count", "distribution"])
        row = body["results"][0]
        self.assertEqual(list(row), RESULT_KEYS)
        self.assertEqual(row["id"], str(review.id))
        self.assertEqual(row["stars"], 5)
        self.assertEqual(row["comment"], "Best fade I have had in Dubai.")
        self.assertEqual(row["submitted_at"], "2026-11-12T09:41:07Z")
        self.assertEqual(row["author"], {"name": "Omar A.", "avatar_url": None})
        self.assertEqual(row["services"], [FADE, BEARD])
        self.assertEqual(row["helpful_count"], 13)
        self.assertEqual(row["photos"][0]["id"], str(photo.file_id))
        self.assertTrue(row["photos"][0]["url"].startswith("http"))
        self.assertEqual(row["tags"], [{"code": "cleanliness", "label": "Cleanliness"}])

    def test_no_comment_is_listed_with_null(self):
        self.review(self.layla, 4)
        row = self.get().json()["results"][0]
        self.assertIsNone(row["comment"])
        self.assertEqual(row["author"], {"name": "Layla H.", "avatar_url": "https://cdn.example.com/customers/layla.jpg"})
        self.assertEqual(row["services"], [])
        self.assertEqual((row["photos"], row["tags"]), ([], []))

    def test_public_name(self):
        for full, shown in (
            ("Omar Al Fahim", "Omar A."),
            ("Layla Hassan", "Layla H."),
            ("  layla   hassan ", "layla H."),
            ("Sara", "Sara"),
            ("", None),
            (None, None),
        ):
            self.assertEqual(salon_reviews.public_name(full), shown, full)

    def test_a_one_word_name_stays(self):
        self.review(self.sara, 4)
        self.assertEqual(self.get().json()["results"][0]["author"]["name"], "Sara")

    def test_with_a_token_is_mine_and_is_helpful(self):
        mine = self.review(self.layla, 4, ago=timedelta(hours=1))
        theirs = self.review(self.omar, 5, ago=timedelta(hours=2))
        ReviewHelpful.objects.create(review=theirs, customer=self.layla)
        self.sign_in(self.layla)
        rows = {row["id"]: row for row in self.get(sort="newest").json()["results"]}
        self.assertEqual((rows[str(mine.id)]["is_mine"], rows[str(mine.id)]["is_helpful"]), (True, False))
        self.assertEqual((rows[str(theirs.id)]["is_mine"], rows[str(theirs.id)]["is_helpful"]), (False, True))

    def test_only_this_salon(self):
        self.review(self.omar, 5, salon_id=uuid.uuid4())
        self.assertEqual(self.get().json()["count"], 0)

    def test_unknown_salon_is_404_not_found(self):
        response = self.client.get(f"/api/v1/salon/{uuid.uuid4()}/reviews")
        self.assertEqual(response.status_code, 404)
        self.assertEqual(self.errors(response), [(None, "not_found")])

    def test_bad_sort_is_422_invalid_sort(self):
        response = self.get(sort="featured")
        self.assertEqual(response.status_code, 422)
        self.assertEqual(self.errors(response), [("sort", "invalid_sort")])

    def test_no_sort_is_top(self):
        self.review(self.omar, 5, "x", helpful=0, ago=timedelta(hours=1))
        best = self.review(self.layla, 3, None, helpful=4, ago=timedelta(days=3))
        self.assertEqual(self.ids(self.get())[0], str(best.id))


class SummaryTests(SalonReviewsTestCase):
    def test_no_reviews(self):
        body = self.get().json()
        self.assertEqual(
            body["summary"],
            {
                "rating": None,
                "review_count": 0,
                "distribution": [{"stars": s, "count": 0} for s in (5, 4, 3, 2, 1)],
            },
        )
        self.assertEqual((body["count"], body["next"], body["previous"], body["results"]), (0, None, None, []))

    def test_the_profile_s_numbers_and_bars_that_add_up(self):
        for stars in (5, 5, 4, 1):
            self.review(self.omar, stars)
        # Two published storefront reviews count in the profile too.
        self.published = [(5, 1), (3, 1)]
        self.salon.avg_rating, self.salon.review_count = 23 / 6, 6

        summary = self.get().json()["summary"]
        self.assertEqual(summary["rating"], 3.8)
        self.assertEqual(summary["review_count"], 6)
        self.assertEqual(
            summary["distribution"],
            [
                {"stars": 5, "count": 3},
                {"stars": 4, "count": 1},
                {"stars": 3, "count": 1},
                {"stars": 2, "count": 0},
                {"stars": 1, "count": 1},
            ],
        )
        self.assertEqual(sum(bar["count"] for bar in summary["distribution"]), summary["review_count"])

    def test_on_every_page_whatever_the_sort(self):
        self.review(self.omar, 5)
        self.salon.avg_rating = 5.0
        first = self.get(sort="lowest", page=3).json()["summary"]
        self.assertEqual(first, self.get(sort="top").json()["summary"])

    @override_settings(RATE_VISIT_V1=False)
    def test_without_rate_visit_the_bars_are_the_profile_s_storefront_ones(self):
        self.review(self.omar, 5)
        self.published = [(4, 2)]
        bars = self.get().json()["summary"]["distribution"]
        self.assertEqual([bar["count"] for bar in bars], [0, 2, 0, 0, 0])


class OrderTests(SalonReviewsTestCase):
    def setUp(self):
        super().setUp()
        self.old_top = self.review(self.omar, 2, "slow", helpful=5, ago=timedelta(days=5))
        self.new_no_comment = self.review(self.layla, 5, None, helpful=1, ago=timedelta(hours=1))
        self.old_comment = self.review(self.sara, 4, "good", helpful=1, ago=timedelta(days=2))
        self.mid = self.review(self.omar, 5, "great", helpful=0, ago=timedelta(days=1))

    def test_top(self):
        # helpful_count, then a comment first, then newest.
        self.assertEqual(
            self.ids(self.get(sort="top")),
            [str(r.id) for r in (self.old_top, self.old_comment, self.new_no_comment, self.mid)],
        )

    def test_newest(self):
        self.assertEqual(
            self.ids(self.get(sort="newest")),
            [str(r.id) for r in (self.new_no_comment, self.mid, self.old_comment, self.old_top)],
        )

    def test_highest(self):
        self.assertEqual(
            self.ids(self.get(sort="highest")),
            [str(r.id) for r in (self.new_no_comment, self.mid, self.old_comment, self.old_top)],
        )

    def test_lowest(self):
        self.assertEqual(
            self.ids(self.get(sort="lowest")),
            [str(r.id) for r in (self.old_top, self.old_comment, self.new_no_comment, self.mid)],
        )

    def test_ties_end_on_the_id(self):
        Review.objects.update(submitted_at=timezone.now(), helpful_count=0, salon_stars=3, comment="same")
        expected = sorted(str(r.id) for r in Review.objects.all())
        for sort in ("top", "newest", "highest", "lowest"):
            self.assertEqual(self.ids(self.get(sort=sort)), expected, sort)


class PagingTests(SalonReviewsTestCase):
    def setUp(self):
        super().setUp()
        for i in range(5):
            self.review(self.omar, 5, ago=timedelta(hours=i + 1))

    def test_pages_like_get_bookings(self):
        body = self.get(sort="newest", page=2, pageSize=2).json()
        self.assertEqual(body["count"], 5)
        self.assertEqual(len(body["results"]), 2)
        self.assertEqual(body["next"], f"http://testserver{URL}?sort=newest&page=3&pageSize=2")
        self.assertEqual(body["previous"], f"http://testserver{URL}?sort=newest&page=1&pageSize=2")

        last = self.get(sort="newest", page=3, pageSize=2).json()
        self.assertEqual((len(last["results"]), last["next"]), (1, None))

    def test_first_page_defaults(self):
        body = self.get().json()
        self.assertEqual((body["count"], len(body["results"]), body["next"], body["previous"]), (5, 5, None, None))

    def test_page_past_the_end_is_an_empty_200(self):
        body = self.get(page=9, pageSize=2).json()
        self.assertEqual((body["results"], body["next"]), ([], None))
        self.assertEqual(body["previous"], f"http://testserver{URL}?sort=top&page=8&pageSize=2")

    def test_bad_numbers_fall_back_and_the_size_is_capped(self):
        body = self.get(page="abc", pageSize="0").json()
        self.assertEqual(len(body["results"]), 5)
        self.assertIsNone(self.get(pageSize=500).json()["next"])
        for i in range(50):
            self.review(self.layla, 4, ago=timedelta(days=1, minutes=i))
        body = self.get(pageSize=500).json()
        self.assertEqual(len(body["results"]), 50)
        self.assertIn("pageSize=50", body["next"])

    def test_page_and_count_match_review_count(self):
        body = self.get().json()
        self.assertEqual(body["count"], body["summary"]["review_count"])


class PhotosFilterTests(SalonReviewsTestCase):
    def test_has_photos_lists_only_those(self):
        with_photos = self.review(self.omar, 5, ago=timedelta(hours=1))
        self.photo(with_photos, 0)
        self.photo(with_photos, 1)
        self.review(self.layla, 4, ago=timedelta(hours=2))
        self.salon.review_count = 2

        body = self.get(has_photos="true", pageSize=1).json()
        self.assertEqual(body["count"], 1)
        self.assertEqual(self.ids(self.client.get(URL, {"has_photos": "true"})), [str(with_photos.id)])
        self.assertEqual(body["summary"]["review_count"], 2)
        self.assertEqual(len(body["results"][0]["photos"]), 2)

        self.assertEqual(self.get().json()["count"], 2)

    def test_links_keep_has_photos(self):
        for i in range(3):
            self.photo(self.review(self.omar, 5, ago=timedelta(hours=i + 1)))
        body = self.get(has_photos="true", pageSize=1).json()
        self.assertEqual(body["next"], f"http://testserver{URL}?sort=top&has_photos=true&page=2&pageSize=1")


class HelpfulTests(SalonReviewsTestCase):
    def setUp(self):
        super().setUp()
        self.target = self.review(self.omar, 5, "Best fade")
        self.helpful_url = f"/api/v1/review/{self.target.id}/helpful"

    def test_guest_is_401(self):
        self.assertEqual(self.client.put(self.helpful_url).status_code, 401)
        self.assertEqual(self.client.delete(self.helpful_url).status_code, 401)

    def test_mark_and_unmark_answer_the_saved_state(self):
        self.sign_in(self.layla)
        response = self.client.put(self.helpful_url)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"is_helpful": True, "helpful_count": 1})

        response = self.client.delete(self.helpful_url)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"is_helpful": False, "helpful_count": 0})
        self.assertFalse(ReviewHelpful.objects.exists())

    def test_both_are_idempotent(self):
        self.sign_in(self.layla)
        self.client.put(self.helpful_url)
        self.assertEqual(self.client.put(self.helpful_url).json(), {"is_helpful": True, "helpful_count": 1})
        self.sign_in(self.sara)
        self.assertEqual(self.client.put(self.helpful_url).json(), {"is_helpful": True, "helpful_count": 2})
        self.assertEqual(self.client.delete(self.helpful_url).json(), {"is_helpful": False, "helpful_count": 1})
        self.assertEqual(self.client.delete(self.helpful_url).json(), {"is_helpful": False, "helpful_count": 1})
        self.target.refresh_from_db()
        self.assertEqual(self.target.helpful_count, 1)

    def test_own_review_is_403_and_nothing_saved(self):
        self.sign_in(self.omar)
        response = self.client.put(self.helpful_url)
        self.assertEqual(response.status_code, 403)
        self.assertEqual(self.errors(response), [(None, "own_review")])
        self.assertFalse(ReviewHelpful.objects.exists())
        self.target.refresh_from_db()
        self.assertEqual(self.target.helpful_count, 0)

    def test_delete_on_own_review_answers_the_state(self):
        self.sign_in(self.omar)
        response = self.client.delete(self.helpful_url)
        self.assertEqual((response.status_code, response.json()), (200, {"is_helpful": False, "helpful_count": 0}))

    def test_no_such_review_is_404_review_not_found(self):
        self.sign_in(self.layla)
        for url in (f"/api/v1/review/{uuid.uuid4()}/helpful", "/api/v1/review/not-a-uuid/helpful"):
            for call in (self.client.put, self.client.delete):
                response = call(url)
                self.assertEqual(response.status_code, 404)
                self.assertEqual(self.errors(response), [(None, "review_not_found")])

    def test_the_list_shows_the_mark(self):
        self.sign_in(self.layla)
        self.client.put(self.helpful_url)
        row = self.get().json()["results"][0]
        self.assertEqual((row["helpful_count"], row["is_helpful"], row["is_mine"]), (1, True, False))


class DeletedAccountTests(SalonReviewsTestCase):
    def test_purge_removes_marks_and_keeps_reviews_without_a_name(self):
        theirs = self.review(self.omar, 5)
        own = self.review(self.layla, 4)
        for account in (self.layla, self.sara):
            salon_reviews.mark_helpful(theirs.id, account)
        salon_reviews.mark_helpful(own.id, self.sara)

        self.layla.purge()

        theirs.refresh_from_db()
        self.assertEqual(theirs.helpful_count, 1)
        self.assertFalse(ReviewHelpful.objects.filter(customer=self.layla).exists())
        own.refresh_from_db()
        self.assertEqual(own.helpful_count, 1)

        rows = {row["id"]: row for row in self.get().json()["results"]}
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[str(own.id)]["author"], {"name": None, "avatar_url": None})
