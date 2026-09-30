"""
The Expert Profile screen (docs/expert-profile-fe-contract.md,
docs/EXPERT_PROFILE_AUDIT.md section 8).

E0: who counts as a salon's stylist. One list (selectors.salon_stylists) for
every screen, and the rule is the platform's and booking-api's own: a stylist
whose home branch is this salon's branch, and whose login account is live.

The querysets are built and inspected, never run: the platform's tables are
unmanaged, so they do not exist in the test database. The views are called
with that one list mocked.
"""

import types
import uuid
from unittest import mock

from django.db.models import Exists
from django.test import SimpleTestCase
from rest_framework.exceptions import ValidationError

from apps.salons import group_views, selectors, views

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
