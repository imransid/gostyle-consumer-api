"""
Which skill a service stage needs, now that a stage can name a salon's OWN
skill (platform migration 20260918100000_service_stage_skill_is_tenant_scoped).

Mirrors the platform's service-eligibility handler (commit 3b6fa171): a salon
skill by id first (a retired one: nobody), then an old catalog_skill bridged
by code, then nobody. Before this fix a stage naming a salon skill was DROPPED,
which showed too many stylists for a service with other stages, and none for
a service with only that stage.

SimpleTestCase: the platform tables are unmanaged, so the ORM is mocked where
a selector is under test.
"""

import uuid
from unittest import mock

from django.test import SimpleTestCase

from apps.salons import selectors, skills


def _id():
    return uuid.uuid4()


class ResolveTests(SimpleTestCase):
    """`skills.resolve`: stage skill id to the tenant skill that satisfies it."""

    def test_a_salon_skill_id_resolves_to_itself(self):
        cut = _id()
        resolved = skills.resolve(
            [cut], [{"id": cut, "code": "CUT", "deleted_at": None}], []
        )
        self.assertEqual(resolved, {cut: cut})

    def test_a_salon_skill_with_no_code_still_resolves_by_id(self):
        # A skill added through POST /v1/skills may have no code; by id it
        # needs none.
        own = _id()
        resolved = skills.resolve(
            [own], [{"id": own, "code": None, "deleted_at": None}], []
        )
        self.assertEqual(resolved, {own: own})

    def test_a_retired_salon_skill_is_a_requirement_nobody_can_meet(self):
        retired = _id()
        resolved = skills.resolve(
            [retired], [{"id": retired, "code": "CUT", "deleted_at": "2026-09-20"}], []
        )
        self.assertEqual(resolved, {retired: None})

    def test_an_old_catalog_id_still_bridges_by_code(self):
        catalog, cut = _id(), _id()
        resolved = skills.resolve(
            [catalog],
            [{"id": cut, "code": " haircut ", "deleted_at": None}],
            [{"id": catalog, "code": "HAIRCUT"}],
        )
        self.assertEqual(resolved, {catalog: cut})

    def test_an_old_catalog_id_never_bridges_to_a_retired_salon_skill(self):
        catalog, retired = _id(), _id()
        resolved = skills.resolve(
            [catalog],
            [{"id": retired, "code": "HAIRCUT", "deleted_at": "2026-09-20"}],
            [{"id": catalog, "code": "HAIRCUT"}],
        )
        self.assertEqual(resolved, {catalog: None})

    def test_an_old_catalog_id_bridges_past_a_retired_twin_to_the_live_one(self):
        catalog, retired, live = _id(), _id(), _id()
        resolved = skills.resolve(
            [catalog],
            [
                {"id": retired, "code": "HAIRCUT", "deleted_at": "2026-09-20"},
                {"id": live, "code": "haircut", "deleted_at": None},
            ],
            [{"id": catalog, "code": "HAIRCUT"}],
        )
        self.assertEqual(resolved, {catalog: live})

    def test_an_id_in_neither_table_is_kept_as_nobody_not_dropped(self):
        """
        Another salon's skill, or a row deleted outright. Before the fix this
        key was missing, and `requirements` skipped the stage.
        """
        stray = _id()
        self.assertEqual(skills.resolve([stray], [], []), {stray: None})

    def test_the_salon_skill_is_looked_up_before_the_catalog(self):
        # The platform checks its own skills first and stops there, even for a
        # retired one; it never falls through to the catalog for that id.
        shared, live = _id(), _id()
        resolved = skills.resolve(
            [shared],
            [
                {"id": shared, "code": "CUT", "deleted_at": "2026-09-20"},
                {"id": live, "code": "CUT", "deleted_at": None},
            ],
            [{"id": shared, "code": "CUT"}],
        )
        self.assertEqual(resolved, {shared: None})

    def test_every_stage_id_comes_back(self):
        own, catalog, stray = _id(), _id(), _id()
        resolved = skills.resolve(
            [own, catalog, stray],
            [{"id": own, "code": "CUT", "deleted_at": None}],
            [{"id": catalog, "code": "NAILS"}],
        )
        self.assertEqual(set(resolved), {own, catalog, stray})


class BridgeRetiredTests(SimpleTestCase):
    """`skills.bridge` now receives retired salon skills too."""

    def test_a_retired_salon_skill_never_matches_by_code(self):
        bridged = skills.bridge(
            [{"id": "cs1", "code": "CUT"}],
            [{"id": "sk1", "code": "CUT", "deleted_at": "2026-09-20"}],
        )
        self.assertEqual(bridged, {"cs1": None})


class WhoCanDoItTests(SimpleTestCase):
    """
    The fix end to end through the pure functions: resolve, requirements,
    coverage. Each test names what the old code answered.
    """

    def test_a_service_whose_only_stage_needs_a_salon_skill_finds_its_stylist(self):
        # Before: the stage was dropped, the service had no requirement entry,
        # and NOBODY could do it.
        own = _id()
        stages = [{"service_id": "svc", "skill_id": own, "min_level": 2}]
        resolved = skills.resolve(
            [own], [{"id": own, "code": None, "deleted_at": None}], []
        )
        required = skills.requirements(stages, resolved)
        self.assertEqual(required, {"svc": {own: "JUNIOR"}})
        self.assertEqual(
            skills.coverage(["svc"], required, {"maya": {own: "SENIOR"}}),
            {"maya": ["svc"]},
        )

    def test_a_mixed_service_no_longer_shows_stylists_without_the_salon_skill(self):
        # Before: only the catalog stage counted, so a stylist with the cut
        # skill alone was shown for a service that also needs colour.
        catalog, cut, colour = _id(), _id(), _id()
        stages = [
            {"service_id": "svc", "skill_id": catalog, "min_level": 1},
            {"service_id": "svc", "skill_id": colour, "min_level": 3},
        ]
        resolved = skills.resolve(
            [catalog, colour],
            [
                {"id": cut, "code": "CUT", "deleted_at": None},
                {"id": colour, "code": None, "deleted_at": None},
            ],
            [{"id": catalog, "code": "CUT"}],
        )
        required = skills.requirements(stages, resolved)
        self.assertEqual(required, {"svc": {cut: "TRAINEE", colour: "SENIOR"}})
        held = {
            "cut_only": {cut: "MASTER"},
            "both": {cut: "TRAINEE", colour: "SENIOR"},
        }
        self.assertEqual(skills.coverage(["svc"], required, held), {"both": ["svc"]})

    def test_a_stage_on_an_unknown_skill_empties_the_service(self):
        # Before: the unknown stage was dropped and everyone with the cut
        # skill was shown. The platform says nobody, and so do we now.
        own, stray = _id(), _id()
        stages = [
            {"service_id": "svc", "skill_id": own, "min_level": 1},
            {"service_id": "svc", "skill_id": stray, "min_level": 1},
        ]
        resolved = skills.resolve(
            [own, stray], [{"id": own, "code": "CUT", "deleted_at": None}], []
        )
        required = skills.requirements(stages, resolved)
        self.assertEqual(
            required, {"svc": {own: "TRAINEE", ("unsatisfiable", stray): "TRAINEE"}}
        )
        self.assertEqual(
            skills.coverage(["svc"], required, {"maya": {own: "MASTER"}}), {}
        )

    def test_two_unsatisfiable_stages_stay_two_requirements(self):
        a, b = _id(), _id()
        stages = [
            {"service_id": "svc", "skill_id": a, "min_level": 1},
            {"service_id": "svc", "skill_id": b, "min_level": 4},
        ]
        required = skills.requirements(stages, skills.resolve([a, b], [], []))
        self.assertEqual(
            required,
            {"svc": {("unsatisfiable", a): "TRAINEE", ("unsatisfiable", b): "MASTER"}},
        )

    def test_a_service_with_no_stages_is_still_absent(self):
        # Unchanged on purpose: the stylists route answers 422
        # service_without_skill for it. (The platform now says "everyone"
        # here; that is a separate decision, not part of this fix.)
        self.assertEqual(skills.requirements([], {}), {})


class SkillBridgeSelectorTests(SimpleTestCase):
    """`selectors.skill_bridge` reads both tables, retired salon skills included."""

    def test_reads_retired_salon_skills_and_answers_for_every_stage_id(self):
        tenant, own, retired, catalog, cut, stray = (_id() for _ in range(6))
        with mock.patch.object(selectors, "Skill") as skill, \
                mock.patch.object(selectors, "CatalogSkill") as catalog_skill:
            skill.objects.filter.return_value.values.return_value = [
                {"id": own, "code": None, "deleted_at": None},
                {"id": retired, "code": "OLD", "deleted_at": "2026-09-20"},
                {"id": cut, "code": "CUT", "deleted_at": None},
            ]
            catalog_skill.objects.filter.return_value.values.return_value = [
                {"id": catalog, "code": "cut"},
            ]

            resolved = selectors.skill_bridge(tenant, {own, retired, catalog, stray})

        # No deleted_at filter: the retired skill has to be seen to be named.
        skill.objects.filter.assert_called_once_with(tenant_id=tenant)
        skill.objects.filter.return_value.values.assert_called_once_with(
            "id", "code", "deleted_at"
        )
        (_, kwargs) = catalog_skill.objects.filter.call_args
        self.assertEqual(set(kwargs["id__in"]), {own, retired, catalog, stray})
        self.assertEqual(
            resolved, {own: own, retired: None, catalog: cut, stray: None}
        )

    def test_no_stage_ids_reads_nothing(self):
        with mock.patch.object(selectors, "Skill") as skill, \
                mock.patch.object(selectors, "CatalogSkill") as catalog_skill:
            self.assertEqual(selectors.skill_bridge(_id(), set()), {})
        skill.objects.filter.assert_not_called()
        catalog_skill.objects.filter.assert_not_called()


class CoverageSelectorTests(SimpleTestCase):
    """
    `selectors.stylist_service_coverage`, the helper behind
    GET /salon/:id/stylists?service_ids= and the group and routine flows.
    """

    def test_a_stylist_holding_the_salon_skill_is_found(self):
        tenant, own, service, maya, noor = (_id() for _ in range(5))
        salon = mock.Mock(tenant_id=tenant)
        stages = [{"service_id": service, "skill_id": own, "min_level": 3}]
        with mock.patch.object(selectors, "Skill") as skill, \
                mock.patch.object(selectors, "CatalogSkill") as catalog_skill, \
                mock.patch.object(selectors, "StaffSkillAssignment") as assignments:
            skill.objects.filter.return_value.values.return_value = [
                {"id": own, "code": None, "deleted_at": None},
            ]
            catalog_skill.objects.filter.return_value.values.return_value = []
            assignments.objects.filter.return_value.values.return_value = [
                {"staff_member_id": maya, "skill_id": own, "level": "SENIOR"},
                {"staff_member_id": noor, "skill_id": own, "level": "JUNIOR"},
            ]

            covered = selectors.stylist_service_coverage(
                salon, [service], [maya, noor], stages=stages
            )

        # Before the fix: {} (the stage was dropped, nobody could do it).
        self.assertEqual(covered, {maya: [service]})
