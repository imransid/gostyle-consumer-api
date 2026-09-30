-- Expert Profile: why does day_off show for fewer stylists than the live
-- checks counted? READ ONLY.
--
-- One SELECT inside a read-only transaction that ends with ROLLBACK, so it
-- cannot change anything. Run it against the platform database (DB_NAME in
-- customer-api's .env), for example:
--
--   psql -h <DB_HOST> -p <DB_PORT> -U <DB_USER> -d <DB_NAME> \
--        -v ON_ERROR_STOP=1 -f expert_profile_day_off_check.sql
--
-- The answer is one table: salon, stylist, line, value. Paste it back whole.
--
-- For every stylist the app lists at a public salon it shows:
--   * the salon's clock, its roster weeks, its open days (salon rows),
--   * each roster week around the window, with the weekdays that have a
--     shift, and how many of those shifts the code counts,
--   * when the newest of those shifts was written (a roster edited between
--     two readings shows here; a deleted shift leaves no trace),
--   * the day off THE CODE gives (views.days_off_for + roster.py), worked out
--     here with the code's own filters,
--   * the day off section E of expert_profile_live_checks.sql gives.
--
-- The code and section E apply the same rule. They differ in four filters,
-- and each is shown:
--   1. the code counts a shift only when shift.tenant_id is the salon's
--      tenant; section E never looks at it ("on another tenant id"),
--   2. the code's "today" is the salon's own date; section E's is the
--      database's date (the two "roster weeks" on the clock row),
--   3. the code reads the FIRST weekly hours row of a day, and any truthy
--      `closed` closes it; section E takes any row, closed only when `true`,
--   4. the code lists only the salon's own stylists with a live login (E0).
--
-- shift and shift_roster have no status and no deleted flag in the platform's
-- schema (rows are hard-deleted). The last two rows list the columns the live
-- tables really have, in case they differ.

BEGIN TRANSACTION READ ONLY;

-- The platform tables live in `public` (customer-api's own are in `consumer`).
SET LOCAL search_path = public;

WITH salon AS (
    SELECT sf.id AS salon_id,
           sf.tenant_id,
           sf.branch_id,
           COALESCE(NULLIF(sv.snapshot -> 'IDENTITY' ->> 'nameEn', ''), b.name) AS salon_name,
           -- timezones.resolve: the branch's zone, else Asia/Dubai.
           COALESCE(NULLIF(b.timezone, ''), 'Asia/Dubai') AS tz,
           (sv.snapshot -> 'HOURS' -> 'weekly')::jsonb AS weekly
    FROM storefront sf
    LEFT JOIN storefront_version sv ON sv.id = sf.live_version_id
    LEFT JOIN branch b ON b.id = sf.branch_id
    WHERE sf.visibility = 'PUBLIC'
      AND sf.deleted_at IS NULL
),
clock AS (
    SELECT salon_id,
           (NOW() AT TIME ZONE tz)::date                  AS today,        -- the code's
           DATE_TRUNC('week', NOW() AT TIME ZONE tz)::date AS code_monday,
           DATE_TRUNC('week', CURRENT_DATE)::date          AS e_monday      -- section E's
    FROM salon
),
weekday (dow, key, short, name) AS (
    VALUES (1, 'mon', 'Mon', 'Monday'), (2, 'tue', 'Tue', 'Tuesday'),
           (3, 'wed', 'Wed', 'Wednesday'), (4, 'thu', 'Thu', 'Thursday'),
           (5, 'fri', 'Fri', 'Friday'), (6, 'sat', 'Sat', 'Saturday'),
           (7, 'sun', 'Sun', 'Sunday')
),
weekly_row AS (
    SELECT sa.salon_id, t.n, t.w
    FROM salon sa
    CROSS JOIN LATERAL JSONB_ARRAY_ELEMENTS(
        CASE WHEN JSONB_TYPEOF(sa.weekly) = 'array' THEN sa.weekly ELSE '[]'::jsonb END
    ) WITH ORDINALITY AS t(w, n)
    WHERE JSONB_TYPEOF(t.w) = 'object'
),
-- The code's open days (roster.open_weekdays): the first row of the day, and
-- not closed (false, null or missing).
code_open AS (
    SELECT sa.salon_id, wd.dow, wd.short, wd.name
    FROM salon sa
    CROSS JOIN weekday wd
    CROSS JOIN LATERAL (
        SELECT wr.w
        FROM weekly_row wr
        WHERE wr.salon_id = sa.salon_id AND wr.w ->> 'day' = wd.key
        ORDER BY wr.n
        LIMIT 1
    ) first_row
    WHERE COALESCE(first_row.w -> 'closed', 'false'::jsonb) IN ('false'::jsonb, 'null'::jsonb)
),
-- Section E's open days: any row of the day that is not `closed: true`.
e_open AS (
    SELECT DISTINCT wr.salon_id, wd.dow, wd.name
    FROM weekly_row wr
    JOIN weekday wd ON wd.key = wr.w ->> 'day'
    WHERE COALESCE(wr.w -> 'closed', 'false'::jsonb) <> 'true'::jsonb
),
-- Who the app lists at each salon (selectors.salon_stylists, E0).
stylist AS (
    SELECT sa.salon_id,
           sp.id AS staff_id,
           COALESCE(NULLIF(BTRIM(CONCAT_WS(' ', u.first_name, u.last_name)), ''), sp.id::text) AS stylist_name
    FROM salon sa
    JOIN staff_profile sp
      ON sp.tenant_id = sa.tenant_id
     AND sp.branch_id = sa.branch_id
    JOIN user_account u
      ON u.id = sp.user_id
     AND u.tenant_id = sp.tenant_id
     AND u.deleted_at IS NULL
    WHERE sp.employment_status = 'ACTIVE'
      AND sp.onboarding_state = 'ACTIVE'
      AND sp.deleted_at IS NULL
),
-- Every shift of a listed stylist, from one week before the earlier window to
-- one week after the later one, with what each rule makes of it.
seen AS (
    SELECT st.salon_id,
           st.staff_id,
           r.week_start_date,
           sh.shift_date,
           EXTRACT(ISODOW FROM sh.shift_date)::int AS dow,
           GREATEST(sh.created_at, sh.updated_at) AS written_at,
           (r.branch_id = sa.branch_id)  AS at_this_branch,
           (sh.tenant_id = sa.tenant_id) AS shift_tenant_ok,
           (r.week_start_date BETWEEN c.code_monday - 21 AND c.code_monday) AS in_code_window,
           (r.week_start_date BETWEEN c.e_monday - 21 AND c.e_monday)       AS in_e_window
    FROM stylist st
    JOIN salon sa ON sa.salon_id = st.salon_id
    JOIN clock c ON c.salon_id = st.salon_id
    JOIN shift sh ON sh.staff_member_id = st.staff_id
    JOIN shift_roster r ON r.id = sh.roster_id
    WHERE r.week_start_date BETWEEN LEAST(c.code_monday, c.e_monday) - 28
                                AND GREATEST(c.code_monday, c.e_monday) + 7
),
-- What the code counts: this salon's branch, the salon's tenant on the shift,
-- the code's window (selectors.roster_shift_days).
code_shift AS (
    SELECT * FROM seen WHERE at_this_branch AND shift_tenant_ok AND in_code_window
),
-- What section E counts: the stylist's home branch (this salon's), its window.
e_shift AS (
    SELECT * FROM seen WHERE at_this_branch AND in_e_window
),
verdict AS (
    SELECT st.salon_id,
           st.staff_id,
           (SELECT COUNT(*) FROM code_open o WHERE o.salon_id = st.salon_id) AS open_days,
           (SELECT COUNT(DISTINCT x.week_start_date) FROM code_shift x
             WHERE x.salon_id = st.salon_id AND x.staff_id = st.staff_id)    AS code_weeks,
           (SELECT STRING_AGG(o.name, ', ' ORDER BY o.dow) FROM code_open o
             WHERE o.salon_id = st.salon_id
               AND NOT EXISTS (SELECT 1 FROM code_shift x
                                WHERE x.salon_id = st.salon_id AND x.staff_id = st.staff_id
                                  AND x.dow = o.dow))                        AS code_days,
           (SELECT COUNT(*) FROM e_open o WHERE o.salon_id = st.salon_id)    AS e_open_days,
           (SELECT COUNT(DISTINCT x.week_start_date) FROM e_shift x
             WHERE x.salon_id = st.salon_id AND x.staff_id = st.staff_id)    AS e_weeks,
           (SELECT STRING_AGG(o.name, ', ' ORDER BY o.dow) FROM e_open o
             WHERE o.salon_id = st.salon_id
               AND NOT EXISTS (SELECT 1 FROM e_shift x
                                WHERE x.salon_id = st.salon_id AND x.staff_id = st.staff_id
                                  AND x.dow = o.dow))                        AS e_days
    FROM stylist st
),
-- The roster weeks to print for each stylist: the 4 of the code's window
-- always, and any other week around it that has a shift.
week AS (
    SELECT st.salon_id, st.staff_id, (c.code_monday - 7 * n) AS week_start_date
    FROM stylist st
    JOIN clock c ON c.salon_id = st.salon_id
    CROSS JOIN GENERATE_SERIES(0, 3) AS n
    UNION
    SELECT salon_id, staff_id, week_start_date FROM seen
),
answer (salon_id, staff_id, ord, line, value) AS (
    -- The salon: its clock, its weeks, its open days.
    SELECT sa.salon_id, NULL::uuid, 10, 'salon',
           'tenant ' || sa.tenant_id || ', branch ' || sa.branch_id || ', timezone ' || sa.tz
    FROM salon sa
    UNION ALL
    SELECT c.salon_id, NULL, 11, 'roster weeks the code looks at',
           (c.code_monday - 21) || ' to ' || c.code_monday || ' (today on the salon''s clock: ' || c.today || ')'
    FROM clock c
    UNION ALL
    SELECT c.salon_id, NULL, 12, 'roster weeks section E looks at',
           (c.e_monday - 21) || ' to ' || c.e_monday || ' (the database''s date: ' || CURRENT_DATE || ')'
    FROM clock c
    UNION ALL
    SELECT sa.salon_id, NULL, 13, 'open days, as the code reads them',
           COALESCE((SELECT STRING_AGG(o.short, ' ' ORDER BY o.dow) FROM code_open o
                      WHERE o.salon_id = sa.salon_id), 'none: no published hours')
    FROM salon sa
    UNION ALL
    SELECT sa.salon_id, NULL, 14, 'published weekly hours, as stored',
           COALESCE((SELECT STRING_AGG(
                                COALESCE(wr.w ->> 'day', '?') || ' '
                                || CASE WHEN COALESCE(wr.w -> 'closed', 'false'::jsonb) IN ('false'::jsonb, 'null'::jsonb)
                                        THEN COALESCE(wr.w ->> 'open', '?') || '-' || COALESCE(wr.w ->> 'close', '?')
                                        ELSE 'closed=' || (wr.w ->> 'closed') END,
                                ', ' ORDER BY wr.n)
                       FROM weekly_row wr WHERE wr.salon_id = sa.salon_id), 'none')
    FROM salon sa
    UNION ALL
    SELECT sa.salon_id, NULL, 15, 'stylists the app lists here',
           (SELECT COUNT(*) FROM stylist st WHERE st.salon_id = sa.salon_id)::text
    FROM salon sa

    -- Each stylist: each roster week, then the two verdicts.
    UNION ALL
    SELECT w.salon_id, w.staff_id, 20 + (w.week_start_date - c.code_monday + 28),
           'week ' || w.week_start_date
           || CASE WHEN w.week_start_date BETWEEN c.code_monday - 21 AND c.code_monday
                   THEN '' ELSE ' (outside the code''s window)' END
           || CASE WHEN EXTRACT(ISODOW FROM w.week_start_date) = 1
                   THEN '' ELSE ' (does not start on a Monday)' END,
           COALESCE(
               (SELECT STRING_AGG(d.short, ' ' ORDER BY d.dow)
                  FROM (SELECT DISTINCT x.dow, wd.short FROM seen x JOIN weekday wd ON wd.dow = x.dow
                         WHERE x.salon_id = w.salon_id AND x.staff_id = w.staff_id
                           AND x.week_start_date = w.week_start_date) d)
               || ' | ' || s.shifts || ' shifts, the code counts ' || s.counted
               || CASE WHEN s.other_tenant > 0
                       THEN ', ' || s.other_tenant || ' ON ANOTHER TENANT ID' ELSE '' END
               || CASE WHEN s.other_branch > 0
                       THEN ', ' || s.other_branch || ' at another branch' ELSE '' END
               || CASE WHEN s.outside_week > 0
                       THEN ', ' || s.outside_week || ' dated outside this week' ELSE '' END,
               'no shift')
    FROM week w
    JOIN clock c ON c.salon_id = w.salon_id
    LEFT JOIN LATERAL (
        SELECT COUNT(*)                                                         AS shifts,
               COUNT(*) FILTER (WHERE x.at_this_branch AND x.shift_tenant_ok
                                  AND x.in_code_window)                         AS counted,
               COUNT(*) FILTER (WHERE NOT x.shift_tenant_ok)                    AS other_tenant,
               COUNT(*) FILTER (WHERE NOT x.at_this_branch)                     AS other_branch,
               COUNT(*) FILTER (WHERE x.shift_date NOT BETWEEN x.week_start_date
                                                           AND x.week_start_date + 6) AS outside_week
        FROM seen x
        WHERE x.salon_id = w.salon_id AND x.staff_id = w.staff_id
          AND x.week_start_date = w.week_start_date
        HAVING COUNT(*) > 0
    ) s ON TRUE
    UNION ALL
    SELECT v.salon_id, v.staff_id, 89, 'newest shift written in these weeks (UTC)',
           COALESCE((SELECT TO_CHAR(MAX(x.written_at), 'YYYY-MM-DD HH24:MI') FROM seen x
                      WHERE x.salon_id = v.salon_id AND x.staff_id = v.staff_id), 'none')
    FROM verdict v
    UNION ALL
    SELECT v.salon_id, v.staff_id, 90, 'rostered weeks the code counts (needs 2)', v.code_weeks::text
    FROM verdict v
    UNION ALL
    SELECT v.salon_id, v.staff_id, 91, 'DAY OFF, THE CODE',
           CASE WHEN v.open_days = 0 THEN 'null: no published hours'
                WHEN v.code_weeks < 2 THEN 'null: fewer than 2 rostered weeks (' || v.code_weeks || ')'
                WHEN v.code_days IS NULL THEN 'null: no open weekday is free in every rostered week'
                ELSE v.code_days END
    FROM verdict v
    UNION ALL
    SELECT v.salon_id, v.staff_id, 92, 'day off, section E',
           CASE WHEN v.e_open_days = 0 THEN 'null: no published hours'
                WHEN v.e_weeks < 2 THEN 'null: fewer than 2 rostered weeks (' || v.e_weeks || ')'
                WHEN v.e_days IS NULL THEN 'null: no open weekday is free in every rostered week'
                ELSE v.e_days END
    FROM verdict v
)
SELECT salon, stylist, line, value
FROM (
    SELECT sa.salon_name                          AS salon,
           COALESCE(st.stylist_name, '(salon)')   AS stylist,
           a.line,
           a.value,
           sa.salon_name                          AS k1,
           a.salon_id                             AS k2,
           (a.staff_id IS NOT NULL)::int          AS k3,
           COALESCE(st.stylist_name, '')          AS k4,
           a.staff_id                             AS k5,
           a.ord                                  AS k6
    FROM answer a
    JOIN salon sa ON sa.salon_id = a.salon_id
    LEFT JOIN stylist st ON st.salon_id = a.salon_id AND st.staff_id = a.staff_id

    UNION ALL
    -- The tables' real columns: a status or a deleted flag would show here.
    SELECT '(database)', '(table)', 'columns of ' || c.table_name,
           STRING_AGG(c.column_name || ' ' || c.udt_name, ', ' ORDER BY c.ordinal_position),
           '~', NULL, 2, c.table_name, NULL, 0
    FROM information_schema.columns c
    WHERE c.table_schema = 'public'
      AND c.table_name IN ('shift', 'shift_roster')
    GROUP BY c.table_name
) rows
ORDER BY k1, k2, k3, k4, k5, k6;

ROLLBACK;
