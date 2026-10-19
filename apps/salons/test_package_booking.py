"""
Booking a package as one visit at its price (docs/BOOKING_CREATE_API.md,
"Booking a package"), the steps after the Package Details screen:

  * the slot search with a package (GET /booking/nearest-available,
    PACKAGE_BOOKING_V1): the package is searched as its parts, each as many
    times as it holds it, by one stylist for all of them;
  * the price check (POST /booking/quote): booking-api's quote, routed
    exactly as create is, its answer unchanged;
  * create (POST /booking) with the package as a line of `services`:
    forwarded as it came, nothing here reads it.

The selectors are mocked; no database. The package fixtures are
test_package_detail's, so both modules read the same packages.
"""

import json
import re
import types
import unittest
import urllib.error
import uuid
from contextlib import ExitStack
from datetime import datetime, timedelta
from unittest import mock

from django.conf import settings
from django.db.models import OuterRef, Q
from django.test import SimpleTestCase, override_settings
from django.urls import resolve, reverse
from drf_spectacular.generators import SchemaGenerator
from rest_framework.test import APIRequestFactory, force_authenticate

from apps.salons import booking_api, packages, selectors
from apps.salons.booking_api import BookingApiUnavailable
from apps.salons.test_group_booking import Customer
from apps.salons.test_package_detail import (
    BEARD,
    BRANCH,
    CUT,
    LONG_HAIR,
    PACKAGE,
    PACKAGE_2,
    SALON,
    TENANT,
    TOWEL,
    Reads,
    item,
    package_row,
    services,
    snap,
    snap_item,
    variant,
)
from apps.salons.views import (
    _QUOTE_EXAMPLE,
    PACKAGE_NOT_BOOKABLE,
    BookingCreateView,
    BookingQuoteView,
    NearestAvailableView,
    service_minutes,
)

MAYA = uuid.UUID("99999999-9999-9999-9999-999999999990")
ANYA = uuid.UUID("99999999-9999-9999-9999-999999999991")

# What each service takes in the diary: the stage sums service_timing_rows
# answers (stage_minutes), which win over the catalogue's duration.
TIMING = {
    CUT: {"id": CUT, "name": "The Gentleman's Cut", "price_minor": 19900, "branch_price_minor": None,
          "duration_minutes": 30, "stage_minutes": 35, "lead_time_minutes": None},
    BEARD: {"id": BEARD, "name": "Beard Trim & Shape", "price_minor": 23000, "branch_price_minor": None,
            "duration_minutes": 20, "stage_minutes": None, "lead_time_minutes": 60},
    TOWEL: {"id": TOWEL, "name": "Hot Towel Shave", "price_minor": 26000, "branch_price_minor": None,
            "duration_minutes": 25, "stage_minutes": 25, "lead_time_minutes": None},
}

# Who can do what: Maya everything, Anya the cut alone.
SKILLS = {MAYA: {CUT, BEARD, TOWEL}, ANYA: {CUT}}

# Far enough ahead that "not in the past" never cuts a start off.
WINDOW = "from=2030-01-15T10:00:00%2B04:00&to=2030-01-15T20:00:00%2B04:00"


def stylist(staff_id, first_name):
    return types.SimpleNamespace(id=staff_id, first_name=first_name, last_name=None,
                                 job_title=None, avatar_url=None)


def a_two_cut_package(package_id=PACKAGE):
    """A package of "2 x The Gentleman's Cut" and a hot towel shave."""
    return package_row(package_id, published_snapshot=snap([snap_item(CUT, quantity=2), snap_item(TOWEL)]))


class SlotSearchSeams(Reads):
    """
    NearestAvailableView with every read mocked: the salon, its packages
    (packages_for_salon), package_lines' three reads, the timing, and what
    _offers asks (hours, roster, skills, busy time). _offers itself runs.
    """

    def search(self, ids, *, packages=(), flag=True, minutes_as_booked=False, **reads):
        salon = types.SimpleNamespace(id=SALON, tenant_id=TENANT, branch_id=BRANCH,
                                      branch_timezone="Asia/Dubai")
        query = f"{WINDOW}&service_ids={','.join(str(i) for i in ids)}"
        request = APIRequestFactory().get(f"/api/v1/booking/nearest-available/{SALON}?{query}")
        force_authenticate(request, user=Customer())
        with ExitStack() as stack, override_settings(
            PACKAGE_BOOKING_V1=flag, SLOT_MINUTES_AS_BOOKED_V1=minutes_as_booked,
        ):
            stack.enter_context(mock.patch("apps.salons.views.salon_profile", return_value=salon))
            self.packages_read = stack.enter_context(mock.patch(
                "apps.salons.views.packages_for_salon", return_value=list(packages)))
            self.reads(stack, **reads)
            self.timing_read = stack.enter_context(mock.patch(
                "apps.salons.views.service_timing_rows",
                side_effect=lambda _salon, asked: [TIMING[i] for i in asked if i in TIMING]))
            stack.enter_context(mock.patch(
                "apps.salons.views._open_span",
                side_effect=lambda _s, _tz, _day, start: (start + timedelta(hours=9),
                                                          start + timedelta(hours=22))))
            stack.enter_context(mock.patch(
                "apps.salons.views.salon_stylists",
                return_value=[stylist(MAYA, "Maya"), stylist(ANYA, "Anya")]))
            self.coverage = stack.enter_context(mock.patch(
                "apps.salons.views.stylist_service_coverage",
                side_effect=lambda _salon, asked, staff_ids: {
                    s: [i for i in asked if i in SKILLS[s]] for s in staff_ids
                    if any(i in SKILLS[s] for i in asked)
                }))
            stack.enter_context(mock.patch("apps.salons.views.shift_rows", return_value=[
                {"staff_member_id": s, "start_time": "09:00", "end_time": "22:00", "break_time": None}
                for s in (MAYA, ANYA)
            ]))
            self.busy = stack.enter_context(mock.patch(
                "apps.salons.views.busy_intervals", return_value=([], None)))
            return NearestAvailableView.as_view()(request, salon_id=SALON)

    def lengths(self, response):
        """Each offer's minutes, start to end."""
        return {
            (datetime.fromisoformat(o["end"]) - datetime.fromisoformat(o["start"])).seconds // 60
            for o in response.data["offers"]
        }


class SlotSearchWithAPackageTests(SlotSearchSeams, SimpleTestCase):
    """GET /booking/nearest-available with a package id, PACKAGE_BOOKING_V1 on."""

    def test_a_package_is_searched_as_its_parts_each_per_quantity(self):
        response = self.search([PACKAGE], packages=[a_two_cut_package()])
        self.assertEqual(response.status_code, 200)
        # Two cuts and a shave: 35 + 35 + 25, not one cut's 35 + 25.
        self.assertEqual(response.data["duration_min"], 95)
        self.assertEqual(self.lengths(response), {95})
        # Timing and skills are asked once per service.
        self.timing_read.assert_called_once_with(mock.ANY, [CUT, TOWEL])
        self.assertEqual(self.coverage.call_args.args[1], [CUT, TOWEL])

    def test_its_parts_are_the_live_content_the_details_screen_shows(self):
        # The snapshot's items, not the draft's (the draft has no items here).
        draft_items = [{"package_id": PACKAGE, **item(BEARD)}]
        self.search([PACKAGE], packages=[a_two_cut_package()], drafts=draft_items)
        self.drafts_read.assert_not_called()
        self.assertEqual(self.coverage.call_args.args[1], [CUT, TOWEL])

    def test_one_stylist_must_do_every_part(self):
        # Anya can do the cuts and not the shave: covering part of the visit
        # is covering none of it.
        response = self.search([PACKAGE], packages=[a_two_cut_package()])
        self.assertTrue(response.data["offers"])
        self.assertEqual({o["stylist"]["id"] for o in response.data["offers"]}, {str(MAYA)})

    def test_a_package_next_to_hand_picked_services_in_the_order_asked(self):
        response = self.search([BEARD, PACKAGE], packages=[a_two_cut_package()])
        self.assertEqual(response.data["duration_min"], 20 + 95)
        self.assertEqual(self.coverage.call_args.args[1], [BEARD, CUT, TOWEL])

    def test_a_hand_picked_service_that_is_also_a_part_counts_twice(self):
        # booking-api books the cut the customer picked and the package's two.
        response = self.search([CUT, PACKAGE], packages=[a_two_cut_package()])
        self.assertEqual(response.data["duration_min"], 35 * 3 + 25)
        self.assertEqual(self.coverage.call_args.args[1], [CUT, TOWEL])

    def test_the_longest_notice_of_any_part_applies(self):
        pkg = package_row(PACKAGE, published_snapshot=snap([snap_item(BEARD)]))
        with mock.patch("apps.salons.views._offers", return_value=[]) as offers:
            self.search([PACKAGE], packages=[pkg])
        self.assertEqual(offers.call_args.kwargs["lead_minutes"], 60)

    def test_ids_that_are_no_package_here_are_left_as_sent(self):
        # Asked about all of them in one read; nothing found, nothing changes.
        stranger = uuid.uuid4()
        response = self.search([CUT, stranger], packages=[])
        self.packages_read.assert_called_once_with(mock.ANY, [CUT, stranger])
        self.services_read.assert_not_called()
        self.assertEqual(response.data["duration_min"], 35)
        # An id this salon does not offer still matches nobody, as before.
        self.assertEqual(response.data["offers"], [])

    def test_two_packages_one_read(self):
        second = package_row(PACKAGE_2, published_snapshot=snap([snap_item(BEARD)]))
        response = self.search([PACKAGE, PACKAGE_2], packages=[a_two_cut_package(), second])
        self.assertEqual(response.data["duration_min"], 95 + 20)
        self.services_read.assert_called_once()

    def test_a_package_that_cannot_be_booked_is_422_package_not_bookable(self):
        cases = {
            "pulled from sale": dict(packages=[package_row(status="HIDDEN")]),
            "not sold at this branch": dict(packages=[package_row(branch_row=False)]),
            "a part off at this branch": dict(packages=[package_row()],
                                              svc=services(towel={"branch_available": False})),
            "pinned to a variant": dict(
                packages=[package_row(published_snapshot=snap([snap_item(CUT, LONG_HAIR)]))],
                variants={LONG_HAIR: variant()}),
            "free": dict(packages=[package_row(published_snapshot=snap(priceMinor=0))]),
            # booking-api's catalogue would refuse the whole basket (409).
            "a part priced 0 here": dict(packages=[package_row()],
                                         svc=services(beard={"branch_price_minor": 0})),
            # An AED package holding a service priced in BDT here: 422
            # BOOKING_CURRENCY_MIXED from booking-api otherwise.
            "a part in another currency": dict(packages=[package_row()],
                                               svc=services(towel={"currency": "BDT"})),
        }
        for name, kwargs in cases.items():
            with self.subTest(name):
                response = self.search([PACKAGE], **kwargs)
                self.assertEqual(response.status_code, 422)
                self.assertEqual(response.data, {
                    "detail": "Please correct the highlighted fields.",
                    "code": "validation_error",
                    "errors": [{
                        "field": "service_ids",
                        "code": "package_not_bookable",
                        "message": PACKAGE_NOT_BOOKABLE,
                    }],
                })
                # Refused before anything is searched or anyone is asked.
                self.timing_read.assert_not_called()
                self.busy.assert_not_called()

    def test_the_refusal_is_the_same_rule_as_can_book(self):
        pkg = package_row(published_snapshot=snap([snap_item(CUT, LONG_HAIR)]))
        content = packages.live_content(pkg)
        rows, complete = packages.lines(content["items"], services(), {LONG_HAIR: variant()})
        self.assertFalse(packages.can_book(pkg, content, rows, complete))
        response = self.search([PACKAGE], packages=[pkg], variants={LONG_HAIR: variant()})
        self.assertEqual(response.status_code, 422)


class SlotSearchSwitchedOffTests(SlotSearchSeams, SimpleTestCase):
    """PACKAGE_BOOKING_V1 off: exactly what the endpoint answered before."""

    def test_a_package_id_is_sent_on_as_it_came(self):
        response = self.search([PACKAGE], packages=[a_two_cut_package()], flag=False)
        self.packages_read.assert_not_called()
        self.timing_read.assert_called_once_with(mock.ANY, [PACKAGE])
        # No service of this salon: no minutes and nobody to offer.
        self.assertEqual(response.data, {"duration_min": 0, "offers": []})

    def test_hand_picked_services_are_counted_as_before(self):
        response = self.search([CUT, BEARD], flag=False)
        self.assertEqual(response.data["duration_min"], 35 + 20)
        self.assertEqual(self.lengths(response), {55})


class SlotMinutesAsBookedTests(SlotSearchSeams, SimpleTestCase):
    """
    SLOT_MINUTES_AS_BOOKED_V1 on: the slot search counts the minutes
    booking-api books (the catalogue's), not the stages' sum, so an offer
    ends where the booking will and create's end_time check agrees with it.
    """

    def test_hand_picked_services_take_their_catalogue_minutes(self):
        response = self.search([CUT, BEARD], flag=False, minutes_as_booked=True)
        # The cut's 30, not its stages' 35.
        self.assertEqual(response.data["duration_min"], 30 + 20)
        self.assertEqual(self.lengths(response), {50})

    def test_a_package_is_its_parts_catalogue_minutes(self):
        response = self.search([PACKAGE], packages=[a_two_cut_package()], minutes_as_booked=True)
        self.assertEqual(response.data["duration_min"], 30 + 30 + 25)
        self.assertEqual(self.lengths(response), {85})

    def test_stages_count_only_for_a_service_with_no_catalogue_minutes(self):
        no_minutes = {**TIMING[BEARD], "duration_minutes": 0, "stage_minutes": 15}
        with mock.patch.dict(TIMING, {BEARD: no_minutes}):
            response = self.search([BEARD], flag=False, minutes_as_booked=True)
        self.assertEqual(response.data["duration_min"], 15)


class ServiceMinutesTests(SimpleTestCase):
    """views.service_minutes: one service's minutes in the diary."""

    # Look Change's "Hair Solution", 2026-10-19: two 30-minute services
    # whose stages came to 53 between them. booking-api booked 60, and
    # refused the end_time the slot search's 53 would give.
    STAGED = {"duration_minutes": 30, "stage_minutes": 23}

    def test_off_the_stages_win(self):
        with override_settings(SLOT_MINUTES_AS_BOOKED_V1=False):
            self.assertEqual(service_minutes(self.STAGED), 23)

    def test_on_the_catalogue_wins(self):
        with override_settings(SLOT_MINUTES_AS_BOOKED_V1=True):
            self.assertEqual(service_minutes(self.STAGED), 30)

    def test_a_service_with_neither_is_no_minutes_both_ways(self):
        for flag in (False, True):
            with self.subTest(flag=flag), override_settings(SLOT_MINUTES_AS_BOOKED_V1=flag):
                self.assertEqual(service_minutes({"duration_minutes": None, "stage_minutes": None}), 0)


class PartsTests(SimpleTestCase):
    """packages.parts: the services one booking of the package performs."""

    def test_each_line_as_many_times_as_it_holds_it_in_order(self):
        rows, _ = packages.lines([item(TOWEL), item(CUT, quantity=2), item(BEARD)], services(), {})
        self.assertEqual(packages.parts(rows), [TOWEL, CUT, CUT, BEARD])

    def test_nothing_for_no_lines(self):
        self.assertEqual(packages.parts([]), [])


class PackagesForSalonTests(SimpleTestCase):
    """selectors.packages_for_salon: package_for_salon's rule, for several ids."""

    salon = types.SimpleNamespace(id=SALON, tenant_id=TENANT, branch_id=BRANCH)

    def test_this_tenant_s_ever_on_sale_with_the_branch_row(self):
        with mock.patch.object(selectors, "ServicePackage") as package, \
                mock.patch.object(selectors, "_package_branch_row", return_value="row") as row:
            chain = package.objects.filter.return_value.filter.return_value
            chain.annotate.return_value = ["found"]
            answer = selectors.packages_for_salon(self.salon, [PACKAGE, CUT])
        package.objects.filter.assert_called_once_with(id__in=[PACKAGE, CUT], tenant_id=TENANT)
        package.objects.filter.return_value.filter.assert_called_once_with(
            Q(published_version__isnull=False) | Q(status__in=("PUBLISHED", "HIDDEN", "ARCHIVED"))
        )
        row.assert_called_once_with(BRANCH)
        chain.annotate.assert_called_once_with(branch_row="row")
        self.assertEqual(answer, ["found"])

    def test_no_ids_no_read(self):
        with mock.patch.object(selectors, "ServicePackage") as package:
            self.assertEqual(selectors.packages_for_salon(self.salon, []), [])
        package.objects.filter.assert_not_called()

    def test_the_branch_row_is_this_branch_s(self):
        with mock.patch.object(selectors, "ServicePackageBranchAvailability") as rows:
            selectors._package_branch_row(BRANCH)
        rows.objects.filter.assert_called_once_with(package_id=OuterRef("pk"), branch_id=BRANCH)


# ---------------------------------------------------------------------------
# POST /api/v1/booking/quote: the price check, booking-api's own figures.
# ---------------------------------------------------------------------------

STOREFRONT = "55555555-5555-5555-5555-555555555555"
BRANCH_ID = "66666666-6666-6666-6666-666666666666"
TENANT_ID = "11111111-1111-1111-1111-111111111111"

QUOTE = b'{"salon_id":"%s","services":[{"id":"%s"}],"date":"2026-10-20","start_time":"2026-10-20T14:00:00+04:00"}'


def quote_body(salon=STOREFRONT, line=str(PACKAGE)):
    return QUOTE % (salon.encode(), line.encode())


class QuoteClientTests(SimpleTestCase):
    """booking_api.quote_booking, with the network replaced (BookingApiClientTests' style)."""

    URL = "http://booking/v1/mobile-booking/quote"

    def urlopen(self, *, status=200, body=b'{"total":546}'):
        response = mock.MagicMock()
        response.status = status
        response.read.return_value = body
        response.__enter__.return_value = response
        return mock.patch.object(booking_api.urllib.request, "urlopen", return_value=response)

    def http_error(self, status, body):
        error = urllib.error.HTTPError(self.URL, status, "", {}, None)
        error.read = lambda: body
        return mock.patch.object(booking_api.urllib.request, "urlopen", side_effect=error)

    def quote(self, **kwargs):
        return booking_api.quote_booking(quote_body(), authorization="Bearer t", **kwargs)

    def test_the_quote_comes_back_whole(self):
        with self.urlopen():
            self.assertEqual(self.quote(), (200, {"total": 546}))

    def test_a_refusal_is_returned_not_raised(self):
        body = b'{"code":"validation_error","errors":[{"field":"services","code":"unknown_service"}]}'
        with self.http_error(422, body):
            self.assertEqual(self.quote(), (422, json.loads(body)))

    def test_an_unreachable_or_html_booking_api_raises(self):
        for patch in (
            mock.patch.object(booking_api.urllib.request, "urlopen",
                              side_effect=urllib.error.URLError("connection refused")),
            mock.patch.object(booking_api.urllib.request, "urlopen", side_effect=TimeoutError("slow")),
            self.http_error(502, b"<html>Bad Gateway</html>"),
        ):
            with self.subTest(patch=patch), patch, self.assertRaises(BookingApiUnavailable):
                self.quote()

    @override_settings(BOOKING_API_URL="http://booking/")
    def test_the_path_the_bytes_and_only_these_headers(self):
        with self.urlopen() as urlopen:
            self.quote(tenant_id="ten-1")
        request = urlopen.call_args.args[0]
        self.assertEqual(request.get_method(), "POST")
        self.assertEqual(request.get_full_url(), self.URL)
        self.assertEqual(request.data, quote_body())
        # No Idempotency-Key: booking-api would replay a stored, older price.
        self.assertEqual(
            {k.lower(): v for k, v in request.headers.items()},
            {"content-type": "application/json", "authorization": "Bearer t", "x-tenant-id": "ten-1"},
        )

    def test_no_tenant_no_header(self):
        with self.urlopen() as urlopen:
            self.quote()
        self.assertNotIn("x-tenant-id", {k.lower() for k in urlopen.call_args.args[0].headers})


class QuoteViewTests(SimpleTestCase):
    """POST /api/v1/booking/quote, with booking-api and the salon lookup replaced."""

    def post(self, body=None, *, view=None, resolves="default", authenticated=True,
             content_type="application/json", upstream=(200, {"total": 546}), **headers):
        request = APIRequestFactory().post(
            "/api/v1/booking/quote", data=quote_body() if body is None else body,
            content_type=content_type, **headers,
        )
        if authenticated:
            force_authenticate(request, user=Customer())
        if resolves == "default":
            resolves = {"tenant_id": TENANT_ID, "branch_id": BRANCH_ID}
        with mock.patch(
            "apps.salons.views.booking_route",
            side_effect=lambda ref: resolves if isinstance(ref, str) and ref.strip() else None,
        ), mock.patch("apps.salons.views.quote_booking", return_value=upstream) as quote, \
                mock.patch("apps.salons.views.create_booking", return_value=(201, {})) as create:
            response = (view or BookingQuoteView).as_view()(request)
        self.quote_call, self.create_call = quote, create
        return response

    def sent(self):
        return self.quote_call.call_args

    def test_the_route(self):
        self.assertIs(resolve("/api/v1/booking/quote").func.view_class, BookingQuoteView)
        self.assertEqual(reverse("booking-quote"), "/api/v1/booking/quote")
        # Still create's own route, and still a booking id's.
        self.assertIs(resolve("/api/v1/booking").func.view_class, BookingCreateView)

    def test_booking_api_s_answer_comes_back_unchanged(self):
        for upstream in ((200, {"total": 546, "services": [{"id": str(PACKAGE), "amount": 520}]}),
                         (422, {"detail": "Please correct the highlighted fields.",
                                "code": "validation_error",
                                "errors": [{"field": "date", "code": "date_mismatch", "message": "x"}]}),
                         (401, {"code": "not_authenticated"})):
            with self.subTest(status=upstream[0]):
                response = self.post(upstream=upstream)
                self.assertEqual((response.status_code, response.data), upstream)

    def test_the_storefront_id_is_rewritten_to_the_branch_and_the_tenant_derived(self):
        self.post()
        body = json.loads(self.sent().args[0])
        self.assertEqual(body["salon_id"], BRANCH_ID)
        self.assertEqual(body["services"], [{"id": str(PACKAGE)}])
        self.assertEqual(self.sent().kwargs["tenant_id"], TENANT_ID)

    def test_a_branch_id_or_an_unknown_salon_crosses_byte_for_byte(self):
        self.post(quote_body(salon=BRANCH_ID))
        self.assertEqual(self.sent().args[0], quote_body(salon=BRANCH_ID))
        self.post(quote_body(salon="marina-walk"), resolves=None)
        self.assertEqual(self.sent().args[0], quote_body(salon="marina-walk"))
        self.assertIsNone(self.sent().kwargs["tenant_id"])

    def test_the_caller_s_tenant_header_wins_and_the_token_is_forwarded(self):
        self.post(HTTP_X_TENANT_ID="ten-mine", HTTP_AUTHORIZATION="Bearer customer-token")
        self.assertEqual(self.sent().kwargs["tenant_id"], "ten-mine")
        self.assertEqual(self.sent().kwargs["authorization"], "Bearer customer-token")

    def test_no_idempotency_key_even_when_one_is_sent(self):
        self.post(HTTP_IDEMPOTENCY_KEY="key-1")
        self.assertEqual(set(self.sent().kwargs), {"authorization", "tenant_id"})

    def test_routed_exactly_as_create(self):
        for body, resolves in ((quote_body(), "default"),
                               (quote_body(salon=BRANCH_ID), "default"),
                               (quote_body(salon="marina-walk"), None)):
            with self.subTest(body=body):
                self.post(body, resolves=resolves, HTTP_AUTHORIZATION="Bearer t")
                quoted = self.sent()
                self.post(body, view=BookingCreateView, resolves=resolves, HTTP_AUTHORIZATION="Bearer t")
                created = self.create_call.call_args
                self.assertEqual(quoted.args[0], created.args[0])
                self.assertEqual(quoted.kwargs["tenant_id"], created.kwargs["tenant_id"])
                self.assertEqual(quoted.kwargs["authorization"], created.kwargs["authorization"])

    def test_an_unreachable_booking_api_is_create_s_503(self):
        request = APIRequestFactory().post("/api/v1/booking/quote", data=quote_body(),
                                           content_type="application/json")
        force_authenticate(request, user=Customer())
        with mock.patch("apps.salons.views.booking_route", return_value=None), \
                mock.patch("apps.salons.views.quote_booking",
                           side_effect=BookingApiUnavailable("connection refused")):
            response = BookingQuoteView.as_view()(request)
        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.data, {
            "detail": "Booking is temporarily unavailable. Please try again.",
            "code": "service_unavailable",
            "errors": [{"field": None, "code": "booking_api_unavailable",
                        "message": "Booking is temporarily unavailable. Please try again."}],
        })

    def test_refused_here_before_the_network(self):
        cases = ((dict(content_type="application/x-www-form-urlencoded", body="salon_id=x"), 415),
                 (dict(authenticated=False), 401))
        for kwargs, code in cases:
            with self.subTest(code=code):
                response = self.post(**kwargs)
                self.assertEqual(response.status_code, code)
                self.quote_call.assert_not_called()

    def test_not_behind_the_package_switch(self):
        # It prices any basket, so it answers with PACKAGE_BOOKING_V1 off too.
        with override_settings(PACKAGE_BOOKING_V1=False):
            self.assertEqual(self.post().status_code, 200)


class QuoteOpenApiTests(SimpleTestCase):
    """The route's OpenAPI entry: placed in the flow, with its body and a package example."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.operation = SchemaGenerator().get_schema(request=None, public=True)[
            "paths"]["/api/v1/booking/quote"]["post"]

    def test_placed_before_book_it(self):
        self.assertEqual(self.operation["tags"], ["8. Book one visit"])
        self.assertTrue(self.operation["summary"].startswith("Step 2: Price it"))

    def test_the_body_is_the_quote_s_keys_only(self):
        schema = self.operation["requestBody"]["content"]["application/json"]["schema"]
        self.assertEqual(sorted(schema["properties"]), ["date", "products", "salon_id", "services", "start_time"])
        self.assertEqual(schema["properties"]["services"]["items"]["required"], ["id"])

    def test_the_example_s_figures_add_up_by_the_contract(self):
        example = self.operation["responses"]["200"]["content"]["application/json"]["examples"]
        q = next(iter(example.values()))["value"]
        package = q["packages"][0]
        # The line is the package price; the parts carry it between them.
        self.assertEqual(q["services"], [{"id": package["id"], "amount": package["amount"]}])
        self.assertEqual(round(sum(p["amount"] for p in q["parts"]), 2), package["amount"])
        self.assertEqual(package["savings"], package["parts_total"] - package["amount"])
        # 5% of (lines - tier discount), and nothing paid on create.
        self.assertEqual(q["tax_amount"], round((q["amount_without_tax"] - q["discount"]) * 0.05, 2))
        self.assertEqual(q["total"], q["amount_without_tax"] - q["discount"] + q["tax_amount"])
        self.assertEqual((q["advance_paid_amount"], q["due_amount"]), (0, q["total"]))
        self.assertEqual(q["duration_min"], sum(p["duration_min"] for p in q["parts"]))


# ---------------------------------------------------------------------------
# POST /api/v1/booking with a package as a line: nothing here reads it.
# ---------------------------------------------------------------------------


class CreateWithAPackageLineTests(SimpleTestCase):
    """
    booking-api takes a package as a line of `services` ({id, amount}, the
    package price), with no package_id field. Nothing in this service's
    create path reads `services`: _route_booking reads `salon_id` alone, and
    the bytes go on as they came.
    """

    LINES = b'"services":[{"id":"%s","amount":520},{"id":"%s","amount":230.5}]' % (
        str(PACKAGE).encode(), str(BEARD).encode())

    def body(self, salon):
        return (b'{"salon_id":"%s",%s,"stylists":["%s"],"total":787.5}'
                % (salon.encode(), self.LINES, str(MAYA).encode()))

    def create(self, body, *, flag):
        request = APIRequestFactory().post("/api/v1/booking", data=body, content_type="application/json")
        force_authenticate(request, user=Customer())
        with override_settings(PACKAGE_BOOKING_V1=flag), mock.patch(
            "apps.salons.views.booking_route",
            return_value={"tenant_id": TENANT_ID, "branch_id": BRANCH_ID},
        ), mock.patch("apps.salons.views.create_booking", return_value=(201, {"id": "bkg_1"})) as create:
            response = BookingCreateView.as_view()(request)
        self.assertEqual(response.status_code, 201)
        return create.call_args

    def test_a_package_line_reaches_booking_api_byte_for_byte(self):
        for flag in (False, True):
            with self.subTest(PACKAGE_BOOKING_V1=flag):
                body = self.body(BRANCH_ID)
                self.assertEqual(self.create(body, flag=flag).args[0], body)

    def test_also_when_the_storefront_id_is_rewritten_to_the_branch(self):
        # Only salon_id changes; the lines are the same bytes, in the same place.
        for flag in (False, True):
            with self.subTest(PACKAGE_BOOKING_V1=flag):
                sent = self.create(self.body(STOREFRONT), flag=flag).args[0]
                self.assertEqual(sent, self.body(BRANCH_ID))
                self.assertIn(self.LINES, sent)


# Create as the app sent it at Look Change, 2026-10-19, on the seeded
# "Groom and Go" (520, its parts 609 apart, saving 89): the saving sent as a
# discount. The money adds up (609 - 89 = 520, + 26 VAT = 546), and
# booking-api refuses it with amount_mismatch on amount_without_tax.
SAVING_AS_DISCOUNT = {
    "salon_id": BRANCH_ID,
    "services": [{"id": str(PACKAGE), "amount": 520}],
    "products": [],
    "stylists": [str(MAYA)],
    "date": "2026-10-20",
    "start_time": "2026-10-20T14:00:00+04:00",
    "end_time": "2026-10-20T15:25:00+04:00",
    "amount_without_tax": 609,
    "tax_amount": 26,
    "discount": 89,
    "total": 546,
    "advance_paid_amount": 0,
    "due_amount": 546,
    "payment_status": "PAY_AFTER_CHECK_IN",
    "status": "BOOKED",
    "booking_type": "SINGLE",
}

# booking-api's quote for that basket and start, for a customer with no tier.
PRICED = {
    "services": [{"id": str(PACKAGE), "amount": 520}],
    "packages": [{"id": str(PACKAGE), "name": "Groom and Go", "amount": 520,
                  "parts_total": 609, "savings": 89, "service_ids": []}],
    "amount_without_tax": 520, "discount": 0, "tax_amount": 26, "total": 546,
    "advance_paid_amount": 0, "due_amount": 546,
    "start_time": "2026-10-20T14:00:00+04:00", "end_time": "2026-10-20T15:25:00+04:00",
}


def app_body(**changes):
    return json.dumps({**SAVING_AS_DISCOUNT, **changes}, separators=(",", ":")).encode()


class PackageCreateSeams:
    """POST /booking with booking_route, the package read, the quote and create mocked."""

    def create(self, body, *, flag=True, end=False, quote=(200, PRICED), packages=("row",), **headers):
        request = APIRequestFactory().post("/api/v1/booking", data=body,
                                           content_type="application/json", **headers)
        force_authenticate(request, user=Customer())
        quote_mock = (mock.Mock(side_effect=quote) if isinstance(quote, Exception)
                      else mock.Mock(return_value=quote))
        with override_settings(PACKAGE_SAVING_AS_DISCOUNT_V1=flag, PACKAGE_END_TIME_FROM_QUOTE_V1=end), mock.patch(
            "apps.salons.views.booking_route",
            return_value={"tenant_id": TENANT_ID, "branch_id": BRANCH_ID},
        ), mock.patch("apps.salons.views.packages_for_salon", return_value=list(packages)) as found, \
                mock.patch("apps.salons.views.quote_booking", quote_mock), \
                mock.patch("apps.salons.views.create_booking", return_value=(201, {"id": "bkg_1"})) as create:
            response = BookingCreateView.as_view()(request)
        self.assertEqual(response.status_code, 201)
        self.packages_read, self.quote_call = found, quote_mock
        return create.call_args


class PackageSavingFoldTests(PackageCreateSeams, SimpleTestCase):
    """
    POST /booking with a package's saving sent as `discount`
    (PACKAGE_SAVING_AS_DISCOUNT_V1): moved back into the price only when
    booking-api's quote says that is all that is wrong; every other body
    crosses as it came.
    """

    def test_the_saving_is_moved_back_into_the_price(self):
        sent = self.create(app_body()).args[0]
        self.assertEqual(json.loads(sent), {**SAVING_AS_DISCOUNT, "amount_without_tax": 520, "discount": 0})
        # Every other key where it was, in the order sent.
        self.assertEqual(list(json.loads(sent)), list(SAVING_AS_DISCOUNT))

    def test_the_quote_is_asked_for_create_s_basket_and_start_alone(self):
        self.create(app_body(products=[{"id": "v1", "amount": 30}]), HTTP_AUTHORIZATION="Bearer customer")
        asked = self.quote_call.call_args
        self.assertEqual(json.loads(asked.args[0]), {
            "salon_id": BRANCH_ID, "services": [{"id": str(PACKAGE)}],
            "products": [{"id": "v1", "amount": 30}],
            "date": "2026-10-20", "start_time": "2026-10-20T14:00:00+04:00",
        })
        # The customer's own token (the tier discount is theirs), no key.
        self.assertEqual(asked.kwargs, {"authorization": "Bearer customer", "tenant_id": TENANT_ID})
        self.packages_read.assert_called_once_with(mock.ANY, [PACKAGE])

    def test_a_tier_discount_stays_and_only_the_saving_moves(self):
        priced = {**PRICED, "discount": 52, "tax_amount": 23.4, "total": 491.4, "due_amount": 491.4}
        body = app_body(discount=141, tax_amount=23.4, total=491.4, due_amount=491.4)
        sent = json.loads(self.create(body, quote=(200, priced)).args[0])
        self.assertEqual((sent["amount_without_tax"], sent["discount"]), (520, 52))

    def test_the_idempotency_key_is_still_the_app_s_body_s(self):
        folded = self.create(app_body(), flag=True).kwargs["idempotency_key"]
        unfolded = self.create(app_body(), flag=False).kwargs["idempotency_key"]
        self.assertEqual(folded, unfolded)
        self.assertEqual(self.create(app_body(), HTTP_IDEMPOTENCY_KEY="k-1").kwargs["idempotency_key"], "k-1")

    def test_off_the_bytes_cross_as_they_came(self):
        self.assertEqual(self.create(app_body(), flag=False).args[0], app_body())
        self.quote_call.assert_not_called()
        self.packages_read.assert_not_called()

    def test_a_create_built_from_the_quote_costs_nothing_extra(self):
        body = app_body(amount_without_tax=520, discount=0)
        self.assertEqual(self.create(body).args[0], body)
        self.packages_read.assert_not_called()
        self.quote_call.assert_not_called()

    def test_a_discount_with_no_package_is_not_quoted(self):
        body = app_body(discount=20)
        self.assertEqual(self.create(body, packages=()).args[0], body)
        self.quote_call.assert_not_called()

    def test_anything_else_wrong_crosses_as_it_came(self):
        cases = {
            "another total": dict(body=app_body(total=547)),
            "another VAT": dict(body=app_body(tax_amount=30.45, total=550.45, due_amount=550.45)),
            "another due": dict(body=app_body(due_amount=0)),
            "a discount bigger than the saving": dict(body=app_body(amount_without_tax=619, discount=99)),
            "a price and a discount that do not move together": dict(body=app_body(discount=79)),
            "a discount as text": dict(body=app_body(discount="89")),
            "a quote refused": dict(body=app_body(), quote=(422, {"code": "validation_error"})),
            "a quote with no package": dict(body=app_body(), quote=(200, {**PRICED, "packages": []})),
            "a quote with no savings": dict(body=app_body(), quote=(
                200, {**PRICED, "packages": [{"id": str(PACKAGE), "amount": 520}]})),
            "booking-api unreachable for the quote": dict(
                body=app_body(), quote=BookingApiUnavailable("connection refused")),
        }
        for name, kwargs in cases.items():
            with self.subTest(name):
                body = kwargs.pop("body")
                self.assertEqual(self.create(body, **kwargs).args[0], body)

    def test_a_body_that_is_not_an_object_crosses_as_it_came(self):
        for body in (b"[1,2]", b"not json"):
            with self.subTest(body=body):
                self.assertEqual(self.create(body).args[0], body)
                self.quote_call.assert_not_called()


class PackageEndTimeMendTests(PackageCreateSeams, SimpleTestCase):
    """
    POST /booking with a package and an end_time of the app's own
    (PACKAGE_END_TIME_FROM_QUOTE_V1): the quote's end_time replaces it when
    the quote starts at the app's start; the start never moves.
    """

    def right(self, **changes):
        """The body built from the quote: right money, right end."""
        return app_body(amount_without_tax=520, discount=0, **changes)

    def test_the_app_s_end_is_replaced_by_the_quote_s(self):
        # 30 minutes, the way an app measuring the package by one part sends it.
        sent = self.create(self.right(end_time="2026-10-20T14:30:00+04:00"), flag=False, end=True).args[0]
        self.assertEqual(json.loads(sent), {**json.loads(self.right()), "end_time": "2026-10-20T15:25:00+04:00"})
        self.assertEqual(list(json.loads(sent)), list(SAVING_AS_DISCOUNT))

    def test_the_live_payload_is_mended_whole(self):
        # Look Change, 2026-10-19: the saving as a discount AND a 30-minute end.
        body = app_body(end_time="2026-10-20T14:30:00+04:00")
        sent = json.loads(self.create(body, flag=True, end=True).args[0])
        self.assertEqual((sent["amount_without_tax"], sent["discount"], sent["end_time"]),
                         (520, 0, "2026-10-20T15:25:00+04:00"))
        self.assertEqual(sent["start_time"], "2026-10-20T14:00:00+04:00")
        self.quote_call.assert_called_once()

    def test_the_same_instant_in_another_offset_is_left_alone(self):
        # booking-api answers on its own clock: the same end, written +06:00.
        quote = (200, {**PRICED, "start_time": "2026-10-20T16:00:00+06:00",
                       "end_time": "2026-10-20T17:25:00+06:00"})
        body = self.right()
        self.assertEqual(self.create(body, flag=False, end=True, quote=quote).args[0], body)

    def test_an_end_is_mended_even_when_the_money_is_wrong_otherwise(self):
        # booking-api still refuses the money: only the end is the quote's business here.
        body = app_body(total=547, end_time="2026-10-20T14:30:00+04:00")
        sent = json.loads(self.create(body, flag=True, end=True).args[0])
        self.assertEqual(sent, {**json.loads(body), "end_time": "2026-10-20T15:25:00+04:00"})

    def test_left_as_it_came(self):
        cases = {
            "a quote at another start": dict(quote=(200, {**PRICED, "start_time": "2026-10-20T14:15:00+04:00"})),
            "a quote with no end": dict(quote=(200, {k: v for k, v in PRICED.items() if k != "end_time"})),
            "a quote with no start": dict(quote=(200, {k: v for k, v in PRICED.items() if k != "start_time"})),
            "a quote refused": dict(quote=(422, {"code": "validation_error"})),
            "booking-api unreachable for the quote": dict(quote=BookingApiUnavailable("connection refused")),
            "no package in the basket": dict(packages=()),
        }
        for name, kwargs in cases.items():
            with self.subTest(name):
                body = self.right(end_time="2026-10-20T14:30:00+04:00")
                self.assertEqual(self.create(body, flag=False, end=True, **kwargs).args[0], body)

    def test_an_end_that_is_not_a_time_is_not_quoted(self):
        for end_time in (None, "half past two", "2026-10-20T14:30:00"):
            with self.subTest(end_time=end_time):
                body = self.right(end_time=end_time)
                self.assertEqual(self.create(body, flag=False, end=True).args[0], body)
                self.quote_call.assert_not_called()

    def test_off_the_bytes_cross_as_they_came(self):
        body = self.right(end_time="2026-10-20T14:30:00+04:00")
        self.assertEqual(self.create(body, flag=False, end=False).args[0], body)
        self.quote_call.assert_not_called()
        self.packages_read.assert_not_called()


CREATE_DOC = settings.BASE_DIR / "docs" / "BOOKING_CREATE_API.md"


@unittest.skipUnless(CREATE_DOC.exists(), "docs/ is not in the Docker image")
class QuoteDocTests(SimpleTestCase):
    """docs/BOOKING_CREATE_API.md §6 shows the quote the OpenAPI page shows."""

    def test_the_doc_s_quote_is_the_openapi_example(self):
        text = CREATE_DOC.read_text(encoding="utf-8")
        blocks = [json.loads(b) for b in re.findall(r"```json\n(.*?)\n```", text, re.S)]
        self.assertIn(_QUOTE_EXAMPLE, blocks)
