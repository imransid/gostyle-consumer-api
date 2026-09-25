"""
Group bookings through booking-api's mobile route (GROUP_BOOKING_V2).

One call to POST /v1/mobile-booking/group instead of hold then confirm; the
party is read back by its group id through GET /booking/<id>, and cancelled
together through POST /booking/<id>/cancel. Everything here is driven through
the same seams as test_group_booking.py: no database, no network.
"""

import json
import uuid
from unittest import mock

from django.test import SimpleTestCase, override_settings
from rest_framework.test import APIRequestFactory, force_authenticate

from apps.salons import group_translate as gt
from apps.salons.booking_api import BookingApiUnavailable
from apps.salons.group_views import GroupBookingCancelView, GroupBookingCreateView
from apps.salons.test_group_booking import (
    BRANCH,
    CUT,
    GROUP_ID,
    MAYA,
    RANA,
    SALON_ID,
    TENANT,
    USER_ID,
    Customer,
    PartyChecks,
    ViewSeams,
    at,
    booking_body,
)
from apps.salons.views import BookingDetailView

VARIANT = "5a1e0000-0000-4000-8000-000000000001"

# What booking-api answers: its own branch, its own +06:00 clock.
ANSWER = {
    "id": GROUP_ID,
    "salon_id": BRANCH,
    "booking_type": "GROUP",
    "status": "CONFIRMED_BY_SALON",
    "payment_status": "PAY_AFTER_CHECK_IN",
    "date": "2026-10-11",
    "start_time": "2026-10-11T17:00:00+06:00",
    "end_time": "2026-10-11T18:00:00+06:00",
    "member_count": 3,
    "members": [
        {"ref": 0, "id": "p0", "user_id": None, "name": "Amal", "kind": "guest",
         "start_time": "2026-10-11T17:00:00+06:00", "end_time": "2026-10-11T17:45:00+06:00"},
        {"ref": 1, "id": "p1", "user_id": str(USER_ID), "name": None, "kind": "self",
         "start_time": "2026-10-11T17:00:00+06:00", "end_time": "2026-10-11T18:00:00+06:00"},
        {"ref": 2, "id": "p2", "user_id": RANA, "name": None, "kind": "registered",
         "start_time": "2026-10-11T17:00:00+06:00", "end_time": "2026-10-11T17:45:00+06:00"},
    ],
    "total": 415.0,
    "tax_amount": 20.75,
    "deposit_amount": 83.0,
    "pass_qr_code": "GS-1280",
    "expires_at": None,
    "created_at": "2026-10-11T09:00:00+06:00",
}

CARD = {
    "id": SALON_ID, "name": "Marina Walk", "logo_url": None, "city": "Dubai",
    "slug": "marina-walk", "cover_url": None, "address": "Marina", "region": None,
    "country_code": "AE", "lat": None, "lng": None, "timezone": "Asia/Dubai",
}


@override_settings(GROUP_BOOKING_V2=True)
class GroupBookingV2CreateTests(PartyChecks, ViewSeams, SimpleTestCase):
    """POST /api/v1/booking/group with the flag on."""

    PATH = "/api/v1/booking/group"
    SERVICES_FIELD = "services"

    def seams(self, **over):
        over.setdefault("create_group_booking", mock.Mock(return_value=(201, dict(ANSWER))))
        return super().seams(**over)

    def send(self, body=None, **kw):
        return self.post(GroupBookingCreateView, self.PATH, body or booking_body(), **kw)

    def with_services(self, service_id):
        body = booking_body()
        body["members"][0]["services"] = [{"id": service_id, "amount": 1}]
        return body

    def with_stylist(self, stylist_id, everyone=False):
        body = booking_body()
        for m in body["members"] if everyone else body["members"][1:2]:
            m["stylist_id"] = stylist_id
        return body

    def test_one_call_and_no_hold_or_confirm(self):
        with self.seams() as s:
            response = self.send()
        self.assertEqual(response.status_code, 201, response.data)
        s.create_group_booking.assert_called_once()
        s.hold_group.assert_not_called()
        s.confirm_group.assert_not_called()

    def test_what_booking_api_is_sent(self):
        with self.seams() as s:
            self.send()
        (body,), kw = s.create_group_booking.call_args
        self.assertEqual((body["salon_id"], body["start_time"]), (BRANCH, "2026-10-11T15:00:00+04:00"))
        self.assertEqual(kw["tenant_id"], TENANT)
        self.assertTrue(kw["idempotency_key"])
        # The app's order, the resolved names, ids only where there is an account.
        self.assertEqual(
            [(m["ref"], m["kind"], m["id"], m["name"], m["age_group"], m["stylist_id"]) for m in body["members"]],
            [
                (0, "guest", None, "Amal", "adult", None),
                (1, "self", str(USER_ID), "Dana", "adult", MAYA),
                (2, "registered", RANA, "Rana Hassan", "child", None),
            ],
        )
        self.assertEqual(body["members"][0]["services"], [{"id": CUT, "amount": 120}])
        # The app's figures ride along for booking-api to check.
        for key in gt.MONEY_FIELDS:
            self.assertEqual(body[key], booking_body()[key], key)

    def test_the_same_request_carries_the_same_key(self):
        with self.seams() as s:
            self.send()
            self.send()
        keys = [c.kwargs["idempotency_key"] for c in s.create_group_booking.call_args_list]
        self.assertEqual(keys[0], keys[1])

    def test_the_answer_is_on_the_salons_clock_and_names_the_salon(self):
        with self.seams():
            data = self.send().data
        self.assertEqual(data["salon_id"], SALON_ID)
        self.assertEqual(data["start_time"], "2026-10-11T15:00:00+04:00")
        self.assertEqual(data["end_time"], "2026-10-11T16:00:00+04:00")
        self.assertEqual(data["created_at"], "2026-10-11T07:00:00+04:00")
        self.assertEqual(data["date"], "2026-10-11")
        self.assertEqual(data["members"][1]["start_time"], "2026-10-11T15:00:00+04:00")
        self.assertEqual((data["payment_status"], data["expires_at"]), ("PAY_AFTER_CHECK_IN", None))

    def test_products_are_forwarded(self):
        body = booking_body()
        body["members"][1]["products"] = [{"id": VARIANT.upper(), "amount": 25, "quantity": 2}]
        with self.seams() as s:
            response = self.send(body)
        self.assertEqual(response.status_code, 201, response.data)
        sent = s.create_group_booking.call_args.args[0]
        self.assertEqual(sent["members"][1]["products"], [{"id": VARIANT, "amount": 25, "quantity": 2}])

    def test_a_product_without_a_variant_id_is_refused_here(self):
        body = booking_body()
        body["members"][1]["products"] = [{"id": "pomade", "amount": 25}]
        with self.seams() as s:
            response = self.send(body)
        self.assertEqual(response.status_code, 422, response.data)
        self.assertEqual(response.data["errors"][0]["code"], "unknown_product")
        s.create_group_booking.assert_not_called()

    def test_a_product_quantity_out_of_range_is_refused_here(self):
        body = booking_body()
        body["members"][1]["products"] = [{"id": VARIANT, "amount": 25, "quantity": 0}]
        with self.seams() as s:
            response = self.send(body)
        self.assertEqual(response.status_code, 422, response.data)
        s.create_group_booking.assert_not_called()

    def test_a_start_outside_the_salons_hours_never_reaches_booking_api(self):
        with self.seams() as s:
            response = self.send(booking_body(start_time="2026-10-11T22:00:00+04:00"))
        self.assertEqual(response.status_code, 422, response.data)
        s.create_group_booking.assert_not_called()

    def test_a_start_inside_the_notice_never_reaches_booking_api(self):
        self.NOW = at("14:50")
        with self.seams() as s:
            response = self.send()
        self.assertEqual(response.status_code, 422, response.data)
        s.create_group_booking.assert_not_called()

    def test_booking_apis_refusal_is_forwarded_as_it_is(self):
        refusal = {"detail": "x", "code": "validation_error",
                   "errors": [{"field": "total", "code": "amount_mismatch", "message": "m", "expected": 415}]}
        with self.seams(create_group_booking=mock.Mock(return_value=(422, refusal))):
            response = self.send()
        self.assertEqual((response.status_code, response.data), (422, refusal))

    def test_slot_taken_is_forwarded(self):
        taken = {"detail": "Only 2 professionals can cover this party.", "code": "slot_taken", "errors": []}
        with self.seams(create_group_booking=mock.Mock(return_value=(409, taken))):
            response = self.send()
        self.assertEqual((response.status_code, response.data["code"]), (409, "slot_taken"))

    def test_booking_api_down_is_a_503(self):
        down = mock.Mock(side_effect=BookingApiUnavailable("timeout"))
        with self.seams(create_group_booking=down):
            response = self.send()
        self.assertEqual(response.status_code, 503)


class GroupBookingV2OffTests(ViewSeams, SimpleTestCase):
    """With the flag off, nothing about POST /booking/group changes."""

    def test_the_old_path_runs_and_the_new_call_is_never_made(self):
        create = mock.Mock()
        with self.seams(create_group_booking=create) as s:
            response = self.post(GroupBookingCreateView, "/api/v1/booking/group", booking_body())
        self.assertEqual(response.status_code, 201, response.data)
        s.hold_group.assert_called_once()
        create.assert_not_called()

    def test_products_are_still_refused(self):
        body = booking_body()
        body["members"][1]["products"] = [{"id": VARIANT, "amount": 25}]
        with self.seams():
            response = self.post(GroupBookingCreateView, "/api/v1/booking/group", body)
        self.assertEqual(response.status_code, 422)
        self.assertEqual(response.data["errors"][0]["code"], "products_not_supported")


class ReadSeams:
    """The read and cancel seams: booking-api's two calls, the card, the names."""

    def patches(self, **over):
        seams = {
            "apps.salons.views.read_booking": mock.Mock(return_value=(404, {"code": "not_found"})),
            "apps.salons.group_views.read_group_booking": mock.Mock(return_value=(200, dict(ANSWER))),
            "apps.salons.group_views.cancel_group_booking": mock.Mock(return_value=(200, dict(ANSWER))),
            "apps.salons.group_views.salon_cards_for_refs": mock.Mock(return_value={BRANCH: CARD}),
            "apps.salons.group_views._account_names": mock.Mock(
                return_value={str(USER_ID): "Dana", RANA: "Rana Hassan"}),
        }
        seams.update(over)
        return seams, [mock.patch(target, fake) for target, fake in seams.items()]

    def run_with(self, call, **over):
        seams, patches = self.patches(**over)
        for p in patches:
            p.start()
        try:
            return call(), seams
        finally:
            for p in patches:
                p.stop()

    def get(self, booking_id=GROUP_ID):
        request = APIRequestFactory().get(f"/api/v1/booking/{booking_id}",
                                          HTTP_AUTHORIZATION="Bearer customer-token")
        force_authenticate(request, user=Customer())
        return BookingDetailView.as_view()(request, booking_id=uuid.UUID(booking_id))

    def cancel(self, booking_id=GROUP_ID):
        request = APIRequestFactory().post(f"/api/v1/booking/{booking_id}/cancel",
                                           HTTP_AUTHORIZATION="Bearer customer-token")
        force_authenticate(request, user=Customer())
        return GroupBookingCancelView.as_view()(request, booking_id=uuid.UUID(booking_id))


@override_settings(GROUP_BOOKING_V2=True)
class GroupReadV2Tests(ReadSeams, SimpleTestCase):

    def test_a_group_id_reads_the_party(self):
        response, s = self.run_with(self.get)
        self.assertEqual(response.status_code, 200, response.data)
        s["apps.salons.group_views.read_group_booking"].assert_called_once()
        data = response.data
        self.assertEqual((data["id"], data["booking_type"], data["salon_id"]), (GROUP_ID, "GROUP", SALON_ID))
        self.assertEqual(data["salon"]["name"], "Marina Walk")
        self.assertEqual(data["start_time"], "2026-10-11T15:00:00+04:00")

    def test_account_names_are_filled_in_and_a_guests_kept(self):
        response, _ = self.run_with(self.get)
        self.assertEqual([m["name"] for m in response.data["members"]], ["Amal", "Dana", "Rana Hassan"])

    def test_a_single_booking_never_asks_for_a_group(self):
        single = mock.Mock(return_value=(200, {"id": "b", "salon_id": BRANCH}))
        response, s = self.run_with(self.get, **{"apps.salons.views.read_booking": single,
                                                  "apps.salons.views.salon_cards_for_refs": mock.Mock(return_value={})})
        self.assertEqual(response.status_code, 200)
        s["apps.salons.group_views.read_group_booking"].assert_not_called()

    def test_an_id_that_is_neither_is_404(self):
        missing = mock.Mock(return_value=(404, {"code": "not_found"}))
        response, _ = self.run_with(self.get, **{"apps.salons.group_views.read_group_booking": missing})
        self.assertEqual(response.status_code, 404)

    def test_the_booker_cancels_the_party(self):
        cancelled = dict(ANSWER, status="CANCELLED")
        response, s = self.run_with(self.cancel, **{
            "apps.salons.group_views.cancel_group_booking": mock.Mock(return_value=(200, cancelled))})
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual((response.data["status"], response.data["salon_id"]), ("CANCELLED", SALON_ID))
        s["apps.salons.group_views.cancel_group_booking"].assert_called_once()

    def test_a_cancel_booking_api_refuses_is_forwarded(self):
        refusal = {"detail": "GS-1281 is checked in", "code": "cannot_cancel", "errors": []}
        response, _ = self.run_with(self.cancel, **{
            "apps.salons.group_views.cancel_group_booking": mock.Mock(return_value=(409, refusal))})
        self.assertEqual((response.status_code, response.data), (409, refusal))

    def test_cancel_with_booking_api_down_is_a_503(self):
        response, _ = self.run_with(self.cancel, **{
            "apps.salons.group_views.cancel_group_booking": mock.Mock(side_effect=BookingApiUnavailable("x"))})
        self.assertEqual(response.status_code, 503)


class GroupReadV2OffTests(ReadSeams, SimpleTestCase):
    """Flag off: a group id is a 404 on read, and cancel does not exist."""

    def test_read_stays_404_and_asks_for_no_group(self):
        response, s = self.run_with(self.get)
        self.assertEqual(response.status_code, 404)
        s["apps.salons.group_views.read_group_booking"].assert_not_called()

    def test_cancel_is_404_and_calls_nothing(self):
        response, s = self.run_with(self.cancel)
        self.assertEqual(response.status_code, 404)
        s["apps.salons.group_views.cancel_group_booking"].assert_not_called()


class MobileGroupBodyTests(SimpleTestCase):
    """The pure half: only what the app sent, and the salon's clock back."""

    def test_money_fields_not_sent_are_not_invented(self):
        body = gt.mobile_group_body("branch", at("15:00"), [], {"total": 1})
        self.assertEqual({k for k in gt.MONEY_FIELDS if k in body}, {"total"})

    def test_present_group_leaves_a_time_it_cannot_read(self):
        out = gt.present_group({"start_time": "soon", "members": []}, salon_id=None, tz=None)
        self.assertEqual(out["start_time"], "soon")

    def test_present_group_does_not_touch_its_input(self):
        answer = json.loads(json.dumps(ANSWER))
        gt.present_group(answer, salon_id=SALON_ID, tz=at("15:00").tzinfo, names={RANA: "Rana"})
        self.assertEqual(answer, ANSWER)
