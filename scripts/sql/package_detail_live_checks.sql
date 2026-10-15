-- Package Details screen: read-only live checks (docs/PACKAGE_DETAIL_AUDIT.md, section 8).
-- Run on the live platform DB. Three SELECTs, nothing writes.

-- A. Every package that is not deleted: how it is set up, and whether the
--    live (published) content differs from the draft row (F2).
SELECT p.tenant_id,
       p.id,
       p.name,
       p.status,
       p.pricing_mode,
       p.price_minor,
       p.discount_bps,
       p.scheduling,
       p.validity_days,
       p.color_theme,
       p.online_booking_enabled,
       p.branch_scoped,
       p.published_version,
       p.published_snapshot IS NOT NULL                                        AS has_snapshot,
       p.published_snapshot IS NOT NULL
         AND p.published_snapshot ->> 'name' IS DISTINCT FROM p.name          AS snap_name_differs,
       p.published_snapshot IS NOT NULL
         AND (p.published_snapshot ->> 'priceMinor')::int
             IS DISTINCT FROM p.price_minor                                     AS snap_price_differs,
       jsonb_array_length(p.published_snapshot -> 'items')                     AS snap_items,
       length(coalesce(p.description, ''))                                     AS description_len,
       length(coalesce(p.cancellation_policy, ''))                             AS policy_len,
       coalesce(array_length(p.highlights, 1), 0)                              AS highlights,
       (SELECT count(*) FROM service_package_item i WHERE i.package_id = p.id) AS items,
       (SELECT count(*) FROM service_package_item i
         WHERE i.package_id = p.id AND i.quantity > 1)                         AS items_qty_over_1,
       (SELECT count(*) FROM service_package_item i
         WHERE i.package_id = p.id AND i.variant_id IS NOT NULL)               AS items_with_variant,
       (SELECT count(*) FROM service_package_item i
          JOIN service s ON s.id = i.service_id
         WHERE i.package_id = p.id
           AND NOT (s.status = 'PUBLISHED' AND s.deleted_at IS NULL
                    AND s.online_booking_enabled))                             AS items_off_menu,
       (SELECT count(*) FROM service_package_branch_availability a
         WHERE a.package_id = p.id)                                            AS branch_rows,
       (SELECT count(*) FROM service_package_branch_availability a
         WHERE a.package_id = p.id AND a.available)                            AS branch_rows_on
  FROM service_package p
 WHERE p.deleted_at IS NULL
 ORDER BY p.tenant_id, p.name;

-- B. Has a package ever been sold at the till? (sale_line.package_id)
SELECT count(*)                   AS sale_lines_with_package,
       count(DISTINCT package_id) AS packages_sold,
       max(created_at)            AS last_sold
  FROM sale_line
 WHERE package_id IS NOT NULL;

-- C. Per salon and published package: on our Packages tab today, and under
--    the platform's branch rule (F1). A row where the two differ is a
--    package the tab will gain (or lose) when F1 is fixed.
WITH pkg AS (
    SELECT st.id   AS salon_id,
           st.slug AS salon,
           p.id    AS package_id,
           p.name  AS package,
           p.branch_scoped,
           p.online_booking_enabled,
           (SELECT a.available FROM service_package_branch_availability a
             WHERE a.package_id = p.id AND a.branch_id = st.branch_id) AS branch_row
      FROM storefront st
      JOIN service_package p ON p.tenant_id = st.tenant_id
     WHERE st.deleted_at IS NULL
       AND p.deleted_at IS NULL
       AND p.status = 'PUBLISHED'
)
SELECT salon,
       package,
       branch_scoped,
       branch_row,
       online_booking_enabled,
       (online_booking_enabled AND branch_row IS TRUE)                 AS on_our_tab_today,
       (online_booking_enabled AND CASE WHEN branch_scoped THEN branch_row IS TRUE
                                        ELSE branch_row IS DISTINCT FROM FALSE END) AS on_platform_rule
  FROM pkg
 ORDER BY salon, package;
