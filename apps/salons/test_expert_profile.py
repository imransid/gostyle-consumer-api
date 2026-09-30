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


class Seams:
    """
    The view with every selector it reads mocked. A test passes only what it
    cares about. Each mock is kept on self (salon_lookup, stylist_lookup) for
    call checks.
    """

    def seams(self, stack, *, salon=True, found=True):
        found_salon = (
            types.SimpleNamespace(id=SALON, tenant_id=TENANT, branch_id=BRANCH) if salon else None
        )
        answers = {
            "salon_profile": found_salon,
            "stylist_for_salon": stylist() if found else None,
        }
        mocks = {
            name: stack.enter_context(mock.patch.object(
                expert_profile_views, name, return_value=answer,
            ))
            for name, answer in answers.items()
        }
        self.salon_lookup = mocks["salon_profile"]
        self.stylist_lookup = mocks["stylist_for_salon"]

    def call(self, path=None, *, salon=True, found=True, **headers):
        with ExitStack() as stack:
            self.seams(stack, salon=salon, found=found)
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
