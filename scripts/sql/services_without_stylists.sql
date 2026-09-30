-- Services with NO stylist after S0 (the stage skill fix), per salon.
--
-- READ ONLY: it runs inside a read-only transaction and ends with ROLLBACK,
-- so it cannot change anything. Run it against the platform database (the one
-- customer-api reads, DB_NAME in its .env), for example:
--
--   psql -h <DB_HOST> -p <DB_PORT> -U <DB_USER> -d <DB_NAME> \
--        -v ON_ERROR_STOP=1 -f services_without_stylists.sql
--
-- What it copies from customer-api (BRANCH_AVAILABILITY_ENABLED off):
--   salons    = storefront, PUBLIC, not deleted (discoverable_salons)
--   services  = the tenant's services: PUBLISHED, not deleted, online booking
--               on (salon_services)
--   stylists  = the tenant's staff_profile: employment ACTIVE, onboarding
--               ACTIVE, not deleted (salon_stylists)
--   a stylist can do a service when they hold EVERY stage's skill at the
--   stage's level (min_level 1..5 -> TRAINEE, JUNIOR, SENIOR, MASTER, MASTER)
--
-- How a stage's skill_id is read:
--   after  (S0, as the platform does): the salon's own skill by id (retired =
--          nobody), else an old catalog_skill bridged by code to a live salon
--          skill, else nobody.
--   before (today): only a catalog_skill id counts; any other stage is
--          DROPPED (ignored).
--
-- Columns:
--   stylists_before / stylists_after   how many stylists can do it
--   newly_empty   true = S0 is what empties it (before > 0, after = 0)
--   reason        no_stages        : no stages at all. The stylists route
--                                    answers 422 service_without_skill, before
--                                    and after S0 (S0 does not change this)
--                 skill_nobody_has : a stage needs a retired skill, another
--                                    salon's skill, or an old catalog skill the
--                                    salon has no skill for
--                 nobody_qualified : every skill is real, but no active
--                                    stylist holds them all at the level
--
-- To see every service S0 changes (also the ones that GAIN stylists), replace
-- the last WHERE with:  WHERE stylists_before <> stylists_after

BEGIN TRANSACTION READ ONLY;

-- The platform tables live in `public` (customer-api's own are in `consumer`).
SET LOCAL search_path = public;

WITH salon AS (
    SELECT sf.id AS salon_id,
           sf.slug,
           sf.tenant_id,
           COALESCE(sv.snapshot -> 'IDENTITY' ->> 'nameEn', b.name) AS salon_name
    FROM storefront sf
    LEFT JOIN storefront_version sv ON sv.id = sf.live_version_id
    LEFT JOIN branch b ON b.id = sf.branch_id
    WHERE sf.visibility = 'PUBLIC'
      AND sf.deleted_at IS NULL
),
live_service AS (
    SELECT s.id, s.tenant_id, s.name
    FROM service s
    WHERE s.status = 'PUBLISHED'
      AND s.deleted_at IS NULL
      AND s.online_booking_enabled
      AND s.tenant_id IN (SELECT tenant_id FROM salon)
),
stage AS (
    SELECT st.service_id,
           LEAST(GREATEST(COALESCE(st.min_level, 1) - 1, 0), 3) AS need_rank,
           own.id         AS own_id,
           own.deleted_at AS own_deleted_at,
           cs.id          AS catalog_id,
           bridged.id     AS bridged_id
    FROM service_stage st
    JOIN live_service ls ON ls.id = st.service_id
    LEFT JOIN skill own
           ON own.id = st.skill_id
          AND own.tenant_id = ls.tenant_id
    LEFT JOIN catalog_skill cs ON cs.id = st.skill_id
    LEFT JOIN LATERAL (
        -- An old catalog stage: a LIVE salon skill with the same code, case and
        -- spaces ignored. Oldest first if two share a code (rare; a data problem).
        SELECT k.id
        FROM skill k
        WHERE k.tenant_id = ls.tenant_id
          AND k.deleted_at IS NULL
          AND NULLIF(LOWER(BTRIM(k.code)), '') = NULLIF(LOWER(BTRIM(cs.code)), '')
        ORDER BY k.created_at, k.id
        LIMIT 1
    ) bridged ON TRUE
),
req AS (
    -- after: every stage counts. A NULL tenant_skill_id is a requirement
    -- nobody can meet.
    SELECT service_id,
           'after' AS rule,
           need_rank,
           CASE
               WHEN own_id IS NOT NULL THEN
                   CASE WHEN own_deleted_at IS NULL THEN own_id END
               WHEN catalog_id IS NOT NULL THEN bridged_id
           END AS tenant_skill_id
    FROM stage
    UNION ALL
    -- before: only stages on a catalog_skill id; the rest were dropped.
    SELECT service_id, 'before', need_rank, bridged_id
    FROM stage
    WHERE catalog_id IS NOT NULL
),
staff AS (
    SELECT sp.id, sp.tenant_id
    FROM staff_profile sp
    WHERE sp.employment_status = 'ACTIVE'
      AND sp.onboarding_state = 'ACTIVE'
      AND sp.deleted_at IS NULL
),
counted AS (
    SELECT ls.id AS service_id,
           r.rule,
           CASE
               -- No requirement at all (no stages, or every stage dropped):
               -- customer-api shows nobody.
               WHEN NOT EXISTS (
                   SELECT 1 FROM req q
                   WHERE q.service_id = ls.id AND q.rule = r.rule
               ) THEN 0
               -- One requirement nobody can meet empties the service.
               WHEN EXISTS (
                   SELECT 1 FROM req q
                   WHERE q.service_id = ls.id AND q.rule = r.rule
                     AND q.tenant_skill_id IS NULL
               ) THEN 0
               ELSE (
                   SELECT COUNT(*)
                   FROM staff sp
                   WHERE sp.tenant_id = ls.tenant_id
                     AND NOT EXISTS (
                         -- a requirement this stylist does not meet
                         SELECT 1 FROM req q
                         WHERE q.service_id = ls.id AND q.rule = r.rule
                           AND NOT EXISTS (
                               SELECT 1
                               FROM staff_skill_assignment a
                               WHERE a.tenant_id = ls.tenant_id
                                 AND a.staff_member_id = sp.id
                                 AND a.skill_id = q.tenant_skill_id
                                 AND ARRAY_POSITION(
                                         ARRAY['TRAINEE', 'JUNIOR', 'SENIOR', 'MASTER'],
                                         a.level::text
                                     ) - 1 >= q.need_rank
                           )
                     )
               )
           END AS stylists
    FROM live_service ls
    CROSS JOIN (VALUES ('before'), ('after')) AS r(rule)
),
per_service AS (
    SELECT sa.salon_name,
           sa.slug,
           sa.salon_id,
           ls.id   AS service_id,
           ls.name AS service_name,
           (SELECT COUNT(*) FROM service_stage st WHERE st.service_id = ls.id) AS stages,
           MAX(c.stylists) FILTER (WHERE c.rule = 'before') AS stylists_before,
           MAX(c.stylists) FILTER (WHERE c.rule = 'after')  AS stylists_after,
           BOOL_OR(q.rule = 'after' AND q.tenant_skill_id IS NULL) AS has_skill_nobody_has
    FROM salon sa
    JOIN live_service ls ON ls.tenant_id = sa.tenant_id
    JOIN counted c ON c.service_id = ls.id
    LEFT JOIN req q ON q.service_id = ls.id
    GROUP BY sa.salon_name, sa.slug, sa.salon_id, ls.id, ls.name
)
SELECT salon_name,
       slug,
       salon_id,
       service_id,
       service_name,
       stages,
       stylists_before,
       stylists_after,
       stylists_before > 0 AS newly_empty,
       CASE
           WHEN stages = 0 THEN 'no_stages'
           WHEN has_skill_nobody_has THEN 'skill_nobody_has'
           ELSE 'nobody_qualified'
       END AS reason
FROM per_service
WHERE stylists_after = 0
ORDER BY salon_name, slug, service_name;

ROLLBACK;
