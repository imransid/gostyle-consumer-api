"""
GET and POST /api/v1/booking/<id>/review, against docs/rate-your-visit-fe-contract.md.

Our tables (review, ratings, photos, totals, uploads, accounts) are real.
booking-api's database and the platform's salon and staff tables are not in
the test database, so the visit, the salon card, the salon type and the
stylist cards are faked at the service's seams.
"""

import uuid
from datetime import timedelta
from unittest import mock

from django.test import TestCase, override_settings
from rest_framework.test import APIClient

from apps.accounts.models import ConsumerAccount
from apps.accounts.services import tokens_for
from apps.reviews import service
from apps.reviews.models import Review, ReviewPhoto, ReviewStylistRating, SalonRating, StylistRating
from apps.reviews.visits import BookingDataUnavailable, Line
from apps.uploads.models import FileType, UploadedFile

from .fixtures import BOOKING, BRANCH, DARIUS, FADE, MARCO, SALON, SALON_CARD, STYLIST_CARDS, TENANT, make_visit

URL = f"/api/v1/booking/{BOOKING}/review"


@override_settings(RATE_VISIT_V1=True)
class ReviewApiTestCase(TestCase):
    def setUp(self):
        self.client = APIClient()
        self.sara = ConsumerAccount.objects.create(phone="+971500000101")
        self.omar = ConsumerAccount.objects.create(phone="+971500000102")
        self.sign_in(self.sara)
        self.visit = make_visit(self.sara.id)
        self.category = "gents"

        seams = {
            "apps.reviews.visits.find_visit": lambda ref, customer_id: self.visit,
            "apps.reviews.service.salon_cards_for_refs": lambda refs: {str(BRANCH): SALON_CARD},
            "apps.reviews.service.salon_category": lambda salon_id: self.category,
            "apps.reviews.service.stylist_cards": lambda ids: {i: STYLIST_CARDS[i] for i in ids if i in STYLIST_CARDS},
        }
        for target, fake in seams.items():
            patcher = mock.patch(target, side_effect=fake)
            patcher.start()
            self.addCleanup(patcher.stop)

    def sign_in(self, account):
        self.client.credentials(HTTP_AUTHORIZATION=f"Bearer {tokens_for(account)['access']}")

    def photo(self, account=None, file_type=FileType.IMAGE, name="cut.jpg"):
        return UploadedFile.objects.create(
            file=f"uploads/2026/11/{uuid.uuid4().hex}-{name}",
            file_type=file_type,
            original_name=name,
            uploaded_by=account or self.sara,
        )

    def post(self, body):
        return self.client.post(URL, body, format="json")

    def doc_example(self, **over):
        """The doc's request: Darius rated, Marco skipped."""
        body = {
            "salon": {"stars": 4, "tags": ["cleanliness", "ambience"]},
            "stylists": [{"stylist_id": str(DARIUS), "stars": 5, "tags": ["skill", "attention_to_detail"]}],
            "comment": "Great fade and they started right on time.",
            "photo_ids": [],
        }
        body.update(over)
        return body

    def errors(self, response):
        return [(e["field"], e["code"]) for e in response.json()["errors"]]


class SwitchAndAuthTests(ReviewApiTestCase):
    @override_settings(RATE_VISIT_V1=False)
    def test_off_both_routes_are_a_404(self):
        self.assertEqual(self.client.get(URL).status_code, 404)
        self.assertEqual(self.post(self.doc_example()).status_code, 404)
        self.assertEqual(Review.objects.count(), 0)

    def test_not_signed_in_is_a_401(self):
        self.client.credentials()
        self.assertEqual(self.client.get(URL).status_code, 401)
        self.assertEqual(self.post(self.doc_example()).status_code, 401)

    def test_no_booking_or_someone_else_s_is_404_booking_not_found(self):
        # find_visit answers None for both (it checks the owner).
        with mock.patch("apps.reviews.visits.find_visit", return_value=None):
            for response in (self.client.get(URL), self.post(self.doc_example())):
                self.assertEqual(response.status_code, 404)
                self.assertEqual(
                    response.json(),
                    {
                        "detail": "We couldn't find this booking.",
                        "code": "not_found",
                        "errors": [
                            {"field": None, "code": "booking_not_found", "message": "We couldn't find this booking."}
                        ],
                    },
                )

    def test_a_salon_that_is_gone_is_404_too(self):
        with mock.patch("apps.reviews.service.salon_cards_for_refs", return_value={}):
            response = self.client.get(URL)
        self.assertEqual(self.errors(response), [(None, "booking_not_found")])

    def test_booking_data_down_is_a_503(self):
        with mock.patch("apps.reviews.visits.find_visit", side_effect=BookingDataUnavailable("down")):
            response = self.client.get(URL)
        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.json()["code"], "service_unavailable")


class FormTests(ReviewApiTestCase):
    def test_the_form_as_the_doc_draws_it(self):
        response = self.client.get(URL)
        self.assertEqual(response.status_code, 200)
        body = response.json()
        completed_at = self.visit.completed_at.astimezone(service.timezones.resolve("Asia/Dubai"))
        self.assertEqual(
            body["booking"],
            {
                "id": str(BOOKING),
                "status": "completed",
                "completed_at": completed_at.isoformat(timespec="seconds"),
                "service_count": 3,
            },
        )
        self.assertTrue(body["booking"]["completed_at"].endswith("+04:00"))
        self.assertEqual(
            body["salon"],
            {
                "id": SALON,
                "name": "The Iron Razor Barbershop",
                "logo_url": "https://cdn.example.com/salons/iron-razor/logo.png",
            },
        )
        self.assertEqual(
            body["stylists"],
            [
                {
                    "id": str(DARIUS),
                    "name": "Darius Stone",
                    "avatar_url": "https://cdn.example.com/stylists/darius.jpg",
                    "services": [{"id": FADE, "name": "Modern Fade"}],
                },
                {
                    "id": str(MARCO),
                    "name": "Marco Bell",
                    "avatar_url": None,
                    "services": [
                        {"id": "6f1d2e3a-0000-4000-8000-000000000002", "name": "Beard trim"},
                        {"id": "6f1d2e3a-0000-4000-8000-000000000003", "name": "Grey blending"},
                    ],
                },
            ],
        )
        self.assertEqual(len(body["tags"]["salon"]), 8)
        self.assertEqual(len(body["tags"]["stylist"]), 8)
        self.assertEqual(body["limits"], {"max_photos": 3, "comment_max_length": 500})
        self.assertEqual((body["can_rate"], body["reason"], body["review"]), (True, None, None))
        self.assertEqual(
            list(body),
            ["booking", "salon", "stylists", "tags", "limits", "can_rate", "reason", "review"],
        )

    def test_a_ladies_salon_s_stylist_tags_have_no_beard_work(self):
        self.category = "ladies"
        codes = [tag["code"] for tag in self.client.get(URL).json()["tags"]["stylist"]]
        self.assertNotIn("beard_work", codes)

    def test_not_completed_is_a_200_with_its_reason_and_real_status(self):
        self.visit = make_visit(self.sara.id, status="confirmed", completed_ago=None)
        body = self.client.get(URL).json()
        self.assertEqual((body["can_rate"], body["reason"]), (False, "not_completed"))
        self.assertEqual(body["booking"]["status"], "confirmed_by_salon")

    def test_no_show_reads_as_not_completed(self):
        self.visit = make_visit(self.sara.id, status="no_show", completed_ago=None)
        body = self.client.get(URL).json()
        self.assertEqual((body["can_rate"], body["reason"], body["booking"]["status"]), (False, "not_completed", "cancelled"))

    def test_settled_is_completed(self):
        self.visit = make_visit(self.sara.id, status="settled")
        body = self.client.get(URL).json()
        self.assertEqual((body["booking"]["status"], body["can_rate"]), ("completed", True))

    def test_window_closed_after_14_days(self):
        self.visit = make_visit(self.sara.id, completed_ago=timedelta(days=15))
        body = self.client.get(URL).json()
        self.assertEqual((body["can_rate"], body["reason"]), (False, "window_closed"))

    def test_no_stylist_recorded_is_an_empty_list_and_still_counted(self):
        with mock.patch("apps.reviews.service.stylist_cards", return_value={}):
            body = self.client.get(URL).json()
        self.assertEqual(body["stylists"], [])
        self.assertEqual(body["booking"]["service_count"], 3)

    def test_after_the_review_the_form_shows_it_read_only(self):
        self.post(self.doc_example())
        body = self.client.get(URL).json()
        self.assertEqual((body["can_rate"], body["reason"]), (False, "already_reviewed"))
        self.assertEqual(body["review"]["salon"]["stars"], 4)
        self.assertEqual(body["review"]["booking_id"], str(BOOKING))


class SubmitTests(ReviewApiTestCase):
    def test_the_doc_s_example_darius_rated_marco_defaulted(self):
        first, second = self.photo(), self.photo(name="after.jpg")
        response = self.post(self.doc_example(photo_ids=[str(first.id), str(second.id)]))
        self.assertEqual(response.status_code, 201, response.content)
        body = response.json()

        review = Review.objects.get()
        self.assertEqual(body["id"], str(review.id))
        self.assertEqual(body["booking_id"], str(BOOKING))
        self.assertTrue(body["submitted_at"].endswith("+04:00"))
        self.assertEqual(
            body["salon"],
            {
                "stars": 4,
                "tags": [
                    {"code": "cleanliness", "label": "Cleanliness"},
                    {"code": "ambience", "label": "Ambience"},
                ],
            },
        )
        self.assertEqual(
            body["stylists"],
            [
                {
                    "stylist_id": str(DARIUS),
                    "name": "Darius Stone",
                    "avatar_url": "https://cdn.example.com/stylists/darius.jpg",
                    "stars": 5,
                    "tags": [
                        {"code": "skill", "label": "Skill"},
                        {"code": "attention_to_detail", "label": "Attention to detail"},
                    ],
                    "defaulted": False,
                },
                {
                    "stylist_id": str(MARCO),
                    "name": "Marco Bell",
                    "avatar_url": None,
                    "stars": 4,
                    "tags": [],
                    "defaulted": True,
                },
            ],
        )
        self.assertEqual(body["comment"], "Great fade and they started right on time.")
        self.assertEqual([photo["id"] for photo in body["photos"]], [str(first.id), str(second.id)])
        self.assertTrue(body["photos"][0]["url"].startswith("http://testserver/media/uploads/"))
        self.assertEqual(
            list(body),
            ["id", "booking_id", "submitted_at", "salon", "stylists", "comment", "photos"],
        )

    def test_storage_is_one_review_and_one_row_per_stylist(self):
        photo = self.photo()
        self.post(self.doc_example(photo_ids=[str(photo.id)]))

        review = Review.objects.get()
        self.assertEqual(
            (review.booking_id, review.customer_id, str(review.salon_id), review.branch_id, review.tenant_id),
            (BOOKING, self.sara.id, SALON, BRANCH, TENANT),
        )
        self.assertEqual((review.salon_stars, review.salon_tags), (4, ["cleanliness", "ambience"]))
        self.assertEqual(review.comment, "Great fade and they started right on time.")

        rows = {row.stylist_id: row for row in ReviewStylistRating.objects.all()}
        self.assertEqual(len(rows), 2)
        self.assertEqual(
            (rows[DARIUS].stars, rows[DARIUS].tags, rows[DARIUS].defaulted),
            (5, ["skill", "attention_to_detail"], False),
        )
        self.assertEqual((rows[MARCO].stars, rows[MARCO].tags, rows[MARCO].defaulted), (4, [], True))
        self.assertEqual({(row.branch_id, row.tenant_id) for row in rows.values()}, {(BRANCH, TENANT)})

        self.assertEqual(list(ReviewPhoto.objects.values_list("file_id", "position")), [(photo.id, 0)])

    def test_totals_count_given_and_defaulted_stars(self):
        self.post(self.doc_example())
        salon = SalonRating.objects.get(salon_id=SALON)
        self.assertEqual((salon.rating_sum, salon.review_count), (4, 1))
        self.assertEqual(
            {row.stylist_id: (row.rating_sum, row.review_count) for row in StylistRating.objects.all()},
            {DARIUS: (5, 1), MARCO: (4, 1)},
        )

    def test_totals_add_up_across_reviews(self):
        SalonRating.objects.create(salon_id=SALON, rating_sum=9, review_count=2)
        StylistRating.objects.create(stylist_id=DARIUS, rating_sum=3, review_count=1)
        self.post(self.doc_example())
        salon = SalonRating.objects.get(salon_id=SALON)
        self.assertEqual((salon.rating_sum, salon.review_count), (13, 3))
        darius = StylistRating.objects.get(stylist_id=DARIUS)
        self.assertEqual((darius.rating_sum, darius.review_count), (8, 2))

    def test_every_stylist_skipped_gets_the_salon_stars(self):
        response = self.post({"salon": {"stars": 3}})
        self.assertEqual(response.status_code, 201)
        self.assertEqual(
            [(s["stylist_id"], s["stars"], s["tags"], s["defaulted"]) for s in response.json()["stylists"]],
            [(str(DARIUS), 3, [], True), (str(MARCO), 3, [], True)],
        )
        self.assertEqual(response.json()["comment"], None)
        self.assertEqual(response.json()["photos"], [])
        self.assertEqual(response.json()["salon"], {"stars": 3, "tags": []})

    def test_no_stylists_on_the_visit_rates_the_salon_only(self):
        with mock.patch("apps.reviews.service.stylist_cards", return_value={}):
            response = self.post({"salon": {"stars": 5}})
        self.assertEqual(response.status_code, 201)
        self.assertEqual(response.json()["stylists"], [])
        self.assertEqual(ReviewStylistRating.objects.count(), 0)

    def test_the_comment_is_trimmed_and_blank_is_none(self):
        response = self.post(self.doc_example(comment="  Lovely.  "))
        self.assertEqual(response.json()["comment"], "Lovely.")
        Review.objects.all().delete()
        response = self.post(self.doc_example(comment="   "))
        self.assertIsNone(response.json()["comment"])

    def test_500_characters_is_fine(self):
        response = self.post(self.doc_example(comment="a" * 500))
        self.assertEqual(response.status_code, 201)

    def test_a_party_lane_is_saved_under_its_own_booking_id(self):
        lane = uuid.uuid4()
        self.visit = make_visit(self.sara.id, booking_id=lane)
        response = self.client.post(f"/api/v1/booking/{uuid.uuid4()}/review", {"salon": {"stars": 5}}, format="json")
        self.assertEqual(response.status_code, 201)
        self.assertEqual(response.json()["booking_id"], str(lane))
        self.assertEqual(Review.objects.get().booking_id, lane)


class ConflictTests(ReviewApiTestCase):
    def test_a_second_review_is_409_already_reviewed(self):
        self.assertEqual(self.post(self.doc_example()).status_code, 201)
        response = self.post(self.doc_example())
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json()["code"], "conflict")
        self.assertEqual(self.errors(response), [(None, "already_reviewed")])
        self.assertEqual(Review.objects.count(), 1)

    def test_a_double_tap_race_is_409_and_nothing_twice(self):
        self.post(self.doc_example())
        # The second submit passed the check before the first was saved.
        with mock.patch("apps.reviews.service.existing_review", return_value=None):
            response = self.post(self.doc_example())
        self.assertEqual(response.status_code, 409)
        self.assertEqual(self.errors(response), [(None, "already_reviewed")])
        self.assertEqual((Review.objects.count(), ReviewStylistRating.objects.count()), (1, 2))
        self.assertEqual(SalonRating.objects.get().review_count, 1)

    def test_not_completed_is_409(self):
        self.visit = make_visit(self.sara.id, status="checked_in", completed_ago=None)
        response = self.post(self.doc_example())
        self.assertEqual((response.status_code, self.errors(response)), (409, [(None, "not_completed")]))

    def test_window_closed_is_409(self):
        self.visit = make_visit(self.sara.id, completed_ago=timedelta(days=14, minutes=1))
        response = self.post(self.doc_example())
        self.assertEqual((response.status_code, self.errors(response)), (409, [(None, "window_closed")]))
        self.assertEqual(Review.objects.count(), 0)


class ValidationTests(ReviewApiTestCase):
    def assert_422(self, body, expected):
        response = self.post(body)
        self.assertEqual(response.status_code, 422, response.content)
        self.assertEqual(self.errors(response), expected)
        self.assertEqual(Review.objects.count(), 0)
        return response

    def test_salon_required_even_with_everything_else(self):
        body = self.doc_example()
        del body["salon"]
        self.assert_422(body, [("salon", "salon_required")])
        self.assert_422(self.doc_example(salon={"tags": ["wifi"]}), [("salon.stars", "salon_required")])
        self.assert_422(self.doc_example(salon={"stars": None}), [("salon.stars", "salon_required")])

    def test_an_empty_body_is_salon_required(self):
        self.assert_422({}, [("salon", "salon_required")])

    def test_invalid_stars(self):
        for stars in (0, 6, "4", 4.5, True, -1):
            self.assert_422(self.doc_example(salon={"stars": stars}), [("salon.stars", "invalid_stars")])

    def test_invalid_stylist_stars_including_missing(self):
        for item in ({"stylist_id": str(DARIUS), "stars": 9}, {"stylist_id": str(DARIUS)}):
            self.assert_422(self.doc_example(stylists=[item]), [("stylists[0].stars", "invalid_stars")])

    def test_unknown_or_repeated_tags(self):
        self.assert_422(
            self.doc_example(salon={"stars": 4, "tags": ["cleanliness", "skill", "cleanliness"]}),
            [("salon.tags[1]", "unknown_tag"), ("salon.tags[2]", "unknown_tag")],
        )
        self.assert_422(
            self.doc_example(stylists=[{"stylist_id": str(DARIUS), "stars": 5, "tags": ["wifi"]}]),
            [("stylists[0].tags[0]", "unknown_tag")],
        )

    def test_beard_work_is_unknown_at_a_ladies_salon(self):
        self.category = "ladies"
        self.assert_422(
            self.doc_example(stylists=[{"stylist_id": str(DARIUS), "stars": 5, "tags": ["beard_work"]}]),
            [("stylists[0].tags[0]", "unknown_tag")],
        )

    def test_stylist_not_on_booking(self):
        self.assert_422(
            self.doc_example(
                stylists=[
                    {"stylist_id": str(DARIUS), "stars": 5},
                    {"stylist_id": str(uuid.uuid4()), "stars": 5},
                ]
            ),
            [("stylists[1].stylist_id", "stylist_not_on_booking")],
        )
        self.assert_422(self.doc_example(stylists=[{"stars": 5}]), [("stylists[0].stylist_id", "stylist_not_on_booking")])

    def test_duplicate_stylist(self):
        self.assert_422(
            self.doc_example(
                stylists=[
                    {"stylist_id": str(MARCO), "stars": 5},
                    {"stylist_id": str(MARCO).upper(), "stars": 2},
                ]
            ),
            [("stylists[1].stylist_id", "duplicate_stylist")],
        )

    def test_comment_too_long(self):
        self.assert_422(self.doc_example(comment="a" * 501), [("comment", "comment_too_long")])

    def test_too_many_photos(self):
        photos = [str(self.photo().id) for _ in range(4)]
        self.assert_422(self.doc_example(photo_ids=photos), [("photo_ids", "too_many_photos")])

    def test_invalid_photos(self):
        mine = self.photo()
        cases = [
            ("unknown id", str(uuid.uuid4())),
            ("not an id", "f-81c2"),
            ("someone else's", str(self.photo(account=self.omar).id)),
            ("not an image", str(self.photo(file_type=FileType.PDF, name="menu.pdf").id)),
        ]
        for label, photo_id in cases:
            with self.subTest(label):
                self.assert_422(
                    self.doc_example(photo_ids=[str(mine.id), photo_id]),
                    [("photo_ids[1]", "invalid_photo")],
                )

    def test_the_same_photo_twice_is_invalid(self):
        mine = str(self.photo().id)
        self.assert_422(self.doc_example(photo_ids=[mine, mine]), [("photo_ids[1]", "invalid_photo")])

    def test_a_photo_already_on_another_review_is_invalid(self):
        used = self.photo()
        other = Review.objects.create(booking_id=uuid.uuid4(), customer=self.sara, salon_id=SALON, salon_stars=5)
        ReviewPhoto.objects.create(review=other, file=used, position=0)
        response = self.post(self.doc_example(photo_ids=[str(used.id)]))
        self.assertEqual(self.errors(response), [("photo_ids[0]", "invalid_photo")])

    def test_every_problem_at_once(self):
        response = self.post(
            {
                "salon": {"stars": 7, "tags": ["nope"]},
                "stylists": [{"stylist_id": str(DARIUS), "stars": 0}],
                "comment": "x" * 600,
            }
        )
        self.assertEqual(
            self.errors(response),
            [
                ("salon.stars", "invalid_stars"),
                ("salon.tags[0]", "unknown_tag"),
                ("stylists[0].stars", "invalid_stars"),
                ("comment", "comment_too_long"),
            ],
        )

    def test_messages_are_for_the_customer(self):
        response = self.post(self.doc_example(stylists=[{"stylist_id": str(uuid.uuid4()), "stars": 5}]))
        self.assertEqual(response.json()["errors"][0]["message"], "This stylist didn't work on this visit.")


class AllOrNothingTests(ReviewApiTestCase):
    def test_a_failure_halfway_saves_nothing(self):
        real = service._add_to_totals
        calls = []

        def fail_on_a_stylist(model, stars, **key):
            calls.append(model)
            if model is StylistRating:
                raise RuntimeError("the database went away")
            return real(model, stars, **key)

        photo = self.photo()
        with mock.patch("apps.reviews.service._add_to_totals", side_effect=fail_on_a_stylist):
            with self.assertRaises(RuntimeError):
                self.post(self.doc_example(photo_ids=[str(photo.id)]))

        self.assertEqual(calls, [SalonRating, StylistRating])
        self.assertEqual(Review.objects.count(), 0)
        self.assertEqual(ReviewStylistRating.objects.count(), 0)
        self.assertEqual(ReviewPhoto.objects.count(), 0)
        self.assertEqual(SalonRating.objects.count(), 0)


class LineOrderTests(ReviewApiTestCase):
    def test_review_stylists_follow_the_form_order(self):
        start = self.visit.start_at
        self.visit = make_visit(
            self.sara.id,
            lines=(
                Line(FADE, "Modern Fade", MARCO, 0, start),
                Line(FADE, "Modern Fade", DARIUS, 1, start + timedelta(minutes=5)),
            ),
        )
        body = self.post({"salon": {"stars": 4}}).json()
        self.assertEqual([s["stylist_id"] for s in body["stylists"]], [str(MARCO), str(DARIUS)])
