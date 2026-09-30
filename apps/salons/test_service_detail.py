"""
GET /api/v1/salon/<salon_id>/service/<service_id>, the Service Detail screen
(docs/service-detail-fe-contract.md, docs/SERVICE_DETAIL_AUDIT.md section 7).

S1: the route, the 404s and auth. Every request goes through the real URL
(the Django test client), because the point of S1 is what the URL layer does
with a bad id: our JSON 404, never Django's HTML page. The two selectors are
mocked; no database.
"""

import types
import uuid
from unittest import mock

from django.db.models import Q
from django.test import SimpleTestCase
from django.urls import resolve, reverse

from apps.salons import selectors
from apps.salons.service_detail_views import (
    NO_SALON,
    NO_SERVICE,
    SalonServiceDetailView,
    path_uuid,
)
from apps.salons.views import SalonServicesView

SALON = uuid.UUID("33333333-3333-3333-3333-333333333333")
TENANT = uuid.UUID("11111111-1111-1111-1111-111111111111")
SERVICE = uuid.UUID("66666666-6666-6666-6666-666666666660")


def url(salon=SALON, service=SERVICE):
    return f"/api/v1/salon/{salon}/service/{service}"


class RouteTests(SimpleTestCase):
    """What the endpoint answers, through the URL layer."""

    def get(self, path, *, salon=True, service=True, **headers):
        found_salon = types.SimpleNamespace(id=SALON, tenant_id=TENANT) if salon else None
        found_service = types.SimpleNamespace(id=SERVICE) if service else None
        with mock.patch(
            "apps.salons.service_detail_views.salon_profile", return_value=found_salon
        ) as salon_lookup, mock.patch(
            "apps.salons.service_detail_views.service_for_salon", return_value=found_service
        ) as service_lookup:
            self.salon_lookup = salon_lookup
            self.service_lookup = service_lookup
            return self.client.get(path, **headers)

    def assert_our_404(self, response, detail):
        self.assertEqual(response.status_code, 404)
        self.assertEqual(response["Content-Type"], "application/json")
        body = response.json()
        self.assertEqual(body["code"], "not_found")
        self.assertEqual(body["detail"], detail)
        self.assertEqual(
            body["errors"], [{"field": None, "code": "not_found", "message": detail}]
        )

    def test_a_service_of_this_salon_answers_200_without_a_token(self):
        response = self.get(url())
        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            response.json(), {"id": str(SERVICE), "salon_id": str(SALON)}
        )

    def test_the_selectors_get_the_parsed_ids(self):
        self.get(url())
        self.salon_lookup.assert_called_once_with(SALON)
        (salon, service_id), _ = self.service_lookup.call_args
        self.assertEqual(salon.id, SALON)
        self.assertEqual(service_id, SERVICE)

    def test_a_salon_id_that_is_not_a_uuid_is_our_json_404(self):
        response = self.get(url(salon="not-a-uuid"))
        self.assert_our_404(response, NO_SALON)
        self.salon_lookup.assert_not_called()
        self.service_lookup.assert_not_called()

    def test_a_service_id_that_is_not_a_uuid_is_our_json_404(self):
        response = self.get(url(service="also-not"))
        self.assert_our_404(response, NO_SERVICE)
        self.service_lookup.assert_not_called()

    def test_both_ids_bad_names_the_salon(self):
        self.assert_our_404(self.get(url(salon="x", service="y")), NO_SALON)

    def test_an_unknown_salon_is_404(self):
        self.assert_our_404(self.get(url(), salon=False), NO_SALON)
        self.service_lookup.assert_not_called()

    def test_a_service_not_found_at_this_salon_is_404(self):
        # Another tenant's service, a service never on sale, or no such id:
        # the selector answers None for all three (see ServiceForSalonTests).
        self.assert_our_404(self.get(url(), service=False), NO_SERVICE)

    def test_an_uppercase_uuid_is_accepted(self):
        response = self.get(url(salon=str(SALON).upper(), service=str(SERVICE).upper()))
        self.assertEqual(response.status_code, 200)
        self.salon_lookup.assert_called_once_with(SALON)

    def test_other_uuid_spellings_are_404(self):
        for spelling in (
            SALON.hex,                     # 32 hex digits, no dashes
            "{%s}" % SALON,                # braces
            "urn:uuid:%s" % SALON,         # a URN
            str(SALON)[:-1],               # one digit short
            str(SALON)[:-1] + "g",         # not hex
        ):
            with self.subTest(spelling=spelling):
                self.assert_our_404(self.get(url(salon=spelling)), NO_SALON)

    def test_a_bad_token_is_401_even_on_this_public_route(self):
        response = self.get(url(), HTTP_AUTHORIZATION="Bearer not.a.token")
        self.assertEqual(response.status_code, 401)
        self.assertEqual(response.json()["code"], "not_authenticated")
        self.salon_lookup.assert_not_called()

    def test_only_get(self):
        with mock.patch("apps.salons.service_detail_views.salon_profile"):
            response = self.client.post(url())
        self.assertEqual(response.status_code, 405)
        self.assertEqual(response.json()["code"], "method_not_allowed")


class UrlTests(SimpleTestCase):
    """The new route next to the old ones."""

    def test_the_route_has_a_name(self):
        self.assertEqual(
            reverse("salon-service-detail", kwargs={"salon_id": SALON, "service_id": SERVICE}),
            url(),
        )

    def test_any_segment_reaches_the_view(self):
        match = resolve(url(salon="nope", service="nope"))
        self.assertIs(match.func.view_class, SalonServiceDetailView)
        self.assertEqual(match.kwargs, {"salon_id": "nope", "service_id": "nope"})

    def test_the_services_tab_route_is_unchanged(self):
        match = resolve(f"/api/v1/salon/{SALON}/services")
        self.assertIs(match.func.view_class, SalonServicesView)
        self.assertEqual(match.kwargs, {"salon_id": SALON})


class PathUuidTests(SimpleTestCase):
    def test_parses_the_dashed_form_in_any_case(self):
        self.assertEqual(path_uuid(str(SALON)), SALON)
        self.assertEqual(path_uuid(str(SALON).upper()), SALON)

    def test_anything_else_is_none(self):
        for value in (None, "", " %s" % SALON, SALON.hex, "{%s}" % SALON, "not-a-uuid"):
            with self.subTest(value=value):
                self.assertIsNone(path_uuid(value))


class ServiceForSalonTests(SimpleTestCase):
    """`selectors.service_for_salon`: which rows the detail screen may open."""

    def lookup(self):
        salon = types.SimpleNamespace(id=SALON, tenant_id=TENANT)
        with mock.patch.object(selectors, "Service") as service:
            first = service.objects.filter.return_value.filter.return_value.first
            first.return_value = "row"
            answer = selectors.service_for_salon(salon, SERVICE)
        return service, answer

    def test_only_this_salon_s_tenant(self):
        # Exactly the id and the tenant: no status, deleted_at or online
        # booking filter, so a service pulled from sale is still found. No
        # branch filter either: every branch of the tenant sells one menu.
        service, answer = self.lookup()
        service.objects.filter.assert_called_once_with(id=SERVICE, tenant_id=TENANT)
        self.assertEqual(answer, "row")

    def test_only_a_service_that_was_on_sale_once(self):
        service, _ = self.lookup()
        service.objects.filter.return_value.filter.assert_called_once_with(
            Q(published_version__isnull=False)
            | Q(status__in=("PUBLISHED", "HIDDEN", "ARCHIVED"))
        )

    def test_without_a_version_only_the_three_draft_statuses_are_404(self):
        # Rafa, 2026-09-30: an old service with no version must never 404 just
        # because it was hidden or archived. These are the platform's six.
        opens = set(selectors.DETAIL_STATUSES_WITHOUT_VERSION)
        self.assertEqual(opens, {"PUBLISHED", "HIDDEN", "ARCHIVED"})
        for status in ("DRAFT", "PENDING_REVISION", "REVISION_REQUESTED"):
            with self.subTest(status=status):
                self.assertNotIn(status, opens)
