"""
Who can perform which service.

The rule the Expert step asks about lives in two platform modules that were
designed apart:

    service ──service_stage.skill_id──▶ skill           (tenant's own catalog)
                                   or ▶ catalog_skill   (old stages only)
    stylist ──staff_skill_assignment──▶ skill           (tenant's own catalog)

Since the platform migration 20260918100000_service_stage_skill_is_tenant_scoped
a stage names one of the salon's OWN skills. Stages written before it hold a
platform-wide catalog_skill id, and nothing joins that table to the salon's
skills, so those are bridged by `code`, case- and space-insensitively.

The platform's own answer to this question
(`packages/nest-staff/.../service-eligibility.handler.ts`, commit 3b6fa171)
does exactly that, and this module mirrors it step for step. Two services
answering the same question differently is worse than either answer being
wrong, so when that handler changes, change this with it.

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

    A retired tenant skill (`deleted_at` set) never matches: nobody can be
    assigned it any more. The platform bridges only to live skills too.
    """
    by_code = {}
    for row in tenant_rows:
        if row.get("deleted_at") is not None:
            continue
        code = normalize_code(row.get("code"))
        # First writer wins: `skill.code` is not unique per tenant, and
        # silently preferring the last row read would make the answer depend
        # on row order. (The platform builds a Map, so its last row wins; it
        # does not order the rows either, so no choice here can match it
        # exactly. Two live skills with one code is a data problem to fix
        # on the platform.)
        if code is not None and code not in by_code:
            by_code[code] = row["id"]

    resolved = {}
    for row in catalog_rows:
        code = normalize_code(row.get("code"))
        resolved[row["id"]] = by_code.get(code) if code else None
    return resolved


def resolve(stage_skill_ids, tenant_rows, catalog_rows):
    """
    A stage's skill id → the tenant skill that satisfies it, or None when
    nothing can.

    Mirrors the platform's service-eligibility handler, in the same order:

      1. One of the salon's own skills, by id. A retired one (`deleted_at`
         set) is None: it still names the requirement, but nobody can hold it.
      2. Else an old catalog_skill id, bridged by code to a live tenant skill
         (`bridge`), or None when the salon has no skill with that code.
      3. Else None: another salon's skill, or a row deleted outright.

    EVERY id comes back as a key. Before this, an id found in neither place
    was dropped, and a dropped stage quietly widened the list of stylists who
    "can do" the service (or, when it was the only stage, emptied it). The
    platform reports it as a requirement nobody can meet, and so does this.

    `tenant_rows` must include the retired skills (`id`, `code`,
    `deleted_at`), as the platform loads them, or step 1 cannot see them.
    """
    own = {row["id"]: row for row in tenant_rows}
    by_catalog = bridge(catalog_rows, tenant_rows)

    resolved = {}
    for skill_id in stage_skill_ids:
        if skill_id in own:
            retired = own[skill_id].get("deleted_at") is not None
            resolved[skill_id] = None if retired else skill_id
        elif skill_id in by_catalog:
            resolved[skill_id] = by_catalog[skill_id]
        else:
            resolved[skill_id] = None
    return resolved


def requirements(stage_rows, resolved):
    """
    service id → the skills one person must hold to perform the whole service.

    `resolved` is what `resolve` answers for the stages' skill ids.

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
        skill_id = row["skill_id"]
        if skill_id not in resolved:
            # Unreachable: `resolve` answers for every stage id. The platform
            # skips here too.
            continue

        target = resolved[skill_id]
        # A requirement nothing can satisfy has no tenant id to key on, and
        # two of them are two different requirements, so the stage's own
        # skill id goes in the key rather than a shared None (the platform
        # keys it `unsatisfiable:<id>`).
        key = target if target is not None else ("unsatisfiable", skill_id)
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
        # A tuple key is a requirement nothing can satisfy (see `requirements`):
        # a retired skill, another salon's skill, or an old catalog skill the
        # salon has no skill for. No staff row can ever match it.
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
