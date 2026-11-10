"""The small rules on their own: tags, the window, the stylist list, the rating."""

import types
import uuid
from datetime import timedelta
from unittest import mock

from django.test import SimpleTestCase, override_settings
from django.utils import timezone

from apps.reviews import tags, visits
from apps.reviews.ratings import rating_of, stylist_ratings
from apps.reviews.service import visit_stylists
from apps.reviews.visits import Line

from .fixtures import BEARD, DARIUS, FADE, GREY, MARCO, NOBODY, STYLIST_CARDS, make_visit

ME = uuid.uuid4()


class TagTests(SimpleTestCase):
    def test_the_lists_are_the_doc_s_word_for_word(self):
        both = tags.tags_for("gents")
        self.assertEqual(
            both["salon"],
            [
                {"code": "cleanliness", "label": "Cleanliness"},
                {"code": "ambience", "label": "Ambience"},
                {"code": "music_vibe", "label": "Music vibe"},
                {"code": "comfortable_seats", "label": "Comfortable seats"},
                {"code": "premium_feel", "label": "Premium feel"},
                {"code": "fair_pricing", "label": "Fair pricing"},
                {"code": "parking", "label": "Parking"},
                {"code": "wifi", "label": "Wi-Fi"},
            ],
        )
        self.assertEqual(
            both["stylist"],
            [
                {"code": "skill", "label": "Skill"},
                {"code": "attention_to_detail", "label": "Attention to detail"},
                {"code": "speed", "label": "Speed"},
                {"code": "professionalism", "label": "Professionalism"},
                {"code": "friendliness", "label": "Friendliness"},
                {"code": "listened", "label": "Listened to me"},
                {"code": "style_advice", "label": "Style advice"},
                {"code": "beard_work", "label": "Beard work"},
            ],
        )

    def test_a_ladies_salon_has_no_beard_work(self):
        stylist = [tag["code"] for tag in tags.tags_for("ladies")["stylist"]]
        self.assertNotIn("beard_work", stylist)
        self.assertEqual(len(stylist), 7)
        # The salon list is the same for every salon type.
        self.assertEqual(tags.tags_for("ladies")["salon"], tags.tags_for(None)["salon"])

    def test_unisex_and_unknown_keep_the_full_list(self):
        for category in ("unisex", None):
            self.assertIn("beard_work", [tag["code"] for tag in tags.tags_for(category)["stylist"]])

    def test_saved_codes_get_today_s_label(self):
        self.assertEqual(
            tags.with_labels(["wifi", "cleanliness"], tags.SALON_LABELS),
            [{"code": "wifi", "label": "Wi-Fi"}, {"code": "cleanliness", "label": "Cleanliness"}],
        )


class RateStateTests(SimpleTestCase):
    def state(self, status="completed", ago=timedelta(days=1), reviewed=False):
        now = timezone.now()
        return visits.rate_state(status=status, finished_at=now - ago, reviewed=reviewed, now=now)

    def test_completed_unrated_inside_the_window(self):
        self.assertEqual(self.state(), (True, None))
        self.assertEqual(self.state(status="settled"), (True, None))

    def test_not_completed(self):
        for status in ("confirmed", "checked_in", "in_service", "cancelled", "no_show", "pending_payment"):
            self.assertEqual(self.state(status=status), (False, "not_completed"))

    def test_already_reviewed_comes_first(self):
        self.assertEqual(self.state(reviewed=True), (False, "already_reviewed"))
        self.assertEqual(self.state(reviewed=True, ago=timedelta(days=30)), (False, "already_reviewed"))

    def test_the_window_is_14_days(self):
        self.assertEqual(self.state(ago=timedelta(days=13, hours=23)), (True, None))
        self.assertEqual(self.state(ago=timedelta(days=14)), (False, "window_closed"))


class VisitTests(SimpleTestCase):
    def test_app_status_is_the_app_s_word_in_lower_case(self):
        cases = {
            "completed": "completed",
            "settled": "completed",
            "confirmed": "confirmed_by_salon",
            "in_service": "checked_in",
            "no_show": "cancelled",
            "pending_payment": "booked",
        }
        for status, word in cases.items():
            self.assertEqual(make_visit(ME, status=status).app_status, word)

    def test_no_completion_time_falls_back_to_the_start(self):
        visit = make_visit(ME, completed_ago=None)
        self.assertEqual(visit.finished_at, visit.start_at)

    def test_find_visit_refuses_a_bad_id_without_a_read(self):
        # No booking database is set up in tests: a read would raise.
        self.assertIsNone(visits.find_visit("not-a-uuid", ME))
        self.assertEqual(visits.lanes_for(["not-a-uuid", None], ME), {})

    def test_no_booking_database_is_unavailable(self):
        no_booking_db = types.SimpleNamespace(DATABASES={"default": {}})
        with mock.patch("apps.reviews.visits.settings", no_booking_db):
            with self.assertRaises(visits.BookingDataUnavailable):
                visits.find_visit(str(uuid.uuid4()), ME)
            with self.assertRaises(visits.BookingDataUnavailable):
                visits.lanes_for([str(uuid.uuid4())], ME)


class VisitStylistsTests(SimpleTestCase):
    def test_one_per_stylist_with_their_services_in_order(self):
        self.assertEqual(
            visit_stylists(make_visit(ME), STYLIST_CARDS),
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
                        {"id": BEARD, "name": "Beard trim"},
                        {"id": GREY, "name": "Grey blending"},
                    ],
                },
            ],
        )

    def test_ordered_by_their_first_service_s_start_not_by_line(self):
        visit = make_visit(ME)
        start = visit.start_at
        lines = (
            Line(FADE, "Modern Fade", DARIUS, 0, start + timedelta(minutes=40)),
            Line(BEARD, "Beard trim", MARCO, 1, start),
        )
        stylists = visit_stylists(make_visit(ME, lines=lines), STYLIST_CARDS)
        self.assertEqual([s["id"] for s in stylists], [str(MARCO), str(DARIUS)])

    def test_no_reservation_falls_back_to_line_order(self):
        lines = (
            Line(FADE, "Modern Fade", MARCO, 0, None),
            Line(BEARD, "Beard trim", DARIUS, 1, None),
        )
        stylists = visit_stylists(make_visit(ME, lines=lines), STYLIST_CARDS)
        self.assertEqual([s["id"] for s in stylists], [str(MARCO), str(DARIUS)])

    def test_a_line_with_no_known_stylist_is_left_out(self):
        visit = make_visit(ME)
        lines = visit.lines + (Line(GREY, "Grey blending", NOBODY, 3, visit.start_at),)
        stylists = visit_stylists(make_visit(ME, lines=lines), STYLIST_CARDS)
        self.assertEqual([s["id"] for s in stylists], [str(DARIUS), str(MARCO)])

    def test_nobody_known_is_an_empty_list(self):
        self.assertEqual(visit_stylists(make_visit(ME), {}), [])


class RatingTests(SimpleTestCase):
    def test_one_decimal_rounded_half_up(self):
        self.assertEqual(rating_of(17, 4), 4.3)
        self.assertEqual(rating_of(9, 2), 4.5)
        self.assertEqual(rating_of(14, 3), 4.7)
        self.assertEqual(rating_of(5, 1), 5.0)

    def test_no_reviews_is_none(self):
        self.assertIsNone(rating_of(0, 0))

    @override_settings(RATE_VISIT_V1=False)
    def test_off_nobody_has_a_rating_and_nothing_is_read(self):
        self.assertEqual(stylist_ratings([DARIUS]), {})
