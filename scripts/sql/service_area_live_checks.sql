-- Service area alerts: read-only live checks
-- (docs/SERVICE_AREA_ALERTS_FE.md, section 8).
-- Run on the live platform DB. SELECTs in a read-only transaction that ends
-- with ROLLBACK; nothing writes.

BEGIN READ ONLY;

-- A. What Build -> Service Area really saved, per live salon. The alerts read
--    primaryRadiusM or primaryRadius, autoTriggerM or autoTrigger,
--    arrivalRadiusM or arrivalRadius (metres) and homeRadiusKm (km). A ring
--    saved under any OTHER key name is not read: send this output back.
SELECT b.operational_tenant_id AS tenant_id,
       b.salon_name,
       b.status,
       b.accepted_at,
       s.step_key,
       s.payload
FROM build_step s
JOIN build b ON b.id = s.build_id
WHERE (s.step_key = 'service-area' OR (s.step_key IS NULL AND s.step_no = 11))
  AND b.operational_tenant_id IS NOT NULL
ORDER BY b.accepted_at DESC NULLS LAST;

-- B. Every key name ever saved in that step, with how many builds use it.
SELECT k.key, count(*) AS builds
FROM build_step s
CROSS JOIN LATERAL jsonb_object_keys(
  CASE WHEN jsonb_typeof(s.payload::jsonb) = 'object' THEN s.payload::jsonb ELSE '{}'::jsonb END
) AS k(key)
WHERE s.step_key = 'service-area' OR (s.step_key IS NULL AND s.step_no = 11)
GROUP BY k.key
ORDER BY builds DESC, k.key;

-- C. Live salons whose branch has no map pin: no alert can ever fire there.
SELECT sf.id AS salon_id, br.id AS branch_id, br.name, br.lat, br.lng
FROM storefront sf
JOIN branch br ON br.id = sf.branch_id
WHERE sf.deleted_at IS NULL
  AND br.deleted_at IS NULL
  AND (br.lat IS NULL OR br.lng IS NULL OR (br.lat = 0 AND br.lng = 0));

ROLLBACK;
