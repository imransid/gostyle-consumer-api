"""
The customer story feed on the platform's v2 table, whoCanSee, and the
engagement calls to gostyle-api (apps/salons/gostyle_api.py).

The platform's story tables are not in the test database, so the visibility
rule is checked as the SQL it compiles to, and the views run with the
selectors mocked. The live proof (real requests on a real database) is in
the PR.
"""

import json
import urllib.error
import uuid
from contextlib import ExitStack
from datetime import datetime
from types import SimpleNamespace
from unittest import mock

from django.contrib.auth.models import AnonymousUser
from django.test import SimpleTestCase, override_settings
from django.urls import resolve
from rest_framework.test import APIRequestFactory, force_authenticate

from apps.accounts.models import ConsumerAccount
from apps.salons import gostyle_api, selectors
from apps.salons.test_group_booking import Customer
from apps.salons.tests import NoCustomerSync
from apps.salons.views import (
    BookingCreateView,
    SalonProfileView,
    SalonStoriesView,
    StoryViewedView,
)

STORY = "d6db82d4-3e2b-4c0a-8c6d-bc03ede3b05e"
SALON = uuid.UUID("33333333-3333-3333-3333-333333333333")
BOOKING = "9495b6b0-3b06-48d1-b8d4-b6a7ed83eb9b"
SERVICE = "98b7f9fd-b680-4a27-b6cf-01c1f0bf6c2d"
APP_AGENT = "GoStyleApp/2.3 (iPhone; iOS 18)"
TENANT = "f2a9882b-c822-4107-b650-29af2e303c24"
PHOTO_1 = "0e5f6f39-1d1c-4b8e-9d6a-2f0f6a8b1c01"
PHOTO_2 = "0e5f6f39-1d1c-4b8e-9d6a-2f0f6a8b1c02"
ACTION = "125f8633-10a7-427d-ad3d-20794877eb50"
MUSIC = "4b1e9a77-8f0c-4c51-9a35-6d1f0a2b3c4d"
TRACK = "a7c3d2e1-5b4f-4e6a-8c9d-0f1e2d3c4b5a"
COMPOSER = "c1e0744f-3836-4e46-81e6-3ad83030708e"


# ---------------------------------------------------------------------------
# Who may see a story: one SQL rule for the ring and the card's has_story
# ---------------------------------------------------------------------------

class VisibilityRuleTests(SimpleTestCase):
    # Django writes the JSON sub-rows of salon_stories by Postgres version
    # (JSON_OBJECT on 16+, JSONB_BUILD_OBJECT before), so printing that SQL
    # asks the server its version. No rows are read.
    databases = {"default"}
    account = ConsumerAccount(id=uuid.UUID("fbfb2ae7-aea3-4d9c-a787-e2f6a2b15979"))

    def sql(self, account):
        return str(selectors.stories_visible_to(account).query)

    def test_live_and_not_expired_only(self):
        for account in (None, self.account):
            sql = self.sql(account)
            self.assertIn('"StoreFrontStory"."status" = LIVE', sql)
            self.assertIn('"StoreFrontStory"."expiresAt" > (STATEMENT_TIMESTAMP())', sql)

    def test_a_guest_sees_everyone_nearby_only(self):
        for account in (None, AnonymousUser()):
            sql = self.sql(account)
            self.assertIn('"whoCanSee" = EVERYONE_NEARBY', sql)
            self.assertNotIn("FOLLOWERS_ONLY", sql)
            self.assertNotIn("salon_follow", sql)

    def test_followers_only_needs_a_follow_row_of_this_account(self):
        sql = self.sql(self.account)
        self.assertIn('"whoCanSee" = FOLLOWERS_ONLY AND EXISTS(SELECT 1 AS "a" FROM "salon_follow"', sql)
        self.assertIn(f'U0."account_id" = {self.account.id}', sql)
        self.assertIn('U0."storefront_id" = ("StoreFrontStory"."storefrontId")', sql)

    def test_style_member_is_shown_to_no_one(self):
        for account in (None, AnonymousUser(), self.account):
            self.assertNotIn("STYLE_MEMBER", self.sql(account))

    def test_the_card_uses_the_same_rule(self):
        guest = str(selectors.discoverable_salons().query)
        signed_in = str(selectors.discoverable_salons(self.account).query)
        self.assertNotIn("storefront_story", guest)
        self.assertIn('FROM "StoreFrontStory"', guest)
        self.assertNotIn("salon_follow", guest)
        self.assertIn("salon_follow", signed_in)

    def test_the_ring_is_one_query_with_frames_button_and_services(self):
        sql = str(selectors.salon_stories(SALON, self.account).query)
        self.assertIn('ARRAY_AGG(U0."photoUrl" ORDER BY U0."position")', sql)
        self.assertIn('FROM "StoryAction"', sql)
        self.assertIn('ARRAY_AGG(U0."serviceId")', sql)
        self.assertIn("salon_follow", sql)

    def test_the_full_sub_rows_ride_in_the_same_query(self):
        sql = str(selectors.salon_stories(SALON, self.account).query)
        self.assertIn("JSONB_AGG(", sql)
        self.assertIn('ORDER BY U0."position"', sql)
        for table in ('"StoryPhoto"', '"StoryAction"', '"StoryMusic"', '"MusicTrack"', '"StoryComposer"'):
            with self.subTest(table=table):
                self.assertIn(f"FROM {table}", sql)
        # The library track is the one this music row points at.
        self.assertRegex(sql, r'FROM "MusicTrack" (\w+) WHERE \1\."id" = \(\w+\."trackId"\)')


# ---------------------------------------------------------------------------
# GET /salon/<id>/stories
# ---------------------------------------------------------------------------

def story_row(**over):
    row = dict(
        id=uuid.UUID(STORY),
        frame_urls=["https://cdn/before.jpg", "https://cdn/after.jpg"],
        caption="Skin fade, before and after",
        destination="gostyle.app/book/modern-fade",
        action_button="BOOK_SERVICE",
        service_ids=[uuid.UUID(SERVICE)],
        scheduled_at=None,
        created_at=datetime(2026, 11, 10, 0, 34, 23),
        expires_at=datetime(2026, 11, 11, 0, 34, 23),
        tenant_id=uuid.UUID(TENANT),
        storefront_id=SALON,
        layout="BOTTOM",
        treatment="DARK_BOX",
        caption_x=0.5,
        caption_y=0.8,
        count_down_timer=True,
        price_tag=False,
        service_tag=True,
        keep_after_it_expr=False,
        who_can_see="EVERYONE_NEARBY",
        format="BEFORE_AND_AFTER",
        status="LIVE",
        updated_at=datetime(2026, 11, 10, 0, 40),
        # The JSON sub-rows come back from Postgres with times as zoneless text.
        photos=[
            {"id": PHOTO_1, "photo_url": "https://cdn/before.jpg", "position": 0,
             "created_at": "2026-11-10T00:34:23.5", "updated_at": "2026-11-10T00:34:23.5"},
            {"id": PHOTO_2, "photo_url": "https://cdn/after.jpg", "position": 1,
             "created_at": "2026-11-10T00:34:24", "updated_at": "2026-11-10T00:34:24"},
        ],
        action={"id": ACTION, "action_button": "BOOK_SERVICE",
                "created_at": "2026-11-10T00:34:23", "updated_at": "2026-11-10T00:34:23"},
        music={
            "id": MUSIC, "track_id": TRACK, "music_url": "https://cdn/golden.mp3",
            "music_name": "Golden Hour", "starts_at": 3, "volume": 70,
            "fade_in_and_out": True, "show_track_name": True,
            "created_at": "2026-11-10T00:34:23", "updated_at": "2026-11-10T00:34:23",
            "track": {
                "id": TRACK, "title": "Golden Hour", "artist": "Noor Collective",
                "mood": "WARM", "bpm": 96, "duration_sec": 8, "url": "https://cdn/golden.mp3",
                "created_at": "2026-11-01T08:00:00", "updated_at": "2026-11-01T08:00:00",
            },
        },
        composer={
            "id": COMPOSER, "timing": "SCHEDULED", "schedule_date": "2026-11-12T00:00:00",
            "schedule_time": "2026-11-12T09:30:00", "stays_up_for": 24,
            "created_at": "2026-11-10T00:34:23", "updated_at": "2026-11-10T00:34:23",
        },
    )
    row.update(over)
    return SimpleNamespace(**row)


class SalonStoriesViewTests(SimpleTestCase):
    def get(self, rows, user=None):
        request = APIRequestFactory().get(f"/api/v1/salon/{SALON}/stories")
        if user is not None:
            force_authenticate(request, user=user)
        salon = SimpleNamespace(id=SALON, branch_name="Iron Razor", logo_url=None)
        with mock.patch("apps.salons.views.salon_profile", return_value=salon), \
                mock.patch("apps.salons.views.read_snapshot", return_value={}), \
                mock.patch("apps.salons.views.salon_stories", return_value=rows) as stories:
            response = SalonStoriesView.as_view()(request, salon_id=SALON)
        return response, stories

    def test_the_shape_old_keys_kept_new_ones_added(self):
        response, _ = self.get([story_row()])
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["stories"], [{
            "id": STORY,
            "media_url": "https://cdn/before.jpg",
            "frames": ["https://cdn/before.jpg", "https://cdn/after.jpg"],
            "caption": "Skin fade, before and after",
            "link_url": "gostyle.app/book/modern-fade",
            "cta": {"button": "BOOK_SERVICE", "destination": "gostyle.app/book/modern-fade"},
            "service_ids": [SERVICE],
            "publish_time": "2026-11-10T00:34:23+00:00",
            "expires_at": "2026-11-11T00:34:23+00:00",
            "tenant_id": TENANT,
            "storefront_id": str(SALON),
            "destination": "gostyle.app/book/modern-fade",
            "layout": "BOTTOM",
            "treatment": "DARK_BOX",
            "caption_x": 0.5,
            "caption_y": 0.8,
            "count_down_timer": True,
            "price_tag": False,
            "service_tag": True,
            "keep_after_it_expr": False,
            "who_can_see": "EVERYONE_NEARBY",
            "format": "BEFORE_AND_AFTER",
            "status": "LIVE",
            "scheduled_at": None,
            "created_at": "2026-11-10T00:34:23+00:00",
            "updated_at": "2026-11-10T00:40:00+00:00",
            "photos": [
                {"id": PHOTO_1, "photo_url": "https://cdn/before.jpg", "position": 0,
                 "created_at": "2026-11-10T00:34:23.500000+00:00",
                 "updated_at": "2026-11-10T00:34:23.500000+00:00"},
                {"id": PHOTO_2, "photo_url": "https://cdn/after.jpg", "position": 1,
                 "created_at": "2026-11-10T00:34:24+00:00", "updated_at": "2026-11-10T00:34:24+00:00"},
            ],
            "action": {"id": ACTION, "action_button": "BOOK_SERVICE",
                       "created_at": "2026-11-10T00:34:23+00:00",
                       "updated_at": "2026-11-10T00:34:23+00:00"},
            "music": {
                "id": MUSIC, "track_id": TRACK, "music_url": "https://cdn/golden.mp3",
                "music_name": "Golden Hour", "starts_at": 3, "volume": 70,
                "fade_in_and_out": True, "show_track_name": True,
                "created_at": "2026-11-10T00:34:23+00:00", "updated_at": "2026-11-10T00:34:23+00:00",
                "track": {
                    "id": TRACK, "title": "Golden Hour", "artist": "Noor Collective",
                    "mood": "WARM", "bpm": 96, "duration_sec": 8, "url": "https://cdn/golden.mp3",
                    "created_at": "2026-11-01T08:00:00+00:00", "updated_at": "2026-11-01T08:00:00+00:00",
                },
            },
            "composer": {
                "id": COMPOSER, "timing": "SCHEDULED",
                "schedule_date": "2026-11-12T00:00:00+00:00",
                "schedule_time": "2026-11-12T09:30:00+00:00", "stays_up_for": 24,
                "created_at": "2026-11-10T00:34:23+00:00", "updated_at": "2026-11-10T00:34:23+00:00",
            },
        }])

    def test_own_audio_has_no_library_track(self):
        music = dict(story_row().music, track_id=None, track=None, music_url="https://cdn/own.mp3")
        response, _ = self.get([story_row(music=music)])
        self.assertIsNone(response.data["stories"][0]["music"]["track"])
        self.assertEqual(response.data["stories"][0]["music"]["music_url"], "https://cdn/own.mp3")

    def test_missing_sub_rows_are_null_or_empty(self):
        response, _ = self.get([story_row(music=None, composer=None, action=None, photos=None,
                                          action_button=None)])
        story = response.data["stories"][0]
        self.assertIsNone(story["music"])
        self.assertIsNone(story["composer"])
        self.assertIsNone(story["action"])
        self.assertEqual(story["photos"], [])

    def test_a_scheduled_story_shows_its_scheduled_time(self):
        response, _ = self.get([story_row(scheduled_at=datetime(2026, 11, 10, 6, 0))])
        self.assertEqual(response.data["stories"][0]["scheduled_at"], "2026-11-10T06:00:00+00:00")

    def test_a_scheduled_story_was_published_when_it_went_up(self):
        row = story_row(scheduled_at=datetime(2026, 11, 10, 6, 0))
        response, _ = self.get([row])
        self.assertEqual(response.data["stories"][0]["publish_time"], "2026-11-10T06:00:00+00:00")

    def test_no_button_no_services(self):
        response, _ = self.get([story_row(action_button=None, service_ids=None)])
        story = response.data["stories"][0]
        self.assertIsNone(story["cta"])
        self.assertEqual(story["service_ids"], [])

    def test_a_story_with_no_frame_is_skipped(self):
        response, _ = self.get([story_row(frame_urls=None), story_row(frame_urls=[])])
        self.assertEqual(response.data["stories"], [])

    def test_the_caller_decides_what_is_visible(self):
        user = Customer()
        _, stories = self.get([], user=user)
        stories.assert_called_once_with(SALON, user)


# ---------------------------------------------------------------------------
# POST /story/<id>/view
# ---------------------------------------------------------------------------

class StoryViewedViewTests(SimpleTestCase):
    def post(self, body, user=None, **headers):
        request = APIRequestFactory().post(
            f"/api/v1/story/{STORY}/view", body, format="json", **headers
        )
        if user is not None:
            force_authenticate(request, user=user)
        with mock.patch("apps.salons.views.gostyle_api.record_view") as record:
            response = StoryViewedView.as_view()(request, story_id=uuid.UUID(STORY))
        self.assertEqual(response.status_code, 204)
        return record

    def test_the_route(self):
        self.assertIs(resolve(f"/api/v1/story/{STORY}/view").func.view_class, StoryViewedView)

    def test_signed_in_the_session_is_the_account(self):
        record = self.post({"max_frame": 1}, user=Customer(), HTTP_USER_AGENT=APP_AGENT)
        record.assert_called_once_with(
            STORY, max_frame=1, session_id=str(Customer().pk),
            ip="127.0.0.1", user_agent=APP_AGENT,
        )

    def test_a_guest_s_session_is_the_install_id(self):
        record = self.post({"max_frame": 0}, HTTP_X_DEVICE_ID="install-42")
        self.assertEqual(record.call_args.kwargs["session_id"], "install-42")

    def test_a_guest_with_no_install_id_is_skipped(self):
        self.post({"max_frame": 0}).assert_not_called()

    def test_no_usable_max_frame_is_skipped(self):
        for body in ({}, {"max_frame": "1"}, {"max_frame": True}, {"max_frame": -1},
                     {"max_frame": 1.5}, {"max_frame": None}):
            with self.subTest(body=body):
                self.post(body, user=Customer()).assert_not_called()


# ---------------------------------------------------------------------------
# GET /salon/<id> with X-Story-Id: a profile visit from the story tray
# ---------------------------------------------------------------------------

class ProfileVisitTests(SimpleTestCase):
    def get(self, user=None, **headers):
        request = APIRequestFactory().get(f"/api/v1/salon/{SALON}", **headers)
        if user is not None:
            force_authenticate(request, user=user)
        with mock.patch("apps.salons.views.salon_profile", return_value=SimpleNamespace(id=SALON)), \
                mock.patch("apps.salons.views.salon_profile_data", return_value={"id": str(SALON)}), \
                mock.patch("apps.salons.views.is_following", return_value=False), \
                mock.patch("apps.salons.views.followers_count", return_value=0), \
                mock.patch("apps.salons.views.gostyle_api.record_profile_visit") as record:
            response = SalonProfileView.as_view()(request, salon_id=SALON)
        self.assertEqual(response.status_code, 200)
        return record

    def test_from_the_story_tray(self):
        record = self.get(user=Customer(), HTTP_X_STORY_ID=STORY)
        record.assert_called_once_with(
            STORY, session_id=str(Customer().pk), ip="127.0.0.1", user_agent=None,
        )

    def test_not_from_a_story(self):
        self.get(user=Customer()).assert_not_called()
        self.get(user=Customer(), HTTP_X_STORY_ID="not-a-uuid").assert_not_called()

    def test_a_guest_without_an_install_id_is_skipped(self):
        self.get(HTTP_X_STORY_ID=STORY).assert_not_called()
        self.assertEqual(
            self.get(HTTP_X_STORY_ID=STORY, HTTP_X_DEVICE_ID="install-42").call_args.kwargs["session_id"],
            "install-42",
        )


# ---------------------------------------------------------------------------
# POST /booking with X-Story-Id: attributed after booking-api said yes
# ---------------------------------------------------------------------------

class StoryBookingTests(NoCustomerSync, SimpleTestCase):
    def create(self, *, upstream=(201, {"id": BOOKING}), services=None, record=True, **headers):
        body = json.dumps({
            "salon_id": str(SALON),
            "services": [{"id": SERVICE}] if services is None else services,
            "date": "2026-11-12",
            "start_time": "10:00",
        }).encode()
        request = APIRequestFactory().post(
            "/api/v1/booking", body, content_type="application/json", **headers
        )
        force_authenticate(request, user=Customer())
        with ExitStack() as stack:
            stack.enter_context(mock.patch("apps.salons.views.booking_route", return_value=None))
            create = stack.enter_context(
                mock.patch("apps.salons.views.create_booking", return_value=upstream)
            )
            recorded = (
                stack.enter_context(mock.patch("apps.salons.views.gostyle_api.record_booking"))
                if record else None
            )
            response = BookingCreateView.as_view()(request)
        return response, create, recorded, body

    def test_reported_with_booking_api_s_own_id(self):
        response, create, record, body = self.create(HTTP_X_STORY_ID=STORY, HTTP_USER_AGENT=APP_AGENT)
        self.assertEqual(response.status_code, 201)
        record.assert_called_once_with(
            STORY, booking_id=BOOKING, service_id=SERVICE,
            session_id=str(Customer().pk), ip="127.0.0.1", user_agent=APP_AGENT,
        )
        # booking-api refuses unknown fields: the story never touches the body.
        self.assertEqual(create.call_args.args[0], body)

    def test_not_from_a_story(self):
        _, _, record, _ = self.create()
        record.assert_not_called()

    def test_a_refused_booking_is_not_reported(self):
        refused = (422, {"detail": "The booking could not be created.", "code": "validation_error"})
        response, _, record, _ = self.create(upstream=refused, HTTP_X_STORY_ID=STORY)
        self.assertEqual(response.status_code, 422)
        record.assert_not_called()

    def test_service_id_only_when_one_service_was_booked(self):
        for services in ([], [{"id": SERVICE}, {"id": str(uuid.uuid4())}], [{"id": "nope"}]):
            with self.subTest(services=services):
                _, _, record, _ = self.create(services=services, HTTP_X_STORY_ID=STORY)
                self.assertIsNone(record.call_args.kwargs["service_id"])

    def test_gostyle_api_down_never_touches_the_booking(self):
        failures = (
            urllib.error.URLError(ConnectionRefusedError("refused")),
            TimeoutError("timed out"),
            urllib.error.HTTPError("http://gostyle_api", 500, "boom", {}, None),
        )
        for failure in failures:
            with self.subTest(failure=type(failure).__name__), \
                    mock.patch("apps.salons.gostyle_api.urllib.request.urlopen", side_effect=failure), \
                    self.assertLogs("apps.salons.gostyle_api", "WARNING"):
                response, _, _, _ = self.create(record=False, HTTP_X_STORY_ID=STORY)
            self.assertEqual(response.status_code, 201)
            self.assertEqual(response.data, {"id": BOOKING})


# ---------------------------------------------------------------------------
# gostyle_api: what is sent, and that nothing ever raises
# ---------------------------------------------------------------------------

class Answer:
    def __init__(self, status=204):
        self.status = status

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


@override_settings(GOSTYLE_API_URL="http://gostyle_api:3849/", GOSTYLE_API_TIMEOUT=2)
class GostyleApiTests(SimpleTestCase):
    def sent(self, call, **kwargs):
        with mock.patch("apps.salons.gostyle_api.urllib.request.urlopen", return_value=Answer()) as urlopen:
            call(STORY, **kwargs)
        request = urlopen.call_args.args[0]
        self.assertEqual(urlopen.call_args.kwargs, {"timeout": 2})
        return request

    def test_view(self):
        request = self.sent(gostyle_api.record_view, session_id="s1", max_frame=1,
                            ip="203.0.113.7", user_agent=APP_AGENT)
        self.assertEqual(request.full_url, f"http://gostyle_api:3849/v2/storefront/stories/{STORY}/view")
        self.assertEqual(request.get_method(), "POST")
        self.assertEqual(json.loads(request.data), {"sessionId": "s1", "maxFrame": 1})
        self.assertEqual(request.get_header("X-forwarded-for"), "203.0.113.7")
        self.assertEqual(request.get_header("User-agent"), APP_AGENT)
        self.assertIsNone(request.get_header("Authorization"))

    def test_booking(self):
        request = self.sent(gostyle_api.record_booking, session_id="s1", booking_id=BOOKING,
                            service_id=None)
        self.assertTrue(request.full_url.endswith(f"/stories/{STORY}/booking"))
        self.assertEqual(json.loads(request.data),
                         {"bookingId": BOOKING, "serviceId": None, "sessionId": "s1"})
        self.assertIsNone(request.get_header("X-forwarded-for"))

    def test_profile_visit(self):
        request = self.sent(gostyle_api.record_profile_visit, session_id="s1")
        self.assertTrue(request.full_url.endswith(f"/stories/{STORY}/profile-visit"))
        self.assertEqual(json.loads(request.data), {"sessionId": "s1"})

    def test_never_raises(self):
        failures = (
            urllib.error.URLError(ConnectionRefusedError("refused")),
            TimeoutError("timed out"),
            ConnectionResetError("reset"),
            urllib.error.HTTPError("http://gostyle_api", 404, "Story not found", {}, None),
        )
        for failure in failures:
            with self.subTest(failure=type(failure).__name__), \
                    mock.patch("apps.salons.gostyle_api.urllib.request.urlopen", side_effect=failure), \
                    self.assertLogs("apps.salons.gostyle_api", "WARNING") as logs:
                gostyle_api.record_view(STORY, session_id="s1", max_frame=0)
            self.assertIn("view not recorded", logs.output[0])

    def test_a_refusal_is_logged_not_raised(self):
        with mock.patch("apps.salons.gostyle_api.urllib.request.urlopen", return_value=Answer(429)), \
                self.assertLogs("apps.salons.gostyle_api", "WARNING") as logs:
            gostyle_api.record_profile_visit(STORY, session_id="s1")
        self.assertIn("gostyle-api answered 429", logs.output[0])
