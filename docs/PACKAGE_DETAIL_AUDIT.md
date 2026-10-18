# Package Details screen: the app team's contract vs our code (audit)

Date: 2026-10-15. **Audit only: no code was changed.**

The contract: the mobile team's "Package Details Screen" (one new route,
`GET /salon/{salon_id}/package/{package_id}`), received 2026-10-15 and saved
as received in `docs/package-detail-fe-contract.md` (never edit it; its
"ยง5" is how their section sign arrived). Section numbers below (§2, §3, ...)
are theirs. The one exception, 2026-10-18: a dated server note at the end of
its §5, saying what replaces their proposed create body (section 10); their
text is untouched.

**Status: Rafa (2026-10-15): "fix all, best way". Every recommendation in
section 6 is taken as the answer. The tab fix (Q3) goes behind a switch,
`PACKAGES_LIVE_RULES_V1`, OFF, so the live Packages tab does not change until
the live checks (section 8) are read and Rafa turns it on. The new route has
no switch: nothing reads it yet.**

Code read (test baseline on this branch: 1419 tests, all pass):

- **customer-api**: branch `feat/package-detail`, made from `imransid/main` =
  `a190ebb`.
- **booking-api**: `origin/main` = `8942284` (imransid's main). Read only.
- **gostyle-platform**: `origin/main` = `e2905663`. Read only. Its working
  tree is on another branch; everything below was read from `origin/main`.
- The local Postgres: one seed package ("Groom and Go", FIXED, SEQUENTIAL,
  2 services, no snapshot). Not enough to judge real data, hence section 8.

Tags: **READY** exists, same meaning. **SHAPE** the data exists, needs mapping
or a choice. **MISSING** no data anywhere today.

---

## 0. Short version

1. **The read is easy. The button is not.** Everything the screen draws can
   be read from the platform tables we already read for the Packages tab.
   `GET /salon/{salon_id}/package/{package_id}` is customer-api only: no
   booking-api change, no platform change, no migration.
2. **"Book with this Package" cannot work today, anywhere.** Nothing records
   a customer owning a package, nothing counts sessions, and booking create
   has no package field. A booking at the package price is refused as
   `amount_mismatch`. This is its own project, with a product question first
   (section 3). **Changed 2026-10-18:** answered with option A, and built
   behind `PACKAGE_BOOKING_V1` (section 10).
3. **Our Packages tab has four bugs** (F1 to F4) that the detail must not copy.
   The worst: a package that is on sale everywhere (the platform's default)
   is missing from our tab unless the owner also ticked this branch.
4. **Their "103 vs 80" puzzle is already solved by the tab.** Our
   `price_before` IS the services added up at this branch, so
   `save_amount = price_before - price`, always. Their 540 is just a mock
   number.
5. **Their mock terms describe something that does not exist** ("activation",
   "sessions deduct", "refund to wallet"). The only terms text a salon can
   write is `cancellation_policy`.

---

## 1. Field by field

| Field | Source | Tag |
|---|---|---|
| `id`, `salon_id` | path, checked | READY |
| `name` | published snapshot `name`, else the row (Q4) | READY |
| `description` | published snapshot `description`, else the row; blank = null | READY |
| `price` | published snapshot `priceMinor`, else `price_minor`. One price at every branch: there is no per-branch package price | READY |
| `price_before` | the included services at this branch's price, times quantity (the tab's rule). Null when it is not more than `price` | READY |
| `save_amount` | `price_before - price`. Null with `price_before` | READY |
| `save_percent` | `save_amount / price_before`, whole number (Q5). Null with `price_before` | SHAPE |
| `duration_minutes` | the included services' minutes added up, times quantity (the tab's rule and the platform's) | READY |
| `is_active` | on sale here now (Q2) | SHAPE |
| `includes[]` | the published items in order: service id, name, one unit's price here. Their contract has no `quantity` (Q6) | SHAPE |
| `timeline[]` | built by us from the items (Q7). The platform has no per-step offset for a package | SHAPE |
| `terms` | `cancellation_policy`, as typed (Q8) | SHAPE |
| `theme` (their §7, not in §2) | `color_theme`, as the tab sends it (Q9) | READY |

How it works, "Pays for itself in one visit": the app's own text, as they say.

---

## 2. Their §3 rules, one by one

- **"save_amount is not the price difference."** It is, in our API. Our
  `price_before` is not a list price the salon types: it is the sum of the
  included services at this branch (`selectors.salon_packages`, `sum_minor`).
  So `save_amount = price_before - price` is "against what the services cost
  separately", which is what they want. The FE guide says this plainly.
- **"price_before and the SAVE chip travel together."** Yes. The tab already
  sends `price_before` and `save_amount` as null together when there is no
  saving; `save_percent` joins them (Q5).
- **"timeline offset counts from the start of the visit, in order, first 0."**
  Yes, with Q7's rule.
- **"The timeline and the included list are two different things."** They
  are two lists, but both come from the same items. Names and minutes will
  match because they are the same rows. Their mock's "Shave / Shape" and 75
  minutes are mock data.
- **"Every service_id must be bookable at that salon."** Not checked by the
  tab today (F4). The detail checks it inside `is_active` (Q2).
- **"Plain text only."** `description` and `cancellation_policy` are free
  text on the platform (policy max 2000 chars). We send them as typed,
  trimmed. A policy with line breaks stays with line breaks; we do not join
  lines (Q8).
- **"Money is a number in the salon's currency."** Yes, `money.major` like
  every other screen.
- **§1 "the same bundle at another branch is another id at another price."**
  Not true. A package belongs to the business (tenant), like a service. One
  id is sold at every branch that shows it, at one price. Only `price_before`
  and `save_amount` can differ by branch, because a service can have a branch
  price. The salon id in the path is still right: it picks the branch.

Auth (§4): public, like every `/salon/{id}/...` route. A token is accepted,
not needed; a bad or expired one is still 401. Matches "a guest may read".

Errors (§4): we answer our own envelope, as the service detail and expert
profile do: `detail` is a sentence for the customer, `errors[0].field` is
null, `code` `not_found`. Their example (`"Please correct the highlighted
fields."`, field `id`) is the 422 wording, not a 404. This read calls no
other service, so a 503 can only mean our own server is down.

---

## 3. "Book with this Package" (§5): what exists

**Nothing a customer can use.**

- **Platform: no ownership.** A package can be sold at the till
  (`sale_line.package_id`, `kind: PACKAGE`), as one line, priced from the
  package row. Nothing records who owns it, sessions left, or expiry. No
  table, no code, no commit in any branch (customer_package, redemption,
  package_session: zero hits). `validity_days` ("days the bundle stays valid
  after purchase") is stored and read by nothing.
- **Platform: no public read of packages.** Every package route needs a
  business login (`catalog.read`). No gRPC message for packages
  (`services.proto` has `ListServices` only). booking-api cannot ask the
  platform for a package today.

  > **Changed 2026-10-18: no longer true.** The platform has a gRPC read of
  > packages, `ServicesDirectory.ListPackages`, merged 2026-10-18 (platform
  > PRs #183 and #184): a tenant's packages on sale at a branch (published,
  > online booking on, the package's branch rule), each with its items, and
  > only those whose every service is sellable there by
  > `isServiceAvailableAtBranch`. booking-api sells from it, behind its
  > `PACKAGES_FROM_PLATFORM`. It serves the published snapshot, the content
  > this service shows, once the platform's `feat/list-packages-snapshot`
  > is in: that change also sends each item's `variant_id`, which
  > booking-api's client already reads. As read here on 2026-10-18 (platform
  > `origin/main` = `7875172e`), the merged code still read the package row
  > and its draft items, and that change was not merged yet.
- **booking-api: no package on create.** `POST /mobile-booking` takes
  services, products, stylists, times and money totals. No `package_id`,
  voucher, prepaid or session field. The booking tables have no package
  column.
- **booking-api: a lower total is refused.** Create quotes the services
  itself and compares `amount_without_tax`, `tax_amount`, `discount`, `total`
  within 1 fil. Booking the included services at the package price is 422
  `amount_mismatch`. (Per-line `services[].amount` is not read at all: F7.)
- **booking-api: a trap.** `src/domain/booking/package.ts` and a hard-coded
  `PACKAGES` list of 3 fixture bundles (`colour-and-finish`,
  `bridal-morning`, `hands-and-feet`) turn a package into a "bundle discount"
  on quote, desk confirm and walk-in. It is test data, not tenant data, and
  mobile create refuses those ids anyway (`unknown_service`). Its own docs
  say it is to be deleted. Do not build on it.

**The product question first.** Two different things are called "package":

- **A. One visit, bundle price.** Book all the included services together,
  once, and pay the package price. The platform's builder fields point here:
  `scheduling` SEQUENTIAL/PARALLEL, `max_guests`, `lead_days`,
  `requires_coordinator`. Needs: booking-api create learns `package_id`,
  reads the package (a new platform gRPC), books its services at the bundle
  price. No ownership table.
- **B. Prepaid sessions.** Buy first, then book included services one by one,
  each one "covered". The mobile screen's How it works, the mock terms and
  "My Packages" (sessions used, expiry) are this. Needs everything A needs
  plus a purchase flow, a payment, an ownership table, session counting,
  expiry and refunds. Much bigger, mostly platform work.

Recommendation (Q10): tell the mobile team the button has nothing behind it
yet, and to hide it (or show it disabled) until package booking is built.
The read endpoint ships alone.

---

## 4. Their §7 open points: proposed answers

1. **A package the customer owns.** Not possible: nothing records ownership
   (section 3). `my_packages` on the tab stays `[]`.
2. **Hero theme.** Yes, send `theme` (the tab's `color_theme` value), cheap
   (Q9).
3. **Share link.** Nothing per package on the platform (no slug, no link).
   Not now.
4. **`duration_minutes`.** All the included services added up, each times its
   quantity: the same number as the tab, and the platform's own sum. For a
   one-visit package it is where the last timeline step ends (Q7).

---

## 5. Findings

**F1. Our tab hides packages the platform shows.** `selectors.salon_packages`
keeps a package only when a `service_package_branch_availability` row says
`available = true` for this branch. The platform's rule (its live menu,
`listForBranch`): `branch_scoped = false` means "everywhere unless a row
says false"; only `branch_scoped = true` needs a true row. A plain publish
sets `branch_scoped = false` and writes no row. So a package published the
normal way is missing from our tab. (The local seed has a true row, so it
shows locally.)

**F2. Our tab reads the draft.** On the platform the row and its
`service_package_item` rows are the DRAFT; publish freezes `name`,
`description`, `priceMinor`, `items` (service, variant, quantity, note),
`pricingMode`, `discountBps` into `published_snapshot`. The platform's live
menu reads the snapshot for those, and the row for the rest (theme,
policy, validity, scheduling, online booking, branch rules). Our tab reads
the row, so an owner's unpublished edit (a new price, a removed service)
shows in the app before it goes live. Fallback when there is no snapshot:
the row, as the platform does.

**F3. Our tab ignores an item's variant.** An item can name a
`service_variant`. The platform prices and times it by the variant
(`variant.price_minor ?? service.price_minor`, same for minutes). Ours
always uses the service. Matters only if live data has variants (section 8).
Note: booking-api books a service id, not a variant, so a package with a
variant item cannot be booked as shown either way.

**F4. Our tab shows a package that holds an unbookable service** (deleted,
hidden, archived, or online booking off). Their §3 rule says every service
must be bookable.

**F5 (platform, for info).** A live package that the owner edits cannot be
published again directly (`PACKAGE_ALREADY_PUBLISHED`); they must hide it
and publish, or archive and restore. Until then the snapshot keeps the old
content, which is right for us.

**F6 (platform, for info).** In DISCOUNT mode the platform works out
`price_minor` when the package is saved, from the base (or variant) prices.
It is not worked out again when a service price changes later, and it does
not use branch prices. So `price` is fixed and `price_before` moves with
today's prices. That is fine for the screen; the saving can grow or shrink.

**F7 (booking-api, for info).** Mobile create never reads per-line
`services[].amount`, although its DTO says "verified, never trusted". Only the
totals are checked. Not ours to fix here.

**F8.** `apps/salons/test_service_detail.py` is the only test that touches
`salon_packages` (the S7 branch price sum). The tab itself is not pinned.

---

## 6. Questions for Rafa (answered 2026-10-15: every recommendation)

**Q1. Which packages open (200) and which are 404?** Recommend the service
detail's rule: a package of this salon's business that was ever on sale
(`published_version` set, or status PUBLISHED, HIDDEN or ARCHIVED) answers
200, even when it has since been pulled or deleted, with `is_active` false.
Another business's package, a never-published draft, or an id that is not a
UUID: 404, our JSON envelope.

**Q2. `is_active` = what?** Recommend: it would be on the tab now, with F1
fixed, AND every included service is on the menu (F4). So: status
PUBLISHED, not deleted, `online_booking_enabled`, the platform's branch rule
for this branch, at least one item, and every item's service PUBLISHED, not
deleted, online booking on.

**Q3. Fix the tab (F1, F2, F4, and F3 if live has variants) in the same
work?** Recommend yes, with one shared selector for the tab and the detail,
so the two screens cannot disagree (as menu.py does for services). The tab
gets pinned by tests first. The live answer of the tab may change, so the
live checks (section 8) come before that step. No switch if the live check
shows no change; a switch if it does. **Answer: a switch from the start,
`PACKAGES_LIVE_RULES_V1`, OFF; the live checks decide when it goes on.**

**Q4. Snapshot first?** Recommend yes: name, description, price and items
from `published_snapshot`, the rest from the row, the row alone when there is
no snapshot. The platform's own live menu does exactly this.

**Q5. `save_percent`.** Recommend: `save_amount * 100 / price_before`,
rounded to the nearest whole number (half up), and at least 1 when there is
any saving, so the chip never says "SAVE 0%". Null when there is no saving,
with `price_before` and `save_amount` (their table says number; we say null,
as the tab does).

**Q6. Quantity.** An item can be "2 x Cut". Recommend: `includes[]` adds
`quantity` (1 almost always) and `price` is ONE unit's price here, so the
rows add up to `price_before` as `price x quantity`. If live has no quantity
above 1, still send it, so the app is ready.

**Q7. Timeline.** Recommend: one step per item, in the package's order,
`name` and `duration_minutes` from the service (the variant's minutes when
F3 applies). `offset_minutes`: SEQUENTIAL = the minutes of the steps before
it; PARALLEL = 0 for every step. A quantity above 1 still gives one step (a
timeline is one visit), and `duration_minutes` in the hero stays the tab's
sum. Section 8 tells us whether PARALLEL or quantity above 1 exist live; if
not, the edge cases are just tested, not designed further.

**Q8. `terms`.** Recommend: `cancellation_policy` as the salon typed it,
trimmed, null when blank. Do not send `validity_days` now: "valid N days
after purchase" means nothing while no one can buy. Do not write our own
sentence.

**Q9. `theme`.** Recommend yes: the same `theme` value the tab sends.

**Q10. The button (§5).** Recommend: answer them "no package booking exists
yet; hide the button (or disable it) until it does", and open the product
question A or B (section 3) as its own piece of work. The read ships alone.

**Q11. Route style.** Recommend the service detail's: path ids as text,
checked by `params.path_uuid`, so a bad id gets our JSON 404 and not
Django's HTML page. Sentences: "This salon is not available." (as the others)
and "This package is no longer available."

---

## 7. Build plan (customer-api only)

One step at a time, stop and show after each.

- **P0.** This audit and the contract, saved as received. (This step.)
- **P1.** Pin the tab: tests for today's answer of `GET /salon/{id}/packages`
  (shape, nulls, order), before anything moves.
- **P2.** One shared package module: a selector that reads the snapshot (Q4)
  and the platform's branch rule (F1), and a pure module for price,
  `price_before`, saving, duration, item rows (F3, F4). The tab moves onto it.
  Its live answer changes only as section 8 says.
- **P3.** The route, the 404s (Q1, Q11), `is_active` (Q2). Answers `{id,
  salon_id, is_active}` until P4.
- **P4.** Hero fields: name, description, price, price_before, save_amount,
  save_percent (Q5), duration_minutes, theme (Q9).
- **P5.** `includes` (Q6), `timeline` (Q7), `terms` (Q8).
- **P6.** Doc-only serializers, a real `EXAMPLE` checked by a test, and the FE
  guide `docs/PACKAGE_DETAIL_API.md` (answers §5 and §7, says what differs
  from their text). `docs/SALON_PROFILE_API.md` section 4 updated for the tab.

No migration, no booking-api change, no platform change.

---

## 8. Read-only checks for the server

`scripts/sql/package_detail_live_checks.sql` (two SELECTs, nothing writes).
What each answer decides:

- **A, every package:** status, pricing mode, scheduling, theme, policy
  length, items with quantity above 1, items with a variant, items whose
  service is off the menu, branch rows. Decides F1 (how many disappear from
  our tab today), F3 (any variants?), F4 (any off-menu items?), Q6 and Q7
  (any quantity above 1, any PARALLEL?), and whether Q3 needs a switch.
- **B, sales:** has any package ever been sold at the till. Tells us how real
  the "My Packages" question is.

---

## 9. Build log (2026-10-15)

Branch `feat/package-detail`, from `imransid/main` `a190ebb`. Not pushed.

| Step | Commit | What |
|---|---|---|
| P0 | `f058adf` | The contract as received, this audit, the live SQL. |
| P1 | `afe0857` | The Packages tab pinned as it answers today. |
| P2 | `1f2fb44` | `packages.py` + the package selectors (F1 to F4), the tab on them behind `PACKAGES_LIVE_RULES_V1` (off). `menu.service_price_minor`, `menu.is_on_menu` reads a dict. |
| P3 | `c971566` | `GET /salon/{salon_id}/package/{package_id}`: route, 404s, every field, doc-only serializers, a real `EXAMPLE`. |
| P4 | (this commit) | FE guide `docs/PACKAGE_DETAIL_API.md`, `SALON_PROFILE_API.md` section 4, this log. |

Checked on the local seed, each in a rolled-back transaction:

- Switch off and on: the same tab answer for the seed (2 vs 4 queries).
- F1: no branch row, not scoped: off hides it, on shows it. Scoped with no
  row, or a false row: hidden both ways.
- F2: a snapshot with another name, price and one item: on shows the
  snapshot, off the draft.
- F3: a variant at 300.00 / 50 min: on counts it; a deleted variant is
  ignored.
- F4: a member service HIDDEN: on hides the package, off still shows it.
- The detail: 3 queries for a published package; bad ids, an unknown package
  and an unknown salon answer our JSON 404.

Suite: 1419 before, all pass; 1511 after P4, all pass.
No migration. No booking-api or platform change. No new spectacular warnings.

**Before turning `PACKAGES_LIVE_RULES_V1` on:** run
`scripts/sql/package_detail_live_checks.sql` live. Section C lists, per salon,
every package whose place on the tab changes (F1). Section A shows how many
hold an off-menu service (F4: they disappear), have a variant (F3: their
numbers change) or a snapshot that differs from the draft (F2: their name or
price changes).

---

## 10. Decision and build: booking a package (2026-10-18)

**Decision: option A, one visit at the bundle price** (section 3). A package
is booked as one booking of its services, at the package price. **Option B,
prepaid sessions and My Packages, is not built**: nothing is bought ahead,
nothing counts sessions, and `my_packages` on the tab stays `[]`.

What A needed (section 3), and how it came:

- **A platform read of packages:** `ServicesDirectory.ListPackages` (the
  changed note in section 3).
- **booking-api create learns packages**, not with a `package_id` (as
  section 3 guessed and the mobile team proposed) but as a **line** of
  `services`: `{ "id": "<package id>", "amount": <package price> }`.
  booking-api expands it into its parts (each item times its quantity) and
  charges the package price, each part carrying its share, so there is no
  bundle discount line any more. A `package_id` key is refused (`422`). It
  also answers `POST /v1/mobile-booking/quote`, and its reads carry
  `packages`. booking-api branch `feat/packages-from-platform`, not deployed.
- **What booking-api sells** (`platform-package-catalogue.ts`): published,
  online booking on, sold at the branch, every part offered at the branch,
  a price above 0, no item pinned to a variant. A package dearer than its
  parts is sold at its price.

This service, branch `feat/package-booking` from `imransid/main` =
`86507b4`, behind `PACKAGE_BOOKING_V1` (off) except the quote:

| Step | Commit | What |
|---|---|---|
| 1 | `ebf88ea` | `bundles[].services` (added in `fcae0ec`) from the package's own lines: its order, this branch's price, `quantity`, no query per bundle. The three failing LiveTabTests pass. |
| 2 | `4ecf6cc` | `can_book` on the tab and the detail: `packages.can_book`, with the platform's branch rule once, in `menu.offered_at_branch`. `is_active` unchanged. |
| 3 | `7f12b7f` | Nearest available takes a package id: its parts, each per quantity, one stylist for all; `422 package_not_bookable`. |
| 4 | `277fdc9` | `POST /api/v1/booking/quote`, forwarded to booking-api's quote, routed as create; no Idempotency-Key. Not behind the switch. |
| 5 | `5b8a0d5` | Create: nothing here reads `services`, so nothing to allow; a test pins a package line crossing byte for byte. |
| 6 | (this commit) | `BOOKING_CREATE_API.md` §6 and §7, `PACKAGE_DETAIL_API.md` §5, the server note in the contract, `SALON_PROFILE_API.md` §4, `BOOKING_NEAREST_AVAILABLE_API.md`, this section. |

Checked on the local seed (the SQL against Postgres; the false cases in
rolled-back transactions): the tab is 3 queries whatever the number of
bundles, its services in the package's order at this branch's price; the
branch rule's subqueries read this branch's service and category rows, and
a service row set to false, a category row false with no service row, or a
branch-scoped service with no row each make `can_book` false and leave
`is_active` true; the slot search finds the seed package in one read and
counts a hand-picked cut beside it twice (95 minutes).

Suite: 1511 before, 3 failing (the LiveTabTests, `fcae0ec`'s query); 1572
after, all pass. No migration. No booking-api or platform change. No new
spectacular warnings.

Before turning `PACKAGE_BOOKING_V1` on: booking-api's branch deployed with
`PACKAGES_FROM_PLATFORM` on, and the platform's snapshot change merged and
deployed. Until then booking-api reads a package's row and draft items, so
an owner's unpublished edit can be what it charges, and it does not see a
variant pin (this service's `can_book` refuses such a package anyway).

Not built, and open:

- A package in a group booking (`members[].packages` is still
  `packages_not_supported`).
- The package's own `lead_time_minutes` and `lead_days`: neither booking-api
  nor the slot search applies them.
- `scheduling: PARALLEL`: booking-api books the parts one after another, so
  the visit is as long as the quote's `end_time`, while the detail's timeline
  draws the steps at the same time.
