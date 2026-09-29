"""
The routine contract on the OLD booking routes, step C6
(docs/ROUTINE_FE_CONTRACT_AUDIT.md), all behind ROUTINE_CONTRACT_V1:

    GET   /api/v1/booking/<id>              a routine's id reads the routine
    PATCH /api/v1/booking/<id>              a routine's id: 409 already_paid
    GET   /api/v1/bookings?filter=recurring each row the routine as one booking

With the switch off, every answer here is exactly what it was. No database,
no network.
"""

import copy
import urllib.parse
import uuid
from contextlib import ExitStack
from unittest import mock

from django.test import SimpleTestCase, override_settings
from rest_framework.test import APIRequestFactory, force_authenticate

from apps.salons import routine_contract as rc
from apps.salons.booking_api import BookingApiUnavailable
from apps.salons.test_group_booking import ANYA, BRANCH, MAYA, SALON, SALON_ID, STYLISTS, Customer
from apps.salons.test_routine_contract import DUBAI, ROUTINE_ID, engine_routine
from apps.salons.test_series_booking import CARD
from apps.salons.views import BookingDetailView, BookingListView, _salon_card

BOOKING = "c4832b75-0ed1-4c70-a005-14a200f6638c"

# booking-api's 404 for an id that is no booking, in its own words.
NO_BOOKING = {
    "detail": "Please correct the highlighted fields.", "code": "not_found",
    "errors": [{"field": "id", "code": "not_found", "message": "No such booking."}],
}
NO_PARTY = {
    "detail": "Please correct the highlighted fields.", "code": "not_found",
    "errors": [{"field": "id", "code": "not_found", "message": "No such party."}],
}
A_BOOKING = {
    "id": BOOKING, "salon_id": BRANCH, "status": "CONFIRMED_BY_SALON",
    "start_time": "2026-10-20T18:30:00+04:00", "end_time": "2026-10-20T19:15:00+04:00",
}
AVATARS = {MAYA: None, ANYA: "https://cdn/anya.png"}


def the_routine(full=True):
    """engine_routine() as the app should read it: salon clock, avatars, card."""
    out = rc.present_routine(engine_routine(), salon_id=SALON_ID, tz=DUBAI, avatars=AVATARS)
    out["salon"] = _salon_card(CARD, full=full)
    return out


class Seams:
    def fakes(self, **over):
        fakes = {
            "apps.salons.views.read_booking": mock.Mock(return_value=(404, copy.deepcopy(NO_BOOKING))),
            "apps.salons.views.patch_booking": mock.Mock(return_value=(404, copy.deepcopy(NO_BOOKING))),
            "apps.salons.views.salon_cards_for_refs": mock.Mock(return_value={BRANCH: CARD}),
            "apps.salons.group_views.read_group_booking": mock.Mock(return_value=(404, copy.deepcopy(NO_PARTY))),
            "apps.salons.routine_views.read_routine_booking": mock.Mock(return_value=(200, engine_routine())),
            "apps.salons.routine_views.salon_cards_for_refs": mock.Mock(return_value={BRANCH: CARD}),
            "apps.salons.group_views.salon_profile": mock.Mock(return_value=SALON),
            "apps.salons.group_views.salon_stylists": mock.Mock(return_value=STYLISTS),
        }
        fakes.update({k if "." in k else f"apps.salons.routine_views.{k}": v for k, v in over.items()})
        return fakes

    def call_view(self, view, request, fakes, **kwargs):
        with ExitStack() as stack:
            for target, fake in fakes.items():
                stack.enter_context(mock.patch(target, fake))
            return view.as_view()(request, **kwargs)

    def get(self, booking_id=ROUTINE_ID, **over):
        fakes = self.fakes(**over)
        request = APIRequestFactory().get(f"/api/v1/booking/{booking_id}",
                                          HTTP_AUTHORIZATION="Bearer customer-token")
        force_authenticate(request, user=Customer())
        return self.call_view(BookingDetailView, request, fakes, booking_id=uuid.UUID(booking_id)), fakes

    def patch(self, booking_id=ROUTINE_ID, **over):
        fakes = self.fakes(**over)
        request = APIRequestFactory().patch(
            f"/api/v1/booking/{booking_id}", data=b'{"payment_status":"FULLY_PAID"}',
            content_type="application/json", HTTP_AUTHORIZATION="Bearer customer-token",
        )
        force_authenticate(request, user=Customer())
        return self.call_view(BookingDetailView, request, fakes, booking_id=uuid.UUID(booking_id)), fakes

    def routine_calls(self, fakes):
        return fakes["apps.salons.routine_views.read_routine_booking"].call_count


# ------------------------------------------------------------ GET /booking/<id>


@override_settings(ROUTINE_CONTRACT_V1=False)
class ReadFlagOffTests(Seams, SimpleTestCase):
    def test_off_a_routine_id_is_the_404_it_always_was(self):
        for group in (False, True):
            with self.subTest(group=group), override_settings(GROUP_BOOKING_V2=group):
                response, fakes = self.get()
                self.assertEqual(response.status_code, 404)
                self.assertEqual(response.data, NO_PARTY if group else NO_BOOKING)
                self.assertEqual(self.routine_calls(fakes), 0)

    def test_off_a_booking_reads_as_it_always_did(self):
        response, fakes = self.get(BOOKING, **{"apps.salons.views.read_booking": mock.Mock(
            return_value=(200, dict(A_BOOKING)))})
        self.assertEqual(response.data, {**A_BOOKING, "salon": _salon_card(CARD, full=True)})
        self.assertEqual(self.routine_calls(fakes), 0)


@override_settings(ROUTINE_CONTRACT_V1=True)
class ReadTests(Seams, SimpleTestCase):
    def test_a_routine_id_reads_the_routine_as_the_create_presents_it(self):
        for group in (False, True):
            with self.subTest(group=group), override_settings(GROUP_BOOKING_V2=group):
                response, fakes = self.get()
                self.assertEqual(response.status_code, 200, response.data)
                self.assertEqual(response.data, the_routine())
                read = fakes["apps.salons.routine_views.read_routine_booking"]
                read.assert_called_once_with(ROUTINE_ID, authorization="Bearer customer-token")
                # The party was asked first, only when parties are on.
                self.assertEqual(
                    fakes["apps.salons.group_views.read_group_booking"].call_count, 1 if group else 0,
                )

    def test_a_real_booking_answers_as_today_and_nothing_else_is_asked(self):
        with override_settings(GROUP_BOOKING_V2=True):
            response, fakes = self.get(BOOKING, **{"apps.salons.views.read_booking": mock.Mock(
                return_value=(200, dict(A_BOOKING)))})
        self.assertEqual(response.data, {**A_BOOKING, "salon": _salon_card(CARD, full=True)})
        self.assertEqual(self.routine_calls(fakes), 0)
        fakes["apps.salons.group_views.read_group_booking"].assert_not_called()

    def test_a_party_answers_as_today_and_the_routine_is_not_asked(self):
        party = {"id": BOOKING, "booking_type": "GROUP", "salon_id": BRANCH, "members": []}
        with override_settings(GROUP_BOOKING_V2=True), \
                mock.patch("apps.salons.group_views._stored_party", side_effect=lambda b: b):
            response, fakes = self.get(BOOKING, **{
                "apps.salons.group_views.read_group_booking": mock.Mock(return_value=(200, party))})
        self.assertEqual((response.status_code, response.data), (200, party))
        self.assertEqual(self.routine_calls(fakes), 0)

    def test_someone_elses_routine_is_the_same_404_unchanged(self):
        not_yours = {"detail": "x", "code": "not_found",
                     "errors": [{"field": "id", "code": "not_found", "message": "No such booking."}]}
        for group in (False, True):
            with self.subTest(group=group), override_settings(GROUP_BOOKING_V2=group):
                response, _ = self.get(read_routine_booking=mock.Mock(return_value=(404, not_yours)))
                self.assertEqual(response.status_code, 404)
                self.assertEqual(response.data, NO_PARTY if group else NO_BOOKING)

    def test_booking_api_down_during_the_routine_read_is_503(self):
        response, _ = self.get(read_routine_booking=mock.Mock(side_effect=BookingApiUnavailable("x")))
        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.data["errors"][0]["code"], "booking_api_unavailable")

    def test_a_routine_whose_salon_cannot_be_found_is_still_read(self):
        response, _ = self.get(salon_cards_for_refs=mock.Mock(return_value={}))
        self.assertEqual(response.status_code, 200)
        self.assertIsNone(response.data["salon"])
        self.assertEqual(response.data["cadence"], "week")


# ------------------------------------------------------------ PATCH /booking/<id>


class PaymentTests(Seams, SimpleTestCase):
    @override_settings(ROUTINE_CONTRACT_V1=False)
    def test_off_a_routine_id_is_the_404_it_always_was(self):
        response, fakes = self.patch()
        self.assertEqual((response.status_code, response.data), (404, NO_BOOKING))
        self.assertEqual(self.routine_calls(fakes), 0)

    @override_settings(ROUTINE_CONTRACT_V1=True)
    def test_a_routine_is_paid_at_the_salon_409_already_paid(self):
        response, _ = self.patch()
        self.assertEqual(response.status_code, 409)
        message = "This routine is paid at the salon, visit by visit. There is nothing to record here."
        self.assertEqual(response.data, {
            "detail": message, "code": "already_paid",
            "errors": [{"field": None, "code": "already_paid", "message": message}],
        })

    @override_settings(ROUTINE_CONTRACT_V1=True)
    def test_a_real_booking_answers_as_today_and_nothing_else_is_asked(self):
        for answer in ((200, dict(A_BOOKING)), (409, {"code": "already_paid"}),
                       (422, {"code": "validation_error", "errors": []})):
            with self.subTest(status=answer[0]):
                response, fakes = self.patch(BOOKING, **{
                    "apps.salons.views.patch_booking": mock.Mock(return_value=answer)})
                self.assertEqual((response.status_code, response.data), answer)
                self.assertEqual(self.routine_calls(fakes), 0)

    @override_settings(ROUTINE_CONTRACT_V1=True)
    def test_an_id_that_is_neither_is_the_same_404_unchanged(self):
        response, _ = self.patch(read_routine_booking=mock.Mock(return_value=(404, {"code": "not_found"})))
        self.assertEqual((response.status_code, response.data), (404, NO_BOOKING))

    @override_settings(ROUTINE_CONTRACT_V1=True)
    def test_booking_api_down_during_the_routine_read_is_503(self):
        response, _ = self.patch(read_routine_booking=mock.Mock(side_effect=BookingApiUnavailable("x")))
        self.assertEqual(response.status_code, 503)


# ------------------------------------------------------------ GET /bookings


class ListTests(Seams, SimpleTestCase):
    def page(self, rows):
        return {
            "count": len(rows), "page": 1, "page_size": 20,
            "counts": {"upcoming": 3, "recurring": len(rows), "archive": 1},
            "results": rows,
        }

    def list(self, shelf, rows, **over):
        fakes = self.fakes(**{"apps.salons.views.list_bookings": mock.Mock(
            return_value=(200, copy.deepcopy(self.page(rows))))}, **over)
        request = APIRequestFactory().get("/api/v1/bookings", {"filter": shelf},
                                          HTTP_AUTHORIZATION="Bearer customer-token")
        force_authenticate(request, user=Customer())
        return self.call_view(BookingListView, request, fakes), fakes

    def query(self, fakes):
        (query,), _ = fakes["apps.salons.views.list_bookings"].call_args
        return dict(urllib.parse.parse_qsl(query))

    @override_settings(ROUTINE_CONTRACT_V1=False)
    def test_off_the_recurring_rows_are_asked_and_shown_as_today(self):
        from apps.salons.test_series_booking import ROUTINE_ROW
        response, fakes = self.list("recurring", [{**ROUTINE_ROW, "salon_id": BRANCH}])
        self.assertEqual(self.query(fakes), {"page": "1", "pageSize": "20", "filter": "recurring"})
        row = response.data["results"][0]
        self.assertEqual(row["time"], "11:00")          # the old row, on the salon's clock
        self.assertNotIn("cadence", row)
        fakes["apps.salons.group_views.salon_stylists"].assert_not_called()

    @override_settings(ROUTINE_CONTRACT_V1=True)
    def test_on_each_row_is_the_routine_as_one_booking_with_the_short_card(self):
        second = {**engine_routine(), "id": "2b000000-0000-4000-8000-000000000002"}
        response, fakes = self.list("recurring", [engine_routine(), second])
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.query(fakes)["view"], "booking")
        first_row = response.data["results"][0]
        self.assertEqual(first_row, the_routine(full=False))
        self.assertEqual(set(first_row["salon"]), {"id", "name", "logo_url", "city"})
        self.assertEqual(first_row["stylists"][1]["avatar_url"], "https://cdn/anya.png")
        # Paging and counts as today.
        self.assertEqual((response.data["count"], response.data["counts"]),
                         (2, {"upcoming": 3, "recurring": 2, "archive": 1}))
        # One salon, read once for the page.
        self.assertEqual(fakes["apps.salons.group_views.salon_profile"].call_count, 1)

    def test_upcoming_and_archive_do_not_change(self):
        visit = {"id": BOOKING, "salon_id": BRANCH, "status": "CONFIRMED_BY_SALON",
                 "booking_type": "ROUTINE", "series_id": ROUTINE_ID,
                 "start_time": "2027-10-20T18:30:00+04:00"}
        for shelf in ("upcoming", "archive"):
            with self.subTest(shelf=shelf):
                with override_settings(ROUTINE_CONTRACT_V1=False):
                    off, off_fakes = self.list(shelf, [dict(visit)])
                with override_settings(ROUTINE_CONTRACT_V1=True):
                    on, on_fakes = self.list(shelf, [dict(visit)])
                self.assertEqual(on.data, off.data)
                self.assertEqual(self.query(on_fakes), self.query(off_fakes))
                self.assertNotIn("view", self.query(on_fakes))
