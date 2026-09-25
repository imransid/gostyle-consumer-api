# Group booking for mobile: audit and plan

Date: 2026-09-25. Status: **built, behind flags that are OFF. Not deployed.**

## Status (read this first)

**Change of plan, 2026-09-25:** payment is SKIPPED (another team owns it). A
party is saved to pay at the salon (`PAY_AFTER_CHECK_IN`): no draft window,
never auto-cancelled. VAT, products, the 50% child price, the deposit figure
and all totals are still saved. Decisions D1 to D6 below were accepted as
suggested. Step 4 (pay the party) is dropped.

What is built:

| Piece | Repo, branch | Flag |
| ----- | ------------ | ---- |
| Split of the mixed branch: `feat/calendar-filters` (business), `fix/tenancy-token`, `feat/group-availability-many` (mobile) | booking-api | none |
| 4 nullable columns, money and contract rules | booking-api `feat/mobile-group-booking` (79e0d5a) | `MOBILE_GROUP_BOOKING` |
| Create with products, read by group id, My Bookings GROUP row, cancel together | booking-api `feat/mobile-group-booking` (c67562d) | `MOBILE_GROUP_BOOKING` |
| Customer cancel security fix (S1 in the follow-ups) | booking-api `fix/lifecycle-customer-ownership` (5f01d92) | none, staff unchanged |
| One-call create, read by group id, cancel route | customer-api `feat/group-booking-v2` (2a73ad5) | `GROUP_BOOKING_V2` |

Differences from the plan below, and why:

- **No re-plan at confirm, and no copied helper (B5).** Hold and confirm run
  back to back in one request, and the hold's own reservations already block
  the time; the confirm re-points each member's reservation by id instead.
- **My Bookings is decorated in the mobile controller**, not in
  `MobileBookingHandler` or `BookingRepository`, so neither changed.
- **The app spec's `DRAFT` is accepted** on create and saved as pay at the
  salon; the answer says `PAY_AFTER_CHECK_IN`.
- **Availability alias (B13) not built**: availability still calls the shared
  `POST /v1/bookings/availability/group`, read only, as before.

Things found on the way are in `docs/GROUP_BOOKING_FOLLOW_UPS.md`.

---

The original audit and plan follow, unchanged.

Target: `docs/APP_GROUP_BOOKING_SPEC.md` (the app team's spec), including products.

Repos:

- **customer-api**: this repo (Django). The app talks only to this.
- **booking-api**: `../gostyle-booking-api` (NestJS). Owns every booking row. The
  business web talks to it too.

---

## 0. Short version

1. Today a mobile group booking goes through the **business web's own group routes**
   (`/v1/groups/holds`, `/v1/groups/:id/confirm`). Those routes write N plain
   bookings: confirmed, nothing to pay, first service only, no products, no VAT, no
   deposit, no pass. The app cannot read the group back, pay it, or see it as a group.
2. The fix: a **new mobile-only path** in booking-api under `/v1/mobile-booking/group`.
   It reuses the group **hold** as it is, then does its own **confirm**: every member
   becomes an ordinary booking with the **same shape as a single mobile DRAFT booking**
   (all services, products, VAT, deposit, 6 hour draft window), linked by `group_id`.
   The group id is the "one booking" the app sees.
3. The business web keeps working with no change: it already shows party lanes and
   single mobile DRAFT bookings, and new lanes are exactly those two things combined.
4. Database: **4 new nullable columns**, no new table, no new enum value, no change to
   any existing column.
5. Two flags, both OFF by default: `MOBILE_GROUP_BOOKING` (booking-api) and
   `GROUP_BOOKING_V2` (customer-api).
6. About **9 to 11 working days**, in 8 small PRs.

---

## 1. Audit

### 1.1 The flow today, endpoint by endpoint

#### `GET /api/v1/user/lookup?contact=` (spec §2)

| Step | Where |
| ---- | ----- |
| View | `apps/accounts/views.py` `UserLookupView` (rate-limited in the service via Redis) |
| booking-api | not called |
| State | **Done** (commit e8f7cf9). 200 `{found, user}` on hit and miss. |

#### `POST /api/v1/booking/group-availability` (spec §3)

| Step | Where |
| ---- | ----- |
| View | `apps/salons/group_views.py` `GroupAvailabilityView.post` |
| Checks | `_service_rows` (foreign_id), `_check_stylists` (stylist_mismatch, stylist_repeated), `_open_span` (salon hours) |
| Clock | `booking_api.engine_clock` -> `GET /v1/bookings/settings` |
| Plan | `booking_api.plan_group` -> `POST /v1/bookings/availability/group` with `targetMins` (up to 24 starts per call) |
| booking-api | `group-availability.controller.ts` -> `GroupAvailabilityHandler` -> `GroupHoldRepository.planMany` -> `domain/availability/party-starts.ts` `planPartyAtEach` -> `party.ts` `planParty` |
| Answer | `group_translate.day_slots` + `banded` build the bands |
| Writes | nothing |

#### `POST /api/v1/booking/group` (spec §4 to §6)

| Step | Where |
| ---- | ----- |
| View | `group_views.py` `GroupBookingCreateView.post` |
| Checks | `GroupBookingRequestSerializer` (kinds, size, **products and packages refused**), `_name_members` (unknown_user), `_service_rows`, `_check_stylists`, `gt.start_refusal` (hours, lead time) |
| Retry safety | Redis receipt (`_claim`, `_remember`, `_replay`) because it is two calls |
| Call 1 | `booking_api.hold_group` -> `POST /v1/groups/holds` (mode TOGETHER, arrangement ORGANIZER) |
| booking-api | `groups.controller.ts` `hold` -> `group-hold.handler.ts` -> `group-hold.repository.ts`: writes `booking_group` (status draft), `group_participant`, `hold`, `staff_reservation`, `resource_reservation` |
| Call 2 | `booking_api.confirm_group` -> `POST /v1/groups/:id/confirm` |
| booking-api | `group-confirm.handler.ts` -> `group-confirm.repository.ts` `confirm`: one transaction, re-plans, writes one `booking` per member with `status confirmed`, `payment_status none_required`, `deposit_fils 0`, `channel desk`, **one `booking_item` holding only the first service id**, guest lanes get `customer_id = toUuid(groupId)`, group -> confirmed, outbox `group.confirmed`, deletes the hold. No status history rows. |
| On failure | `booking_api.release_group_hold` -> `DELETE /v1/groups/holds/:holdId` |
| Answer | `gt.booking_from_confirm` rebuilds prices from the salon price list |

#### `GET /api/v1/booking/:id`, `PATCH /api/v1/booking/:id`, `GET /api/v1/bookings`

| Endpoint | customer-api | booking-api | With a group today |
| -------- | ------------ | ----------- | ------------------ |
| read | `views.py` `BookingDetailView.get` | `GET /v1/mobile-booking/:id` -> `MobileBookingHandler.read` | 404: the group id is not a booking id |
| pay | `BookingDetailView.patch` | `PATCH /v1/mobile-booking/:id` -> `recordPayment` | 409: lanes are `none_required`, not DRAFT |
| list | `BookingListView` | `GET /v1/mobile-booking` -> `list` -> `BookingRepository.customerPage` | booker's lane shows as a SINGLE row; guests' lanes are invisible |
| cancel | **no route at all** (not even for single bookings) | `POST /v1/bookings/:id/cancel` exists (shared with business) | not reachable from the app |

#### Work in progress that is not merged

- **customer-api, uncommitted**: `group_translate.py` and `group_views.py` read a new
  `participantId` for `members[].id`.
- **booking-api, uncommitted**: adds `participantId` to the response of
  `POST /v1/groups/:id/confirm` (a **business** route), plus 2 spec files and
  `docs/api/MOBILE.md`.
- **booking-api branch `feat/booking-products`**: 16 commits not on `origin/main`,
  **mixed**: 12 business calendar commits, 2 tenancy fixes (all routes), and the 3
  `targetMins` commits that customer-api availability needs. This breaks the
  "separate PRs" rule. See step 0 in §2.6.

### 1.2 Business web: do not touch

Source: booking-api controllers, and the business team's docs
(`docs/api/BOOKINGS-API-FOR-FRONTEND.md`, `FE-CONTRACT-AUDIT*.md`, `BOOKINGS-FE-ROUND-*.md`).

**Desk-only routes** (`@DeskOnly`, a customer token is refused):

| Controller | Routes under `/v1` |
| ---------- | ------------------ |
| `read-models.controller.ts` | `GET bookings`, `bookings/summary`, `worklist`, `calendar/day`, `calendar/week`, `calendar/month`, `search`, `events`, `events/:id`, `waitlist`, `series` |
| `desk-actions.controller.ts` | `POST bookings/:id/move`, `GET bookings/:id/check-in`, `GET customers/:id/risk` |
| `desk-extras.controller.ts` | `bookings/:id/remind`, `reminders/bulk`, `check-in/undo`, `shorten`, `waiver`, `late-capture`, `series-admin/:id/course-draw` |
| `money.controller.ts` | `bookings/:id/capture`, `refund`, `goodwill`, `revive` |
| `compaction.controller.ts` | `compaction`, `compaction/apply` |
| `roster-changes.controller.ts` | `roster-changes` / `bookings/conflicts` and sub-routes |
| `walk-ins.controller.ts` | `walk-ins` and sub-routes |
| `stylists.controller.ts` | `stylists` CRUD |

**Shared routes** (business uses them, customer tokens also pass):

| Controller | Routes | Also called by customer-api today? |
| ---------- | ------ | ---------------------------------- |
| `groups.controller.ts` | `POST groups/holds`, `DELETE groups/holds/:holdId`, `POST groups/:id/confirm` | **yes** (group create) |
| `group-availability.controller.ts` | `POST bookings/availability/group` | **yes** (group availability) |
| `settings.controller.ts` | `GET bookings/settings` | **yes** (clock) |
| `services-directory.controller.ts` | `GET services-directory/services` | **yes** |
| `bookings.controller.ts` | `POST bookings`, `GET bookings/:id`, `POST bookings/:id/payment-link` | no |
| `lifecycle.controller.ts` | `bookings/:id/reschedule`, `check-in`, `start`, `complete`, `settle`, `cancel`, `no-show` | no |
| `holds.controller.ts` | `holds`, `bookings/holds` | no |
| `availability.controller.ts`, `quote.controller.ts`, `eligible-staff.controller.ts` | availability, catalogue, quote, eligible staff | no |
| `series.controller.ts`, `booking-series.controller.ts`, `waitlist.controller.ts` | series and waitlist | no |
| `webhooks.controller.ts` | `POST webhooks/payments` | no |

**Tables**: the business reads and writes **every** table (`booking`, `booking_item`,
`booking_product`, `hold`, `staff_reservation`, `resource_reservation`,
`deposit_ledger`, `booking_status_history`, `event_outbox`, `idempotency_key`,
`booking_group`, `group_participant`, `waitlist_entry`, series, roster change, walk-in
tables). So the rule is: **add nullable columns only; never change an existing
column, enum, index or constraint.**

**Background jobs that act on every booking row** (and will act on mobile lanes too,
which is what we want): hold sweeper, payment link sweeper, no-show sweeper, reminder
scheduler, risk flag sweeper, group status listener.

### 1.3 Mobile-only today

| Route | File | Business uses it? |
| ----- | ---- | ----------------- |
| `POST /v1/mobile-booking` | `mobile-booking.controller.ts` | no |
| `GET /v1/mobile-booking` (list) | same | no |
| `GET /v1/mobile-booking/busy` | same | no |
| `GET /v1/mobile-booking/:id` | same | no |
| `PATCH /v1/mobile-booking/:id` | same | no |

No business doc mentions `mobile-booking`, and `customerPage` / `pageRows` in
`booking.repository.ts` are called only by the mobile list. **No table is
mobile-only.**

### 1.4 App spec vs today, section by section

| Spec | What it asks | Today | Status | Why |
| ---- | ------------ | ----- | ------ | --- |
| §1 | 3 new endpoints | all 3 exist in customer-api | works | |
| §1 | `GET /booking/:id`, `PATCH /booking/:id`, `/bookings` work for groups | they do not | **missing** | a group is N bookings in booking-api, nothing reads a group by id |
| §2 | lookup 200 `{found, user}`, exact match, rate limit | done | works | `docs/BOOKING_GROUP_API.md` §2 still says 404 (stale doc) |
| §3 | bands, `available`, `plan` per member | done | works | needs the unmerged `targetMins` commits in booking-api |
| §3 | stylist must be free | busy checked | **partial** | booking-api ignores stylist shifts when planning a party (known gap) |
| §4 | `products` per member (variant ids) | refused with `products_not_supported` | **missing** | the group confirm has no product step |
| §4 | `deposit_percent`, money fields | ignored | **missing** | nothing on the group path prices money |
| §4 | `payment_status: DRAFT`, `status: BOOKED` | ignored | **missing** | lanes are written confirmed, nothing to pay |
| §5.1 | prices from salon records | rebuilt in customer-api from the price list | **partial** | booking-api stores the sum on one line, first service only |
| §5.2 | child pays 50% per service | `age_group` echoed, never stored | **missing** | no child rule anywhere |
| §5.3 | `deposit_amount` = percent of total | always 0 | **missing** | no deposit on group lanes |
| §5.4 | stylists verified, null filled | done | works | |
| §5.5 | slot re-checked, `slot_taken` | done | works | hold and confirm both re-plan |
| §6 | `status BOOKED`, `payment_status DRAFT` | `CONFIRMED_BY_SALON`, `PAY_AFTER_CHECK_IN` | **wrong** | see §4 row |
| §6 | `members[].id` | null (uncommitted work adds it via a business route) | **missing** | |
| §6 | numeric `tax_amount` | null | **missing** | no VAT on group lanes |
| §6 | one `pass_qr_code` for the party | null | **missing** | each lane has its own code, none is "the group's" |
| §6 | `expires_at` draft hold | null | **missing** | no draft window on group lanes |
| §6 | `GET /booking/:id` returns the same object | 404 | **missing** | |
| §6 | `/bookings` rows `booking_type GROUP` + member count | SINGLE rows | **missing** | |
| §6 | pay with `PATCH /booking/:id` | 409 | **missing** | |
| §7 | error codes | all codes exist | works | |
| (asked) | cancel the party together | no app route | **missing** | the spec defines no cancel route (not even for single) |

Note on the spec's example: the 3 members in §4 add up to 660, not 685, and §6 says
"the four numbers". The example was cut from a bigger party. Harmless, because §5 says
the server's figures win, but see decision D6.

### 1.5 How a single mobile booking does money, products, DRAFT and expiry

All in booking-api. "Reuse" means: the group path imports it and calls it, with **no
change** to it.

| Piece | Where (booking-api) | How it works | Group reuse |
| ----- | ------------------- | ------------ | ----------- |
| Services price | `context.loadServices` + `priceOfService(s, priceOf)` | salon's own record | reuse as is |
| Products check | `PlatformProductCatalogue.resolve` + `domain/booking/mobile-products.ts` `checkProducts` | variant ids, price, currency, stock; behind `PRODUCTS_FROM_PLATFORM` | reuse as is, per member |
| Products money | `productMoney()` | net + VAT 5% | reuse as is |
| Products saved | `BookingRepository.confirm`: `booking_product` rows (snapshot of name and price) | one row per line | same rows, written per lane by the new repository |
| VAT | `domain/booking/quote.ts` `VAT_PERCENT = 5`, on the discounted subtotal | `Money.percent` | reuse the constant and `Money` |
| Discount | tier + bundle from `quote()`; `promo_code` is echoed, never applied | | see D2 |
| Deposit | `requirementFor()` "ladder" (risk, service percent, new customer) | **server decides, app sends no percent** | differs from the group spec, see D1 |
| Money check | `verifyMoney` (private in `MobileBookingHandler`) using `aedToFils`, `amountsAgree` (1 fil tolerance), `filsToAed` | 422 `amount_mismatch` with the right figure | reuse the 3 exported helpers; the 20 line loop is repeated in the new handler (it is private, and we do not touch that file for this) |
| DRAFT | `createIntentOf('DRAFT')` -> confirm on the `link` rail | `status pending_payment`, `payment_status unpaid` | same values on every lane |
| Expiry | `domain/booking/payment-link.ts` `linkWindow(now, start)` | 6 hours, closes 2 hours before start; too close is a 422 | reuse as is |
| Expire job | `payment-link-sweeper.service.ts` | `pending_payment` + `link_expires_at <= now` -> `expired`, chairs freed | works on lanes with no change |
| Pass | `booking.code` (`GS-nnnn`, `booking_code_seq`) | `pass_qr_code` = code | the booker's lane code is the party pass, see D5 |
| Pay | `checkPatch` (domain) + `MobilePaymentRepository.record` | one booking, one transaction: ledger, status, clears `link_expires_at`, history; replay by `payment_reference` | reuse `checkPatch`, `paymentStatusAfterPatch`, `methodToRail`; new repository does all lanes in one transaction |
| Read | `present()`, `moneyFor()`, `storedTotalFils` | quote, or stored figures as fallback | reuse `toMobileStatus`, `toMobilePaymentStatus`, `productsOut`, `storedTotalFils`, `toOffsetIso` |
| Items and time | `BookingRepository.confirm` | **one `booking_item` per service; all held reservations point at the first item** | lanes use exactly this shape, which the desk already handles |
| Retry | `IdempotentInterceptor` on the controller | same key, same answer | put it on the new controller |

### 1.6 Found on the way (outside this task, not fixing now)

These touch business routes, so each needs its own PR and Rafa's OK.

1. `lifecycle.controller.ts`: `POST /v1/bookings/:id/cancel` (and the other lifecycle
   routes) **do not check that a customer token owns the booking**, and they accept
   `nowMs` from the body. A customer who knows a booking id could cancel it, and could
   choose the clock the refund band is measured with.
2. `groups.controller.ts`: `POST /groups/:id/confirm` and `DELETE /groups/holds/:id`
   do not check the organiser either.
3. The business group confirm writes no `booking_status_history` rows.

---

## 2. Plan

### 2.1 The idea

- New controller in booking-api: `/v1/mobile-booking/group`. Business never calls it.
- **Create** = the existing group **hold** (called, not changed) + a **new confirm**
  that writes each member as an ordinary booking shaped like a single mobile DRAFT
  booking: all services as items, products, net / VAT / discount / deposit columns,
  `pending_payment`, `unpaid`, `link_expires_at`, `channel online`, `group_id` set.
- **The group is the booking the app sees.** `id` = `booking_group.id`. Totals are the
  sums of the lanes. `expires_at` = the lanes' window (the same on all).
- **Why lanes and not a new table:** the salon calendar, the double-booking guard
  (`staff_reservation` exclusion constraint), check-in, no-show and the sweepers all
  work on booking rows. A new table would be invisible to the salon and would hold no
  time.
- **Why the business is safe:** the business already shows (a) party lanes with
  `group: {id}` and (b) single mobile DRAFT bookings with `payment.state` unpaid and a
  link window. A mobile group lane is just (a) + (b). No new status, no new enum, no
  new shape.
- customer-api keeps all its checks, then makes **one** call instead of hold + confirm.

### 2.2 Database changes (additive only)

One migration in booking-api, `prisma/migrations/<ts>_mobile_group_booking`, plus 4
optional fields in `schema.prisma`:

| Column | Type | Why |
| ------ | ---- | --- |
| `booking_group.source` | `TEXT NULL`, CHECK `source IS NULL OR source = 'mobile'` | NULL = made by the desk (every row today). `'mobile'` = made by the new path. The mobile read and list only treat `'mobile'` groups as GROUP, so desk parties look exactly as they do today. |
| `booking_group.deposit_percent` | `SMALLINT NULL` | the percent the deposit was worked out with |
| `group_participant.age_group` | `TEXT NULL`, CHECK in (`adult`, `child`) | drives the child price, read back in `members[]` |
| `group_participant.client_ref` | `SMALLINT NULL` | the app's `ref`, echoed back |

Not added, on purpose:

- **No `group` value in the `BookingType` enum.** `booking_type: "GROUP"` is derived
  on the mobile read from `group_id` + `source = 'mobile'` (CLAUDE.md rule 4: derive,
  do not store). A new enum value could reach business screens and cannot be removed
  from Postgres later.
- **No pass code column.** The party pass is the booker's lane `booking.code`, which
  the desk can already look up. See D5.
- **No money columns on the group.** Lanes already have `net_fils`, `tax_fils`,
  `discount_fils`, `deposit_fils`, `promo_code`, plus `booking_product`.

Business impact: none. Business code never selects these columns, Prisma adds them as
optional fields, and rows the business writes get NULL. Rollback: leave the columns,
they are harmless. Migration written with `IF NOT EXISTS` so it can run twice
(CLAUDE.md rule 3).

### 2.3 New routes (booking-api, all behind `MOBILE_GROUP_BOOKING`)

| Route | Does |
| ----- | ---- |
| `POST /v1/mobile-booking/group` | create the party in one call, answers spec §6 |
| `GET /v1/mobile-booking/group/:groupId` | read it back, same §6 shape |
| `PATCH /v1/mobile-booking/group/:groupId` | record the payment for the whole party |
| `POST /v1/mobile-booking/group/:groupId/cancel` | cancel every member |
| `POST /v1/mobile-booking/group/availability` | same answer as `POST /v1/bookings/availability/group`, so customer-api stops calling a business route |

Flag OFF: every one answers **404**, as if it did not exist.

Route clash check: the existing `GET /v1/mobile-booking/:id` matches one path segment
only, so it cannot swallow `/group/<id>`. There is no `POST` or `PATCH` on
`/mobile-booking/:id` that could swallow `POST /mobile-booking/group`. The new
controller is still registered **before** `MobileBookingController`, and
`route-order.spec.ts` gets cases to prove it.

### 2.4 What create does, step by step

1. customer-api validates as today (salon, `foreign_id`, stylists, accounts,
   `start_time` rules), now **allowing products**.
2. customer-api sends one call to `POST /v1/mobile-booking/group` with members
   (`customerId` or `guestName`, `ref`, `age_group`, services, products, stylist),
   `date`, `start_time`, the money figures, `deposit_percent`, and an
   `Idempotency-Key` (derived the same way `POST /booking` does it).
3. booking-api, in order:
   1. flag check, then the contract check (party size, one `self` = the token, etc.)
   2. services per member (`context.loadServices`), products per member
      (`checkProducts`, needs `PRODUCTS_FROM_PLATFORM` on, same as single)
   3. `groupMoney()`: child price, VAT once on the party, deposit, lane shares
   4. compare with the app's figures: 422 `amount_mismatch` with the right number
   5. `linkWindow()`: 422 if the start is under 2 hours away (same as single)
   6. `GroupHoldHandler.execute` (arrive together, organiser pays all), **unchanged**
   7. `MobileGroupConfirmRepository.confirm`, **one transaction**:
      - lock the hold, re-plan the party (`planParty`), refuse `slot_taken` if it no
        longer fits
      - per member: a `booking` row (`pending_payment`, `unpaid`, `link_expires_at`,
        `channel online`, `group_id`; guests keep `customer_id = toUuid(groupId)` as
        today), one `booking_item` per service (child price applied), its
        `booking_product` rows, its share of net / VAT / deposit, a status history
        row, and the hold's reservations re-pointed at its first item
      - `group_participant`: `booking_id`, `share_fils`, `age_group`, `client_ref`
      - `booking_group`: `source 'mobile'`, `deposit_percent`, `active_count`; status
        stays derived (`draft` while the party is unpaid)
      - outbox: the same `booking.confirmed` event per lane that a single DRAFT writes
      - delete the hold
   8. read it back and answer 201
4. Anything fails after step 6: release the hold. The transaction in step 7 is all or
   nothing, so no half party is ever written.

Money rules in `groupMoney()` (pure, one place):

- service price from the salon record; `age_group child` pays 50% of each service
  (`Money.percent(50)`), products never discounted
- VAT 5% **once** on the party's taxable base, then split to lanes with
  `Money.allocate` so the lanes add up exactly (CLAUDE.md rule 2: round once)
- deposit = `deposit_percent` of the party total, rounded to fils once, split to lanes
  the same way
- the spec's own example checks out: 685 net, 34.25 VAT, 719.25 total, 143.85 deposit

### 2.5 Every piece

#### booking-api (`../gostyle-booking-api`)

| # | Piece | Create / modify | File | Touches business? | Tests to add |
| - | ----- | --------------- | ---- | ----------------- | ------------ |
| B1 | Migration + schema | CREATE migration; MODIFY `schema.prisma` (4 optional fields) | `prisma/migrations/<ts>_mobile_group_booking/migration.sql`, `prisma/schema.prisma` | No. Nullable, never read by business. | migration runs twice cleanly; `prisma/proof-mobile-group.sql`: bad `age_group` and bad `source` MUST FAIL |
| B2 | Flag | CREATE | `src/interface/http/mobile-group.flag.ts` (`MOBILE_GROUP_BOOKING=true` turns it on, same pattern as `PRODUCTS_FROM_PLATFORM`) | No | off -> 404 on every new route |
| B3 | Group money | CREATE | `src/domain/booking/group-money.ts` + `.spec.ts` | No (pure) | child 50%; products not discounted; VAT once; lanes add up exactly; deposit 143.85 from 719.25; spec example end to end; 8 members with odd fils |
| B4 | Group contract | CREATE | `src/domain/booking/mobile-group-contract.ts` + `.spec.ts` | No (pure) | every §7 code: `invalid_party_size`, `member_no_services`, `invalid_member_kind` (none, two, self not the token), `member_id_required`; only `DRAFT`, `BOOKED`, `GROUP` accepted |
| B5 | Party diary helper | CREATE | `src/infrastructure/persistence/party-diary.ts` | No. It is a copy of the private `freshContext` in `group-confirm.repository.ts`; that file is not changed. A later business PR can switch it over after Rafa's check. | covered by B6 tests |
| B6 | Confirm repository | CREATE | `src/infrastructure/persistence/mobile-group-confirm.repository.ts` + `.spec.ts` | Reads and writes shared tables, **same row shapes the business already handles**. Does not call or change `GroupConfirmRepository`. | spec: one booking per member, all items, products, figures add up, hold deleted, re-plan refusal writes nothing, expired hold -> 410. Proof SQL: a desk booking on a lane's stylist and time MUST FAIL; the payment link sweeper expires every lane and frees the chairs |
| B7 | Create handler | CREATE | `src/application/commands/mobile-group-booking.handler.ts` + `.spec.ts` | Calls `GroupHoldHandler.execute` / `.release` **unchanged** | hold released on every failure after it; hold 409 -> `slot_taken`; products refused when `PRODUCTS_FROM_PLATFORM` is off; `amount_mismatch` carries the right figure; start under 2 hours -> 422 |
| B8 | Controller + DTOs | CREATE; MODIFY `availability.module.ts` and `persistence.module.ts` (register the new classes, lines added only) | `src/interface/http/mobile-group-booking.controller.ts` | Module lists only | `route-order.spec.ts`: new routes reachable; `/mobile-booking/:id` and `/mobile-booking/busy` still reachable; DTO spec for field errors in the contract envelope |
| B9 | Read | CREATE | `src/application/queries/mobile-group-read.handler.ts` + `.spec.ts` | Reads only | organiser sees all; registered member sees it (D3); stranger and bad id -> 404, never 403; a desk group id -> 404; a lane cancelled at the desk shows as CANCELLED |
| B10 | List shows GROUP | MODIFY | `src/application/commands/mobile-booking.handler.ts` `list()`; `src/infrastructure/persistence/booking.repository.ts` `customerPage` (also select `groupId`) | **No.** Mobile-only route; `customerPage` has no other caller. Only rows of `source 'mobile'` groups change; every other row is byte for byte the same. | snapshot: flag off -> identical output; flag on -> GROUP row with group `id`, `member_count`, party `total`; a desk party lane stays SINGLE |
| B11 | Pay the party | CREATE | `src/infrastructure/persistence/mobile-group-payment.repository.ts` + `.spec.ts`; handler method in B7's file | No. `MobilePaymentRepository` is not changed. | all lanes in one transaction; the amount split by lane total adds up; `payment_reference` stored on the booker's lane (it is unique), replay returns the same party; expired -> 409; already paid -> 409; `PARTIALLY` below the deposit refused (`checkPatch`) |
| B12 | Cancel together | CREATE | `src/application/commands/mobile-group-cancel.handler.ts` + `.spec.ts` | Calls `LifecycleHandler.execute` per lane **unchanged** (the same function `/bookings/:id/cancel` uses) | organiser only; every lane checked first, nothing cancelled if one cannot be; a retry finishes a half-done cancel; a lane already cancelled is skipped |
| B13 | Availability alias | CREATE (route in B8's controller) | calls `GroupAvailabilityHandler` **unchanged** | No | answer equals the business route's answer for the same body |
| B14 | Docs | MODIFY | `docs/api/MOBILE.md`, new section | No | `scripts/verify-docs.mjs` passes |

Not changed at all in booking-api: `groups.controller.ts`, `group-hold.*`,
`group-confirm.*`, `group-availability.*`, `bookings.controller.ts`,
`lifecycle.*`, `mobile-payment.repository.ts`, every sweeper, every enum, every
existing column.

#### customer-api (this repo)

| # | Piece | Create / modify | File | Touches business? | Tests to add |
| - | ----- | --------------- | ---- | ----------------- | ------------ |
| C1 | Flag | MODIFY (1 setting) | `config/settings/base.py`: `GROUP_BOOKING_V2 = env.bool("GROUP_BOOKING_V2", default=False)` | No | |
| C2 | Client calls | CREATE functions | `apps/salons/booking_api.py`: `create_group_booking`, `read_group_booking`, `pay_group_booking`, `cancel_group_booking`, `plan_group_v2` | No | |
| C3 | Accept products and money | MODIFY behind flag | `apps/salons/group_serializers.py`: products allowed (variant ids, amount, quantity); money fields and `deposit_percent` read and forwarded | No | flag off: `products_not_supported` as today; flag on: products forwarded |
| C4 | Create | MODIFY behind flag | `apps/salons/group_views.py` `GroupBookingCreateView`: same checks, then **one** call; forward the key; map booking-api refusals to spec §7 | No. Flag off = today's path, untouched. | all 95 existing group tests pass with flag off; flag on: one upstream call, body shape, 201 passthrough, 409 `slot_taken`, 422 `amount_mismatch` passthrough, timeout -> 503 |
| C5 | Read and pay | MODIFY behind flag | `apps/salons/views.py` `BookingDetailView.get` / `.patch`: on 404 from the single route, try the group route | No | single booking unchanged; group id read; group id paid; unknown id still 404 |
| C6 | List | none | `BookingListView` needs no change: GROUP rows come from booking-api | No | test that GROUP rows get `salon`, `can_cancel` like any row |
| C7 | Cancel route | CREATE | `apps/salons/urls.py` + view: `POST /api/v1/booking/<uuid>/cancel` (group only for now, D4) | No | organiser cancels; stranger 404; single id 404 |
| C8 | Times in salon time | MODIFY behind flag | group read and create answers re-written to the salon's offset (the helpers already exist in `group_translate.py`) | No | `+04:00` Dubai example |
| C9 | Docs | MODIFY | `docs/BOOKING_GROUP_API.md` (§2 lookup is 200 now; §6 to §8 rewritten) | No | |

### 2.6 Build order (fastest safe path) and rough time

Each step is its own PR, mobile and business never mixed. Every deploy goes out with
the flags **OFF**, then Rafa checks the business web by hand (create, drag, cancel a
booking), then the flag goes on in staging only.

| Step | What | Repo | Time |
| ---- | ---- | ---- | ---- |
| 0 | Housekeeping. Split `feat/booking-products` into 3 PRs: business calendar (12 commits), tenancy fixes (2), group `targetMins` (3, mobile). Park the uncommitted `participantId` work in both repos (the new path gives `members[].id` natively, and it edits a business route's answer). | both | 0.5 day |
| 1 | B1, B2, B3, B4: migration, flag, money and contract rules with their specs | booking-api | 1.5 days |
| 2 | B5 to B9, B14: create and read, proof SQL, docs | booking-api | 3 days |
| 3 | C1 to C4, C8 (create), C5 read half: the app can book and see the pass | customer-api | 1 day |
| 4 | B11 + C5 pay half: pay the party | both | 1.5 days |
| 5 | B10 + C6: GROUP rows in My Bookings | both | 0.75 day |
| 6 | B12 + C7: cancel together | both | 1.5 days |
| 7 | B13 + switch availability to the alias; C9 docs | both | 0.5 day |
| 8 | Staging run of the whole flow, business web check, flags on | both | 0.5 day |
|   | **Total** | | **about 9 to 11 working days** |

After step 3 the app team can already test create and read on staging.

### 2.7 Open decisions (with the default I suggest)

| # | Question | Suggested default |
| - | -------- | ----------------- |
| D1 | Deposit: the spec says `deposit_percent` from the app; single bookings use the server's ladder. | Server percent from config, **20** by default. The app's value must match it, or 422 `amount_mismatch` with the right number. |
| D2 | Tier discount and promo codes for a party? | None in v1: `discount` is 0, `promo_code` echoed and not applied (as single does today). |
| D3 | Can a registered member (not the booker) see the party? | Yes, read only. Only the booker can pay or cancel. |
| D4 | The spec has no cancel route. Which path does the app call? | `POST /api/v1/booking/:id/cancel`, group only for now. Ask the app team. |
| D5 | What is the party pass? | The booker's lane code (`GS-nnnn`). The desk can scan it today with no business change. |
| D6 | Is a child's service `amount` in the payload the full price or the halved one? (The spec example does not add up.) | Only the totals are checked, as in single booking, so either works. The response shows the halved price. Ask the app team to confirm. |

Waiting on Olu already (from earlier notes): per-salon timezone. Until then the group
answers are shown in the salon's own offset by customer-api (C8).

### 2.8 Risks, and what guards them

| Risk | Guard |
| ---- | ----- |
| A future business change to the group hold also changes mobile | the hold is called, not copied; its existing specs plus B7's spec run on every PR |
| The salon sees a mobile party before it is paid | same as a single mobile DRAFT today: `pending_payment` with a link window, freed by the sweeper if unpaid |
| The payment link sweeper sends one reminder event per lane | messaging is not built yet (events are only logged); revisit when it is |
| The desk cancels or moves one lane of a mobile party | allowed, as for desk parties today; the mobile read shows that member as cancelled |
| The business `POST /groups/:id/confirm` is called on a mobile group | its hold is already deleted, so it answers 410 and writes nothing |
| `party-diary.ts` is a copy of private code | written down here; one small business PR later removes the copy |
