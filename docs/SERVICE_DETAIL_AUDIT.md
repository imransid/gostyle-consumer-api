# Service Detail screen: the app team's contract vs our code (audit)

Date: 2026-09-30. **Audit only: no code was changed.**

The contract: the app team's "Service Detail Screen" (one new route,
`GET /salon/{salon_id}/service/{service_id}`), received 2026-09-30 and saved as
received in `docs/service-detail-fe-contract.md` (never edit it). Section
numbers below (§2, §3, ...) are theirs.

**Status: Q1 to Q15 answered by Rafa (2026-09-30), all as recommended, with one
addition to Q9 (see section 6).** Live checks (Rafa, 2026-09-30): no branch
price differs from the base price, so S7 stays last. S0's impact
(`scripts/sql/services_without_stylists.sql`): exactly one service changes,
Green Wave "Love and thunder", from 0 stylists to 3; no service loses
stylists and none has no stages.

Code read:

- **customer-api**: branch `feat/service-detail`, made from `imransid/main` =
  `5fb4dc6` (that is `c470a89` plus the routine FE guide docs; no code change
  after `c470a89`).
- **booking-api**: `main` = `cb84fe7`. Read only.
- **gostyle-platform**: `ffd6406e` (already merged into its `origin/main` as
  `aa8ea613`). Read only.
- The local Postgres (schema only, no rows) for real column types and enum values.

Tags: **READY** exists, same meaning. **SHAPE** the data exists, needs mapping
or a choice. **MISSING** no data anywhere today.

---

## 0. Short version

1. **One new read route, all in customer-api.** booking-api has no "one
   service" endpoint and does not need one. Nothing in booking-api or the
   platform has to change for a first version.
2. **Field tags (19 fields):** READY 6 (`id`, `salon_id`, `name`,
   `description`, `duration_min`, `duration_max`). SHAPE 10 (`price`,
   `category`, `is_active`, `hero_url`, `gallery`, `gallery_count`,
   `included`, `details`, `preparation`, `experts`). MISSING 3 (`rating`,
   `review_count`, `products`). The contract allows all 3 MISSING ones to be
   empty (`null`, `0`, `[]`).
3. **Price has a real problem already, on the Services tab.** booking-api
   charges the **branch price** (`service_branch_availability.price_minor`,
   else `service.price_minor`), through the platform's gRPC `ListServices`. Our
   Services tab shows only the base price, because `BRANCH_AVAILABILITY_ENABLED`
   is `False`. For a branch with its own price, the app shows one number and the
   create checks another: `amount_mismatch`. Price does **not** change by stylist
   or by date in booking-api (the platform has tables for both, booking-api
   reads neither). See 2.1 and Q1. **Fixed in S7:** every shown service price
   now reads the salon's own branch price with booking-api's rule.
4. **Experts reuse is almost free, but the helper it reuses has drifted from
   the platform.** Since the platform migration of 2026-09-18, a stage's skill
   can be the salon's **own** skill. Our filter (`skills.py`) only knows the 8
   catalog skills and silently drops those stages. See F1 and step S0.
5. **Three rules differ from what Django does today:** a non-UUID path id
   answers Django's **HTML** 404, not our JSON. Every other `/salon/:id/...`
   route is **public**, while the contract asks for a token. And our 404 puts one
   item in `errors`, not `[]`. See section 3.
6. **Two contract assumptions are wrong for our data:** "the same haircut at
   another branch is another id" (no: a service belongs to the tenant, so it has
   one id at every branch), and "`experts: []` means the salon assigns" (no:
   the single booking create refuses a booking with no stylist,
   `stylist_required`). See 3.7.

---

## 1. Field by field

Platform tables are read through `apps/platform_data/models.py` (inspectdb,
`managed = False`). "Tab" = `GET /salon/:id/services` (`SalonServicesView`).

| # | Field | Where it would come from today | Helper that already reads it | Tag | Notes |
|---|---|---|---|---|---|
| 1 | `id` | `service.id` | `salon_services`, `services_by_ids` | READY | Echo of the path id. |
| 2 | `salon_id` | the path id = `storefront.id` | `salon_profile` | READY | Echo. |
| 3 | `name` | `service.name` | Tab `_service`, `ServiceDetailSerializer` | READY | See F3 (edited but not re-approved names show early). |
| 4 | `description` | `service.description` (text, null allowed) | Tab `_service`, `ServiceDetailSerializer` | READY | Same as the tab. `description_ar` also exists (not asked for). |
| 5 | `price` | `service.price_minor`, and per branch `service_branch_availability.price_minor` | Tab `_service` (`branch_price_minor or price_minor`, but the branch part only when `BRANCH_AVAILABILITY_ENABLED`), `money.major` | SHAPE | Tab shows the base price today; booking-api charges the branch price. Before VAT: yes (booking-api adds 5% on top). See 2.1, Q1. |
| 6 | `duration_min` | `service.duration_minutes` (one integer, minutes) | Tab `_service`, `ServiceDetailSerializer` | READY | The only duration there is. See note A. |
| 7 | `duration_max` | same number | same | READY | Equal to `duration_min`, which §2 allows ("fixed length"). |
| 8 | `category` | `service.category_id` then `category.parent_id` (tenant scoped, **UUID** ids) | Tab chips, `ServiceDetailSerializer.get_category` | SHAPE | Ids are UUIDs, never `haircut_styling` (the platform slug, when set, is kebab case and optional). No category: the tab says `other`, services-details says `null`. See 2.4, Q6. |
| 9 | `rating` | nothing per service | none | MISSING | Salon rating is `storefront_review` (per storefront). Per service would have to go review → `booking_id` → `booking_item.service_id` (ambiguous for a booking with 2 services). The platform's own gRPC sends `rating_bps: 0` ("no source on platform yet"). Contract allows `null`. Q12. |
| 10 | `review_count` | nothing per service | none | MISSING | Same. Contract allows `0`. |
| 11 | `is_active` | `service.status = PUBLISHED` and `deleted_at IS NULL` and `online_booking_enabled` | `ServiceDetailSerializer.get_is_active` (same 3 rules as the menu) | SHAPE | Needs a selector that does **not** drop inactive rows (the tab's `salon_services` does). See 3.1, Q3. |
| 12 | `hero_url` | `service_media.url`, primary first (`is_primary`, then `sort_order`, then `created_at`) | `services_by_ids` (its `image_url` annotation) | SHAPE | Plain public S3 URL (`https://{bucket}.s3.{region}.amazonaws.com/tenants/{tenant}/services/{service}/media/...`), no CDN, no signing. Same kind of URL as `cover_url` today. |
| 13 | `gallery` | `service_media` (the service's own photos) and `storefront_media` where `linked_service_id` = the service, `kind = GALLERY`, public, approved, this storefront | none reads either list today | SHAPE | Two sources. Cap 5. See Q7. |
| 14 | `gallery_count` | count of the same rows | none | SHAPE | Counted from the same lists, no extra query. |
| 15 | `included` | `service_stage.name_en`, by `sort_order` | none (we read stages only for skills: `service_stage_rows`) | SHAPE | The platform does exactly this already: its gRPC fills `included_steps` from stage names (`services-directory.grpc.controller.ts:137`). No real "included" field exists. Q8. |
| 16 | `details` | built: a duration row from `duration_minutes`; optional rows from `requires_consultation`, `requires_patch_test` + `patch_test_hours`, `min_age` | none | SHAPE | "Suitability" (hair types) is **MISSING**: no column. Q9. |
| 17 | `preparation` | `service.pre_care_instructions`, `service.post_care_instructions` (text, up to 2000 chars each, live fields, applied at once) | none | SHAPE | Split into bullets by line. Q10. |
| 18 | `products` | no customer-facing link from a service to a product | none | MISSING | `service_stage.products` is free text (not ids). `pos_service_consumable` links service → `product_variant`, but it is the back-bar costing recipe and its own comment says it "never affects what the customer is shown". Contract allows `[]`. Q11. |
| 19 | `experts` | `staff_profile`, `user_account`, `file_item`, filtered by stage skills | `stylist_rows(salon, [id], stages=...)`, the same code as `GET /salon/:id/stylists?service_ids=` | SHAPE | Same list and order. Differences in 2.2. Needs F1 fixed. |

**Note A, duration.** Other durations exist but none is right here:
`service_variant.duration_minutes` (variants: booking-api knows no variants,
so a range from them would show a length nobody can book), and the stage sum
(`buffer_pre + duration + duration_impact + buffer_post`, not kept in sync with
`service.duration_minutes`). booking-api books exactly `duration_minutes`
(`end_time` must equal start + the sum). So the pair is that one number twice,
like the tab.

---

## 2. Reuse notes

### 2.1 `price`

What the Services tab shows today: `major(service.price_minor)`, the base
price. The `branch_price_minor` branch in `_service` is dead code while
`BRANCH_AVAILABILITY_ENABLED = False` (`selectors.py:275`).

What booking-api checks against: the platform gRPC `ListServices(tenant, branch)`
answers `price_minor = service_branch_availability.price_minor ?? service.price_minor`
(`gostyle-platform/apps/gostyle-api/src/services-directory.grpc.controller.ts:126-131`).
The mobile create checks `amount_without_tax` (services' net + products' net),
`tax_amount` and `total` against those figures, ±1 fil. `services[].amount` is
never checked.

**So "matching the Services tab to the fil" and "agreeing with the booking
payload" are two different numbers wherever a branch has its own price.** Today
the tab already has this bug, and the new screen would copy it.

**Fixed in S7.** `selectors.branch_price_minor` reads
`service_branch_availability.price_minor` for the salon's own branch (price
only, the availability filter stays off, F2), and `menu.service_price` applies
booking-api's rule: a set branch price is used (0 means 0), no row or a null
price is the service's own. Every place a service price is shown uses it: the
Services tab, this screen, the group booking's service lines
(`group_views._catalogue`, from `service_timing_rows`), and the Packages tab's
"price before" (the same rule in SQL, `Coalesce`). services-details stays on
the base price: it has no branch (its known gap). Note: booking-api refuses a
price of 0 (`BOOKING_STATE_INVALID`), so a branch price of 0 shows as 0 and
cannot be booked, exactly as booking-api would have it.

Can one service's price differ:

| By | Platform has | booking-api uses it | Verdict |
|---|---|---|---|
| Branch | `service_branch_availability.price_minor` | **yes**, through gRPC | real, see Q1 |
| Stylist | `service_provider_price (service_id, staff_id, price_minor)` | no (the quote takes no stylist) | not today |
| Tier | `service_tier`, `tier_pricing_rule` | no (fixture tier, 0 for real customers) | not today |
| Date / time | `service_pricing_rule` (days, minutes, valid from/to) | no | not today |
| Promo | `promo`, `storefront_offer` | no (`promo_code` echoed, never applied) | not today |
| Variant | `service_variant.price_minor` | no | not today |

The platform's own `compute-effective-price` handler uses the order tier, then
stylist, then branch, then base, then time rules. booking-api follows none of
that except branch. **The one price this screen should send is the one
booking-api will charge: branch price if set, else base price, before VAT.**
If booking-api ever prices by stylist or by date, this single number becomes a
"from" price, and the contract would need a new field.

VAT: booking-api treats `price_minor` as net and adds 5%. It ignores
`service.tax_mode` (INCLUSIVE / EXCLUSIVE). Not ours to fix; noted as F7.

`price_type` (FIXED / FROM / FREE / VARIABLE) exists on `service`. Neither the
tab nor the contract shows it. Not a question now.

JSON: `major()` gives a `Decimal`; in a plain `Response` dict DRF writes it as a
float (`180.0`). Same as the tab. Equal to `180` in JS.

### 2.2 `experts` vs `GET /salon/:id/stylists?service_ids={id}`

Reuse `stylist_rows(salon, [service_id], stages=stages)` (`views.py:403`) as is.
Same code, so same list and same order by construction. Differences from the
contract's §2 example:

1. **Extra keys** we send: `tenant_id`, `branch_id`, `years_experience`,
   `day_off`, `service_ids`. Harmless; the contract says "same shape as the
   stylists endpoint", so keep them.
2. **`rating` is always `null`.** No per-stylist rating exists anywhere (no
   column, no review table with a staff id; the platform's staff gRPC sends it
   empty). Contract example shows `4.8`.
3. **`review_count` is always `null`**, not a number. The contract says "0 when
   none" only for the service's own count. Recommend: keep `null` here, because
   §3 says the list must be the same shape as the stylists endpoint.
4. **Order:** "best first" in the contract; ours is rating desc (all null), then
   name. So **name order** in practice.
5. **`name`** can be `null` (no first and last name). Contract types it string.
6. **`title` / `role`**: same meaning (`staff_profile.position` /
   `user_account.job_title`), both nullable. Same.
7. **`avatar_url`**: `file_item.url`, nullable. Same.
8. **Errors:** the stylists route answers **422** `unknown_service` (service not
   on the menu) and **422** `service_without_skill` (service with no stages).
   The detail screen must not fail for those; it should answer `experts: []`.
   See Q13.
9. **Whole tenant, not the branch:** `salon_stylists` ignores the branch while
   `BRANCH_AVAILABILITY_ENABLED` is off. The platform's eligibility is branch
   scoped. Known, same as the Expert step today.
10. **Skills drift (F1):** stages that name a tenant skill are dropped, so a
    service can show too many experts (one stage dropped) or none (all dropped).
    Same bug in the Expert step today.

### 2.3 `products` vs `GET /salon/:id/products`

Same shape: `id` (product), `variant_id` (the default variant: first
non-deleted by `position`), `name`, `price` (`product_variant.sale_price_minor`,
net), `image_url` (`product.image_url`; variants have no image). The tab lists
`type = RETAIL`, `status = ACTIVE`, price > 0, tenant wide.

`variant_id` is what booking-api wants (`products[].id` is the variant id;
sending the product id answers `unknown_product`). The unit price is checked ±1
fil against the platform's `ListProducts`. So once a service → product link
exists, the answer is `salon_products(salon).filter(id__in=linked_ids)`, and
the numbers agree with the Shop tab by construction. Until then: `[]`.

### 2.4 `category`

The tab groups services by their own category (`service_groups[].id`) and
draws chips by the parent (`service_categories[].id` =
`service_groups[].category_id`). services-details answers the chip (parent).
Reuse that: `{ id: chip uuid, label: chip name_en }`. Only open point is a
service with no category (Q6).

---

## 3. Rules checked against the code

### 3.1 A service pulled from sale answers 200 with `is_active: false`

Today the tab's `salon_services` filters on the three rules, so a pulled service
is simply not there. The new route needs its own lookup (like
`services_by_ids`, which keeps inactive rows) scoped to the salon's tenant.

Everything that can pull a service (platform): status `HIDDEN` or `ARCHIVED`
(also set by the scheduled `hide_from` / `auto_show_after` job),
`deleted_at`, `online_booking_enabled = false`, and per branch
`service_branch_availability.available` / `branch_scoped` /
`category_branch_availability.available`. A `DRAFT` service was never on sale.
Which of these answer 200 and which 404 is Q3.

### 3.2 Another salon's service is 404

A service has **no salon column**; it belongs to a tenant. The check is
`service.tenant_id == salon.tenant_id`. Two branches of one tenant are two
salons (two storefronts) with **one** menu, so the same service id opens at
both. That is correct for our data, but it breaks the contract's sentence "the
same haircut at another branch is another id". Once branch prices are read,
the price can differ between the two answers.

### 3.3 A path id that is not a UUID

Checked locally with the Django test client, `DEBUG = False`:

```
GET /api/v1/salon/not-a-uuid/services
404 text/html  <!doctype html> ... <h1>Not Found</h1> ...
```

The `<uuid:...>` converter does not match, so Django's own HTML 404 answers.
Not our envelope. A valid UUID for an unknown salon does give our JSON:

```
404 {"detail":"Salon not found","code":"not_found",
     "errors":[{"field":null,"code":"not_found","message":"Salon not found"}]}
```

Fix for this route: `<str:...>` in the path and parse the UUID in the view
(Q5).

### 3.4 Auth

| Route | Today |
|---|---|
| `/salon/:id`, `/services`, `/stylists`, `/products`, `/packages`, `/stories` | `AllowAny`: no token = 200 |
| `/services-details` | `IsAuthenticated`: no token = 401 |
| any `AllowAny` route with a **bad or expired** token | **401** anyway (simplejwt checks any token sent) |

The contract wants a token and a 401. Its siblings on the same screen family
are public. Q4.

### 3.5 One call draws the whole screen: queries

Estimate, reading the code paths we would reuse:

| Part | Queries |
|---|---|
| salon (`salon_profile`, one query with subqueries) | 1 |
| the service row (+ category ids, branch price if Q1) | 1 |
| the tenant's categories | 1 |
| `service_media` rows | 1 |
| linked `storefront_media` rows | 1 |
| stages (used for `included` **and** experts) | 1 |
| staff (`salon_stylists`) | 1 |
| skill bridge (catalog skills, tenant skills) | 2 |
| staff skill levels | 1 |
| products, rating | 0 (empty for now) |
| **Total** | **about 10** |

For comparison: `/salon/:id/stylists?service_ids=` is 7 today, the tab is 3.
No query per row anywhere.

### 3.6 Error envelope

Same envelope, one difference: for a 404 our handler puts the single null-field
error **inside** `errors` (`errors: [{field: null, code: "not_found", ...}]`),
where the contract's example shows `errors: []`. The app reads `detail` and
`code`, so this only matters if it checks `errors.length`. Recommend: keep ours
(every 404 in this API looks like that) and say so in our doc. `detail` will be
a sentence for a customer: "This service is no longer on the menu." / "This
salon is not available."

### 3.7 Other contract assumptions to correct

- **`experts: []` "means the salon assigns".** Not in our booking flow: the
  single create refuses an empty stylist list (`stylist_required`). Only
  routines have "Any Available Expert". For us `[]` means "nobody here can do
  it", and the screen should keep Book disabled.
- **"the same haircut at another branch is another id"**: see 3.2.
- **Example URLs** `cdn.gostyle.uk`: ours are plain S3 URLs. The app must not
  assume a host.

---

## 4. MISSING fields: owner, and can we answer empty now

| Field | Who would add it | Empty now allowed by the contract? |
|---|---|---|
| `rating` (service) | Platform: a per-service rating source (a `service_id` on reviews, or a rule for multi-service bookings). The platform gRPC has the same hole. | Yes: `null` (§2 "null when nobody has rated it") |
| `review_count` (service) | Same | Yes: `0` (§2) |
| `products` | Platform schema: a real "products used / recommended" link service → `product_variant` (retail), plus a field in the business web service editor. Not `pos_service_consumable` (costing), not `stage.products` (free text) | Yes: `[]` hides the block (§2) |
| `details` "Suitability" | Platform schema + business web form (no column) | Yes: rows are optional; we send only rows we have |
| `experts[].rating`, `review_count` | Platform: per-stylist reviews | Contract example has numbers, but §3 says "same as the stylists endpoint", which sends `null` |

Business web write paths that already exist (so salons can fill the SHAPE fields
today): description (`PATCH /v1/catalog/services/:id`), photos
(`/v1/catalog/services/:serviceId/media`), stage names
(`/v1/catalog/services/:serviceId/stages`), pre/post care
(`builder.preCareInstructions` / `postCareInstructions`, or
`PUT /services/:id/booking-policy`). The business web source is not on this
machine, so which forms show these fields is not confirmed.

---

## 5. Other findings (found on the way, not in the contract)

- **F1. Expert filter drift (real bug, today).** Platform migration
  `20260918100000_service_stage_skill_is_tenant_scoped` lets
  `service_stage.skill_id` hold a **tenant** `skill.id`; the platform's
  `service-eligibility.handler.ts` (commit `3b6fa171`) resolves the tenant skill
  by id first, then falls back to `catalog_skill` by code, and treats an unknown
  id as "nobody can do it". Our `skills.requirements` looks the id up only in
  `catalog_skill` and **drops** it when not found (`skills.py`, the `continue`).
  Effect on `GET /salon/:id/stylists?service_ids=` (the Expert step) and on
  this screen: a service with one tenant-skill stage shows stylists who lack
  that skill; a service with only tenant-skill stages shows nobody.
  `skills.py` itself says "when that handler changes, change this with it".
- **F2. `BRANCH_AVAILABILITY_ENABLED = True` would hide too much.** Our code
  keeps only services **with** a `service_branch_availability` row that says
  `available`. The platform rule (`branch-visibility.ts`): an explicit row wins;
  else a `branch_scoped` service is hidden; else the category's branch row
  decides; else **available**. Switching the flag on as written would empty most
  menus. Only matters if Q1 reads the branch price: read the price, not the
  filter.
- **F3. Edited, not re-approved content shows early.** A `PUBLISHED` service can
  be edited in place; the row columns become the draft and the platform's own
  menu serves `published_snapshot` until the edit is approved. Our tab, our
  services-details and the platform's gRPC (so booking-api's price) all read the
  row columns. Price must agree with booking-api, so this screen should read the
  same columns for now. Q2.
- **F4. booking-api accepts a service with online booking off.** The platform
  gRPC filters only `PUBLISHED` and not deleted. Our menu hides it; a hand-made
  request can still book it. Platform ticket.
- **F5. A bad token's 401 lists `field: "code"` and `field: "messages"`** in
  `errors` (simplejwt's error dict flattened by `_flatten`). Known envelope
  issue (routine audit finding 2).
- **F6. Primary photo.** `service_media.is_primary` is set only on the first
  upload; no platform endpoint changes it, and reorder ignores it. So the hero
  may not be the photo the salon put first. Same rule as services-details today.
- **F7. VAT mode ignored.** booking-api always adds 5% on top of `price_minor`,
  even for a service marked `tax_mode = INCLUSIVE`. booking-api / platform
  question, not this screen's.

---

## 6. Questions for Rafa (answered 2026-09-30)

All answered **yes, as recommended**. One addition to Q9, below the table.

| # | Question | Recommendation | Why |
|---|---|---|---|
| Q1 | Which `price`: the base price (what the tab shows) or the branch price (what booking-api charges)? | **Branch price if set, else base price, on this screen AND the tab in the same step** (S7), reading only the price, not the F2 filter. First check live: `select count(*) from service_branch_availability where price_minor is not null;` If 0, the bug is not live yet, but the fix is still cheap | The contract's own rule: a price shown must agree with the payload, or `amount_mismatch` |
| Q2 | Read the row columns (draft leaks, F3) or `published_snapshot`? | **Row columns, like the tab and the platform gRPC.** Ask the platform to move gRPC and us to the snapshot together later | If we read the snapshot and booking-api reads the row, price and duration disagree after an edit |
| Q3 | Which services answer 200? | **Any service of this salon's tenant that was published at least once** (`published_version` not null, or `status = PUBLISHED` for services published before the platform had versions; not `published_at`, which archive and revision requests clear): 200 with `is_active` from the 3 menu rules. A never-published `DRAFT`: **404**. **Changed by Rafa after S1 (2026-09-30): 200 when `published_version` is not null OR the status is `PUBLISHED`, `HIDDEN` or `ARCHIVED`; only a `DRAFT`, `PENDING_REVISION` or `REVISION_REQUESTED` service with no version is 404**, so an old service with no version never turns into a 404 when it is hidden or archived. (The platform allows `DRAFT` to `ARCHIVED`, so a draft archived without ever being published also opens, as inactive.) | A deep link or an old booking only ever points at something that was on sale; a draft may have an unfinished name and price |
| Q4 | Auth: token required (contract) or public (like every other `/salon/:id/...` route)? | **Public (`AllowAny`)**, token accepted. A bad token still gets 401. Tell the app team | A guest can already open the salon, its services and its stylists; a 401 on the detail would break that flow |
| Q5 | Non-UUID ids: fix only this route, or all `/api/` paths? | **This route only** (`<str:...>` + parse in the view). A JSON 404 for every unmatched `/api/` path is its own small PR later | A global `handler404` changes every route's answer |
| Q6 | Service with no category: `{"id":"other","label":"Other"}` (the tab) or `null` (services-details)? | **`other`, like the tab** | The screen is opened from the tab; the contract types `category` as an object, never null |
| Q7 | Gallery sources, and is the hero repeated in the gallery? | **`service_media` first (primary, then `sort_order`), then this storefront's public, approved `GALLERY` photos linked to the service. Hero = the first one, and it stays in the gallery.** `gallery_count` = all of them | Uses both places a salon can attach a photo; one count the app can trust |
| Q8 | `included` from stage names? | **Yes**, non-blank names in stage order, duplicates removed | The platform already sends stage names as `included_steps`; the salon controls them in the stage editor |
| Q9 | Which `details` rows? | **Time Duration (always, icon `clock`)**, plus only when set: Consultation (`requires_consultation`, `sparkles`), Patch test (`requires_patch_test` + hours, `drop`), Minimum age (`min_age`, `scissors`) | Real data only; no Suitability until a column exists. **Rafa added a "Suitable for" row**, see below |
| Q10 | `preparation` from pre-care + post-care text? | **Yes: one bullet per non-empty line, pre-care first, then post-care** | The block is titled "Preparation & Aftercare"; both are free text |
| Q11 | `products` | **`[]` now**, and a platform ticket for a real service → retail variant link | The two existing links mean something else (costing recipe, free text) |
| Q12 | Service `rating` / `review_count` | **`null` / `0` now.** Do not derive from bookings yet | Multi-service bookings make it ambiguous; the platform has the same hole |
| Q13 | `experts` for a pulled service or a service with no stages | **`[]`, never an error** | The screen must open; Book is hidden (inactive) or has nobody to pick |
| Q14 | Fix F1 (skills drift) first, as its own commit? | **Yes (S0)**, mirroring the platform handler, before experts reuse it | It changes the Expert step too, so it deserves its own review |
| Q15 | Behind a flag? | **No flag for the new route** (new, read only, nothing else calls it). S0 and S7 change existing answers, so each gets its own commit and tests; no flag either unless you want one | Same as other read-only routes here |

**Q9 addition (Rafa):** a "Suitable for" row from `service.audience`, icon
`scissors`: `MALE` = "Men", `FEMALE` = "Women", `UNISEX` = "Everyone",
`KIDS` = "Kids". `audience` is never null (platform default `UNISEX`); an
unknown value gets no row.

---

## 7. Build plan (customer-api only)

Each step: its own commit on `feat/service-detail`, tests first, suite run
before commit. Baseline to compare against: 714 tests, the same 18 known
failures on `main`. Tests follow the existing pattern (`SimpleTestCase`,
selectors mocked, pure helpers tested directly), because every platform table
is unmanaged. `apps/salons/urls.py` and `selectors.py` use CRLF: keep `\r\n`.

| Step | What | Files | Tests |
|---|---|---|---|
| S0 | **F1:** resolve a stage's skill id from the tenant's skills first (by id), then `catalog_skill` by code; retired tenant skill and unknown id = nobody can do it (not dropped). Load tenant skills with soft-deleted ones, as the platform does | `skills.py` (`bridge`, `requirements`), `selectors.skill_bridge` | tenant skill id stage; retired tenant skill = nobody; unknown id = nobody; old catalog id still bridges by code; mixed stages; existing `SkillBridgeTests` / `CoverageTests` still green |
| S1 | Route `salon/<str:salon_id>/service/<str:service_id>`, view skeleton: parse both ids (bad = our JSON 404), salon 404, service 404 when not this tenant or never published (Q3 as changed: `published_version` not null, or status `PUBLISHED`, `HIDDEN` or `ARCHIVED`), auth per Q4. `FLOW` entry in `config/openapi_flow.py` | `urls.py`, `views.py` (or new `service_detail_views.py`), `selectors.py` (new `service_for_salon`) | non-UUID in each position = JSON 404 `not_found`; unknown salon; other tenant's service; never-published draft 404; archived (published once) 200; no token 200 (if Q4); bad token 401; `test_openapi_flow` green |
| S2 | Core fields: `id`, `salon_id`, `name`, `description`, `price`, `duration_min/max`, `category`, `is_active`. Share the price and category code with the tab and services-details (one helper each, no copy) | `views.py`, `serializers.py` | price equals the tab's for the same row; pulled service 200 `is_active: false` (each of HIDDEN, ARCHIVED, deleted, online off); parent chip; no category = `other` (Q6); duration twice |
| S3 | Photos: `hero_url`, `gallery` (max 5), `gallery_count` | `selectors.py` (new `service_photos`), view | order (primary, sort order, then linked photos); cap 5; count > 5; no photos = `null`, `[]`, `0`; deleted, unapproved, private and other storefronts' photos left out |
| S4 | Content: `included`, `preparation`, `details`. Pure functions | new `apps/salons/service_detail.py` | blank and repeated stage names; `None` and whitespace care text; CRLF lines; pre before post; each details row only when set; "20 min" text; "Suitable for" for each of the 4 audiences, unknown audience = no row |
| S5 | `experts`: `stylist_rows(salon, [id], stages=...)` with the S4 stages (one query); `[]` when pulled or no stages | view | same list and order as `SalonStylistsView` on the same mocks; pulled = `[]`; no stages = `[]`; no 422 ever |
| S6 | `rating: null`, `review_count: 0`, `products: []`, with a comment saying why. OpenAPI (`extend_schema`, response serializer). Our FE doc `docs/SERVICE_DETAIL_API.md`: what differs from their draft (auth, 404 `errors`, UUID category ids, `experts` meaning, one id at every branch, URLs) | view, `docs/` | keys present with those values; `manage.py spectacular` builds; openapi flow test |
| S7 | **Built (Rafa to review).** Also the group lines and the Packages tab's "price before", see 2.1. (Q1) Branch price on the tab and this screen together: read `service_branch_availability.price_minor` for the salon's branch, price only, no filter (F2 untouched). **Copy booking-api's rule exactly (Rafa, 2026-09-30): the branch price whenever one is set, so 0 means 0** (`menu.service_price` uses `or` today, so 0 falls back to the base price; keep that until S7) | `selectors.py`, `views.py` (tab `_service`), `menu.py` | override wins; null override = base; no row = base; **0 override = 0**; tab and detail give the same number |
| S7b | The tab's two "Other" groups (found in S2): a service with no category and one whose category was deleted give two groups with the same id `other`. Fix in its own commit next to S7, since both change the tab (Rafa, 2026-09-30) | `views.py` (`SalonServicesView`) | one "Other" group holding both; `ServicesTabTests` updated on purpose |

Order: S0, S1 to S6, then S7. S0 and S7 each change an existing answer, so each
stays a separate commit you can review or leave out. Nothing to deploy in
booking-api or the platform. No migration.

Live check after deploy (one curl at a time, your token):
`GET /api/v1/salon/{salon}/service/{service}`, then the same service id
through `/salon/{salon}/stylists?service_ids=` to compare the experts, then a
non-UUID id.
