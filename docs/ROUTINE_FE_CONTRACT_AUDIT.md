# Routine booking: the app team's contract vs our code (audit)

Date: 2026-09-29. **Audit only: no code was changed.**

**Status: built.** booking-api B1 to B7 are merged (`cb84fe7`); customer-api
C1 to C7 are on `feat/routine-contract`. The final contract for the app team is
`docs/ROUTINE_BOOKING_API.md`. The switches and their order: `docs/DEPLOYMENT.md`.

- Contract: `docs/routine-booking-fe-contract.md` (as received, stops in §3).
- Our draft of the missing sections: `docs/routine-booking-fe-contract-draft.md`.
- Our design: `docs/SERIES_BOOKING_AUDIT.md`.

Code read:

- **customer-api** at `imransid/main` = local `main` = `896485f`. Careful: `origin`
  now fetches from **Entity-tech-solutions/gostyle-customer**, whose `main`
  (`0731daa`) has **none** of the routine work (20 commits behind). The live code
  is `imransid/main`. `origin/main` only has one unrelated fix on top
  (`c201489`, map endpoint demo fallback).
- **booking-api** at `origin/main` = `0798643` (clean checkout).

Tags: **SAME** nothing to do. **RENAME** shape only, customer-api.
**LOGIC** booking-api must change. **DECISION** needs your answer (section 3).

---

## 0. Short version

1. **Most of the contract is a new shape over the logic we already have.** The
   preview is our `dry_run`, the create is our create, the move is our
   RESCHEDULE. The new routes can live in customer-api and call the same
   booking-api routes. The old `/booking/series` routes do not change.
2. **Six real logic gaps in booking-api:**
   (a) "Any Available Expert" does not exist (`stylist_required`);
   (b) sessions past 90 days get `free: null`, not `available`;
   (c) no `reason` for a busy session (only customer-api's hours reasons);
   (d) alternatives: other stylists, days before, only ±2 days, and cut to 3
   **before** customer-api removes closed days, so a closed day often gets none;
   (e) a pick is not checked against what was offered (any free time within 90
   days is accepted today);
   (f) the read has no booking-shaped money (whole plan split, per service
   amounts, passes).
3. **Two things the contract cannot do as written**, so the draft asks for them:
   the create needs the preview's **`start_time`** (else the server cannot
   rebuild the cadence when session 0 took an alternative), and **alternatives
   must stay with the session's stylist** (they are bare strings, and the create
   has one `stylist_id`).
4. **Decision 3 (far sessions show `available`) is possible.** booking-api's
   availability has no 90 day limit and the stylists' week is a weekly template,
   so any date can be checked against today's calendar. Caveat: it is a
   forecast, nothing is held that far out.
5. **GET `/booking/{id}` with a routine id answers 404 today.** It needs a small
   fallback, which touches `BookingDetailView` (E.3 had kept it untouched).
6. **The contract's own example start (20:00 Dubai) would be refused today**:
   booking-api's day is 10:00 to 22:00 at +06:00, which is 08:00 to 20:00 in
   Dubai (K18, per-salon time zone, waits for Olu). Real offers from
   nearest-available never show it, only the example does.

---

## 1. Field by field

### 1.1 §1 Endpoints

| # | Contract | Today | Change | Tag |
| - | -------- | ----- | ------ | --- |
| E1 | `POST /booking/routine-preview` | `POST /booking/series` with `dry_run: true` and a `time` | New customer-api route. Maps the shape, calls the same booking-api dry run, same hours check | RENAME + LOGIC (1.2, 1.3) |
| E2 | `POST /booking/routine` | `POST /booking/series` (no `dry_run`) | New customer-api route, same flow (plan first, hours, then book) | RENAME + LOGIC (1.5) |
| E3 | `PATCH /booking/:id/sessions/:session_id` | `PATCH /booking/series/:id` `{action: RESCHEDULE, session_id, date, time, stylist_id?}` | New customer-api route: ISO `start_time` to salon day + HH:MM, then today's RESCHEDULE. Add the stylist check (`foreign_id`, `stylist_mismatch`) that today's RESCHEDULE does not do in customer-api | RENAME |
| E4 | `GET /salon/:id/services` | live | none | SAME |
| E5 | `GET /salon/:id/stylists?service_ids=` | live, never returns "Any Available Expert" (`BOOKING_EXPERT_API.md:91`) | none | SAME |
| E6 | `GET /booking/nearest-available/:salon_id` | live: `offers[{start, end, bookings_today, stylist}]`, salon offset, 15 minute grid, clamped to booking-api's day | none | SAME |
| E7 | `GET /booking/:id` "reused as-is" | routine id: **404** (booking-api `not_found`, after a group lookup when `GROUP_BOOKING_V2` is on). A session's booking id: the plain single booking, **no `booking_type`, no `series_id`** (`mobile-booking.handler.ts:1195-1334` never reads them) | customer-api: after the 404s, ask the routine read; if it answers, give the routine in §5 shape. booking-api: add `booking_type` and `series_id` to the single read (mobile route only) | LOGIC + DECISION Q6 |
| E8 | `PATCH /booking/:id` "reused as-is" | routine id: 404 | customer-api: a routine id answers 409 `already_paid` (pays at the salon), like a single `PAY_AFTER_CHECK_IN` booking | LOGIC (customer-api) |
| E9 | `GET /bookings?filter=recurring` "only the extra array" | rows are a routine summary: `id, booking_type, salon_id, status, frequency, time, stylist, services{id,name}, payment_plan, counts, next_session, created_at` + `salon`. **No `sessions`**, no money | Add the booking-shaped fields and `sessions` (additive; old fields stay) | LOGIC + RENAME |

### 1.2 §2 Preview request

| # | Contract | Today | Change | Tag |
| - | -------- | ----- | ------ | --- |
| R1 | `salon_id` | same (storefront uuid, customer-api resolves branch and tenant) | none | SAME |
| R2 | `service_ids: string[]` | `services: [{id}]` | map | RENAME |
| R3 | `stylist_id` optional | customer-api serializer allows null (`series_serializers.py:59`), booking-api refuses: `stylist_required` (`mobile-series-contract.ts:297-305`), and `execute` does `stylistId!.trim()` (`mobile-series.handler.ts:287`) | Any Available Expert (2.7) | LOGIC (decision 1 made) |
| R4 | `start_time` ISO with offset | `start_date` + `time` HH:MM on the salon's clock | parse, put on the salon's zone, split. See 2.1 | RENAME |
| R5 | `cadence`: `week`, `fortnight`, `month` | `frequency`: `WEEKLY`, `EVERY_2_WEEKS`, `MONTHLY` (+ `DAILY`, `CUSTOM`, not offered in the new route) | map; `invalid_frequency` becomes `invalid_cadence` | RENAME |
| R6 | `sessions` 1 to 6 | 2 to 6 (`MIN_SESSIONS`, `mobile-series.ts:49`) | keep 2 to 6, contract text changes | DECISION (made: 2) |
| R7 | (none) | `payment_plan` required | new route always sends `PAY_AT_SALON` | RENAME |

### 1.3 §2 Preview response

| # | Contract | Today | Change | Tag |
| - | -------- | ----- | ------ | --- |
| P1 | `cadence` | `frequency` | map | RENAME |
| P2 | `sessions[].index` from 0 | same | none | SAME |
| P3 | `date` | same (salon-local day of `start_time`, `series_translate.py:189`) | none | SAME |
| P4 | `start_time` / `end_time`, salon offset, from the duration | same (end = start + quote duration, `mobile-series.handler.ts:1081-1082`; offset rewritten by customer-api) | none | SAME |
| P5 | `available` boolean | `free`: true / false, **null past 90 days** (`mobile-series.handler.ts:1072-1073`) | rename; check far sessions too (2.10) | RENAME + LOGIC |
| P6 | `reason` when not available | customer-api `refusal: {code, message}` for `salon_closed`, `too_soon`, `outside_hours` only. booking-api's "not free" has no reason | booking-api gives `stylist_unavailable`; customer-api maps. See 2.3 | LOGIC + RENAME |
| P7 | `stylist: {id, name}`, `null` when not available | `stylist_id` string, always the asked one | name from customer-api's roster (`_roster`), null when not available | RENAME |
| P8 | `alternatives`: ISO strings, up to 3, same day first, then next open day | objects `{date, time, start_time, stylist_id}`, other stylists allowed, days −2 to +2, cut to 3 before the hours filter | see 2.4 | LOGIC + RENAME |
| P9 | `per_session_total` | `money.plans.PAY_AT_SALON.sessions[0].total` | map | RENAME |
| P10 | `plan_total` | `money.plans.PAY_AT_SALON.total` | map | RENAME |
| P11 | (none) | extra: `dry_run, time, stylist_id, payment_plan, all_free, money` (3 plans), `rules, available_times, later, picked, moved_from_day_of_month` | dropped by the new route | RENAME |

Note on P9: each session is priced by its own day's quote, so two sessions can
differ (a price change, a tier). Then `per_session_total × n` is not
`plan_total`. The draft says `plan_total` is the true sum.

### 1.4 §2 Rules

| # | Contract rule | Today | Tag |
| - | ------------- | ----- | --- |
| U1 | A busy session does not block the plan; the create takes the resolved time | yes: `picks` replace busy sessions (`applyPicks`) | SAME (shape: RENAME, 2.5) |
| U2 | Nothing held; a lost start fails at create with `slot_taken` | yes, but the code is `session_not_free` (409, field `sessions[i]`) | RENAME to `slot_taken` |
| U3 | A closed day comes back `salon_closed` with the nearest open day | `salon_closed` yes (customer-api); alternatives on a closed day: often **none** (2.4) | LOGIC |

### 1.5 §3 Create payload

| # | Contract | Today | Change | Tag |
| - | -------- | ----- | ------ | --- |
| C1 | `salon_id` | same | none | SAME |
| C2 | `cadence` | `frequency` | map | RENAME |
| C3 | `services[{id, amount}]` | `id` used; `amount` accepted and **never checked** ("echoed by the app, never trusted", `mobile-series.controller.ts:66-77`). The single create does not check it either | none: not checked, like the single create (Q5: no) | SAME |
| C4 | `products: []` | products accepted on session 1 (D7) when the catalogue is on | new route: must be empty, else `products_not_supported` | RENAME |
| C5 | `stylist_id` | see R3 | | LOGIC |
| C6 | `sessions[{index, date, start_time, end_time}]` | `picks[{index, date, time, stylist_id}]`, only for sessions not on the cadence. A pick is **not** checked against the alternatives | map + check. See 2.5 | RENAME + LOGIC + DECISION Q1, Q3 |
| C7 | `amount_without_tax`, `tax_amount`, `discount`, `total` | checked for the whole plan, `amount_mismatch` with `expected` (`checkRoutineMoney`) | none | SAME |
| C8 | `promo_code: null` | no field | refuse non-null: `invalid_promo` | RENAME |
| C9 | `advance_paid_amount: 0`, `due_amount = total` | no fields | check: `amount_mismatch` with `expected` (as the single create) | RENAME |
| C10 | `payment_status: DRAFT` | `payment_plan: PAY_AT_SALON`; sessions booked `PAY_AFTER_CHECK_IN` (`mobile-series.handler.ts:1227`) | DRAFT maps to PAY_AT_SALON; anything else `invalid_payment_status`. **Never** pass DRAFT to the single create: there it means a payment link that expires | RENAME (decision 4 made) |
| C11 | `status: BOOKED` | no field | else `invalid_status` | RENAME |
| C12 | `booking_type: ROUTINE` | implied by the route | else `invalid_booking_type` (new code) | RENAME |
| C13 | `Idempotency-Key` | yes; derived from the bytes when absent (`_idempotency_key`); none on dry runs | none | SAME |
| C14 | 201 response | the hub (`MobileSeriesView`) + salon card | the §5 routine object (draft §3) | LOGIC (money split) + RENAME |

---

## 2. The special checks

### 2.1 `start_time` (one ISO time) vs our `start_date` + `time`

- Today the app sends a day and HH:MM on the salon's clock. customer-api turns
  them into one instant and onto booking-api's clock (+06:00), both ways
  (`series_translate.py:48-63`). A salon whose offset changes between the
  routine's days is refused (`TimeNotConstant`, `invalid_time`).
- New route: parse `start_time`; **no offset is `invalid_time`**; put it on the
  salon's zone; take its date and HH:MM; the rest is today's code.
- **Offset not the salon's** (for example `20:00+06:00` for a Dubai salon): it
  is still one exact instant, so we read it as that instant (18:00 Dubai) and
  answer in the salon's offset. The single create does the same
  (`toBranchMoment`, any offset, `mobile-booking.handler.ts:268`). The app echoes
  starts from nearest-available, which are always in the salon's offset, so a
  mismatch only comes from an app bug, and then the answer shows the real time.
  Recommended: accept (Q7). The strict option: refuse unless the offset equals
  the salon's at that instant (`invalid_time`).
- The cadence runs on the salon's wall clock: "20:00 every month" stays 20:00.

### 2.2 The month clamp

We do the same for February and **go back** afterwards. `MONTHLY` keeps the
first session's day of the month and falls back to the month's last day
(`recurrence.ts:257-283`, the desk's own expander):

| First session | 2nd | 3rd | 4th |
| ------------- | --- | --- | --- |
| 31 Jan 2027 | 28 Feb | **31 Mar** | 30 Apr |
| 31 Jan 2028 | 29 Feb | 31 Mar | 30 Apr |
| 30 Jan 2027 | 28 Feb | 30 Mar | 30 Apr |

The contract's text ("+1 calendar month, clamps") could be read as chaining
(31 Jan, 28 Feb, 28 Mar). Ours never drifts; EXTEND also goes back to the 31st.
The draft states our rule. Only a doc change.

### 2.3 Preview `reason` codes

| Code | Who knows it today | Status |
| ---- | ------------------ | ------ |
| `salon_closed` | customer-api (`start_refusal`: the salon's own hours that weekday, or shut by hand) | live, as `refusal.code` |
| `outside_hours` | customer-api (visit would not start and finish inside the hours) | live |
| `too_soon` | customer-api (past, or inside the services' longest notice) | live |
| `stylist_unavailable` | booking-api knows "not free" (`DayOffers.isFree`) but says no reason | **new**, small |

- `stylist_unavailable` covers "off that day" and "busy then". The availability
  answer also lists per-stylist refusals (`AvailabilityView.refusals`), so a finer
  split (`stylist_off` / `stylist_busy`) is possible later if the app wants it.
- If two apply, the hours reason wins (a closed day says `salon_closed`, not
  `stylist_unavailable`). Order: `salon_closed`, `outside_hours`, `too_soon`,
  `stylist_unavailable`.
- booking-api itself never knows a closed day: `closureReason` is never set
  (the fixture removed its invented Sunday: "Real closures come from the roster
  service, which does not exist yet", `fixture-booking-context.ts:401-408`).
  So closed days stay customer-api's to say, as today.

### 2.4 Alternatives

Today (`pickAlternatives`, `mobile-series.ts:1230-1281`; days in
`mobile-series.handler.ts:166,1099-1116`), up to 3, in this order:

1. same stylist, same day, another time (nearest first);
2. **another stylist**, same day, same time;
3. same stylist, same time, another day, **days −2 to +2** (on a tie, the day
   **before** wins);
4. **another stylist**, same day, another time.

Then customer-api removes the ones outside the salon's hours
(`within_hours`, `series_translate.py:347-393`). **The cut to 3 happens first.**

Why a closed day gets none: when the salon is shut by hand (or its published
hours say closed) but the stylists are on the weekly roster, booking-api sees a
normal day and fills all 3 places with same-day times; customer-api then drops
all 3. (When the roster has everyone off that weekday, booking-api does offer
other days, but it may offer the day before.)

Differences from the contract: other stylists (the contract's bare strings and
single `stylist_id` cannot carry them), days before the session, only ±2 days,
and the cut before the hours filter.

Needed (LOGIC, booking-api, only when the new route asks):

- a routine rule: **same stylist only**; same day, nearest time first; then the
  following days (+1, +2, ... up to 7, never on another session's day), same
  time first, then nearest time;
- return a **longer ranked pool** (for example 12). customer-api removes what is
  outside the hours, then keeps the first 3. So a closed day naturally shows the
  next open day. booking-api does not need to learn closed days.
- for a session past 90 days, the window may go past 90 days too (2.10).

### 2.5 Create with an explicit `sessions[]`

How to map it safely:

1. **The anchor.** The cadence is rebuilt from the preview's `start_time`. The
   contract's create does not carry it. Without it, if session 0 took an
   alternative (say 19:00 instead of 20:00, or the next day), the server would
   rebuild the cadence from the wrong time or day. Recommended: add
   `start_time` to the create (Q1). Fallback: "session 0 is never an alternative;
   if the first start is gone, go back to step 5".
2. **Shape.** 2 to 6 entries, `index` 0..n-1 in order (`invalid_sessions`),
   `date` = salon date of `start_time` (`date_mismatch`), no two on one day
   (`session_day_taken`).
3. **Each session:** equal to its cadence slot, else it becomes a `pick`. A pick
   must pass the **alternatives rule** of 2.4: same stylist, same day or a later
   day inside the window, not another session's day, inside the hours, free now.
   Else `session_not_offered`. Recommended check: the rule, not "is it one of
   the exact 3 shown" (Q3): the top 3 can change between preview and create
   when another time frees up, which would refuse a fair pick. The rule still
   never accepts a random schedule.
4. **`end_time`:** refuse a mismatch with `invalid_window`, like the single
   create (`mobile-booking.handler.ts:378-385`). Recommended (Q8): it catches an
   app that shows the wrong length.
5. Today's `picks` check only the index, a date within 90 days and the time
   grid (`checkPicks`, `mobile-series-contract.ts:476-528`). **Any free time is
   accepted today.** The new check lives in booking-api (option on the request,
   so the old route keeps today's behaviour).

### 2.6 `services[].amount`

Not checked today, in the routine or in the single create. Only the four totals
are checked (they are what the customer pays). **Decided (Q5): it stays that
way.** A line's price can differ by stylist or by session date, so checking the
lines could refuse a good booking. The amount is echoed back, not checked.

### 2.7 "Any Available Expert"

- booking-api has no skills for platform stylists (`platform-staff-roster.ts:162-176`),
  so "does all the services" cannot be answered there. customer-api can: the
  same measure as the Expert step (`stylist_service_coverage`, used by
  `_check_stylists`, `group_views.py:159-201`).
- Plan: customer-api, when `stylist_id` is null, sends the list of stylists who
  do all the services (`stylist_candidates`). If the list is empty:
  `no_stylist_available` (new code). booking-api, for each candidate, counts the
  sessions free at their cadence slot (far ones too, 2.10), with `DayOffers`
  (one availability call per day, already cached), and picks the most. Ties:
  free on session 0; then the fewest minutes already taken on session 0's day
  in booking-api's own diary (`day.staffBookings`; a stylist the engine does
  not have that day comes last); then a fixed shuffle per customer (a hash of
  customer id and stylist id, lowest first), then the id. **Not the roster's
  `bookingsToday`**: platform publishes 0 for everyone, so every tie would go
  to the lowest id and one stylist would get every quiet-day routine (Rafa,
  2026-09-29). The shuffle keeps one customer on one stylist (preview and
  create agree) and spreads different customers. Never random. Pure function
  `chooseRegularStylist` in `mobile-series.ts` with a spec.
- The picked id answers as every session's `stylist` and is stored as the
  routine's `preferred_staff_id` (column exists; **no migration**).
- The create should get the preview's stylist back. With null, it picks again
  over the sent sessions, and every bookable one must be free for that stylist.

### 2.8 `GET /api/v1/booking/{id}` with a routine id

Today: `BookingDetailView` forwards to `GET /v1/mobile-booking/<id>`, which
looks in the booking table only: **404** `not_found`
(`mobile-booking.handler.ts:1037-1056`). With `GROUP_BOOKING_V2` on it then
tries the group read, also 404, passed through. It never tries the routine.

Needed: behind the new flag, after the 404s, ask `GET /v1/mobile-booking/series/<id>`
with the caller's token; a 200 answers the routine in §5 shape; anything else
the original 404. A booking id answers exactly as today plus `booking_type` and
`series_id` (booking-api, additive, mobile route only). Series and booking ids
are both uuids, so the view cannot tell them apart without asking; the extra
call happens only for ids that are not a booking. This touches
`BookingDetailView`, which E.3 had kept untouched: Q6.

### 2.9 The error envelope

- Ours (both services) is the contract's (`booking-create.md` §9):
  `{detail, code, errors: [{field, code, message, expected?}]}`. A 422 has
  top `code: "validation_error"`; a 409 and a 404 have the error's own code at
  the top (`mobile-booking.error.ts:189-198`, `apps/accounts/exceptions.py:86-114`).
- Codes to rename in the new routes: `session_not_free` to `slot_taken`,
  `invalid_frequency` to `invalid_cadence`, RESCHEDULE's `invalid_sessions` on
  `session_id` to 404 `not_found`.
- Leaks in booking-api's other envelope `{statusCode, code, message, details, error}`,
  which the new routes should turn into ours: `IDEMPOTENCY_KEY_REUSED` (409,
  `idempotency.repository.ts:49-55`) and the flag-off 404 (only if the two flags
  disagree).
- The contract never wrote §7. The draft lists every code with its status.

### 2.10 Decision 3: far sessions and `available`

**Possible.** Why:

- `GetAvailabilityHandler` has no horizon limit; only the routine code skips far
  days (`later ? null`, `mobile-series.handler.ts:1073`) and keeps alternatives
  and picks inside 90 days (`:1105-1107`, `checkPicks`).
- The stylists' week comes from a weekly template (opening, closing, off
  weekdays: `platform-staff-roster.ts:20-37`), so any date has a roster. The
  diary that far is mostly empty. customer-api's hours check already runs on
  every session, far ones included.

What it means: "free on the calendar as it is today". It is a forecast. Nothing
is held (planned sessions are not reservations, so two routines can plan the
same far slot), a stylist's leave is not known, and the hourly job books the
session only when it is inside 90 days. If the time is gone then, the session
shows `NEEDS_ACTION`, never moved silently. The draft says so.

Needed (LOGIC, option on the request): compute `free` for far sessions, give
them alternatives past 90 days, accept their picks past 90 days, and treat a far
"not available" session at create like a near one (it must be its slot or a
picked alternative). They stay `planned` and the job books them as today.

### 2.11 Other findings

| # | Finding | Where |
| - | ------- | ----- |
| F1 | customer-api `origin` fetches Entity-tech-solutions, not imransid; its `main` lacks the routine work. Build from `imransid/main` | `git remote -v` |
| F2 | 20:00 Dubai (the contract's example) is outside booking-api's day for a 45 minute visit: `invalid_time` | `grid.ts:14-17`, `hold.repository.ts:6` |
| F3 | `DAILY` never skips a closed day in booking-api (no closures known); customer-api then refuses that session as `salon_closed`. Old route only; the new contract has no daily | `mobile-series.handler.ts:984-1019` |
| F4 | A replayed `dry_run` with the same key would answer 201, not 200 (the interceptor skips the handler, `res.status(200)` never runs). Not hit: customer-api sends no key on dry runs | `idempotent.interceptor.ts`, `mobile-series.controller.ts:256` |
| F5 | `booking-create.md` §4 says pay on arrival is refused with 402 when a deposit is due. The code now defers the deposit instead | `mobile-booking.handler.ts:443-458` |
| F6 | The Recurring tab rows are rewritten even with `SERIES_BOOKING_V1` off; the docs still say "always empty today". **Fixed in C7** (`BOOKING_LIST_API.md` and the OpenAPI text) | `views.py:2252-2254`, `BOOKING_LIST_API.md:75` |
| F7 | On every mobile route, `main.ts`'s global ValidationPipe runs before `mobileValidationPipe` (Nest runs global pipes first), so a bad DTO field answers **400 in booking-api's own shape** (`{statusCode, message[], error, code}`), not the 422 envelope of `booking-create.md` §9. The single create has the same issue. **Not fixed now** (Rafa, 2026-09-29): the new routes translate it in customer-api (C2); the single create gets its own small PR later | `main.ts:36-42`, `@nestjs/core/helpers/context-creator.js` (global, class, method, param order), `mobile-validation.pipe.ts` |
| F8 | **A pay-at-salon visit paid in full at the desk still shows the VAT as due.** The desk capture for an on-arrival booking (`confirmed` + `none_required`) takes `price_fils`, which is the NET price of the services (confirm stores it as the sum of the items' net prices). The single read's `total` is the VAT-inclusive quote, `advance_paid_amount` is what was captured, `due_amount` is total minus captured. Example: a 100 AED service is `total` 105; after the desk capture it reads `advance_paid_amount` 100, `due_amount` 5, `payment_status` FULLY_PAID. **Products are not captured at all** (price_fils holds the services only). Affects **every pay-at-salon booking captured at the desk: single, group and routine.** Not fixed in the routine branch (Rafa, 2026-09-29): B7 sums the visits exactly as the single read reports them, so the routine shows what its visits show; the capture gets its own audit and business PR later, with Rafa's OK and a desk check after the deploy | `money.repository.ts` `capture`, `confirm-booking.handler.ts:183` and `:266-291`, `mobile-booking.handler.ts:1317-1322` |
| F9 | Known edge case (left as it is, Rafa, 2026-09-29): **a routine visit that adds nothing can have had money taken.** The desk may capture a pay-at-salon booking any time before the day (the guard only needs `confirmed` + `none_required`). If the customer then skips it or cancels the routine, the refund bands refund or keep that money, and the session adds nothing to the routine's figures (skipped, cancelled and missed add nothing). The single read's `advance_paid_amount` still shows the capture, by design (what was handed over) | `money.repository.ts` `capture`, `lifecycle.ts` `cancellationOutcome` |

---

## 3. Decisions (answered by Rafa, 2026-09-29)

All as recommended, **except Q5: no** (the line amount is not checked).

| # | Question | Recommended | Why | Answer |
| - | -------- | ----------- | --- | ------ |
| Q1 | The create needs the anchor | **Add `start_time` to the create** (same value as the preview) | Else a session 0 on an alternative breaks the cadence | yes |
| Q2 | Alternatives with another stylist | **Same stylist only** | The contract's alternatives are bare times and the create has one `stylist_id`; decision 1 says one stylist | yes |
| Q3 | How strict is "one of the alternatives" | **The rule** (same stylist, same day or a later day in the window, free, inside hours), not the exact 3 shown | The exact 3 can change between preview and create | yes |
| Q4 | Routine `status` word | **ACTIVE / PAUSED / ENDED / COMPLETED** (as the Recurring rows already say) | The booking words (BOOKED, CONFIRMED_BY_SALON, ...) cannot say "paused". Other option: booking word in `status` + a new `routine_status` | yes |
| Q5 | Check `services[].amount` | Yes | The app draws those lines; cheap | **no** |
| Q6 | Touch `BookingDetailView` for routine ids | **Yes, behind `ROUTINE_CONTRACT_V1`, only after the 404s** | The contract reads the routine at `GET /booking/:id` | yes |
| Q7 | `start_time` offset not the salon's | **Accept, read as that instant** | Same as the single create; the answer shows the salon time | yes |
| Q8 | `end_time` mismatch | **Refuse (`invalid_window`)** | Same as the single create | yes |
| Q9 | Top-level `date` / `start_time` of a routine | **The next session** (last one when none left) | That is the pass the customer shows | yes |
| Q10 | Alternatives window | **Same day, then up to 7 days after**, never another session's day | "Next open day"; a holiday week still finds one | yes |

---

## 4. Build plan

Same rules as before: separate PRs per repo, additive only, every new behaviour
behind a flag **OFF by default**, tests before every commit, stop for your OK
after each step, you deploy.

**Flags.** customer-api `ROUTINE_CONTRACT_V1` (default `False`): off, the three
new routes are 404 and `GET`/`PATCH /booking/{id}` and the Recurring rows answer
exactly as today. booking-api `MOBILE_ROUTINE_CONTRACT` (default `false`): off,
a request carrying any new option is refused (422), so nothing changes for the
old route. The old `/booking/series` routes never send the new options.

**No database migration** is needed in either repo.

**Where.** booking-api: a new branch from `origin/main` in the main checkout
(clean today). Not `gostyle-booking-api-series`. customer-api: a new branch from
`imransid/main` only, and **never push to `origin`** (it pushes to 3 repos):
push by URL to imransid.

### 4.1 booking-api

| Step | What | Files | Tests |
| ---- | ---- | ----- | ----- |
| B1 | **Done: `bb4dcbd`.** Flag + the new optional DTO fields (`stylist_candidates`, `check_later`, `alternative_rule`, `alternatives_max`, `strict_picks`, `with_reasons`), refused when the flag is off | `mobile-series.flag.ts`, `mobile-series.controller.ts` | flag off: each new field 422; old body answers byte for byte as today (snapshot) |
| B2 | Far sessions: `free` checked on today's calendar, alternatives and picks past 90 days for those sessions, create treats them like near ones (still stored `planned`, booked by the job). **A session more than 90 days away keeps the routine's stylist** until it is booked (no stylist column on `series_occurrence`, no migration; Rafa, 2026-09-29): a far pick naming another stylist is `invalid_pick`, far alternatives offer the routine's stylist only. **Done: `3c25040`** | `mobile-series.handler.ts` (`check`, `alternativesFor`), `mobile-series-contract.ts` (`checkPicks` bound) | a 6 x monthly preview: sessions 4 to 6 have `free` true/false; a far pick past 90 days accepted only with the option; the create books 1 to 3, stores 4 to 6 planned; the job spec still books them |
| B3 | **Done: `8577553`.** `reason: stylist_unavailable` on a session that is not free | `mobile-series.handler.ts` | busy stylist; stylist off that weekday; free session has no reason |
| B4 | **Done: `faf0356`.** Routine alternatives rule: same stylist, same day first, then +1 to +7, a longer pool | new pure `routineAlternatives` in `mobile-series.ts` (old `pickAlternatives` untouched) | order, spacing, other session days skipped, pool size, days before never offered, window past 90 days for far sessions |
| B5 | **Done: `4074b62`.** Any Available Expert: pick one stylist from `stylist_candidates`, free on the most sessions | new pure `chooseRegularStylist` + `execute` | most free wins; ties (session 0, fewest diary minutes that day, fixed shuffle per customer, id); no candidate free at all; create re-picks and books all or nothing; stored as `preferred_staff_id` |
| B6 | **Done: `fb0e039`.** `strict_picks`: every pick must follow the B4 rule | `mobile-series.ts` + `execute` | a random free time refused (`session_not_offered`); a fair alternative accepted even if no longer in the top 3; old route still accepts any free time |
| B7 | **Done: `ec2dba2`.** Booking-shaped read, `?view=booking` on the hub and on `filter=recurring`: whole-plan money split, per service amounts, per session `pass_qr_code` and total, next session times. `booking_type` + `series_id` on `GET /v1/mobile-booking/:id` (flag on) | `mobile-series-read.handler.ts`, `mobile-booking.handler.ts` (`present`), `mobile-booking.controller.ts` | without `view`: byte for byte today; money = sum of sessions (skipped and cancelled out, planned at today's price); a single booking without a routine: `booking_type SINGLE`, `series_id null` |

Every step: `tsc --noEmit`, `vitest run`, `eslint`. The desk-job spec
(`mobile-series.desk-job.spec.ts`) and the desk series specs must pass unchanged.
After the deploy you check the business web by hand, as always.

### 4.2 customer-api

| Step | What | Files | Tests |
| ---- | ---- | ----- | ----- |
| C1 | **Done: `bfe1bbc` (with C2).** Flag + 3 routes (404 when off) | `config/settings/base.py`, `apps/salons/urls.py` (CRLF: keep `\r\n`) | flag off: 404, booking-api never called; `booking/<uuid>` and `booking/series/...` still resolve as today |
| C2 | **Done: `bfe1bbc`.** Pure translator `routine_contract.py`: `start_time` to salon day + HH:MM; cadence words; `service_ids`; `sessions[]` to picks (with the anchor); payment field checks; answer mapping (preview to contract shape, hub to routine object); code renames; booking-api's other envelope to ours. **`stylists[].avatar_url` comes back null from booking-api** (its single read never has one, and the B7 booking shape keeps that): customer-api fills it from its own stylist list (the same `avatar_url` as `GET /salon/{id}/stylists`) | new `apps/salons/routine_contract.py` | every field both ways; offsets (+04, +06, Z, none); 31 Jan monthly; each §4 refusal; code renames |
| C3 | **Done: `9c673c2`.** `POST /booking/routine-preview`: today's dry run + hours + trim alternatives to 3 + stylist names + Any Expert candidates. **customer-api decides the final `reason`**, in this order: `salon_closed`, `outside_hours`, `too_soon`, `stylist_unavailable`. booking-api knows no closed days or opening hours, so it only ever sends `stylist_unavailable` (B3); customer-api's hours check wins over it | new `apps/salons/routine_views.py` | available/reason for every code; a closed day shows the next open day; far sessions checked; Any Expert (`no_stylist_available` when nobody does all services); `sessions: 1` refused |
| C4 | **Done: `7b19c6e`.** `POST /booking/routine`: payment checks, `sessions[]` checks, plan first, hours, book, 201 routine object; Idempotency-Key as today; long timeout | `routine_views.py` | each §4 code; `session_not_offered`; `date_mismatch`; `invalid_window`; `slot_taken`; replay with the same key; 503 |
| C5 | **Done: `841bd1b`.** `PATCH /booking/{id}/sessions/{session_id}`: ISO to today's RESCHEDULE, stylist check, dry run | `routine_views.py` | lock, out of range, day taken, hours codes, `slot_taken`, unknown session 404, someone else's routine 404, no key on dry run |
| C6 | **Done: `4a07e4d`.** `GET /booking/{id}` routine fallback, `PATCH /booking/{id}` routine 409, Recurring rows in the new shape (all behind the flag) | `apps/salons/views.py` (`BookingDetailView`, `BookingListView._enrich`) | flag off: byte for byte today (single, group, recurring); flag on: routine id answers the routine, a stranger 404, PATCH 409 `already_paid` |
| C7 | **Done: the commit that adds this line.** Docs: the final FE contract, OpenAPI for the new routes, fix the stale list docs (F6) | `docs/` | `manage.py spectacular` builds |

Every step: `manage.py test apps.salons` (compare with `main`: 18 known local
failures in `apps.accounts` and `config.tests.test_observability`),
`makemigrations --check`, `check`.

### 4.3 Order and deploy

1. B1 to B7 (booking-api), one step at a time. Deploy with the flag off, check
   the business web. C2 can be built at the same time (pure).
2. C1, C3 to C7 (customer-api). Deploy with the flag off.
3. Staging: both flags on, the app team tests. Then production.
