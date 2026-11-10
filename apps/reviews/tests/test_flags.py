"""
`is_rated` and `can_rate` on GET /bookings and GET /booking/:id
(docs/rate-your-visit-fe-contract.md, section 3).

booking-api's database is faked at visits.lanes_for / party_lanes /
completion_times; the review table is real.
"""

import contextlib
import uuid
from datetime import timedelta
from unittest import mock

from django.test import TestCase, override_settings
from django.utils import timezone
from rest_framework.response import Response
from rest_framework.test import APIRequestFactory, force_authenticate

from apps.accounts.models import ConsumerAccount
from apps.reviews import flags
from apps.reviews.models import Review
from apps.reviews.visits import BookingDataUnavailable
from apps.salons.views import BookingDetailView, BookingListView

from .fixtures import SALON

ME = uuid.uuid4()
SOMEONE = uuid.uuid4()


def lane(customer_id=ME, status="completed", started_ago=timedelta(hours=3), booking_id=None):
    return {
        "id": booking_id or uuid.uuid4(),
        "group_id": None,
        "customer_id": customer_id,
        "status": status,
        "start_at": timezone.now() - started_ago,
    }


def reviewed(booking_id):
    account = ConsumerAccount.objects.create(phone=f"+9715{uuid.uuid4().int % 10**8:08d}")
    Review.objects.create(booking_id=booking_id, customer=account, salon_id=SALON, salon_stars=5)


class FakeBookingData:
    """The booking database as a few dicts."""

    def __init__(self, lanes=None, members=None, completed=None):
        self.lanes = lanes or {}
        self.members = members or {}
        self.completed = completed or {}

    def patches(self):
        return [
            mock.patch("apps.reviews.visits.lanes_for", side_effect=lambda refs, customer: {
                ref: self.lanes[ref] for ref in refs if ref in self.lanes
            }),
            mock.patch("apps.reviews.visits.party_lanes", side_effect=lambda ref: self.members.get(ref, {})),
            mock.patch("apps.reviews.visits.completion_times", side_effect=lambda ids: {
                i: self.completed[i] for i in ids if i in self.completed
            }),
        ]

    @contextlib.contextmanager
    def active(self):
        with contextlib.ExitStack() as stack:
            for patcher in self.patches():
                stack.enter_context(patcher)
            yield


class RowFlagTests(TestCase):
    def flags_for(self, lanes, rows, completed=None):
        with FakeBookingData(lanes=lanes, completed=completed).active():
            flags.add_to_rows(ME, rows)
        return [(row["is_rated"], row["can_rate"]) for row in rows]

    def test_the_four_cases_of_the_doc_s_table(self):
        fresh, rated, old, upcoming = lane(), lane(), lane(started_ago=timedelta(days=20)), lane(status="confirmed")
        reviewed(rated["id"])
        rows = [{"id": str(x["id"])} for x in (fresh, rated, old, upcoming)]
        lanes = {str(x["id"]): x for x in (fresh, rated, old, upcoming)}
        self.assertEqual(
            self.flags_for(lanes, rows),
            [(False, True), (True, False), (False, False), (False, False)],
        )

    def test_the_window_counts_from_completion_not_the_start(self):
        late = lane(started_ago=timedelta(days=20))
        rows = [{"id": str(late["id"])}]
        completed = {late["id"]: timezone.now() - timedelta(days=2)}
        self.assertEqual(self.flags_for({rows[0]["id"]: late}, rows, completed), [(False, True)])

    def test_a_routine_or_unknown_id_is_false_and_false(self):
        rows = [{"id": str(uuid.uuid4())}, {"id": "not-a-uuid"}, {}]
        self.assertEqual(self.flags_for({}, rows), [(False, False)] * 3)

    def test_someone_else_s_lane_is_never_can_rate(self):
        theirs = lane(customer_id=SOMEONE)
        rows = [{"id": str(theirs["id"])}]
        self.assertEqual(self.flags_for({rows[0]["id"]: theirs}, rows), [(False, False)])

    def test_without_booking_data_a_review_still_shows_and_nothing_offers_rate(self):
        rated, other = uuid.uuid4(), uuid.uuid4()
        reviewed(rated)
        rows = [{"id": str(rated)}, {"id": str(other)}]
        with mock.patch("apps.reviews.visits.lanes_for", side_effect=BookingDataUnavailable("down")):
            flags.add_to_rows(ME, rows)
        self.assertEqual([(r["is_rated"], r["can_rate"]) for r in rows], [(True, False), (False, False)])


class PartyFlagTests(TestCase):
    def test_the_party_is_the_caller_s_lane_and_each_member_their_own(self):
        party = str(uuid.uuid4())
        mine = lane()
        friend = lane(customer_id=SOMEONE)
        reviewed(friend["id"])
        booking = {
            "id": party,
            "booking_type": "GROUP",
            "members": [{"id": "p-me"}, {"id": "p-friend"}, {"id": "p-guest"}],
        }
        data = FakeBookingData(lanes={party: mine}, members={party: {"p-me": mine, "p-friend": friend}})
        with data.active():
            flags.add_to_booking(ME, booking)

        self.assertEqual((booking["is_rated"], booking["can_rate"]), (False, True))
        self.assertEqual(
            [(m["is_rated"], m["can_rate"]) for m in booking["members"]],
            # Mine to rate; the friend's is rated (by them); the guest has no lane here.
            [(False, True), (True, False), (False, False)],
        )

    def test_a_single_booking_has_no_members_to_read(self):
        single = lane()
        booking = {"id": str(single["id"]), "booking_type": "SINGLE"}
        data = FakeBookingData(lanes={booking["id"]: single})
        with data.active():
            flags.add_to_booking(ME, booking)
        self.assertEqual((booking["is_rated"], booking["can_rate"]), (False, True))


TOKEN = "Bearer test"


class Customer:
    is_authenticated = True
    id = ME


class BookingRoutesTests(TestCase):
    """The two booking routes, with booking-api's HTTP answers faked."""

    def call(self, view, path, kwargs, upstream_patch, upstream):
        request = APIRequestFactory().get(path, HTTP_AUTHORIZATION=TOKEN)
        force_authenticate(request, user=Customer())
        with mock.patch(upstream_patch, return_value=upstream), mock.patch(
            "apps.salons.views.salon_cards_for_refs", return_value={}
        ):
            return view.as_view()(request, **kwargs)

    def list_page(self, rows):
        return self.call(
            BookingListView, "/api/v1/bookings?filter=archive", {},
            "apps.salons.views.list_bookings", (200, {"count": len(rows), "results": rows}),
        )

    def read(self, booking_id, body):
        return self.call(
            BookingDetailView, f"/api/v1/booking/{booking_id}", {"booking_id": booking_id},
            "apps.salons.views.read_booking", (200, body),
        )

    @override_settings(RATE_VISIT_V1=False)
    def test_off_no_new_keys(self):
        done = lane()
        with FakeBookingData(lanes={str(done["id"]): done}).active():
            row = self.list_page([{"id": str(done["id"]), "status": "COMPLETED"}]).data["results"][0]
            read = self.read(done["id"], {"id": str(done["id"]), "status": "COMPLETED"}).data
        for body in (row, read):
            self.assertNotIn("is_rated", body)
            self.assertNotIn("can_rate", body)

    @override_settings(RATE_VISIT_V1=True)
    def test_on_every_row_and_the_read_carry_both(self):
        done, rated = lane(), lane()
        reviewed(rated["id"])
        lanes = {str(done["id"]): done, str(rated["id"]): rated}
        rows = [
            {"id": str(done["id"]), "status": "COMPLETED", "booking_type": "SINGLE"},
            {"id": str(rated["id"]), "status": "COMPLETED", "booking_type": "SINGLE"},
            {"id": str(uuid.uuid4()), "status": "CONFIRMED_BY_SALON", "booking_type": "SINGLE"},
        ]
        with FakeBookingData(lanes=lanes).active():
            results = self.list_page(rows).data["results"]
            read = self.read(done["id"], {"id": str(done["id"]), "status": "COMPLETED"}).data
        self.assertEqual(
            [(r["is_rated"], r["can_rate"]) for r in results],
            [(False, True), (True, False), (False, False)],
        )
        self.assertEqual((read["is_rated"], read["can_rate"]), (False, True))

    @override_settings(RATE_VISIT_V1=True, GROUP_BOOKING_V2=True, ROUTINE_CONTRACT_V1=False)
    def test_a_party_read_gets_them_on_the_party_and_its_members(self):
        party = str(uuid.uuid4())
        mine = lane()
        answer = Response({"id": party, "booking_type": "GROUP", "members": [{"id": "p-me"}]})
        request = APIRequestFactory().get(f"/api/v1/booking/{party}", HTTP_AUTHORIZATION=TOKEN)
        force_authenticate(request, user=Customer())
        data = FakeBookingData(lanes={party: mine}, members={party: {"p-me": mine}})
        with data.active(), mock.patch("apps.salons.views.read_booking", return_value=(404, {})), mock.patch(
            "apps.salons.group_views.read_group_response", return_value=answer
        ):
            body = BookingDetailView.as_view()(request, booking_id=uuid.UUID(party)).data
        self.assertEqual((body["is_rated"], body["can_rate"]), (False, True))
        self.assertEqual((body["members"][0]["is_rated"], body["members"][0]["can_rate"]), (False, True))

    @override_settings(RATE_VISIT_V1=True)
    def test_an_error_answer_is_left_alone(self):
        missing = uuid.uuid4()
        with FakeBookingData().active(), mock.patch(
            "apps.salons.views.read_booking", return_value=(404, {"detail": "nope"})
        ), override_settings(GROUP_BOOKING_V2=False, ROUTINE_CONTRACT_V1=False):
            request = APIRequestFactory().get(f"/api/v1/booking/{missing}", HTTP_AUTHORIZATION=TOKEN)
            force_authenticate(request, user=Customer())
            response = BookingDetailView.as_view()(request, booking_id=missing)
        self.assertEqual((response.status_code, response.data), (404, {"detail": "nope"}))
