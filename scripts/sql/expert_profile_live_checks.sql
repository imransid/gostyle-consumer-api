-- Expert Profile audit: live data checks. READ ONLY.
--
-- One SELECT inside a read-only transaction that ends with ROLLBACK, so it
-- cannot change anything. Run it against the platform database (DB_NAME in
-- customer-api's .env), for example:
--
--   psql -h <DB_HOST> -p <DB_PORT> -U <DB_USER> -d <DB_NAME> \
--        -v ON_ERROR_STOP=1 -f expert_profile_live_checks.sql
--
-- The answer is one table: section, what, value. Paste it back whole.
--
-- "Stylist" = what the app shows today (selectors.salon_stylists): a
-- staff_profile of the salon's TENANT, employment ACTIVE, onboarding ACTIVE,
-- not deleted. "Salon" = a storefront, PUBLIC, not deleted.

BEGIN TRANSACTION READ ONLY;

-- The platform tables live in `public` (customer-api's own are in `consumer`).
SET LOCAL search_path = public;

WITH salon AS (
    SELECT sf.id AS salon_id, sf.tenant_id, sf.branch_id,
           (sv.snapshot -> 'HOURS' -> 'weekly')::jsonb AS weekly
    FROM storefront sf
    LEFT JOIN storefront_version sv ON sv.id = sf.live_version_id
    WHERE sf.visibility = 'PUBLIC'
      AND sf.deleted_at IS NULL
),
-- The weekdays a salon is open (1 = Monday), from its published weekly hours.
-- A salon with no published hours has no row here.
open_day AS (
    SELECT sa.branch_id,
           ARRAY_POSITION(ARRAY['mon', 'tue', 'wed', 'thu', 'fri', 'sat', 'sun'], w ->> 'day') AS dow
    FROM salon sa
    CROSS JOIN LATERAL JSONB_ARRAY_ELEMENTS(
        CASE WHEN JSONB_TYPEOF(sa.weekly) = 'array' THEN sa.weekly ELSE '[]'::jsonb END
    ) AS w
    WHERE JSONB_TYPEOF(w) = 'object'
      AND COALESCE(w -> 'closed', 'false'::jsonb) <> 'true'::jsonb
      AND ARRAY_POSITION(ARRAY['mon', 'tue', 'wed', 'thu', 'fri', 'sat', 'sun'], w ->> 'day') IS NOT NULL
),
stylist AS (
    SELECT sp.id,
           sp.tenant_id,
           sp.branch_id,
           sp.user_id,
           sp.position,
           sp.skills,
           sp.shifts,
           u.id         AS user_found,
           u.deleted_at AS user_deleted_at,
           u.first_name,
           u.last_name,
           u.job_title,
           f.url        AS avatar_url
    FROM staff_profile sp
    LEFT JOIN user_account u ON u.id = sp.user_id
    LEFT JOIN file_item f ON f.id = u.avatar_file_item_id
    WHERE sp.employment_status = 'ACTIVE'
      AND sp.onboarding_state = 'ACTIVE'
      AND sp.deleted_at IS NULL
      AND sp.tenant_id IN (SELECT tenant_id FROM salon)
),
-- Every (salon, stylist) pair the app would open a profile for.
pair AS (
    SELECT sa.salon_id, sa.branch_id AS salon_branch_id, st.id AS staff_id, st.branch_id AS home_branch_id
    FROM salon sa
    JOIN stylist st ON st.tenant_id = sa.tenant_id
),
multi AS (
    SELECT tenant_id
    FROM salon
    GROUP BY tenant_id
    HAVING COUNT(*) > 1
),
-- Photos tagged with a stylist that the app could show: the salon gallery's
-- own rules (GALLERY, public, APPROVED, not deleted).
photo AS (
    SELECT m.staff_id, m.tenant_id
    FROM storefront_media m
    WHERE m.staff_id IS NOT NULL
      AND m.kind = 'GALLERY'
      AND m.is_public
      AND m.moderation_status = 'APPROVED'
      AND m.deleted_at IS NULL
),
photo_count AS (
    SELECT st.id, COUNT(p.staff_id) AS photos
    FROM stylist st
    LEFT JOIN photo p ON p.staff_id = st.id AND p.tenant_id = st.tenant_id
    GROUP BY st.id
),
live_story AS (
    SELECT s.id, s.tenant_id, s.storefront_id, s.created_by_id, m.staff_id AS media_staff_id
    FROM storefront_story s
    LEFT JOIN storefront_media m ON m.id = s.media_id
    WHERE s.deleted_at IS NULL
      AND s.expires_at > (NOW() AT TIME ZONE 'UTC')
),
-- The roster: the 4 whole weeks that end with this week (a roster week starts
-- on Monday).
shift_day AS (
    SELECT sh.staff_member_id,
           r.branch_id,
           r.week_start_date,
           sh.shift_date,
           sh.shift_type::text AS shift_type
    FROM shift sh
    JOIN shift_roster r ON r.id = sh.roster_id
    WHERE r.week_start_date >= DATE_TRUNC('week', CURRENT_DATE)::date - 21
      AND r.week_start_date <= DATE_TRUNC('week', CURRENT_DATE)::date
),
-- Day off, as Q7 reads it: a weekday the stylist's home salon is OPEN, with no
-- shift at that salon in ANY of the weeks they were rostered there.
roster AS (
    SELECT st.id,
           EXISTS (SELECT 1 FROM salon sa WHERE sa.branch_id = st.branch_id)    AS has_home_salon,
           EXISTS (SELECT 1 FROM open_day od WHERE od.branch_id = st.branch_id) AS salon_hours_known,
           (SELECT COUNT(DISTINCT x.week_start_date) FROM shift_day x
             WHERE x.staff_member_id = st.id AND x.branch_id = st.branch_id)    AS weeks_rostered,
           (SELECT COUNT(DISTINCT x.branch_id) FROM shift_day x
             WHERE x.staff_member_id = st.id)                                   AS branches_rostered_at,
           EXISTS (SELECT 1 FROM shift_day x
                    WHERE x.staff_member_id = st.id
                      AND x.branch_id IS DISTINCT FROM st.branch_id)            AS away_from_home_branch,
           (SELECT COUNT(DISTINCT od.dow) FROM open_day od
             WHERE od.branch_id = st.branch_id
               AND NOT EXISTS (
                   SELECT 1 FROM shift_day x
                   WHERE x.staff_member_id = st.id
                     AND x.branch_id = st.branch_id
                     AND EXTRACT(ISODOW FROM x.shift_date)::int = od.dow
               ))                                                               AS steady_days_off
    FROM stylist st
),
-- Who can do which service: the Expert step's rule (skills.py), tenant wide.
live_service AS (
    SELECT s.id, s.tenant_id
    FROM service s
    WHERE s.status = 'PUBLISHED'
      AND s.deleted_at IS NULL
      AND s.online_booking_enabled
      AND s.tenant_id IN (SELECT tenant_id FROM salon)
),
stage AS (
    SELECT sg.service_id,
           LEAST(GREATEST(COALESCE(sg.min_level, 1) - 1, 0), 3) AS need_rank,
           own.id         AS own_id,
           own.deleted_at AS own_deleted_at,
           cs.id          AS catalog_id,
           bridged.id     AS bridged_id
    FROM service_stage sg
    JOIN live_service ls ON ls.id = sg.service_id
    LEFT JOIN skill own ON own.id = sg.skill_id AND own.tenant_id = ls.tenant_id
    LEFT JOIN catalog_skill cs ON cs.id = sg.skill_id
    LEFT JOIN LATERAL (
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
    SELECT service_id,
           need_rank,
           CASE
               WHEN own_id IS NOT NULL THEN CASE WHEN own_deleted_at IS NULL THEN own_id END
               WHEN catalog_id IS NOT NULL THEN bridged_id
           END AS tenant_skill_id
    FROM stage
),
can_do AS (
    SELECT st.id AS staff_id, COUNT(ls.id) AS services
    FROM stylist st
    LEFT JOIN live_service ls
           ON ls.tenant_id = st.tenant_id
          AND EXISTS (SELECT 1 FROM req q WHERE q.service_id = ls.id)
          AND NOT EXISTS (
              SELECT 1 FROM req q
              WHERE q.service_id = ls.id
                AND (
                    q.tenant_skill_id IS NULL
                    OR NOT EXISTS (
                        SELECT 1
                        FROM staff_skill_assignment a
                        WHERE a.tenant_id = st.tenant_id
                          AND a.staff_member_id = st.id
                          AND a.skill_id = q.tenant_skill_id
                          AND ARRAY_POSITION(
                                  ARRAY['TRAINEE', 'JUNIOR', 'SENIOR', 'MASTER'],
                                  a.level::text
                              ) - 1 >= q.need_rank
                    )
                )
          )
    GROUP BY st.id
),
answer (ord, section, what, value) AS (
    -- A. The fields we already send: how many stylists have them
    SELECT 10, 'A stylists', 'public salons', COUNT(*)::text FROM salon
    UNION ALL SELECT 11, 'A stylists', 'stylists the app shows', COUNT(*)::text FROM stylist
    UNION ALL SELECT 12, 'A stylists', 'with a name', COUNT(*)::text FROM stylist
        WHERE NULLIF(BTRIM(CONCAT(first_name, last_name)), '') IS NOT NULL
    UNION ALL SELECT 13, 'A stylists', 'with a title (staff_profile.position)', COUNT(*)::text FROM stylist
        WHERE NULLIF(BTRIM(position), '') IS NOT NULL
    UNION ALL SELECT 14, 'A stylists', 'with a role (user_account.job_title)', COUNT(*)::text FROM stylist
        WHERE NULLIF(BTRIM(job_title), '') IS NOT NULL
    UNION ALL SELECT 15, 'A stylists', 'with an avatar', COUNT(*)::text FROM stylist
        WHERE avatar_url IS NOT NULL
    UNION ALL SELECT 16, 'A stylists', 'login account missing or deleted (we still show them)', COUNT(*)::text FROM stylist
        WHERE user_found IS NULL OR user_deleted_at IS NOT NULL

    -- B. One branch, or the whole business?
    UNION ALL SELECT 20, 'B branch', 'businesses with 2 or more public salons', COUNT(*)::text FROM multi
    UNION ALL SELECT 21, 'B branch', 'stylists in those businesses', COUNT(*)::text FROM stylist
        WHERE tenant_id IN (SELECT tenant_id FROM multi)
    UNION ALL SELECT 22, 'B branch', 'stylists with no home branch (staff_profile.branch_id null)', COUNT(*)::text FROM stylist
        WHERE branch_id IS NULL
    UNION ALL SELECT 23, 'B branch', 'salon + stylist pairs the app shows', COUNT(*)::text FROM pair
    UNION ALL SELECT 24, 'B branch', 'pairs where the stylist home branch is ANOTHER branch', COUNT(*)::text FROM pair
        WHERE home_branch_id IS NOT NULL AND home_branch_id <> salon_branch_id
    UNION ALL SELECT 25, 'B branch', 'pairs where the stylist has no home branch', COUNT(*)::text FROM pair
        WHERE home_branch_id IS NULL

    -- C. The stylist's own photos
    UNION ALL SELECT 30, 'C photos', 'storefront_media rows with a staff_id (any state)', COUNT(*)::text FROM storefront_media
        WHERE staff_id IS NOT NULL
    UNION ALL SELECT 31, 'C photos', 'of those: GALLERY, public, APPROVED, not deleted', COUNT(*)::text FROM photo
    UNION ALL SELECT 32, 'C photos', 'of those: also processing_state PROCESSED', COUNT(*)::text FROM storefront_media
        WHERE staff_id IS NOT NULL AND kind = 'GALLERY' AND is_public
          AND moderation_status = 'APPROVED' AND deleted_at IS NULL
          AND processing_state = 'PROCESSED'
    UNION ALL SELECT 33, 'C photos', 'stylists with at least one showable photo', COUNT(*)::text FROM photo_count
        WHERE photos > 0
    UNION ALL SELECT 34, 'C photos', 'most showable photos on one stylist', COALESCE(MAX(photos), 0)::text FROM photo_count
    UNION ALL SELECT 35, 'C photos', 'kinds of the tagged rows', COALESCE(STRING_AGG(k, ', ' ORDER BY k), 'none')
        FROM (SELECT kind::text || ' ' || COUNT(*) AS k FROM storefront_media
              WHERE staff_id IS NOT NULL GROUP BY kind) t
    UNION ALL SELECT 36, 'C photos', 'file types of the tagged rows', COALESCE(STRING_AGG(k, ', ' ORDER BY k), 'none')
        FROM (SELECT mime_type || ' ' || COUNT(*) AS k FROM storefront_media
              WHERE staff_id IS NOT NULL GROUP BY mime_type) t
    UNION ALL SELECT 37, 'C photos', 'showable photos with a GALLERY_THUMB rendition', COUNT(*)::text FROM storefront_media m
        WHERE m.staff_id IS NOT NULL AND m.kind = 'GALLERY' AND m.is_public
          AND m.moderation_status = 'APPROVED' AND m.deleted_at IS NULL
          AND m.variants::jsonb @> '[{"label": "GALLERY_THUMB"}]'::jsonb

    -- D. Stories
    UNION ALL SELECT 40, 'D stories', 'live stories (not deleted, not expired)', COUNT(*)::text FROM live_story
    UNION ALL SELECT 41, 'D stories', 'salons with a live story', COUNT(DISTINCT storefront_id)::text FROM live_story
    UNION ALL SELECT 42, 'D stories', 'live stories whose photo is tagged with a stylist', COUNT(*)::text FROM live_story
        WHERE media_staff_id IS NOT NULL
    UNION ALL SELECT 43, 'D stories', 'live stories created by a stylist login', COUNT(*)::text FROM live_story ls
        WHERE EXISTS (SELECT 1 FROM stylist st WHERE st.user_id = ls.created_by_id)

    -- E. Day off, from the roster (the 4 weeks that end with this week)
    UNION ALL SELECT 50, 'E day off', 'stylists whose home branch is a public salon', COUNT(*)::text FROM roster
        WHERE has_home_salon
    UNION ALL SELECT 51, 'E day off', 'of those: the salon has no published hours (day off stays null)', COUNT(*)::text FROM roster
        WHERE has_home_salon AND NOT salon_hours_known
    UNION ALL SELECT 52, 'E day off', 'rostered in fewer than 2 of the 4 weeks (day off stays null)', COUNT(*)::text FROM roster
        WHERE has_home_salon AND salon_hours_known AND weeks_rostered < 2
    UNION ALL SELECT 53, 'E day off', 'of those: not rostered at all', COUNT(*)::text FROM roster
        WHERE has_home_salon AND salon_hours_known AND weeks_rostered = 0
    UNION ALL SELECT 54, 'E day off', '2+ weeks, works every day the salon is open (null)', COUNT(*)::text FROM roster
        WHERE has_home_salon AND salon_hours_known AND weeks_rostered >= 2 AND steady_days_off = 0
    UNION ALL SELECT 55, 'E day off', '2+ weeks, exactly 1 steady day off', COUNT(*)::text FROM roster
        WHERE has_home_salon AND salon_hours_known AND weeks_rostered >= 2 AND steady_days_off = 1
    UNION ALL SELECT 56, 'E day off', '2+ weeks, 2 or more steady days off', COUNT(*)::text FROM roster
        WHERE has_home_salon AND salon_hours_known AND weeks_rostered >= 2 AND steady_days_off > 1
    UNION ALL SELECT 57, 'E day off', 'most steady days off on one stylist', COALESCE(MAX(steady_days_off), 0)::text FROM roster
        WHERE has_home_salon AND salon_hours_known AND weeks_rostered >= 2
    UNION ALL SELECT 58, 'E day off', 'rostered at 2 or more branches', COUNT(*)::text FROM roster
        WHERE branches_rostered_at > 1
    UNION ALL SELECT 59, 'E day off', 'rostered at a branch that is not their home branch', COUNT(*)::text FROM roster
        WHERE away_from_home_branch
    UNION ALL SELECT 60, 'E day off', 'shift types in the 4 weeks', COALESCE(STRING_AGG(k, ', ' ORDER BY k), 'none')
        FROM (SELECT shift_type || ' ' || COUNT(*) AS k FROM shift_day GROUP BY shift_type) t
    UNION ALL SELECT 61, 'E day off', 'staff_profile.shifts JSON filled', COUNT(*)::text FROM stylist
        WHERE shifts::text NOT IN ('[]', '{}', 'null')
    UNION ALL SELECT 62, 'E day off', 'one staff_profile.shifts sample', COALESCE(
        (SELECT LEFT(shifts::text, 200) FROM stylist
          WHERE shifts::text NOT IN ('[]', '{}', 'null') ORDER BY id LIMIT 1), 'none')

    -- F. Price by stylist
    UNION ALL SELECT 70, 'F price', 'service_provider_price rows', COUNT(*)::text FROM service_provider_price
    UNION ALL SELECT 71, 'F price', 'of those: differ from the service price', COUNT(*)::text
        FROM service_provider_price p JOIN service s ON s.id = p.service_id
        WHERE p.price_minor <> s.price_minor

    -- G. Services a stylist can do (their menu on the profile)
    UNION ALL SELECT 80, 'G services', 'stylists who can do NO service (empty menu)', COUNT(*)::text FROM can_do
        WHERE services = 0
    UNION ALL SELECT 81, 'G services', 'fewest / average / most services per stylist',
        COALESCE(MIN(services)::text, '-') || ' / ' || COALESCE(ROUND(AVG(services), 1)::text, '-')
        || ' / ' || COALESCE(MAX(services)::text, '-') FROM can_do
    UNION ALL SELECT 82, 'G services', 'stylists with no skill at all', COUNT(*)::text FROM stylist st
        WHERE NOT EXISTS (SELECT 1 FROM staff_skill_assignment a
                          WHERE a.staff_member_id = st.id AND a.tenant_id = st.tenant_id)
)
SELECT section, what, value
FROM answer
ORDER BY ord;

ROLLBACK;
