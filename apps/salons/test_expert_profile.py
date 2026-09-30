"""
The Expert Profile screen (docs/expert-profile-fe-contract.md,
docs/EXPERT_PROFILE_AUDIT.md section 8).

E0: who counts as a salon's stylist. One list (selectors.salon_stylists) for
every screen, and the rule is the platform's and booking-api's own: a stylist
whose home branch is this salon's branch, and whose login account is live.

E1: the route GET /api/v1/salon/<salon_id>/stylist/<stylist_id>, its 404s and
auth. Every request goes through the real URL (the Django test client),
because the point is what the URL layer does with a bad id: our JSON 404,
never Django's HTML page.

E2: the person (id, salon_id, name, title, role, avatar_url, rating,
review_count), built from the stylist's own row of GET /salon/<id>/stylists
and compared with that route on the same rows.

E3b: is_favorite, and which stylists a heart may be saved on. The heart
itself, and is_favorite read from the real table, are in test_favourite.py.

E4: service_groups, only what this stylist does, grouped as the Services
tab. The tab is pinned first, before its grouping moved to menu.py.

The querysets are built and inspected, never run: the platform's tables are
unmanaged, so they do not exist in the test database. The views are called
with their selectors mocked.
"""

import types
import uuid
from contextlib import ExitStack
from unittest import mock

from django.db.models import Exists, QuerySet
from django.test import SimpleTestCase
from django.urls import resolve, reverse
from drf_spectacular.generators import SchemaGenerator
from rest_framework.exceptions import ValidationError
from rest_framework.test import APIRequestFactory, force_authenticate

from apps.salons import (
    expert_profile,
    expert_profile_views,
    group_views,
    menu,
    params,
    selectors,
    service_detail_views,
    views,
)
from apps.salons.expert_profile_views import NO_SALON, NO_STYLIST, SalonExpertProfileView

SALON = uuid.UUID("33333333-3333-3333-3333-333333333333")
TENANT = uuid.UUID("11111111-1111-1111-1111-111111111111")
BRANCH = uuid.UUID("22222222-2222-2222-2222-222222222222")
OTHER_BRANCH = uuid.UUID("22222222-2222-2222-2222-2222222222ff")

HERE = uuid.UUID("99999999-9999-9999-9999-999999999990")       # at this salon
ELSEWHERE = uuid.UUID("99999999-9999-9999-9999-9999999999ff")  # not in the list


def conditions(query):
    """
    Every plain WHERE condition of a query, as (column, lookup, value). A
    value that points at the outer row reads ("outer", column).
    """
    out = set()
    for cond in query.where.children:
        if isinstance(cond.lhs, Exists):
            continue
        value = cond.rhs
        if hasattr(value, "target"):
            value = ("outer", value.target.name)
        out.add((cond.lhs.target.name, cond.lookup_name, value))
    return out


def login_check(queryset):
    """The EXISTS condition of the stylist list: the login account it needs."""
    found = [c.lhs for c in queryset.query.where.children if isinstance(c.lhs, Exists)]
    assert len(found) == 1, found
    return found[0].query


def stylist(staff_id=HERE, first="Liam", last="Johnson"):
    """A row of the list, as salon_stylists gives one."""
    return types.SimpleNamespace(
        id=staff_id, tenant_id=TENANT, branch_id=BRANCH,
        first_name=first, last_name=last, position="Senior Barber",
        job_title="Barber and Grooming Expert", avatar_url=None,
    )


class SalonStylistsRuleTests(SimpleTestCase):
    """`selectors.salon_stylists`: who is a stylist of ONE salon."""

    salon = types.SimpleNamespace(id=SALON, tenant_id=TENANT, branch_id=BRANCH)

    def setUp(self):
        self.qs = selectors.salon_stylists(self.salon)

    def test_this_business_s_employed_joined_and_not_deleted_staff(self):
        # As before E0: an invite never accepted, a stylist who left and a
        # deleted record are all out.
        self.assertLessEqual(
            {
                ("tenant_id", "exact", TENANT),
                ("employment_status", "exact", "ACTIVE"),
                ("onboarding_state", "exact", "ACTIVE"),
                ("deleted_at", "isnull", True),
            },
            conditions(self.qs.query),
        )

    def test_only_a_stylist_whose_home_branch_is_this_salon_s_branch(self):
        # Another salon of the same business has another branch id, so its
        # stylists are not in this list (the contract's section 3: never a
        # cross-salon read).
        self.assertIn(("branch_id", "exact", BRANCH), conditions(self.qs.query))

    def test_a_stylist_with_no_home_branch_is_left_out(self):
        self.assertIn(("branch_id", "isnull", False), conditions(self.qs.query))

    def test_a_salon_with_no_branch_lists_nobody(self):
        # Django reads `branch_id=None` as IS NULL, which alone would list
        # exactly the stylists with no home branch. With IS NOT NULL beside
        # it, nothing can match.
        salon = types.SimpleNamespace(id=SALON, tenant_id=TENANT, branch_id=None)
        found = conditions(selectors.salon_stylists(salon).query)
        self.assertIn(("branch_id", "isnull", True), found)
        self.assertIn(("branch_id", "isnull", False), found)

    def test_a_branch_passed_in_wins_over_the_salon_s(self):
        found = conditions(selectors.salon_stylists(self.salon, OTHER_BRANCH).query)
        self.assertIn(("branch_id", "exact", OTHER_BRANCH), found)
        self.assertNotIn(("branch_id", "exact", BRANCH), found)

    def test_the_login_account_must_be_live_and_of_the_same_tenant(self):
        # The platform's ListStylists test, exactly: the account of this staff
        # row, in the same tenant, not deleted. A deleted login, a missing one
        # and another tenant's all fail it, and the stylist is left out.
        login = login_check(self.qs)
        self.assertIs(login.model, selectors.UserAccount)
        self.assertEqual(
            conditions(login),
            {
                ("id", "exact", ("outer", "user_id")),
                ("tenant_id", "exact", ("outer", "tenant_id")),
                ("deleted_at", "isnull", True),
            },
        )

    def test_the_account_s_status_is_not_checked(self):
        # SUSPENDED or DISABLED still lists: the platform's list does not look
        # at it either (audit 7.2, reading 1).
        self.assertNotIn("status", {column for column, _, _ in conditions(login_check(self.qs))})

    def test_the_name_title_and_avatar_are_read_from_that_same_live_account(self):
        # Never from a deleted account or another tenant's.
        wanted = conditions(login_check(self.qs))
        for name in ("first_name", "last_name", "job_title", "avatar_file_id"):
            with self.subTest(annotation=name):
                self.assertEqual(conditions(self.qs.query.annotations[name]), wanted)

    def test_the_services_flag_does_not_change_the_list(self):
        # BRANCH_AVAILABILITY_ENABLED is about which services a branch sells.
        # It is off, and must stay free to be off: the stylist rule is not
        # behind it.
        with mock.patch.object(selectors, "BRANCH_AVAILABILITY_ENABLED", False):
            off = selectors.salon_stylists(self.salon)
        with mock.patch.object(selectors, "BRANCH_AVAILABILITY_ENABLED", True):
            on = selectors.salon_stylists(self.salon)
        self.assertEqual(conditions(off.query), conditions(on.query))
        self.assertEqual(conditions(login_check(off)), conditions(login_check(on)))
        self.assertIn(("branch_id", "exact", BRANCH), conditions(off.query))

    def test_oldest_first(self):
        self.assertEqual(tuple(self.qs.query.order_by), ("created_at",))


class EveryScreenReadsTheOneListTests(SimpleTestCase):
    """
    The Stylists tab and the Expert step, nearest available and the group
    booking's check all ask `salon_stylists(salon)`. So a stylist who is not
    in it (another salon's, no home branch, a deleted login) is gone from all
    of them together.
    """

    salon = types.SimpleNamespace(id=SALON, tenant_id=TENANT, branch_id=BRANCH)

    def test_the_stylists_tab_lists_exactly_the_list(self):
        with mock.patch.object(views, "salon_stylists", return_value=[stylist()]) as listed:
            rows = views.stylist_rows(self.salon)
        listed.assert_called_once_with(self.salon)
        self.assertEqual([row["id"] for row in rows], [str(HERE)])

    def test_the_expert_step_filters_inside_the_list(self):
        service = uuid.uuid4()
        with mock.patch.object(views, "salon_stylists", return_value=[stylist()]) as listed, \
                mock.patch.object(views, "stylist_service_coverage",
                                  return_value={HERE: [service]}) as coverage:
            rows = views.stylist_rows(self.salon, [service], stages=[])
        listed.assert_called_once_with(self.salon)
        # Only the listed stylists are even asked about.
        self.assertEqual(coverage.call_args.args[2], [HERE])
        self.assertEqual([row["id"] for row in rows], [str(HERE)])

    def test_nearest_available_offers_nothing_for_a_stylist_not_in_the_list(self):
        with mock.patch.object(views, "salon_stylists", return_value=[stylist()]) as listed:
            away = views._qualified_stylists(self.salon, [], ELSEWHERE)
            here = views._qualified_stylists(self.salon, [], HERE)
        listed.assert_called_with(self.salon)
        # No stylist to search means `"offers": []` (see _offers).
        self.assertEqual(away, [])
        self.assertEqual([s.id for s in here], [HERE])

    def test_the_group_booking_refuses_a_stylist_not_in_the_list(self):
        with mock.patch.object(group_views, "salon_stylists", return_value=[stylist()]) as listed:
            roster = group_views._roster(self.salon)
        listed.assert_called_once_with(self.salon)
        self.assertEqual(set(roster), {str(HERE)})

        with self.assertRaises(ValidationError) as refused:
            group_views._check_stylists(
                self.salon,
                [{"stylist_id": str(ELSEWHERE), "service_ids": []}],
                roster,
            )
        (error,) = refused.exception.detail["stylist_id"]
        self.assertEqual(error.code, "foreign_id")
        self.assertEqual(str(error), "That stylist does not work at this salon.")


class StylistListRouteTests(SimpleTestCase):
    """
    GET /api/v1/stylists?tenant_id=&branch_id=: the salon OF THAT BRANCH.

    It used to take the tenant's first public salon whatever `branch_id` said.
    Now that the list is one salon's own stylists, that would answer with the
    wrong people in a business with two salons.
    """

    def get(self, query, *, salon=True, rows=()):
        found = types.SimpleNamespace(id=SALON, tenant_id=TENANT, branch_id=BRANCH)
        with mock.patch.object(views, "discoverable_salons") as salons, \
                mock.patch.object(views, "stylist_rows", return_value=list(rows)) as listed:
            salons.return_value.filter.return_value.first.return_value = found if salon else None
            response = self.client.get(f"/api/v1/stylists?{query}")
        self.salons, self.listed, self.found = salons, listed, found
        return response

    def test_the_salon_is_found_by_tenant_and_branch(self):
        rows = [{"id": str(HERE), "name": "Liam Johnson"}]
        response = self.get(f"tenant_id={TENANT}&branch_id={BRANCH}", rows=rows)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"stylists": rows})
        self.salons.return_value.filter.assert_called_once_with(
            tenant_id=TENANT, branch_id=BRANCH,
        )
        self.listed.assert_called_once_with(self.found)

    def test_a_branch_that_is_no_public_salon_of_the_tenant_is_404(self):
        response = self.get(f"tenant_id={TENANT}&branch_id={OTHER_BRANCH}", salon=False)
        self.assertEqual(response.status_code, 404)
        self.assertEqual(response["Content-Type"], "application/json")
        self.assertEqual(response.json()["code"], "not_found")
        self.assertEqual(response.json()["detail"], "Salon not found")
        self.listed.assert_not_called()

    def test_branch_id_is_still_required(self):
        response = self.get(f"tenant_id={TENANT}")
        self.assertEqual(response.status_code, 422)
        self.assertEqual(response.json()["errors"][0]["field"], "branch_id")
        self.salons.assert_not_called()

    def test_tenant_id_is_still_required(self):
        response = self.get(f"branch_id={BRANCH}")
        self.assertEqual(response.status_code, 422)
        self.assertEqual(response.json()["errors"][0]["field"], "tenant_id")
        self.salons.assert_not_called()


# ---------------------------------------------------------------------------
# E1: the route, the 404s and auth
# ---------------------------------------------------------------------------

def url(salon=SALON, stylist=HERE):
    return f"/api/v1/salon/{salon}/stylist/{stylist}"


def mock_platform_reads(stack, *, salon=True, found=True, person=None,
                        services=(), covered=(), categories=None):
    """
    Every read the view makes of the PLATFORM's tables, mocked: the salon, the
    stylist, the menu, its stages, who covers what, the categories. Returns
    the mocks by selector name.

    `covered` is the services this stylist can do. One place, because the
    test database has none of these tables: test_favourite.py uses it too,
    beside the real favourite table.
    """
    found_salon = (
        types.SimpleNamespace(id=SALON, tenant_id=TENANT, branch_id=BRANCH) if salon else None
    )
    if person is None:
        person = stylist()
    answers = {
        "salon_profile": found_salon,
        "stylist_for_salon": person if found else None,
        "salon_services": list(services),
        "service_stage_rows": [],
        "stylist_service_coverage": {person.id: [s.id for s in covered]} if covered else {},
        "salon_categories": MENU_CATEGORIES if categories is None else categories,
    }
    return {
        name: stack.enter_context(mock.patch.object(
            expert_profile_views, name, return_value=answer,
        ))
        for name, answer in answers.items()
    }


class Seams:
    """
    The view with every selector it reads mocked. A test passes only what it
    cares about. Each mock is kept on self (salon_lookup, stylist_lookup,
    favourite_lookup, services_lookup, stages_lookup, coverage_lookup,
    categories_lookup) for call checks.
    """

    def seams(self, stack, *, favourite=False, **reads):
        mocks = mock_platform_reads(stack, **reads)
        self.salon_lookup = mocks["salon_profile"]
        self.stylist_lookup = mocks["stylist_for_salon"]
        self.services_lookup = mocks["salon_services"]
        self.stages_lookup = mocks["service_stage_rows"]
        self.coverage_lookup = mocks["stylist_service_coverage"]
        self.categories_lookup = mocks["salon_categories"]
        self.favourite_lookup = stack.enter_context(mock.patch.object(
            expert_profile_views, "is_favourite_stylist", return_value=favourite,
        ))

    def call(self, path=None, *, salon=True, found=True, favourite=False,
             services=(), covered=(), **headers):
        with ExitStack() as stack:
            self.seams(stack, salon=salon, found=found, favourite=favourite,
                       services=services, covered=covered)
            return self.client.get(path or url(), **headers)


class RouteTests(Seams, SimpleTestCase):
    """What the endpoint answers, through the URL layer."""

    def assert_our_404(self, response, detail):
        self.assertEqual(response.status_code, 404)
        self.assertEqual(response["Content-Type"], "application/json")
        body = response.json()
        self.assertEqual(body["code"], "not_found")
        self.assertEqual(body["detail"], detail)
        self.assertEqual(
            body["errors"], [{"field": None, "code": "not_found", "message": detail}]
        )

    def test_a_stylist_of_this_salon_answers_200_without_a_token(self):
        response = self.call()
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["id"], str(HERE))
        self.assertEqual(response.json()["salon_id"], str(SALON))

    def test_a_signed_in_customer_gets_the_same_answer(self):
        # The token is optional (Q1): with one, the route answers the same.
        request = APIRequestFactory().get(url())
        force_authenticate(request, user=types.SimpleNamespace(is_authenticated=True))
        with ExitStack() as stack:
            self.seams(stack)
            response = SalonExpertProfileView.as_view()(
                request, salon_id=str(SALON), stylist_id=str(HERE),
            )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data, self.call().json())

    def test_the_selectors_get_the_parsed_ids(self):
        self.call()
        self.salon_lookup.assert_called_once_with(SALON)
        (salon, stylist_id), _ = self.stylist_lookup.call_args
        self.assertEqual(salon.id, SALON)
        self.assertEqual(stylist_id, HERE)

    def test_a_salon_id_that_is_not_a_uuid_is_our_json_404(self):
        response = self.call(url(salon="not-a-uuid"))
        self.assert_our_404(response, NO_SALON)
        self.salon_lookup.assert_not_called()
        self.stylist_lookup.assert_not_called()

    def test_a_stylist_id_that_is_not_a_uuid_is_our_json_404(self):
        response = self.call(url(stylist="also-not"))
        self.assert_our_404(response, NO_STYLIST)
        self.stylist_lookup.assert_not_called()

    def test_both_ids_bad_names_the_salon(self):
        self.assert_our_404(self.call(url(salon="x", stylist="y")), NO_SALON)

    def test_an_unknown_salon_is_404(self):
        self.assert_our_404(self.call(salon=False), NO_SALON)
        self.stylist_lookup.assert_not_called()

    def test_a_stylist_not_found_at_this_salon_is_404(self):
        # Another business's stylist, another salon's, one who left, one never
        # joined, a deleted one, or no such id: the selector answers None for
        # all of them (see StylistForSalonTests).
        self.assert_our_404(self.call(found=False), NO_STYLIST)

    def test_an_uppercase_uuid_is_accepted(self):
        response = self.call(url(salon=str(SALON).upper(), stylist=str(HERE).upper()))
        self.assertEqual(response.status_code, 200)
        self.salon_lookup.assert_called_once_with(SALON)
        self.assertEqual(self.stylist_lookup.call_args.args[1], HERE)

    def test_other_uuid_spellings_are_404(self):
        for spelling in (
            HERE.hex,                     # 32 hex digits, no dashes
            "{%s}" % HERE,                # braces
            "urn:uuid:%s" % HERE,         # a URN
            str(HERE)[:-1],               # one digit short
            str(HERE)[:-1] + "g",         # not hex
        ):
            with self.subTest(spelling=spelling):
                self.assert_our_404(self.call(url(stylist=spelling)), NO_STYLIST)
                self.assert_our_404(self.call(url(salon=spelling)), NO_SALON)

    def test_a_bad_token_is_401_even_on_this_public_route(self):
        response = self.call(HTTP_AUTHORIZATION="Bearer not.a.token")
        self.assertEqual(response.status_code, 401)
        self.assertEqual(response.json()["code"], "not_authenticated")
        self.salon_lookup.assert_not_called()

    def test_only_get(self):
        with mock.patch.object(expert_profile_views, "salon_profile"):
            response = self.client.post(url())
        self.assertEqual(response.status_code, 405)
        self.assertEqual(response.json()["code"], "method_not_allowed")


class SentenceTests(SimpleTestCase):
    """`detail` is shown to the customer on the app's empty state."""

    def test_the_stylist_sentence_is_the_contract_s_word_for_word(self):
        self.assertEqual(NO_STYLIST, "This stylist is no longer at this salon.")

    def test_the_salon_sentence_is_the_service_detail_s(self):
        # One sentence for the same case on both screens.
        self.assertEqual(NO_SALON, service_detail_views.NO_SALON)


class UrlTests(SimpleTestCase):
    """The new route next to the old ones."""

    def test_the_route_has_a_name(self):
        self.assertEqual(
            reverse("salon-expert-profile", kwargs={"salon_id": SALON, "stylist_id": HERE}),
            url(),
        )

    def test_any_segment_reaches_the_view(self):
        match = resolve(url(salon="nope", stylist="nope"))
        self.assertIs(match.func.view_class, SalonExpertProfileView)
        self.assertEqual(match.kwargs, {"salon_id": "nope", "stylist_id": "nope"})

    def test_the_stylists_list_route_is_unchanged(self):
        # `stylists` (plural) is the list; `stylist/<id>` is one person.
        match = resolve(f"/api/v1/salon/{SALON}/stylists")
        self.assertIs(match.func.view_class, views.SalonStylistsView)
        self.assertEqual(match.kwargs, {"salon_id": SALON})

    def test_the_service_detail_route_is_unchanged(self):
        match = resolve(f"/api/v1/salon/{SALON}/service/{HERE}")
        self.assertIs(match.func.view_class, service_detail_views.SalonServiceDetailView)
        self.assertEqual(match.kwargs, {"salon_id": str(SALON), "service_id": str(HERE)})


class PathUuidTests(SimpleTestCase):
    """`path_uuid` moved to params.py, shared by both detail screens."""

    def test_parses_the_dashed_form_in_any_case(self):
        self.assertEqual(params.path_uuid(str(SALON)), SALON)
        self.assertEqual(params.path_uuid(str(SALON).upper()), SALON)

    def test_anything_else_is_none(self):
        for value in (None, "", " %s" % SALON, SALON.hex, "{%s}" % SALON, "not-a-uuid", SALON):
            with self.subTest(value=value):
                self.assertIsNone(params.path_uuid(value))

    def test_both_screens_use_the_one_function(self):
        # Still importable from the service detail's module, where it was.
        self.assertIs(service_detail_views.path_uuid, params.path_uuid)
        self.assertIs(expert_profile_views.path_uuid, params.path_uuid)


class StylistForSalonTests(SimpleTestCase):
    """`selectors.stylist_for_salon`: which stylists the profile may open."""

    salon = types.SimpleNamespace(id=SALON, tenant_id=TENANT, branch_id=BRANCH)

    def test_it_is_the_salon_s_own_list_narrowed_to_one_id(self):
        # The Expert step's list, so exactly the Expert step's people (Q2).
        with mock.patch.object(selectors, "salon_stylists") as listed:
            listed.return_value.filter.return_value.first.return_value = "row"
            answer = selectors.stylist_for_salon(self.salon, HERE)
        listed.assert_called_once_with(self.salon)
        listed.return_value.filter.assert_called_once_with(id=HERE)
        self.assertEqual(answer, "row")

    def query(self):
        """The real query, caught where it would run."""
        with mock.patch.object(QuerySet, "first", autospec=True, return_value=None) as first:
            answer = selectors.stylist_for_salon(self.salon, HERE)
        self.assertIsNone(answer)
        (queryset,), _ = first.call_args
        return queryset

    def test_the_query_asks_for_that_one_stylist(self):
        self.assertIn(("id", "exact", HERE), conditions(self.query().query))

    def test_everyone_the_contract_wants_404_is_shut_out_by_a_condition(self):
        found = conditions(self.query().query)
        for who, condition in (
            ("another business's stylist", ("tenant_id", "exact", TENANT)),
            ("another salon's stylist, same business", ("branch_id", "exact", BRANCH)),
            ("a stylist with no home branch", ("branch_id", "isnull", False)),
            ("invited, never joined", ("onboarding_state", "exact", "ACTIVE")),
            ("left: INACTIVE or ARCHIVED", ("employment_status", "exact", "ACTIVE")),
            ("a deleted staff record", ("deleted_at", "isnull", True)),
        ):
            with self.subTest(who=who):
                self.assertIn(condition, found)

    def test_a_stylist_with_a_deleted_login_is_shut_out_too(self):
        self.assertIn(("deleted_at", "isnull", True), conditions(login_check(self.query())))


class OpenApiTests(Seams, SimpleTestCase):
    """The route's OpenAPI entry describes what the view answers."""

    PATH = "/api/v1/salon/{salon_id}/stylist/{stylist_id}"

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.schema = SchemaGenerator().get_schema(request=None, public=True)

    def operation(self):
        return self.schema["paths"][self.PATH]["get"]

    def test_only_get_is_documented(self):
        methods = {key for key in self.schema["paths"][self.PATH] if key != "parameters"}
        self.assertEqual(methods, {"get"})

    def test_it_sits_on_the_salon_s_page_in_the_flow(self):
        self.assertEqual(self.operation()["tags"], ["5. A salon's page"])
        self.assertRegex(self.operation()["summary"], r"^Step \d+: One of its stylists")

    def test_both_path_ids_are_uuids(self):
        in_path = {
            p["name"]: p["schema"] for p in self.operation()["parameters"] if p["in"] == "path"
        }
        self.assertEqual(set(in_path), {"salon_id", "stylist_id"})
        for name, schema in in_path.items():
            with self.subTest(name=name):
                self.assertEqual(schema, {"type": "string", "format": "uuid"})

    def test_the_answers_are_200_401_and_404(self):
        self.assertEqual(set(self.operation()["responses"]), {"200", "401", "404"})

    def test_the_200_has_exactly_the_fields_the_view_answers(self):
        content = self.operation()["responses"]["200"]["content"]["application/json"]
        self.assertEqual(list(content["schema"]["properties"]), list(self.call().json()))

    def test_the_200_example_has_the_answer_s_keys(self):
        content = self.operation()["responses"]["200"]["content"]["application/json"]
        example = content["examples"]["AStylistOfThisSalon"]["value"]
        self.assertEqual(list(example), list(self.call().json()))

    def test_the_404_example_is_the_real_body(self):
        example = self.operation()["responses"]["404"]["content"]["application/json"]
        real = self.call(found=False).json()
        self.assertEqual(example["examples"]["NoSuchStylist"]["value"], real)

    def test_a_token_is_accepted_not_needed(self):
        # `{}` is "no token": the route is public, and the JWT is optional.
        self.assertEqual(self.operation()["security"], [{"jwtAuth": []}, {}])


# ---------------------------------------------------------------------------
# E2: the person
# ---------------------------------------------------------------------------

PERSON_KEYS = ("name", "title", "role", "avatar_url")


def member(number, first, last, position=None, job_title=None, avatar=None):
    """A stylist as `salon_stylists` gives one."""
    return types.SimpleNamespace(
        id=uuid.UUID(int=number), tenant_id=TENANT, branch_id=BRANCH,
        first_name=first, last_name=last, position=position,
        job_title=job_title, avatar_url=avatar,
    )


class Listed(list):
    """
    The salon's stylist list, standing in for the queryset: what the list
    route iterates, and what `stylist_for_salon` narrows to one id.
    """

    def filter(self, id):
        return Listed(s for s in self if s.id == id)

    def first(self):
        return self[0] if self else None


class CoreFieldsTests(SimpleTestCase):
    """`expert_profile.core_fields`: pure, the Expert step's row in, the person out."""

    salon = types.SimpleNamespace(id=SALON, tenant_id=TENANT, branch_id=BRANCH)

    def fields(self, **overrides):
        row = views._stylist_row(stylist())
        row.update(overrides)
        return expert_profile.core_fields(self.salon, row)

    def test_the_person_so_far(self):
        self.assertEqual(self.fields(), {
            "id": str(HERE),
            "salon_id": str(SALON),
            "name": "Liam Johnson",
            "title": "Senior Barber",
            "role": "Barber and Grooming Expert",
            "avatar_url": None,
            "rating": None,
            "review_count": 0,
        })

    def test_the_ids_are_strings(self):
        fields = self.fields()
        self.assertIsInstance(fields["id"], str)
        self.assertIsInstance(fields["salon_id"], str)

    def test_name_title_role_and_avatar_are_the_row_s_own(self):
        fields = self.fields(
            name="Zara Khan", title="Senior Stylist", role="Colour Expert",
            avatar_url="https://x/z.png",
        )
        self.assertEqual(
            [fields[key] for key in PERSON_KEYS],
            ["Zara Khan", "Senior Stylist", "Colour Expert", "https://x/z.png"],
        )

    def test_each_can_be_null(self):
        fields = self.fields(name=None, title=None, role=None, avatar_url=None)
        self.assertEqual([fields[key] for key in PERSON_KEYS], [None] * 4)

    def test_no_reviews_yet_is_null_and_0(self):
        # The contract's types (Q14): rating null = "nobody has rated them",
        # review_count 0 = "No reviews yet". Never the row's own null count.
        fields = self.fields(rating=None, review_count=None)
        self.assertIsNone(fields["rating"])
        self.assertEqual(fields["review_count"], 0)
        self.assertIsInstance(fields["review_count"], int)

    def test_only_the_contract_s_keys(self):
        # The list row also carries tenant_id and branch_id (and, until their
        # own steps, years_experience and day_off): not sent from here.
        self.assertEqual(
            list(self.fields()),
            ["id", "salon_id", "name", "title", "role", "avatar_url", "rating", "review_count"],
        )


class PersonFieldsTests(Seams, SimpleTestCase):
    """The route answers the person."""

    def test_the_answer_is_the_person(self):
        self.assertEqual(self.call().json(), {
            "id": str(HERE),
            "salon_id": str(SALON),
            "name": "Liam Johnson",
            "title": "Senior Barber",
            "role": "Barber and Grooming Expert",
            "avatar_url": None,
            "rating": None,
            "review_count": 0,
            "is_favorite": False,      # E3b
            "service_groups": [],      # E4
        })

    def test_salon_id_is_the_salon_s_id_whatever_case_the_path_used(self):
        response = self.call(url(salon=str(SALON).upper()))
        self.assertEqual(response.json()["salon_id"], str(SALON))


class PersonMatchesTheStylistsRouteTests(SimpleTestCase):
    """
    The same person reads the same on both screens: name, title, role and
    avatar_url here equal that stylist's row of GET /salon/:id/stylists.

    Both routes run for real down to the salon's stylist list; only the salon
    and the list are mocked, and they are the SAME rows for both.
    """

    def setUp(self):
        self.staff = Listed([
            member(1, "Zara", "Khan", "Senior Stylist", "Colour Expert", "https://x/z.png"),
            member(2, "Adam", "Lee", None, "Barber"),                 # no title
            member(3, "Cara", None, "Stylist", None),                 # no role, one name
            member(4, None, None),                                    # no name at all
            member(5, "Eve", "Stone", "", "", None),                  # blanks, not nulls
        ])

    def both(self, staff_id):
        salon = types.SimpleNamespace(id=SALON, tenant_id=TENANT, branch_id=BRANCH)
        with ExitStack() as stack:
            def patch(target, **kw):
                return stack.enter_context(mock.patch(target, **kw))
            patch("apps.salons.views.salon_profile", return_value=salon)
            patch("apps.salons.expert_profile_views.salon_profile", return_value=salon)
            # The one list, for both: the tab iterates it, the profile narrows it.
            patch("apps.salons.views.salon_stylists", return_value=self.staff)
            patch("apps.salons.selectors.salon_stylists", return_value=self.staff)
            # Not what this compares: the profile's menu (E4), empty here.
            patch("apps.salons.expert_profile_views.salon_services", return_value=[])

            listed = self.client.get(f"/api/v1/salon/{SALON}/stylists")
            profile = self.client.get(url(stylist=staff_id))
        self.assertEqual(listed.status_code, 200)
        rows = {row["id"]: row for row in listed.json()["stylists"]}
        return rows, profile

    def test_every_stylist_reads_the_same_on_both(self):
        for one in self.staff:
            with self.subTest(stylist=one.first_name):
                rows, profile = self.both(one.id)
                self.assertEqual(profile.status_code, 200)
                row, person = rows[str(one.id)], profile.json()
                self.assertEqual(person["id"], row["id"])
                for key in PERSON_KEYS:
                    self.assertEqual(person[key], row[key], key)

    def test_the_comparison_is_not_trivial(self):
        rows, profile = self.both(self.staff[0].id)
        self.assertEqual(
            [profile.json()[key] for key in PERSON_KEYS],
            ["Zara Khan", "Senior Stylist", "Colour Expert", "https://x/z.png"],
        )
        _, blanks = self.both(self.staff[4].id)
        # A blank title or role is null, as in the list: never "".
        self.assertEqual(
            [blanks.json()[key] for key in PERSON_KEYS], ["Eve Stone", None, None, None]
        )
        _, nameless = self.both(self.staff[3].id)
        self.assertIsNone(nameless.json()["name"])

    def test_review_count_is_0_here_and_null_in_the_list(self):
        # Decided (Q14): the contract types it a number on this screen. The
        # list row is left as it is.
        rows, profile = self.both(self.staff[0].id)
        self.assertEqual(profile.json()["review_count"], 0)
        self.assertIsNone(rows[str(self.staff[0].id)]["review_count"])
        self.assertIsNone(profile.json()["rating"])
        self.assertIsNone(rows[str(self.staff[0].id)]["rating"])

    def test_a_stylist_who_is_not_in_the_list_is_404_on_the_profile(self):
        _, profile = self.both(uuid.UUID(int=99))
        self.assertEqual(profile.status_code, 404)
        self.assertEqual(profile.json()["detail"], NO_STYLIST)


# ---------------------------------------------------------------------------
# E3b: is_favorite, and who a heart may be saved on
# ---------------------------------------------------------------------------

class IsFavoriteFieldTests(Seams, SimpleTestCase):
    """The route answers the heart the selector reads."""

    def test_false_when_the_selector_says_so(self):
        self.assertIs(self.call().json()["is_favorite"], False)

    def test_true_when_the_selector_says_so(self):
        self.assertIs(self.call(favourite=True).json()["is_favorite"], True)

    def test_the_selector_is_asked_about_this_caller_and_this_stylist(self):
        self.call()
        (user, staff_id), _ = self.favourite_lookup.call_args
        self.assertEqual(staff_id, HERE)
        # No token: the caller is a guest.
        self.assertFalse(user.is_authenticated)

    def test_a_signed_in_caller_is_the_one_asked_about(self):
        customer = types.SimpleNamespace(is_authenticated=True)
        request = APIRequestFactory().get(url())
        force_authenticate(request, user=customer)
        with ExitStack() as stack:
            self.seams(stack, favourite=True)
            response = SalonExpertProfileView.as_view()(
                request, salon_id=str(SALON), stylist_id=str(HERE),
            )
        self.assertIs(response.data["is_favorite"], True)
        self.assertIs(self.favourite_lookup.call_args.args[0], customer)

    def test_a_404_never_asks(self):
        self.call(found=False)
        self.favourite_lookup.assert_not_called()


class IsFavouriteStylistTests(SimpleTestCase):
    """`selectors.is_favourite_stylist` for a guest: false, and no query."""

    def test_no_user_and_a_guest_are_false_without_the_table(self):
        # SimpleTestCase refuses any database access, so a query would fail.
        guest = types.SimpleNamespace(is_authenticated=False)
        self.assertIs(selectors.is_favourite_stylist(None, HERE), False)
        self.assertIs(selectors.is_favourite_stylist(guest, HERE), False)

    def test_a_signed_in_customer_is_looked_up_by_account_and_stylist(self):
        customer = types.SimpleNamespace(is_authenticated=True)
        with mock.patch.object(selectors, "FavouriteStylist") as table:
            table.objects.filter.return_value.exists.return_value = True
            answer = selectors.is_favourite_stylist(customer, HERE)
        table.objects.filter.assert_called_once_with(account=customer, staff_id=HERE)
        self.assertIs(answer, True)


class ShowableStylistsTests(SimpleTestCase):
    """
    `selectors.showable_stylists`: a stylist the app can show at SOME salon.
    The salon_stylists rules without the salon. A heart may be saved on these
    (`stylist_is_showable`), and `salon_stylists` is built on them.
    """

    RULES = {
        ("employment_status", "exact", "ACTIVE"),
        ("onboarding_state", "exact", "ACTIVE"),
        ("deleted_at", "isnull", True),
        ("branch_id", "isnull", False),
    }
    LOGIN = {
        ("id", "exact", ("outer", "user_id")),
        ("tenant_id", "exact", ("outer", "tenant_id")),
        ("deleted_at", "isnull", True),
    }

    def test_employed_joined_not_deleted_with_a_home_branch(self):
        self.assertEqual(conditions(selectors.showable_stylists().query), self.RULES)

    def test_with_a_live_login(self):
        self.assertEqual(conditions(login_check(selectors.showable_stylists())), self.LOGIN)

    def test_no_salon_in_the_rule(self):
        columns = {column for column, _, _ in conditions(selectors.showable_stylists().query)}
        self.assertNotIn("tenant_id", columns)
        self.assertNotIn(("branch_id", "exact"), {
            (column, lookup) for column, lookup, _ in conditions(selectors.showable_stylists().query)
        })

    def test_a_salon_s_list_is_those_rules_plus_the_salon(self):
        salon = types.SimpleNamespace(id=SALON, tenant_id=TENANT, branch_id=BRANCH)
        listed = selectors.salon_stylists(salon)
        self.assertEqual(
            conditions(listed.query),
            self.RULES | {("tenant_id", "exact", TENANT), ("branch_id", "exact", BRANCH)},
        )
        self.assertEqual(conditions(login_check(listed)), self.LOGIN)

    def query(self, found):
        """`stylist_is_showable`'s real query, caught where it would run."""
        with mock.patch.object(QuerySet, "exists", autospec=True, return_value=found) as exists:
            answer = selectors.stylist_is_showable(HERE)
        (queryset,), _ = exists.call_args
        return answer, queryset

    def test_one_stylist_is_checked_by_id_against_the_same_rules(self):
        answer, queryset = self.query(True)
        self.assertIs(answer, True)
        self.assertEqual(conditions(queryset.query), self.RULES | {("id", "exact", HERE)})
        self.assertEqual(conditions(login_check(queryset)), self.LOGIN)

    def test_nobody_found_is_false(self):
        answer, _ = self.query(False)
        self.assertIs(answer, False)


# ---------------------------------------------------------------------------
# E4: service_groups. The menu, its categories, its stages and who holds what.
# ---------------------------------------------------------------------------

HAIR = uuid.UUID("77777777-7777-7777-7777-777777777701")      # a parent: the chip
CUTS = uuid.UUID("77777777-7777-7777-7777-777777777702")
COLOUR = uuid.UUID("77777777-7777-7777-7777-777777777703")
BEARD = uuid.UUID("77777777-7777-7777-7777-777777777704")

MENU_CATEGORIES = {
    HAIR: {"id": HAIR, "name_en": "Hair", "slug": None, "icon": "scissors",
           "parent_id": None, "sort_order": 0},
    CUTS: {"id": CUTS, "name_en": "Precision Cuts", "slug": None, "icon": None,
           "parent_id": HAIR, "sort_order": 1},
    COLOUR: {"id": COLOUR, "name_en": "Colour", "slug": None, "icon": None,
             "parent_id": HAIR, "sort_order": 2},
    BEARD: {"id": BEARD, "name_en": "Beard", "slug": None, "icon": "beard",
            "parent_id": None, "sort_order": 3},
}


def menu_service(number, name, category, price_minor, minutes, description=None, **extra):
    """A service row as `salon_services` gives one (the tab's own list)."""
    return types.SimpleNamespace(
        id=uuid.UUID(int=0x5E00 + number), tenant_id=TENANT, name=name,
        description=description, price_minor=price_minor, duration_minutes=minutes,
        category_0_id=category, **extra,
    )


# In the tab's order (by name). Groups, first seen: Precision Cuts, Beard,
# Colour, Other.
BUZZ = menu_service(1, "A Buzz Cut", CUTS, 12000, 30, "Clippers all over.")
TRIM = menu_service(2, "B Beard Trim", BEARD, 8050, 20)
TINT = menu_service(3, "C Colour", COLOUR, 30000, 90, branch_price_minor=25000)
LOOSE = menu_service(4, "D Loose", None, 5000, 15)
FADE = menu_service(5, "E Fade", CUTS, 15000, 45, branch_price_minor=0)
BARE = menu_service(6, "F No Stages", BEARD, 4000, 10)
MENU = [BUZZ, TRIM, TINT, LOOSE, FADE, BARE]

CUT_SKILL, CLIPPER_SKILL, BEARD_SKILL, COLOUR_SKILL = (uuid.UUID(int=0xA000 + n) for n in range(4))


def needs(service, skill, level):
    return {"service_id": service.id, "skill_id": skill, "min_level": level}


# F No Stages has none: nobody can do it (the stylists route says 422).
MENU_STAGES = [
    needs(BUZZ, CUT_SKILL, 2), needs(BUZZ, CLIPPER_SKILL, 1),
    needs(TRIM, BEARD_SKILL, 1),
    needs(TINT, COLOUR_SKILL, 3), needs(TINT, CUT_SKILL, 1),
    needs(LOOSE, CUT_SKILL, 1),
    needs(FADE, CUT_SKILL, 4),
]


def tab_line(service, price):
    """One row of GET /salon/<id>/services, as JSON."""
    return {
        "id": str(service.id), "name": service.name, "description": service.description,
        "price": price, "duration_min": service.duration_minutes,
        "duration_max": service.duration_minutes,
    }


class ServicesTabPinnedTests(SimpleTestCase):
    """
    GET /salon/<id>/services, pinned BEFORE its grouping moved to
    menu.service_groups (E4), so the move provably changes nothing on the tab.
    The whole answer, through the real URL, key order included.
    """

    def tab(self, rows=MENU, path=None):
        salon = types.SimpleNamespace(id=SALON, tenant_id=TENANT, branch_id=BRANCH)
        with ExitStack() as stack:
            for name, answer in (("salon_profile", salon),
                                 ("salon_categories", MENU_CATEGORIES),
                                 ("salon_services", list(rows))):
                stack.enter_context(mock.patch.object(views, name, return_value=answer))
            salons = stack.enter_context(mock.patch.object(views, "discoverable_salons"))
            salons.return_value.filter.return_value.first.return_value = salon
            response = self.client.get(path or f"/api/v1/salon/{SALON}/services")
        self.assertEqual(response.status_code, 200)
        return response.json()

    EXPECTED = {
        "service_categories": [
            {"id": "all", "label": "All"},
            {"id": str(HAIR), "label": "Hair", "icon": "scissors"},
            {"id": str(BEARD), "label": "Beard", "icon": "beard"},
            {"id": "other", "label": "Other", "icon": None},
        ],
        "service_groups": [
            {"id": str(CUTS), "category_id": str(HAIR), "name": "Precision Cuts",
             "services": [tab_line(BUZZ, 120.0), tab_line(FADE, 0.0)]},
            {"id": str(BEARD), "category_id": str(BEARD), "name": "Beard",
             "services": [tab_line(TRIM, 80.5), tab_line(BARE, 40.0)]},
            {"id": str(COLOUR), "category_id": str(HAIR), "name": "Colour",
             "services": [tab_line(TINT, 250.0)]},
            {"id": "other", "category_id": "other", "name": "Other",
             "services": [tab_line(LOOSE, 50.0)]},
        ],
    }

    def test_the_whole_answer(self):
        self.assertEqual(self.tab(), self.EXPECTED)

    def test_the_key_order(self):
        body = self.tab()
        self.assertEqual(list(body), ["service_categories", "service_groups"])
        for group in body["service_groups"]:
            self.assertEqual(list(group), ["id", "category_id", "name", "services"])
            for row in group["services"]:
                self.assertEqual(
                    list(row),
                    ["id", "name", "description", "price", "duration_min", "duration_max"],
                )

    def test_an_empty_menu(self):
        self.assertEqual(self.tab(rows=[]), {
            "service_categories": [{"id": "all", "label": "All"}],
            "service_groups": [],
        })

    def test_the_old_services_route_still_filters_the_same_groups_by_chip(self):
        # GET /services?tenant_id=&category_id= reads the tab's answer and
        # keeps the groups of one chip (or one group).
        body = self.tab(path=f"/api/v1/services?tenant_id={TENANT}&category_id={HAIR}")
        self.assertEqual(
            body["service_groups"],
            [self.EXPECTED["service_groups"][0], self.EXPECTED["service_groups"][2]],
        )
        self.assertEqual(body["service_categories"], self.EXPECTED["service_categories"])


def group(category, name, *lines):
    """One of the profile's groups, as JSON: no `category_id`."""
    return {"id": str(category) if category else "other", "name": name, "services": list(lines)}


class MenuGroupingTests(SimpleTestCase):
    """`menu.service_groups` and `menu.service_row`: the tab's own code, moved."""

    def test_the_chips_and_the_groups_of_the_tab(self):
        chips, groups = menu.service_groups(MENU, MENU_CATEGORIES)
        self.assertEqual(
            [chip["id"] for chip in chips], [str(HAIR), str(BEARD), "other"],
        )
        self.assertEqual(
            [(g["id"], g["category_id"], g["name"], [row["name"] for row in g["services"]])
             for g in groups],
            [
                (str(CUTS), str(HAIR), "Precision Cuts", ["A Buzz Cut", "E Fade"]),
                (str(BEARD), str(BEARD), "Beard", ["B Beard Trim", "F No Stages"]),
                (str(COLOUR), str(HAIR), "Colour", ["C Colour"]),
                ("other", "other", "Other", ["D Loose"]),
            ],
        )

    def test_the_row_is_the_tab_s(self):
        row = menu.service_row(TINT)
        self.assertEqual(
            list(row), ["id", "name", "description", "price", "duration_min", "duration_max"],
        )
        # The branch price, as booking-api charges (menu.service_price).
        self.assertEqual(row["price"], 250)
        self.assertEqual(menu.service_row(FADE)["price"], 0)      # 0 means 0
        self.assertEqual(menu.service_row(BUZZ)["price"], 120)    # no branch price

    def test_the_tab_view_s_old_name_is_the_same_function(self):
        self.assertIs(views.SalonServicesView._service, menu.service_row)

    def test_nothing_on_the_menu(self):
        self.assertEqual(menu.service_groups([], MENU_CATEGORIES), ([], []))


class ServiceGroupsRuleTests(SimpleTestCase):
    """`expert_profile.service_groups`: pure, the tab's groups in, this stylist's out."""

    def setUp(self):
        _, self.tab_groups = menu.service_groups(MENU, MENU_CATEGORIES)

    def names(self, covered):
        groups = expert_profile.service_groups(self.tab_groups, covered)
        return [(g["name"], [row["name"] for row in g["services"]]) for g in groups]

    def test_only_the_services_this_stylist_can_do(self):
        self.assertEqual(self.names([BUZZ.id, TINT.id, LOOSE.id, FADE.id]), [
            ("Precision Cuts", ["A Buzz Cut", "E Fade"]),
            ("Colour", ["C Colour"]),
            ("Other", ["D Loose"]),
        ])

    def test_a_group_with_nothing_left_is_dropped(self):
        self.assertEqual(self.names([TRIM.id]), [("Beard", ["B Beard Trim"])])

    def test_the_groups_stay_in_the_tab_s_order(self):
        # Without "A Buzz Cut", Precision Cuts' first service comes after the
        # Beard and Other ones by name. The group still comes first, as on
        # the tab: the customer sees one menu, in one order.
        self.assertEqual(self.names([TRIM.id, LOOSE.id, FADE.id]), [
            ("Precision Cuts", ["E Fade"]),
            ("Beard", ["B Beard Trim"]),
            ("Other", ["D Loose"]),
        ])

    def test_a_group_is_id_name_services(self):
        (only,) = expert_profile.service_groups(self.tab_groups, [TRIM.id])
        self.assertEqual(list(only), ["id", "name", "services"])
        self.assertEqual(only["id"], str(BEARD))

    def test_the_rows_are_the_tab_s_own_rows(self):
        groups = expert_profile.service_groups(self.tab_groups, [BUZZ.id, FADE.id])
        self.assertIs(groups[0]["services"][0], self.tab_groups[0]["services"][0])
        self.assertIs(groups[0]["services"][1], self.tab_groups[0]["services"][1])

    def test_ids_are_matched_as_uuids_or_as_text(self):
        self.assertEqual(self.names([str(TRIM.id)]), self.names([TRIM.id]))

    def test_nothing_covered_is_an_empty_list(self):
        self.assertEqual(expert_profile.service_groups(self.tab_groups, []), [])

    def test_the_tab_s_groups_are_not_changed(self):
        expert_profile.service_groups(self.tab_groups, [TRIM.id])
        self.assertEqual(len(self.tab_groups), 4)
        self.assertEqual(len(self.tab_groups[1]["services"]), 2)
        self.assertIn("category_id", self.tab_groups[1])


class ServiceGroupsFieldTests(Seams, SimpleTestCase):
    """The route answers service_groups, and reads the menu once."""

    def test_the_covered_services_grouped_as_the_tab(self):
        body = self.call(services=MENU, covered=[BUZZ, TINT, LOOSE, FADE]).json()
        self.assertEqual(body["service_groups"], [
            group(CUTS, "Precision Cuts", tab_line(BUZZ, 120.0), tab_line(FADE, 0.0)),
            group(COLOUR, "Colour", tab_line(TINT, 250.0)),
            group(None, "Other", tab_line(LOOSE, 50.0)),
        ])

    def test_the_whole_menu_is_asked_about_in_one_go(self):
        self.call(services=MENU, covered=[TRIM])
        salon = self.salon_lookup.return_value
        ids = [service.id for service in MENU]
        self.services_lookup.assert_called_once_with(salon)
        self.stages_lookup.assert_called_once_with(ids)
        # The Expert step's own question, asked for this one stylist, with the
        # stages read above (no second stage read inside).
        self.coverage_lookup.assert_called_once_with(
            salon, ids, [HERE], stages=self.stages_lookup.return_value,
        )
        self.categories_lookup.assert_called_once_with(TENANT)

    def test_a_stylist_who_can_do_nothing_has_an_empty_list(self):
        body = self.call(services=MENU, covered=[]).json()
        self.assertEqual(body["service_groups"], [])
        self.categories_lookup.assert_not_called()

    def test_an_empty_menu_is_an_empty_list_and_nothing_more_is_read(self):
        body = self.call(services=[]).json()
        self.assertEqual(body["service_groups"], [])
        self.stages_lookup.assert_not_called()
        self.coverage_lookup.assert_not_called()
        self.categories_lookup.assert_not_called()

    def test_a_404_never_reads_the_menu(self):
        self.call(found=False, services=MENU)
        self.services_lookup.assert_not_called()


# Who holds what. The stage levels are 1 TRAINEE, 2 JUNIOR, 3 SENIOR, 4 MASTER.
ZARA = member(11, "Zara", "Khan")     # cuts at any level, clippers, colour
ADAM = member(12, "Adam", "Lee")      # junior cuts, clippers, beards
EVE = member(13, "Eve", "Stone")      # master cuts and beards, no clippers
NOAH = member(14, "Noah", "Reed")     # no skill at all
TEAM = [ZARA, ADAM, EVE, NOAH]

HELD = [
    {"staff_member_id": who.id, "skill_id": skill, "level": level}
    for who, skill, level in (
        (ZARA, CUT_SKILL, "MASTER"), (ZARA, CLIPPER_SKILL, "JUNIOR"), (ZARA, COLOUR_SKILL, "SENIOR"),
        (ADAM, CUT_SKILL, "JUNIOR"), (ADAM, CLIPPER_SKILL, "TRAINEE"), (ADAM, BEARD_SKILL, "TRAINEE"),
        (EVE, CUT_SKILL, "MASTER"), (EVE, BEARD_SKILL, "TRAINEE"),
    )
]

# Worked out by hand from MENU_STAGES and HELD.
CAN_DO = {
    ZARA.id: [BUZZ, TINT, LOOSE, FADE],
    ADAM.id: [BUZZ, TRIM, LOOSE],
    EVE.id: [TRIM, LOOSE, FADE],
    NOAH.id: [],
}


class RealSkillRules:
    """
    The tab, the stylists route and the profile, all running for real down to
    the skill rules (skills.py). Only the rows at the edges are mocked, and
    they are the SAME rows for all three: the salon, the menu, its stages, the
    stylist list, how a stage's skill resolves, who holds what.
    """

    def edges(self, stack):
        salon = types.SimpleNamespace(id=SALON, tenant_id=TENANT, branch_id=BRANCH)
        menu_ids = {service.id for service in MENU}

        def stages_of(ids):
            return [row for row in MENU_STAGES if row["service_id"] in set(ids)]

        def patch(target, **kw):
            return stack.enter_context(mock.patch(target, **kw))

        # The tab and the stylists route.
        patch("apps.salons.views.salon_profile", return_value=salon)
        patch("apps.salons.views.salon_categories", return_value=MENU_CATEGORIES)
        patch("apps.salons.views.salon_services", return_value=list(MENU))
        patch("apps.salons.views.salon_service_ids",
              side_effect=lambda _salon, ids: {i for i in ids if i in menu_ids})
        patch("apps.salons.views.service_stage_rows", side_effect=stages_of)
        patch("apps.salons.views.salon_stylists", return_value=Listed(TEAM))
        # The profile.
        patch("apps.salons.expert_profile_views.salon_profile", return_value=salon)
        patch("apps.salons.expert_profile_views.salon_categories", return_value=MENU_CATEGORIES)
        patch("apps.salons.expert_profile_views.salon_services", return_value=list(MENU))
        patch("apps.salons.expert_profile_views.service_stage_rows", side_effect=stages_of)
        patch("apps.salons.expert_profile_views.is_favourite_stylist", return_value=False)
        patch("apps.salons.selectors.salon_stylists", return_value=Listed(TEAM))
        # Shared by both, under stylist_service_coverage.
        patch("apps.salons.selectors.skill_bridge",
              side_effect=lambda _tenant, ids: {i: i for i in ids})
        patch("apps.salons.selectors.staff_skill_rows",
              side_effect=lambda _tenant, staff: [r for r in HELD if r["staff_member_id"] in set(staff)])

    def profile_groups(self, who):
        with ExitStack() as stack:
            self.edges(stack)
            response = self.client.get(url(stylist=who.id))
        self.assertEqual(response.status_code, 200)
        return response.json()["service_groups"]

    def tab(self):
        with ExitStack() as stack:
            self.edges(stack)
            return self.client.get(f"/api/v1/salon/{SALON}/services").json()

    def expert_step(self, service):
        """The stylist ids GET /salon/:id/stylists?service_ids={service} lists."""
        with ExitStack() as stack:
            self.edges(stack)
            response = self.client.get(f"/api/v1/salon/{SALON}/stylists?service_ids={service.id}")
        if response.status_code == 422:
            # A service with no stages: the route refuses to name anyone.
            self.assertEqual(response.json()["errors"][0]["code"], "service_without_skill")
            return set()
        self.assertEqual(response.status_code, 200)
        return {row["id"] for row in response.json()["stylists"]}


class ServiceGroupsAgreeWithTheExpertStepTests(RealSkillRules, SimpleTestCase):
    """
    Their section 3: "the same filter /salon/:id/stylists?service_ids= applies,
    in reverse". A service is in a stylist's service_groups exactly when that
    stylist is in the Expert step's list for that service.
    """

    def test_every_stylist_and_every_menu_service_agree(self):
        listed = {service.id: self.expert_step(service) for service in MENU}
        for who in TEAM:
            mine = {row["id"] for g in self.profile_groups(who) for row in g["services"]}
            for service in MENU:
                with self.subTest(stylist=who.first_name, service=service.name):
                    self.assertEqual(
                        str(service.id) in mine,
                        str(who.id) in listed[service.id],
                    )

    def test_the_agreement_is_not_trivial(self):
        # Each stylist gets their own, different, hand-worked list.
        for who in TEAM:
            with self.subTest(stylist=who.first_name):
                mine = [row["id"] for g in self.profile_groups(who) for row in g["services"]]
                self.assertCountEqual(mine, [str(s.id) for s in CAN_DO[who.id]])
        self.assertEqual(self.expert_step(FADE), {str(ZARA.id), str(EVE.id)})

    def test_a_service_with_no_stages_is_in_nobody_s_list(self):
        self.assertEqual(self.expert_step(BARE), set())
        for who in TEAM:
            with self.subTest(stylist=who.first_name):
                mine = {row["id"] for g in self.profile_groups(who) for row in g["services"]}
                self.assertNotIn(str(BARE.id), mine)

    def test_a_stylist_with_no_skills_has_an_empty_list(self):
        self.assertEqual(self.profile_groups(NOAH), [])


class ServiceGroupsMatchTheTabTests(RealSkillRules, SimpleTestCase):
    """The same menu on both screens: groups, order, rows and prices."""

    def test_every_row_is_the_tab_s_row_for_that_service(self):
        tab_rows = {row["id"]: row for g in self.tab()["service_groups"] for row in g["services"]}
        for who in TEAM:
            for g in self.profile_groups(who):
                for row in g["services"]:
                    with self.subTest(stylist=who.first_name, service=row["name"]):
                        self.assertEqual(row, tab_rows[row["id"]])
                        self.assertEqual(list(row), list(tab_rows[row["id"]]))

    def test_the_branch_price_is_the_one_shown(self):
        (colour,) = [g for g in self.profile_groups(ZARA) if g["name"] == "Colour"]
        self.assertEqual(colour["services"][0]["price"], 250.0)       # not the base 300
        cuts = self.profile_groups(ZARA)[0]
        self.assertEqual([row["price"] for row in cuts["services"]], [120.0, 0.0])   # 0 means 0

    def test_each_group_is_the_tab_s_group_without_category_id(self):
        tab_groups = {g["id"]: g for g in self.tab()["service_groups"]}
        for who in TEAM:
            for g in self.profile_groups(who):
                with self.subTest(stylist=who.first_name, group=g["name"]):
                    self.assertEqual(list(g), ["id", "name", "services"])
                    self.assertEqual(g["name"], tab_groups[g["id"]]["name"])
                    self.assertTrue(g["services"])          # never an empty group

    def test_the_groups_come_in_the_tab_s_order(self):
        tab_order = [g["id"] for g in self.tab()["service_groups"]]
        for who in TEAM:
            with self.subTest(stylist=who.first_name):
                mine = [g["id"] for g in self.profile_groups(who)]
                self.assertEqual(mine, [gid for gid in tab_order if gid in mine])
        # Eve cannot do "A Buzz Cut", the tab's first row: her Precision Cuts
        # group still comes before Beard.
        self.assertEqual(
            [g["name"] for g in self.profile_groups(EVE)], ["Precision Cuts", "Beard", "Other"],
        )

    def test_inside_a_group_the_tab_s_order(self):
        self.assertEqual(
            [row["name"] for row in self.profile_groups(ZARA)[0]["services"]],
            ["A Buzz Cut", "E Fade"],
        )


class ServiceGroupsOpenApiTests(Seams, SimpleTestCase):
    """The 200's shape names the groups and their rows."""

    def shape(self):
        schema = SchemaGenerator().get_schema(request=None, public=True)
        operation = schema["paths"]["/api/v1/salon/{salon_id}/stylist/{stylist_id}"]["get"]
        content = operation["responses"]["200"]["content"]["application/json"]
        return content["schema"]["properties"]["service_groups"], content["examples"]

    def test_a_group_and_a_row_have_exactly_the_answer_s_keys(self):
        groups, _ = self.shape()
        (one,) = self.call(services=MENU, covered=[TRIM]).json()["service_groups"]
        self.assertEqual(list(groups["items"]["properties"]), list(one))
        rows = groups["items"]["properties"]["services"]
        self.assertEqual(list(rows["items"]["properties"]), list(one["services"][0]))

    def test_the_example_has_a_group(self):
        _, examples = self.shape()
        (one,) = examples["AStylistOfThisSalon"]["value"]["service_groups"][:1]
        self.assertEqual(list(one), ["id", "name", "services"])
