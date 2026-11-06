-- Service Details redesign: read-only live checks
-- (docs/SERVICE_DETAIL_REDESIGN_API.md, server steps).
-- Run on the live platform DB. Three SELECTs in a read-only transaction that
-- ends with ROLLBACK; nothing writes.

BEGIN READ ONLY;

-- A. The lines `included_items` would send: every stage with a description,
--    on a service that is not deleted. The platform calls this text the
--    stage's note, so read them: a line written for staff would show to
--    customers once SERVICE_DETAIL_REDESIGN_V1 is on.
SELECT s.tenant_id,
       s.name                AS service,
       s.status,
       st.sort_order,
       st.name_en            AS stage,
       btrim(st.description) AS line
FROM service_stage st
JOIN service s ON s.id = st.service_id
WHERE s.deleted_at IS NULL
  AND btrim(coalesce(st.description, '')) <> ''
ORDER BY s.tenant_id, s.name, st.sort_order, st.created_at, st.id;

-- B. Photos per service (its own, not deleted), most first: is 10 enough?
--    Tagged gallery photos come on top of these at each salon.
SELECT s.tenant_id,
       s.name,
       count(m.id) AS own_photos
FROM service s
JOIN service_media m ON m.service_id = s.id
                    AND m.tenant_id = s.tenant_id
                    AND m.deleted_at IS NULL
WHERE s.deleted_at IS NULL
GROUP BY s.tenant_id, s.id, s.name
ORDER BY own_photos DESC, s.name
LIMIT 20;

-- C. How `group` will read: services filed under a child category (the path
--    shows "Category > Group"), under a top-level one (the group is the
--    category itself) or under none or a deleted one ("Other").
SELECT CASE
         WHEN c.id IS NULL OR c.deleted_at IS NOT NULL THEN 'other'
         WHEN p.id IS NOT NULL AND p.deleted_at IS NULL THEN 'child category'
         ELSE 'top-level category'
       END      AS filed_under,
       count(*) AS services
FROM service s
LEFT JOIN category c ON c.id = s.category_id
LEFT JOIN category p ON p.id = c.parent_id
WHERE s.deleted_at IS NULL
GROUP BY 1
ORDER BY 1;

ROLLBACK;
