"""
Who can perform which service.

The rule the Expert step asks about lives in two platform modules that were
designed apart, so answering it takes a bridge:

    service ──service_stage.skill_id──▶ catalog_skill   (platform-wide, seeded)
    stylist ──staff_skill_assignment──▶ skill           (tenant's own catalog)

Nothing joins those two tables. The platform's own answer to this question
(`packages/nest-staff/.../service-eligibility.handler.ts`) bridges them by
`code`, case- and space-insensitively, and this module mirrors it step for
step. Two services answering the same question differently is worse than
either answer being wrong, so when that handler changes, change this with it.

Everything here is pure: rows in, dicts out, no queryset and no import from
`.selectors`. The matching rules are the part worth testing, and they are
testable without a database the test runner cannot create (every platform
table is `managed = False`).
"""

# The staff proficiency ladder, ascending. The order lives here and in the
# platform's skill-level value object; the database column is a bare enum.
LEVELS = ("TRAINEE", "JUNIOR", "SENIOR", "MASTER")

_RANK = {level: index for index, level in enumerate(LEVELS)}


def rank(level):
    """Position on the ladder, or -1 for a level this build does not know.

    An unknown level never satisfies a requirement. A platform release that
    adds a rung above MASTER would otherwise silently make its holders
    unbookable-or-universal depending on which way an unknown sorted; -1 makes
    the direction a decision rather than an accident.
    """
    return _RANK.get(level, -1)


def stage_level(min_level):
    """
    A stage's numeric requirement (1..5) as a rung of the staff ladder.

    The menu module grades a stage 1..5; the staff module grades a person
    TRAINEE..MASTER, four rungs. 1→TRAINEE, 2→JUNIOR, 3→SENIOR, 4→MASTER, and
    5 clamps to MASTER — the same mapping, and the same clamp, as the
    platform handler. Both scales were invented independently; if the product
    ever defines a real correspondence, this function and the platform's
    `mapStageMinLevel` are the two places to change.
    """
    if min_level is None:
        return LEVELS[0]
    index = min(max(int(min_level) - 1, 0), len(LEVELS) - 1)
    return LEVELS[index]


def normalize_code(code):
    """A skill code as the bridge compares it: trimmed, lowercased, or None."""
    if not code:
        return None
    code = str(code).strip().lower()
    return code or None


def bridge(catalog_rows, tenant_rows):
    """
    catalog_skill id → the tenant's own skill id, or None when nothing matches.

    None is not the same as absent. A requirement that reaches None is a skill
    no one at this salon can possibly hold — the tenant never added it to their
    catalogue — and a service needing it is unsatisfiable rather than open to
    everyone. Keeping the key with a None value is what lets the caller tell
    those two apart.
    """
    by_code = {}
    for row in tenant_rows:
        code = normalize_code(row.get("code"))
        # First writer wins: `skill.code` is not unique per tenant, and
        # silently preferring the last row read would make the answer depend
        # on row order.
        if code is not None and code not in by_code:
            by_code[code] = row["id"]

    resolved = {}
    for row in catalog_rows:
        code = normalize_code(row.get("code"))
        resolved[row["id"]] = by_code.get(code) if code else None
    return resolved


def requirements(stage_rows, catalog_to_tenant):
    """
    service id → the skills one person must hold to perform the whole service.

    Each entry is `{tenant_skill_id_or_None: level}`. Stages needing the same
    skill fold into one entry at the highest level any of them asks for: a
    service whose colour stage wants SENIOR and whose wash stage wants TRAINEE
    needs a SENIOR colourist, not two people.

    A service with no stages comes back absent, not empty — "no skills
    attached" is a catalogue gap the caller reports as an error, and an empty
    requirement set would otherwise read as "anyone can do this".
    """
    out = {}
    for row in stage_rows:
        service_id = row["service_id"]
        catalog_id = row["skill_id"]
        if catalog_id not in catalog_to_tenant:
            # A stage pointing at a catalog_skill row that is gone. The FK
            # says this cannot happen; ignoring it matches the platform.
            continue

        target = catalog_to_tenant[catalog_id]
        # An unbridged skill has no tenant id to key on, and two different
        # unbridged skills are two different requirements — hence the
        # catalog id in the key rather than a shared None.
        key = target if target is not None else ("catalog", catalog_id)
        level = stage_level(row.get("min_level"))

        entry = out.setdefault(service_id, {})
        if key not in entry or rank(level) > rank(entry[key]):
            entry[key] = level
    return out


def held_levels(assignment_rows):
    """staff id → {tenant skill id: level}, from staff_skill_assignment rows."""
    out = {}
    for row in assignment_rows:
        out.setdefault(row["staff_member_id"], {})[row["skill_id"]] = row["level"]
    return out


def can_perform(required, held):
    """True when one person holds EVERY skill the service needs, at level.

    Partial coverage is not coverage, and two people never pool skills to
    cover one service between them: this takes one person's skills and one
    service's requirements, and there is no path through it that says
    otherwise.
    """
    for key, level in required.items():
        # A tuple key is an unbridged catalog skill (see `requirements`), which
        # no staff row can ever match: the tenant does not have that skill in
        # their catalogue, so nobody can hold it.
        if isinstance(key, tuple):
            return False
        holder_level = held.get(key)
        if holder_level is None or rank(holder_level) < rank(level):
            return False
    return True


def coverage(service_ids, required_by_service, levels_by_staff):
    """
    staff id → the requested services that person can perform, in the order asked.

    Staff who can perform none are left out entirely, so the result doubles as
    the filter over the stylist list. `service_ids` drives the order so the
    response reads in the order the customer picked, not in dictionary order.
    """
    out = {}
    for staff_id, held in levels_by_staff.items():
        covered = [
            service_id
            for service_id in service_ids
            if service_id in required_by_service
            and can_perform(required_by_service[service_id], held)
        ]
        if covered:
            out[staff_id] = covered
    return out
