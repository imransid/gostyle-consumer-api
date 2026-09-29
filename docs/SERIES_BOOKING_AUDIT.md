# Series booking (Routine Schedule) for mobile: audit and plan

Date: 2026-09-26. Status: **plan decided (D1 to D9 in section C). Building step by step (E.6).**

Repos:

- **customer-api**: this repo (Django). The app talks only to this.
- **booking-api**: `../gostyle-booking-api` (NestJS), branch `main` at `37c317c`.
  Owns every booking row and every series row. The business web talks to it too.

Target: the Figma brief (six points, given as text; no Figma file was opened).
Where this doc says "desk" it means the salon desk, which is the business web.

---

## 0. Short version

1. **booking-api already has a full desk series module**: two tables
   (`booking_series`, `series_occurrence`), create, preview, panel, pause, resume,
   end, skip one, change cadence at a scope, a nightly job that books the visits,
   a repair ladder, series health, and a prepaid "course" meter. About 4,800 lines.
2. **None of it is built for the app.** customer-api has no series route at all.
   The mobile create refuses `ROUTINE` (422 `routine_not_supported`) and the
   mobile list's `recurring` tab is hard-coded to an empty page.
3. **The desk module has real bugs** that the app would hit on day one. The worst:
   a new series gets no bookings from the nightly job for 70 days (K1); resume
   brings nothing back (K2); a customer token can create, pause or end anyone's
   series (K3); every real customer's visit is born "unpaid" with no way to pay (K4);
   a cadence change on a 6-session series makes 9 sessions (K5, proven).
4. **Payment, texts and "auto-confirm 24h" do not exist for anyone.** There is no
   SMS or push transport at all (reminders are logged, never sent), no 48 hour
   reminder, no series discount, no series refund. Payment is owned by another team.
5. **Plan**: a new mobile-only path `/v1/mobile-booking/series`, same idea as group
   booking: each session is an **ordinary mobile booking** made by the existing
   single create (unchanged), linked to a desk-shaped series row, so the desk
   board and panel show it with no desk change. Two flags, OFF by default:
   `MOBILE_SERIES_BOOKING` (booking-api) and `SERIES_BOOKING_V1` (customer-api).
   Only nullable columns are added. v1 pays at the salon, like group.
   The app gets **its own series routes** (create with `dry_run`, the hub, one PATCH
   with an `action`, cancel); only the Recurring tab uses the existing list route
   (E.3). No existing booking route changes.
6. **Time for Claude Code**: about **19 to 25 hours of Claude work** (3 to 4 working
   days of sessions), or **5 to 7 working days** of calendar time with Rafa's
   review and staging checks between steps. Details in E.6.

---

## A. Figma vs today

Legend: **DONE** works as the Figma wants. **PARTLY** something exists but not
what the Figma wants, or it is broken. **MISSING** nothing exists.

customer-api has **no series code at all**, so every customer-api cell is MISSING
unless said otherwise. The only related lines are:

- `apps/salons/views.py:1577` documents that `booking_type: ROUTINE` is refused.
- `apps/salons/views.py` `BookingListView` forwards `?filter=recurring` to booking-api,
  which answers an empty page (`docs/BOOKING_LIST_API.md:75`: "Always empty today").

### A.1 Create

| Figma | booking-api | customer-api | Where (booking-api unless said) |
| ----- | ----------- | ------------ | ----- |
| Pick service(s) | **PARTLY**: a series holds **one** `service_id` | MISSING | `prisma/schema.prisma` `BookingSeries.serviceId`; `CreateSeriesDto.serviceId` in `src/interface/http/series.controller.ts:177` |
| Regular stylist | **PARTLY**: `preferred_staff_id` is stored, but the repair ladder may give the visit to someone else, or move it 15 or 30 minutes, or anywhere in the day, **without asking** (K11) | MISSING | `src/domain/availability/series-ladder.ts`, `src/application/commands/materialise-series.handler.ts:377` |
| Frequency: weekly | **DONE** (`WEEKLY` + weekdays) | MISSING | `src/domain/booking/recurrence.ts:44-57` |
| Every 2 weeks | **DONE** (`EVERY_N_WEEKS`, weeks 2) | MISSING | same |
| Monthly | **DONE** (`MONTHLY_ON_DATE`, last day fallback for the 31st) | MISSING | same, `:257-283` |
| Custom | **DONE** (`CUSTOM`, a list of dates) | MISSING | same, `:285-289` |
| Daily | **PARTLY**: no `DAILY` kind. `WEEKLY` with all 7 weekdays works, but a closed day becomes "needs attention", not skipped | MISSING | same |
| 2 to 6 sessions | **PARTLY**: `end AFTER_COUNT` exists, minimum 1, **no maximum** | MISSING | `series.controller.ts:105-120` |
| Same time each visit | **DONE** (`start_min`), but only 10:00 to 22:00 (a DB CHECK) | MISSING | migration `20260828114420_booking_series`, `series_start_inside_trading_day` |
| See the dates before paying | **DONE for the desk**: `POST /v1/bookings/series/preview` returns every date with a verdict (EXACT, REPAIRABLE, NEEDS_ATTENTION, DEFERRED, CLOSED) and alternatives. Writes nothing | MISSING | `src/interface/http/booking-series.controller.ts:116`, `src/application/queries/series-preview.handler.ts` |
| Book it | **PARTLY**: `POST /v1/series` (alias `/v1/bookings/series-admin`) stores the wanted dates. **Nothing is booked until "materialise" runs**, and the nightly job skips new series for 70 days (K1) | MISSING. Mobile create refuses ROUTINE | `series.controller.ts:266`, `src/domain/booking/mobile-contract.ts:268` |

### A.2 Payment

| Figma | booking-api | customer-api | Where |
| ----- | ----------- | ------------ | ----- |
| Pay as you go: 20% deposit, rest each visit | **MISSING**. The nightly job writes `deposit_fils 0` on every visit. When the deposit rules say money is due it writes `pending_payment` / `unpaid` with **no payment link and no expiry** (K4). The only configurable 20% is the group one (`MOBILE_GROUP_DEPOSIT_PERCENT`) | MISSING | `materialise-series.handler.ts:398-447`, `src/interface/http/mobile-group.flag.ts:18-39` |
| Pay upfront, 10% off | **PARTLY**. A prepaid "course" exists: total net, number of visits, a meter the desk draws one visit at a time (`course-draw`, desk only). **Nothing records the upfront payment itself**, draws are manual, there is **no discount** anywhere for a series, and nothing refunds unused visits (K13) | MISSING | `src/domain/booking/course.ts`, `src/interface/http/desk-extras.controller.ts:222`, `booking_series.course_*` |
| Pay at salon, no deposit | **PARTLY**. A series visit is `confirmed` + `none_required` only when the deposit rules say nothing is due, and for real customers they never do (K4). The single mobile booking already does pay at salon properly (`PAY_AFTER_CHECK_IN`), and so does group | MISSING | `src/domain/booking/auto-confirm.ts:128-155`, `src/application/commands/mobile-booking.handler.ts:434-466` |

### A.3 Confirm screen and rules

| Figma | booking-api | customer-api | Where |
| ----- | ----------- | ------------ | ----- |
| List of all dates | **PARTLY**. Preview lists them all. Create returns only the first 5 as text (`firstOccurrences`) plus a count. The panel (`GET /v1/series/:id`) lists all, with booking ids | MISSING | `src/application/commands/series.handler.ts:195-203` |
| Text 48h before each visit | **MISSING**. The number 48 exists (`SCHEDULE_REMINDER_LEAD_HOURS`) but is thrown away. The reminder ladder is 24h, 3h, 15m only. And **no SMS, push or email is sent by anything**: reminders are outbox events that a logging publisher prints | MISSING | `auto-confirm.ts:27`, `src/domain/booking/reminders.ts:24-43`, `src/infrastructure/messaging/logging-event-publisher.ts` |
| Auto-confirm 24h before | **MISSING**. The desk model is different: "auto confirm on schedule" means born confirmed; "ask each time" means born `pending_confirmation`, a 7 day ask that is never sent, and **no route to answer it** (K12). Nothing changes state at 24h | MISSING | `auto-confirm.ts:80-156`, `src/infrastructure/scheduling/confirm-ask-sweeper.service.ts` |
| Miss 2 in a row = series pauses | **MISSING**. Health says `AT_RISK` after 2 unanswered asks in a row, or after **any** no-show (a total, not a streak). It is only a label: nothing ever pauses a series by itself | MISSING | `src/domain/booking/series-health.ts:59-77`, `src/infrastructure/scheduling/no-show-sweeper.service.ts` |

### A.4 Manage hub

| Figma | booking-api | customer-api | Where |
| ----- | ----------- | ------------ | ----- |
| Pause: 2 weeks / 1 month / custom date, max 60 days, reason | **PARTLY**. `POST /v1/series/:id/pause` has **no end date, no reason, no maximum**. It cancels every future visit more than 48h away and marks it skipped (lost for good). Visits inside 48h are only listed for the desk | MISSING | `series.handler.ts:371-394`, `src/domain/booking/series-edit.ts:158-195` |
| Skip sessions | **PARTLY**. One at a time: `POST /v1/series/:id/occurrences/:occId/skip`. **No 48h window and no state check**: it would cancel a visit that is checked in or done (K7). A skipped visit still counts toward "after 6" | MISSING | `series.handler.ts:416-434` |
| Reschedule one session | **PARTLY**. `POST /v1/series/:id/pattern` with `THIS_OCCURRENCE` only **detaches** the visit, it does not move it. Moving needs the shared `POST /v1/bookings/:id/reschedule` with a hold, which leaves the series timeline showing the old day (K17) | MISSING (no reschedule route for any booking) | `series.handler.ts:494-508`, `src/application/commands/reschedule.handler.ts` |
| Extend series | **MISSING**. No route changes the end. The cadence change keeps the end on purpose | MISSING | `series.handler.ts:537-541` |
| Cancel with refund calculation | **PARTLY**. `POST /v1/series/:id/end` cancels future visits more than 48h away, but **writes no refund, no ledger row, no event** (K8). The single booking cancel has real refund bands (free over 24h, deposit kept 2 to 24h, all kept under 2h), but nothing adds them up for a series | MISSING (cancel exists for groups only) | `src/infrastructure/persistence/series.repository.ts:675-749`, `src/domain/booking/lifecycle.ts:590-658` |

### A.5 Resume

| Figma | booking-api | customer-api | Where |
| ----- | ----------- | ------------ | ----- |
| Auto resume on a date | **MISSING** (no date is stored, no job) | MISSING | |
| Resume early | **PARTLY, broken**. `POST /v1/series/:id/resume` only sets `active`. The paused visits were marked skipped and are never re-planned, and the nightly job does not look at the series again until its old horizon passes (K2) | MISSING | `series.handler.ts:396-413` |
| Change frequency or time | **PARTLY**. `POST /v1/series/:id/pattern` with `THIS_AND_FUTURE` or `ENTIRE_SERIES`. But on a counted series it **restarts the count** and `ENTIRE_SERIES` **plans visits in the past** (K5) | MISSING | `series.handler.ts:471-589` |
| Change stylist | **MISSING** (nothing updates `preferred_staff_id`) | MISSING | |

### A.6 My Bookings

| Figma | booking-api | customer-api | Where |
| ----- | ----------- | ------------ | ----- |
| "Recurring" tab | **MISSING**. `GET /v1/mobile-booking?filter=recurring` returns an empty page without reading the database. `booking-list.md:58-59` promises "one row per routine", not built | forwards, MISSING | `mobile-booking.handler.ts:687-702` |
| "1 of 6 done, 5 remaining" | **MISSING** for mobile. The data exists (occurrences + booking status); the desk board counts `total`, `needsAttention`, `skipped`, not "done" | MISSING | `src/application/queries/read-models.handler.ts:1760-1832` |
| Sessions in "Upcoming" | **PARTLY**. A booked series visit is an ordinary booking for the customer, so it shows in Upcoming, but as `SINGLE`: nothing writes `booking.series_id` or `booking_type routine` (K6) | works the same | `series.repository.ts:493-509` |

---

## B. What the salon desk already has (reuse it, do not break it)

### B.1 Routes

All under `/v1`. One global guard: a token is needed, and customers are refused only
where `@DeskOnly` is set (`src/auth/booking-auth.guard.ts:70-76`).

| Route | Desk only? | Does | Desk front end uses it? |
| ----- | ---------- | ---- | ----------------------- |
| `POST series` = `POST bookings/series-admin` | **no** (K3) | create | yes, via `series-admin` (BOOKINGS-FE-ROUND-3.md:136) |
| `GET series/:id`, `GET bookings/series/:id`, `GET bookings/series-admin/:id` | **no** | the panel: status, health, horizon, `summary`, timeline, course | yes |
| `GET …/:id/occurrences` | **no** | timeline only (drops health) | yes |
| `POST …/:id/materialise` | **no** | book the planned visits now | yes, a button (BOOKINGS-API-FOR-FRONTEND.md:690) |
| `POST …/:id/pause`, `/resume`, `/end` | **no** | as in A.4, A.5 | yes |
| `POST …/:id/occurrences/:occId/skip` | **no** | skip one, by occurrence id | yes (ROUND-2:15-21) |
| `POST …/:id/pattern`, `GET …/:id/occurrences/:occId/edit-scope` | **no** | change cadence at a scope, and its preview | yes |
| `POST bookings/series/preview` | **no** | dry run of every date | yes |
| `GET bookings/series?status=` | **yes** | the Recurring board | yes |
| `POST bookings/series-admin/:id/course-draw` | **yes** | draw one course visit | yes |
| worklist tile `SERIES_AT_RISK` | **yes** | links to the RECURRING screen | yes |

### B.2 What the desk front end depends on (keep byte for byte)

- The `series-admin` alias paths, and skip keyed by occurrence **id** (never index).
- `OccurrenceView`: `id`, `index`, `day`, `start`, `startsAt`, `state`, `bookingId`,
  `bookingCode`, `bookingStatus`, `movedFromDayOfMonth`, `alternatives`.
- The panel's `summary` (the board row) and `status` / `health` enums:
  series `ACTIVE PAUSED ENDED COMPLETED`, health `HEALTHY AT_RISK`,
  occurrence `PLANNED MATERIALISED NEEDS_ATTENTION SKIPPED DETACHED`.
- The only series marker on a calendar row is `channel: "RECURRING"`
  (`read-models.handler.ts:233`, `get-booking.handler.ts:35`).
- The desk always sends **staff** tokens. So adding `@DeskOnly` or an owner check to
  the series routes does not change anything for the desk (same as fix 5f01d92).

### B.3 Tables

- `booking_series`: the definition. Many CHECKs: pattern payload matches the kind,
  end condition is coherent, start 10:00 to 22:00, course all or nothing.
- `series_occurrence`: one row per wanted visit. `(series_id, index)` is unique,
  `booking_id` is unique. CHECK: `materialised` must have a booking, `planned` and
  `needs_attention` must not; `alternatives` only on `needs_attention`.
- `booking.series_id` and `booking.booking_type` (`single`, `routine`) **exist and are
  never written** (K6). The enum value `routine` already exists, so the app can use it
  with no enum change.

### B.4 Background jobs that touch series visits

| Job | When | Series aware? |
| --- | ---- | ------------- |
| Series materialiser | 02:00 Asia/Dhaka, 200 series per run | yes (only `active` series with `materialised_through < today`) |
| Confirm-ask sweeper | hourly | expires `pending_confirmation` 48h after **creation**, not before the visit |
| Reminder scheduler | every 60s | no. 24h, 3h, 15m, for `confirmed` and `pending_payment`. Outbox only |
| No-show sweeper | every 60s | no. `confirmed` bookings 30 min after start |
| Payment link sweeper | periodic | no. Needs `link_expires_at`, which series visits never have |
| Risk flag sweeper | 03:00 | no. Reads fixture customers only |

### B.5 Safe to reuse as it is (call it, do not change it)

| Piece | File | Use for |
| ----- | ---- | ------- |
| `expandSeries` | `src/domain/booking/recurrence.ts` | turn a frequency into dates |
| `SeriesPreviewHandler`, `MaterialiseSeriesHandler.candidatesFor` (read only) | `series-preview.handler.ts`, `materialise-series.handler.ts:182` | "is this exact slot free on each date" |
| `MobileBookingHandler.execute` | `src/application/commands/mobile-booking.handler.ts` | make each session a normal mobile booking: many services, stylist, VAT, pay at salon |
| `LifecycleHandler` cancel | `src/application/commands/lifecycle.handler.ts` | cancel a session with refund bands, history, events, owner check (5f01d92) |
| `PlaceHoldHandler` + `RescheduleHandler` | `reschedule.handler.ts` | move one session, deposit carried or forfeited by the existing rules |
| `cancellationOutcome` | `src/domain/booking/lifecycle.ts:590` | the refund quote |
| `deriveSeriesHealth`, `planDraws`, `courseState` | `series-health.ts`, `course.ts` | hub health, upfront plan later |
| flag pattern | `src/interface/http/mobile-group.flag.ts` | the new flag and its 404 guard |

**Do not reuse for mobile**: `SeriesRepository.cancelOccurrences` and `rewritePattern`
(K8, K5), the repair ladder (K11), `birthState` / `requirementFor` on the series path
(K4).

---

## C. Business rules

### C.1 Decisions (final, Rafa, 2026-09-26)

| # | Decision | Replaces |
| - | -------- | -------- |
| D1 | Several services per session. | R1 |
| D2 | v1 payment is `PAY_AT_SALON` only. `PAY_AS_YOU_GO` and `UPFRONT` are shown in the `dry_run` money but refused on create (`payment_plan_not_available`). | R2 |
| D3 | `DAILY` skips the days the salon is closed. | R5 |
| D4 | A date that is not free is never changed silently. The answer gives up to 3 alternatives and the customer picks. | R7 |
| D5 | 2 no-shows in a row pause the routine. | R11 |
| D6 | Pause moves the remaining sessions to after the resume date. The count is kept. | R12 |
| D7 | Products go on the first session only. | new |
| D8 | 10% off only for `UPFRONT`: services only, before VAT. | R4 |
| D9 | Only no-shows marked by staff count for D5. Env switch `ROUTINE_COUNT_AUTO_NO_SHOWS`, default `false` (when `true`, the sweeper's automatic no-shows count too). | R11 |

Fixed numbers: 2 to 6 sessions. Lock 24h before a session (no skip or move inside
it). Pause at most 60 days. Extend by 1 to 6, never more than 6 future sessions.
Reschedule within 90 days.

Figma fixes (Rafa, 2026-09-26, after step 1):

- Pause reasons are the Figma's five: `TRAVEL`, `HEALTH`, `BUSY` ("Busy Period"),
  `BUDGET`, `OTHER`.
- `RESUME` may "customize first": an optional new `frequency` (not `CUSTOM`), `time`
  and `stylist_id`, each checked like the create. Nothing sent is a plain resume.
  Still all or nothing.
- Cancel takes an optional reason ("Why are you cancelling?"): `NOT_SATISFIED`,
  `TOO_EXPENSIVE`, `MOVING`, `OTHER`. Refused with `invalid_cancel_reason`. No new
  column: see E.3.

The rows below that no decision replaces keep their suggested default.

### C.2 The questions, and the defaults that were suggested

The ones marked **(blocks v1)** had to be answered before step 1. They are, in C.1.

| # | Question | Suggested default |
| - | -------- | ----------------- |
| R1 (blocks v1) | One service or several per session? The desk series holds one. | **Several**, because each session is made by the single mobile create, which already plans several services. The series keeps `service_id` = the first (for the desk board) plus a new `service_ids` list. |
| R2 (blocks v1) | Which payment plans ship in v1? Payment belongs to another team (same as group). | **Pay at salon only** (`PAY_AFTER_CHECK_IN`, no deposit). The server still works out and shows the figures for all three plans, and stores the chosen plan, so turning the other two on later is additive. The app hides "pay as you go" and "upfront" until then. |
| R3 | Pay as you go: 20% of what? | 20% of each session's total (services + VAT), due when that session is booked. Server percent from config (`MOBILE_SERIES_DEPOSIT_PERCENT`, default 20); the app's figure must match, like group D1. |
| R4 | Upfront: what gets 10% off? | Services only, before VAT; VAT on the discounted amount. Stored as a desk "course" (`course_total_net_fils`, `course_visits`) so the desk meter works. |
| R5 (blocks v1) | "Daily" when the salon is closed some days? | Daily = the next N **open** days. Closed days are skipped and not counted. |
| R6 | Session limits | 2 to 6 at create. First session no sooner than the single booking's lead time. Custom dates: all inside the 90 day booking horizon. |
| R7 (blocks v1) | A date where the exact time with the chosen stylist is not free? | **No silent change.** Create is refused for that date with up to 3 alternatives (same stylist other time first, then same time other stylist). The customer picks, then creates. |
| R8 | Monthly with more than 3 sessions reaches past the 90 day horizon. | Sessions past 90 days are stored as "planned" and booked by the mobile job when they enter the horizon. If the exact slot is gone then, the session shows "needs your action" in the app with alternatives. |
| R9 | What does "auto-confirm 24h before" mean? | Sessions are **confirmed from the start** (pay at salon). "24h" is the **lock**: before it the customer may skip or move a session in the app; inside it only the normal cancel applies. The app shows `SCHEDULED` before 24h and `CONFIRMED` inside it, derived from the time, not stored. |
| R10 | "Text 48h before": which channel? | No transport exists. v1 writes an outbox event `series.session_reminder_48h` per session (the sending team picks it up later). The app can show it in-app from the hub. |
| R11 (blocks v1) | "Miss 2 in a row": what is a miss, and what happens? | A miss = `no_show` (the sweeper marks it 30 min after start). Late cancels do not count. Two no-shows in a row: series goes `PAUSED` with reason `MISSED_TWICE`, no end date, future sessions more than 24h away are released. Only the customer (app) or the desk can resume. |
| R12 (blocks v1) | Pause: are the paused sessions lost or moved? The desk pause loses them. | **Moved.** The count is kept: remaining sessions restart from the resume date at the same frequency, time and stylist. |
| R13 | Pause limits | Presets 14 days, 30 days, or a date. Max 60 days from the day of the pause. Reason: optional code (`TRAVEL`, `HEALTH`, `BUDGET`, `OTHER`) plus optional text up to 200 characters. Sessions inside the 24h lock are not paused. |
| R14 | Skip: is the session lost or added at the end? | **Lost** (the series gets shorter, "1 of 5"), which matches the desk. "Extend" can add it back. Several sessions can be skipped in one call. Not inside the 24h lock. |
| R15 | Reschedule one session: limits? | Any open slot up to 90 days ahead, same stylist by default, not on the same day as another session of the series, not inside the 24h lock. The session stays in the series. |
| R16 | Extend: how many? | 1 to 6 more sessions at the same frequency, time and stylist, after the last one. Never more than 6 future sessions at once. Checked like a create (R7). |
| R17 | Resume with a new frequency, time or stylist | Applies to future sessions only. All or nothing: if one new date cannot be booked, nothing changes and the answer lists alternatives. |
| R18 | Cancel the series: refund? | Pay at salon: nothing was paid, the quote says 0 (and says which sessions are inside 24h, which count as a late cancel under the single rules). Later, pay as you go: sum of each session's single-booking refund. Upfront: amount paid, minus the delivered visits at the **full** price, never below 0. Needs the salon owners' OK. |
| R19 | "1 of 6 done, 5 remaining": what is "done"? | Done = `completed`, `settled` or `no_show`. Remaining = future sessions not cancelled or skipped. Skipped are shown apart. |
| R20 | Do sessions also show in the Upcoming tab? | Yes, each one (it is a real appointment), marked `ROUTINE` with its `series_id` so the app links to the hub. The Recurring tab shows one row per series. |
| R21 | Can the desk change a mobile series with its own tools? | Yes. The app reads what is there (rows, not the pattern). The desk tools have desk behaviour (for example the desk pause loses sessions). |
| R22 | Can a customer have two series at the same time? | Yes, if the dates do not clash. |

Waiting on Olu already: per-salon timezone. Until then booking-api's series clock is
Asia/Dhaka (UTC+6) and customer-api rewrites answers into the salon's offset, as group does.

---

## D. Known bugs and risks in the existing series code

Severity is for the desk today; the last column says if the app would hit it.
K5 was proven by running the real `expandSeries` code (no file changed).

| # | Sev | Bug | Where | App hits it? |
| - | --- | --- | ----- | ------------ |
| K1 | High | **A new series gets no bookings from the nightly job for 70 days.** The job picks series with `materialised_through < today`, and create sets it to anchor + 70 days. Visits are booked only if someone presses "materialise". After that, the next run is again 70 days later. | `src/infrastructure/scheduling/series-materialiser.service.ts:63`, `series.repository.ts:170`, `:419-435` | only if we reuse the desk create |
| K2 | High | **Resume brings nothing back.** Pause marks future visits `skipped`; resume only sets `active`; the top-up skips existing ordinals; `materialised_through` is still in the future. (`docs/api/MOBILE.md:2803` admits it.) | `series.handler.ts:396-413`, `series.repository.ts:675-749` | yes, if reused |
| K3 | High | **Security: any token reaches the series routes.** A customer token can create a series for any `customerId` (taken from the body), and read, pause, end, skip, re-pattern or materialise any series. Only the board and course-draw are desk only. | `series.controller.ts:257`, `booking-series.controller.ts:110`, `src/auth/booking-auth.guard.ts:70-76` | yes: once the app has series ids |
| K4 | High | **Every series visit for a real customer is born unpaid with no way to pay.** The customer context is a fixture (`persistence.module.ts:95`); an unknown id is treated as a new customer, so the 20% first-visit rung fires; the series path stores `deposit 0`, `pending_payment`, `unpaid`, **no link window**; nothing sends a link and the link sweeper never expires it. The visit holds the chair as unpaid until the desk acts. | `materialise-series.handler.ts:398-447`, `src/infrastructure/fixtures/fixture-customer-context.ts:96-105` | yes, if reused |
| K5 | High | **Changing the cadence breaks counted series.** `THIS_AND_FUTURE` re-expands from the chosen visit with the same "after N", so the count restarts: 6 sessions, 3 done, change to every 2 weeks = **9 sessions**. `ENTIRE_SERIES` re-expands from the original anchor, so it plans visits **in the past** (4 of 6 in the test), and the materialiser reads planned visits with no lower date bound. | `series.handler.ts:528-543`, `series.repository.ts:437-462` | yes, if reused |
| K6 | Medium | **Series visits carry no link.** The materialiser never writes `booking.series_id` or `booking_type routine` (the schema says "write both or neither"), only `channel recurring`. The mobile list shows them as SINGLE. | `series.repository.ts:493-509`, `schema.prisma:168-176` | yes |
| K7 | Medium | **Skip has no guard.** Any occurrence of the series, any state: a checked-in or completed visit's booking would be set to cancelled and its chair released. No 48h window. The answer always says `ACTIVE`. | `series.handler.ts:416-434` | yes, if reused |
| K8 | Medium | **Series cancels skip the lifecycle.** Pause, end and skip write `cancelled` straight onto the booking: history `from` is hard-coded `confirmed`, no refund or forfeit rows, no `payment_status` change, no `booking.cancelled` event (the waitlist is never woken), and the occurrence's `booking_id` is cleared so the panel loses the link. `rewritePattern` does the same with `rescheduled`. | `series.repository.ts:675-749`, `:283-387` | yes, if reused |
| K9 | Medium | **Nothing ever sets `completed`.** A counted series stays `ACTIVE` after its last visit. | ROUND-2 Q6 | yes |
| K10 | Medium | **Stuck visits are never retried, and no route accepts an alternative.** The materialiser reads only `planned`; a `needs_attention` visit waits until someone skips or detaches it. | `series.repository.ts:437-462` | yes, if reused |
| K11 | Medium | **The repair ladder changes stylist or time without asking** (any stylist at the same time; then 15 or 30 minutes off; then anywhere in the day). Fine for the desk, but it breaks the app's "regular stylist, same time" promise. | `series-ladder.ts`, `materialise-series.handler.ts:377-386` | yes, if reused |
| K12 | Medium | **"Ask each time" is half built.** The 7 day ask is never sent; there is no route to answer it; the sweeper expires it 48h after it was **created**, so a visit booked 70 days out expires about 68 days before the day. | `confirm-ask-sweeper.service.ts:60-66`, `src/interface/http/lifecycle.controller.ts:212-319` | no (we do not use it) |
| K13 | Medium | **Course money.** Nothing records the upfront sale; draws are manual; course-draw does not check the booking belongs to the series, reads then increments (two clicks with different keys can both draw), and the last draw does not end the series. `payment-link.handler.ts:113-117` counts `course_draw` (a negative amount) as captured, so what the link says is owed goes **up**. The mobile `due_amount` ignores course draws. | `desk-extras.handler.ts:136-180`, `series.repository.ts:918-942` | later, for upfront |
| K14 | Medium | **Not series only: the 50% refund band never fires.** `paidInFull: false` is hard-coded, so a fully prepaid booking cancelled 2 to 24h out keeps 100%. | `src/infrastructure/persistence/lifecycle.repository.ts:438` | later, for refunds |
| K15 | Low | Board health and panel health can disagree: the board hard-codes no-shows and expiries to 0. | `read-models.handler.ts:1765-1772` | no |
| K16 | Low | `planEdit` throws a plain `Error` for an occurrence not in the series: a 500 on edit-scope and on pattern. | `series-edit.ts:98` | no |
| K17 | Low | A lifecycle reschedule moves the booking but not the occurrence's day, and does not detach it, so a later `THIS_AND_FUTURE` can clear a visit the customer moved by hand. A cancel through `/bookings/:id/cancel` does not touch the occurrence either. | `reschedule.repository.ts`, `series.repository.ts:792-853` | yes, handled in the plan |
| K18 | Low | Time zone: series run on Asia/Dhaka, UTC+6 (the job's comment says Dubai +4). The day is fixed 10:00 to 22:00 by a CHECK. Known, waits for Olu. | `hold.repository.ts:6,20`, `series-materialiser.service.ts:20-21`, `recurrence.ts:12` | yes, as for group |
| K19 | Low | No unit specs for the series handlers or repository. The pure domain has 123 tests; the proof SQL covers constraints only. | `src/application/commands/series.handler.ts`, `materialise-series.handler.ts` | risk |
| K20 | Low | Stale docs: `docs/api/MOBILE.md` series section (lines 2438 to 2836) and `README.md:283` (says skip publishes an event; it does not). | | no |
| K21 | Info | **No message transport at all.** Every reminder, ask and nudge is an outbox event printed by `LoggingEventPublisher`. BullMQ is installed but not wired. | `src/infrastructure/messaging/` | yes: R10 |

The plan in E does **not** depend on fixing K1 to K16, because the mobile path does
not use the broken pieces. K3 must be fixed before the flag goes on. The others are
listed as separate business PRs (E.5, F1 to F7) for Rafa to schedule.

---

## E. Build plan

### E.1 The idea

- **New mobile-only controller** in booking-api: `/v1/mobile-booking/series`. The
  desk never calls it. Flag `MOBILE_SERIES_BOOKING`, OFF means every route answers 404.
- **A mobile series is a desk-shaped series.** One `booking_series` row (with
  `source 'mobile'`) and one `series_occurrence` row per session. The desk board and
  panel show it with **no desk change**.
- **Each session is an ordinary mobile booking**, made by calling
  `MobileBookingHandler.execute` **unchanged**: several services, the stylist, VAT,
  pay at salon. Then linked in one small mobile-only write: `booking.series_id`,
  `booking_type 'routine'`, `channel 'recurring'` (the desk's RECURRING chip), and the
  occurrence set `materialised` with its `booking_id`. Same idea as group booking.
- **Dates are decided at create and stored as explicit dates** (`pattern CUSTOM`,
  `end AFTER_COUNT n`), with the app's frequency kept in a new display column. Why:
  every Figma action (pause and move, skip, move one, extend, new frequency) changes
  dates in ways "a pattern from the anchor" cannot say, and the desk's expander would
  re-derive wrong dates from it (K5). With explicit dates the desk's top-up finds
  nothing new to add. `materialised_through` is set to **9999-12-31** on every mobile
  routine (changed in step 2: the last session day was not enough, the desk job
  picks any active series whose date has passed), so the desk nightly job never
  picks it. Proven by a spec and live (E.6 step 2).
- **Strict booking**: exact time, chosen stylist, or the customer chooses (R7). The
  repair ladder is never used for mobile.
- **Changes use the app's normal handlers**: cancel a session = `LifecycleHandler`
  (refund bands, history, events, owner check); move = hold + `RescheduleHandler`,
  then the occurrence's day is updated. Never `cancelOccurrences` or `rewritePattern`.
- **"Done", "remaining", `SCHEDULED` / `CONFIRMED`, health: derived** from the rows on
  every read, never stored (CLAUDE.md rule 4 of booking-api).
- **customer-api** keeps its own checks (salon, service ids, stylist belongs to the
  salon, opening hours, lead time; reuses `group_translate` helpers), converts times
  into minutes, calls booking-api, and rewrites answers into the salon's offset.
  Flag `SERIES_BOOKING_V1`.

### E.2 Database changes (additive only)

One migration in booking-api, `prisma/migrations/<ts>_mobile_series`, `IF NOT EXISTS`,
plus optional fields in `schema.prisma`. All on `booking_series`:

| Column | Type | Why |
| ------ | ---- | --- |
| `source` | `TEXT NULL`, CHECK `source IS NULL OR source = 'mobile'` | NULL = made by the desk (every row today). Mobile reads and lists only `'mobile'` series. |
| `frequency` | `TEXT NULL`, CHECK in (`daily`, `weekly`, `every_2_weeks`, `monthly`, `custom`) | what the customer chose, for display (the stored pattern is CUSTOM) |
| `service_ids` | `UUID[] NULL`, CHECK a `mobile` row has at least one (scoped to mobile, so a desk insert can never trip it) | all services of a session (D1). `service_id` stays the first one for the desk board |
| `payment_plan` | `TEXT NULL`, CHECK in (`pay_at_salon`, `pay_as_you_go`, `upfront`) | D2. v1 only ever writes `pay_at_salon` |
| `paused_until` | `DATE NULL` | the resume date (D6) |
| `pause_reason` | `TEXT NULL`, CHECK in (`travel`, `health`, `busy`, `budget`, `other`, `missed_twice`) | the Figma's five reasons; `missed_twice` for D5 |
| `pause_note` | `TEXT NULL`, CHECK 1 to 200 characters | the customer's own words (R13) |
| `miss_streak_after` | `DATE NULL` | D5: only sessions after this day count toward "2 in a row". Set when the routine is paused for two misses, so a resume (from the app or the desk) does not pause it again at once |

**No CHECK ties `paused_until` to `status = 'paused'`** (the first draft had one).
The desk's resume sets `status = 'active'` and does not know the new columns, so
that CHECK would make the desk's resume fail on a paused mobile series. Instead
every mobile read ignores `paused_until`, `pause_reason` and `pause_note` unless the
status is `paused`. The proof SQL shows the desk's resume still works.

Not added, on purpose: no new table, **no new enum value** (`routine`, `paused`,
`completed`, `skipped` all exist), no change to any existing column, CHECK or index.
Money columns for pay as you go and upfront come with the payment work (the course
columns already exist).

The existing CHECK `series_start_inside_trading_day` (10:00 to 22:00) stays, so the
routine time must be inside it too. The contract refuses anything else before a row
is written.

### E.3 Routes (decided 2026-09-26)

The app gets **its own series routes** (changed 2026-09-26, Rafa). Nothing is
reused: `BookingDetailView` and `GroupBookingCancelView` are **not touched**, so the
single and group answers cannot change. Each new route forwards **1 to 1** to its
booking-api route (same body, same answer, times rewritten into the salon offset).

#### customer-api (what the app calls)

The 4 new routes are behind `SERIES_BOOKING_V1` (off: 404). The Recurring tab is the
existing list route; flag off, it answers byte for byte what it answers today.

| # | Route | New? | Forwards to | Does |
| - | ----- | ---- | ----------- | ---- |
| 1 | `POST /api/v1/booking/series` | new | `POST /v1/mobile-booking/series` | Create a routine. `"dry_run": true` = preview, saves nothing |
| 2 | `GET /api/v1/booking/series/{id}` | new | `GET /v1/mobile-booking/series/:id` | The hub: the routine, every session, done / remaining / skipped, next session, money, what the customer may do now |
| 3 | `PATCH /api/v1/booking/series/{id}` | new | `PATCH /v1/mobile-booking/series/:id` | The 5 actions, picked by `"action"`. `dry_run` supported. Always answers the full routine |
| 4 | `POST /api/v1/booking/series/{id}/cancel` | new | `POST /v1/mobile-booking/series/:id/cancel` | Cancel the routine. `"dry_run": true` answers the refund summary and cancels nothing |
| 5 | `GET /api/v1/bookings?filter=recurring` | existing | `GET /v1/mobile-booking?filter=recurring` | The Recurring tab: one row per routine. Each session also shows in `upcoming` with `booking_type: "ROUTINE"` and its `series_id` |

**1. `POST /api/v1/booking/series`**

Body (snake case, like the single create):

| Field | Rule |
| ----- | ---- |
| `salon_id` | required |
| `services` | 1 or more `{ "id" }` (D1). Every session gets all of them |
| `stylist_id` | required: the regular stylist |
| `frequency` | `DAILY`, `WEEKLY`, `EVERY_2_WEEKS`, `MONTHLY`, `CUSTOM` |
| `start_date` + `sessions` | not `CUSTOM`: the first day and how many (2 to 6) |
| `dates` | `CUSTOM` only: 2 to 6 days, all inside the 90 day horizon |
| `time` | `"HH:MM"`, 10:00 to 21:55, 5 minute steps. Optional on `dry_run`, required on create |
| `payment_plan` | `PAY_AT_SALON`, `PAY_AS_YOU_GO`, `UPFRONT` |
| `products` | optional, on the **first session only** (D7) |
| `picks` | optional: `[{ "index", "date", "time", "stylist_id" }]`, the alternatives the customer chose (D4) |
| `amount_without_tax`, `tax_amount`, `discount`, `total` | create only: the app's figures for the chosen plan, checked like the single create (`amount_mismatch` with `expected`) |
| `dry_run` | `true` = preview. Default `false` |

- `dry_run` **without** `time`: answers the dates and `available_times`, the times
  that are free on **all** the dates with that stylist. Nothing else is worked out.
- `dry_run` **with** `time`: answers `sessions` (each: `index`, `date`, `start_time`,
  `free`, `alternatives` up to 3, and `later: true` when past the 90 day horizon),
  `money` for all three plans (D2, D8), and `rules` (2 to 6, lock 24h, reminder 48h,
  pause 60 days, extend 6, reschedule 90 days, 2 misses pause).
- **Without** `dry_run`: books every session, **all or nothing**. Needs
  `Idempotency-Key`. `PAY_AS_YOU_GO` and `UPFRONT` are refused with
  `payment_plan_not_available` (D2). A session that is not free (and not picked) is
  refused with `session_not_free` (409), never moved (D4): the app runs `dry_run`
  again to show the alternatives. 201 answers the routine hub.

Money (pure rule, booking-api): every session costs what the single create would
charge that day (same quote, tier and all). Products go on session 1 only (D7).

- `PAY_AT_SALON`: total = the sum of the sessions. Pay now 0.
- `PAY_AS_YOU_GO`: the same total. Deposit = 20% of **each** session
  (`MOBILE_SERIES_DEPOSIT_PERCENT`, rounded per session, as each session is its own
  booking), all taken at create; the rest at each visit.
- `UPFRONT`: 10% off the services (after any tier discount), before VAT (D8). VAT on
  what is left. Products are never discounted. Pay now = the whole total.

**2. `PATCH /api/v1/booking/series/{id}`**

One route, field `action`, plus `dry_run`. Always answers the full routine (after
the change, or as it would be on `dry_run`).

| `action` | Body | Rule |
| -------- | ---- | ---- |
| `SKIP` | `session_ids: [...]` | one or more. Not inside the 24h lock, only sessions still to come. A skipped session is lost (R14) |
| `RESCHEDULE` | `session_id`, `date`, `time`, optional `stylist_id` | the session is not locked; the new time is after the lock and within 90 days; not on the day of another session (R15) |
| `EXTEND` | `sessions` (1 to 6), or `dates` for `CUSTOM`; optional `picks` | never more than 6 future sessions. Same frequency, time and stylist, after the last one. Checked like a create (D4) |
| `PAUSE` | `until` (a date), optional `reason` (`TRAVEL`, `HEALTH`, `BUSY`, `BUDGET`, `OTHER`), optional `note` (up to 200); optional `picks` | at most 60 days from today. The remaining sessions move to after `until` and are booked there right away, the count is kept (D6). Sessions inside the lock stay |
| `RESUME` | optional `frequency` (`DAILY`, `WEEKLY`, `EVERY_2_WEEKS`, `MONTHLY`; never `CUSTOM`), optional `time`, optional `stylist_id` ("Customize first"); optional `picks` | only a paused routine. The remaining sessions are planned again from the next bookable day, count kept. Nothing sent: same frequency, time and stylist. Each one sent is checked like the create (`invalid_frequency`, `invalid_time`, `stylist_required`). A switch between `WEEKLY` and `EVERY_2_WEEKS` keeps the routine's weekday (the Figma's "Same time slot: 4:30 PM, Sunday"); any other switch starts its cadence on the resume day. **All or nothing**: if one session cannot be booked with the new values, nothing changes and the answer lists alternatives (D4) |

**`picks` on EXTEND, PAUSE and RESUME** (same shape as on create:
`[{ "index", "date", "time", "stylist_id" }]`). These three plan new dates, so one of
them can be busy (D4). The `dry_run` answer lists every session it plans with its
`index` (the session's number in the routine, as the hub shows it: new ones for
EXTEND, the moved ones for PAUSE and RESUME), `free`, and up to 3 `alternatives`. The
customer picks one per busy session and sends the action again with `picks`. An
index the action does not plan is `invalid_pick`. Without picks, a busy session
refuses the whole action (`session_not_free`), and nothing changes.

SKIP, RESCHEDULE, EXTEND and PAUSE need an active routine; RESUME needs a paused one.

**The salon's hours (customer-api), for every action that sets a new time:
RESCHEDULE, EXTEND and RESUME, and PAUSE too, since it books the moved sessions
right away.** Exactly as for the create (below): every session the action plans,
and every pick, is held to the salon's own hours on its own day and the services'
notice, with group's codes `salon_closed`, `too_soon`, `outside_hours`. On
`dry_run` a session outside them is not free (with `refusal`) and alternatives
outside them are left out; for real, the action is refused before booking-api is
asked to change anything (customer-api asks booking-api's `dry_run` for the plan
first, as the create does). SKIP sets no new time and needs no check.

**2, 4, 5.** The hub (2) is what `POST` (1) and `PATCH` (3) answer too. Cancel (4)
cancels every session still to come. A session inside the lock is cancelled under the single booking's rules (late
cancel). `dry_run` lists each session with its refund band and adds them up
(pay at salon: 0). Body: optional `reason` (`NOT_SATISFIED`, `TOO_EXPENSIVE`,
`MOVING`, `OTHER`), else `invalid_cancel_reason`.

**Where the cancel reason is stored (no new column).** Each session is cancelled
through the single booking's lifecycle, which must write a reason on
`booking_status_history` for any cancel (CHECK `bsh_destructive_needs_reason`). That
row's `reason` is `Routine cancelled in the app. Reason: TOO_EXPENSIVE.` (or
`Routine cancelled in the app.` with no reason). The desk sees it in the booking's
history as it is, and `cancelReasonFromHistory` reads the word back. A routine with
nothing booked yet (only sessions past 90 days) has no history row, so the routine's
own outbox event `series.cancelled` (table `event_outbox`, `aggregate_type
'series'`) carries the same word too. The Recurring row: routine id, `booking_type: "ROUTINE"`, salon,
frequency, time, stylist, services, `done`, `remaining`, `skipped`, `next_session`,
status (`ACTIVE`, `PAUSED`, `ENDED`, `COMPLETED`), `paused_until`.

**Error codes** (mobile envelope `{detail, code: "validation_error", errors: [{field,
code, message, expected?}]}`, 422 unless said):
`no_services`, `unknown_service`, `stylist_required`, `invalid_frequency`,
`invalid_session_count`, `invalid_date`, `date_out_of_range`, `time_required`,
`invalid_time`, `invalid_payment_plan`, `payment_plan_not_available`, `invalid_pick`,
`amount_mismatch`, `session_not_free` (409), `invalid_action`, `invalid_sessions`,
`session_locked`, `session_not_changeable`, `routine_not_active`, `invalid_pause`,
`pause_too_long`, `invalid_pause_reason`, `invalid_cancel_reason`, `invalid_extend`,
`too_many_sessions`,
`reschedule_out_of_range`, `session_day_taken`, `not_found` (404, never 403),
`cannot_cancel`. The list lives in `mobile-series-contract.ts` (S4).

#### booking-api (what customer-api calls)

New mobile-only controller, all behind `MOBILE_SERIES_BOOKING` (OFF = 404), customer
token, owner checked (a stranger's id is 404, never 403):

| Route | Does |
| ----- | ---- |
| `POST /v1/mobile-booking/series` | create, or preview with `dryRun` |
| `GET /v1/mobile-booking/series/:id` | the hub |
| `PATCH /v1/mobile-booking/series/:id` | the five actions, `dryRun` supported |
| `POST /v1/mobile-booking/series/:id/cancel` | cancel (optional `reason`), or the refund summary with `dryRun` |
| `GET /v1/mobile-booking?filter=recurring` (existing) | one row per mobile routine. Flag off: the empty page, as today |

customer-api keeps its own checks first (salon, service ids, stylist belongs to the
salon, hours; the `group_translate` helpers), turns times into minutes, and rewrites
answers into the salon's offset, as group does.

**The salon's hours (step 3, fixed 2026-09-26).** booking-api knows its diary and
its trading day, not the salon's published hours or the services' notice.
customer-api holds every session to the rule a group start is held to
(`start_refusal`: the salon's own hours on THAT day, which differ by weekday and
can be shut by hand; booking-api's day; now plus the longest notice of the chosen
services), with group's codes `salon_closed`, `too_soon`, `outside_hours`. A
routine's days are booking-api's to plan (DAILY skips its closed days), so the
create first asks booking-api for its plan with `dry_run` (nothing is saved),
checks every session and pick, and refuses with 422 on `sessions[i]` (or
`picks[j]` when the customer picked that time) before anything is booked. A
`dry_run` is answered from that plan: a session outside the hours is not free and
says why (`refusal: {code, message}`), alternatives outside the hours are left
out, and a free time is kept only if it is inside the hours on every day.

**Times, both ways (step 3).** booking-api reads every branch at +06:00; the app
speaks the salon's clock (+04:00 in Dubai). customer-api converts every time, the
plain HH:MM ones included, with its date as one instant: `time`, `picks[].time`,
`start_date` and `dates` (they can change day near midnight) on the way in;
`time`, `sessions[].start_time` / `end_time` / `date`, `alternatives[]`,
`available_times` and `created_at` on the way out. The app's 16:30 reaches
booking-api as 18:30, and booking-api's 18:30 comes back as 16:30. A salon whose
clock changes between the routine's days (daylight saving) is refused
(`invalid_time`), because booking-api stores one time per routine; no Gulf salon
does.

Route clash check: `GET /v1/mobile-booking/:id` matches one path segment, so it
cannot swallow `/series/<id>`. The new controller is still registered before
`MobileBookingController`, with cases in `route-order.spec.ts`. In customer-api,
`booking/<uuid>` cannot match `booking/series` (not a uuid).

### E.4 The mobile series job (booking-api, same flag)

One `@Interval` job, hourly, only `source 'mobile'` series:

1. **Auto resume**: `paused_until <= today`: status back to `active`. The sessions
   were already moved to after that day by the pause (D6).
2. **Two misses**: the last two past sessions after `miss_streak_after` are both
   `no_show` **marked by staff** (D5, D9; the sweeper's own no-shows count only when
   `ROUTINE_COUNT_AUTO_NO_SHOWS=true`). Skipped and cancelled sessions are passed
   over; a visit that happened breaks the streak. Then: pause with `missed_twice`,
   no end date, and set `miss_streak_after` to the second miss's day.
3. **Book what entered the 90 day horizon** (monthly), strictly; else "needs your action".
4. **Complete**: last session closed: status `completed` (so the desk job ignores it).
5. **48h reminder event** per session, once (R10).
6. **Keep the desk job away**: a mobile routine whose `materialised_through` is not
   9999-12-31 (the desk's "materialise" button rewrites it to today + 70) is put back.

### E.5 Every piece

#### booking-api (`../gostyle-booking-api`)

| # | Piece | Create / modify | File | Touches business? | Tests to add |
| - | ----- | --------------- | ---- | ----------------- | ------------ |
| S1 | Flags | CREATE | `src/interface/http/mobile-series.flag.ts` (the group flag pattern): `MOBILE_SERIES_BOOKING`, `ROUTINE_COUNT_AUTO_NO_SHOWS` (D9), `MOBILE_SERIES_DEPOSIT_PERCENT` | No | off: 404 on every new route |
| S2 | Migration + schema | CREATE migration; MODIFY `schema.prisma` (8 optional fields) | `prisma/migrations/<ts>_mobile_series/` | No. Nullable, never read by the desk | runs twice cleanly; `prisma/proof-mobile-series.sql`: bad `source`, `frequency`, `payment_plan`, `pause_reason`, long note, a mobile row with no `service_ids` MUST FAIL; the desk's resume on a paused mobile series MUST WORK |
| S3 | Routine rules (pure) | CREATE | `src/domain/booking/mobile-series.ts` + spec | No | dates for every frequency (daily skips closed days, monthly 31st); 2 to 6; lock at 24h; pause max 60 days and D6 re-plan; skip, extend and reschedule limits; done / remaining; two-miss rule with D9; cancel summary; money for three plans (D8); alternatives (D4); times free on all dates |
| S4 | Contract (pure) | CREATE; MODIFY `mobile-booking.error.ts` (codes added only) | `src/domain/booking/mobile-series-contract.ts` + spec | No | every error code, in the mobile error envelope |
| S5 | Series repository | CREATE | `src/infrastructure/persistence/mobile-series.repository.ts` + spec | Writes `booking_series`, `series_occurrence`, and three columns on `booking`, in the **same shapes the desk already writes** | desk board and panel read a mobile series with no error (snapshot); link step writes series id, `routine`, `recurring`; **required: a test that runs the desk nightly job (series materialiser) over a mobile routine, before, during and after its last day, and proves it changes nothing** (no occurrence, booking or `materialised_through` touched) |
| S6 | Create + preview handler | CREATE | `src/application/commands/mobile-series.handler.ts` + spec | Calls `MobileBookingHandler.execute`, `candidatesFor`, `LifecycleHandler` **unchanged** | all or nothing: a failure on session 4 cancels 1 to 3; replay by key; strict slot refused with alternatives |
| S7 | Controller + DTOs | CREATE; MODIFY module lists (lines added only) | `src/interface/http/mobile-series.controller.ts`: `POST`, `GET :id`, `PATCH :id`, `POST :id/cancel` | Module lists only | `route-order.spec.ts`; owner check: stranger and bad id 404, never 403 |
| S8 | Hub read | CREATE | `src/application/queries/mobile-series-read.handler.ts` + spec | Reads only | a desk series id is 404; a session cancelled at the desk shows as cancelled; desk skip and pause show correctly |
| S9 | Recurring tab | MODIFY | `mobile-booking.handler.ts` `list()` only for `filter=recurring` | **No.** Mobile-only route. Flag off: byte for byte today's empty page | snapshot flag off / on |
| S10 | PATCH `SKIP`, `RESCHEDULE`, `EXTEND` | CREATE (handler methods) | S6's file | Calls `LifecycleHandler`, `PlaceHoldHandler`, `RescheduleHandler` unchanged | lock refused; a done visit refused (not K7); moved session keeps the series link; `dryRun` writes nothing |
| S11 | PATCH `PAUSE`, `RESUME` (with the optional new frequency, time, stylist); cancel with its optional reason (and its `dryRun` summary) | CREATE | S6's file | same | sessions moved not lost (D6); resume with nothing sent keeps frequency, time and stylist; resume with a new frequency, time or stylist changes every remaining session, **all or nothing** (one session not free: nothing changes, alternatives answered); the new frequency is stored in `frequency`; summary equals the sum of `cancellationOutcome`; the cancel reason lands in each session's `booking_status_history.reason` and in the `series.cancelled` event |
| S12 | Mobile series job | CREATE | `src/infrastructure/scheduling/mobile-series-job.service.ts` + spec | No. Reads `source 'mobile'` only | auto resume; two misses pause; horizon booking; complete; one 48h event per session |
| S13 | Docs | MODIFY | `docs/api/MOBILE.md` new section (and mark the old series section stale) | No | `scripts/verify-docs.mjs` passes |

Not changed at all: `series.controller.ts`, `booking-series.controller.ts`,
`series.handler.ts`, `materialise-series.handler.ts`, `series.repository.ts`, every
sweeper, every enum, every existing column.

#### customer-api (this repo)

| # | Piece | Create / modify | File | Tests to add |
| - | ----- | --------------- | ---- | ------------ |
| M1 | Flag | MODIFY | `config/settings/base.py`: `SERIES_BOOKING_V1 = env.bool(..., default=False)` | off: 404 |
| M2 | Client calls | CREATE functions | `apps/salons/booking_api.py` (one per booking-api route in E.3) | |
| M3 | Serializers | CREATE | `apps/salons/series_serializers.py` | 2 to 6; frequency; custom dates; stylist required; each PATCH action |
| M4 | Views + urls | CREATE 4 views: `POST booking/series`, `GET booking/series/<id>`, `PATCH booking/series/<id>`, `POST booking/series/<id>/cancel`, each forwarding 1 to 1 to its booking-api route; MODIFY `urls.py` (4 lines added). **`BookingDetailView` and `GroupBookingCancelView` are not touched** | `apps/salons/series_views.py` | dry_run with and without time, create, hub, every action, cancel and its dry_run; booking-api refusals passed through; timeout 503; salon offset `+04:00`; flag off: 404 on all 4 |
| M5 | Recurring rows | MODIFY behind flag | `apps/salons/views.py` `_enrich` (a routine row gets `can_skip`, `can_reschedule` from the lock) | flag off: identical output |
| M6 | Docs | CREATE | `docs/SERIES_BOOKING_API.md`, `docs/MOBILE_SERIES_BOOKING_FE.md` | |

#### Business fixes (each its own PR, Rafa's OK first, desk checked by hand after)

| # | Fix | Needed for mobile? |
| - | --- | ------------------ |
| F1 | `@DeskOnly` on `SeriesController` and `BookingSeriesController` (K3). The desk sends staff tokens only. | **Yes, before the flag goes on** |
| F2 | Nightly job picks new and resumed series (K1, K2) | no |
| F3 | Desk materialiser writes `series_id` + `routine` (K6) | no |
| F4 | Skip guard: state and 48h (K7) | no |
| F5 | Counted series keep their count on a cadence change; no past dates (K5) | no |
| F6 | Series cancels go through the lifecycle (K8) | no |
| F7 | `paidInFull` and `course_draw` money bugs (K13, K14) | before upfront ships |

### E.6 Build order and honest time (Claude Code)

**How the estimate is made.** Group booking was planned at 9 to 11 human days. Claude
built it (about 6,500 lines with tests and docs, both repos) in about one working
day on 2026-09-25. Series is about 2.5 times the size: 10 routes instead of 4, a
background job, and state changes (pause, move, resume) that group did not have.

Hours are Claude working time. They do not include Rafa's review, deploys or the
app team.

| Step | What | Repo | Claude time |
| ---- | ---- | ---- | ----------- |
| 0 | Rafa and the app team answer R1, R2, R5, R7, R11, R12. **Done 2026-09-26 (D1 to D9)** | none | 0 |
| 1 | S1 to S4: flag, migration, pure rules and contract, with specs. **Built 2026-09-26: booking-api branch `feat/mobile-series-rules`, commits `1498ce5` and `a286258` (Figma fixes). OK'd by Rafa with the fixes, 2026-09-26** | booking-api | 2 to 3 h |
| 2 | S5 to S8: create, preview, hub read, proof SQL. **Must include the test that the desk nightly job never touches a mobile routine (S5)**. **Built 2026-09-26: commit `9ea2a67` on `feat/mobile-series-rules` (after fix commit `66b3188`), proven live on a throwaway database, waiting for Rafa's OK** | booking-api | 3 to 4 h |
| 3 | M1 to M4 for `dry_run`, create, read. **The app team can start testing on staging here**. **Built 2026-09-26: customer-api commits `f045b46` and `a81a1fe` (the salon's hours) on `feat/mobile-series-routes`, waiting for Rafa's OK** | customer-api | 2 h |
| 4 | F1 (security, own PR, business) | booking-api | 0.5 h |
| 5 | S9 + M5: Recurring tab. **Also: My Bookings must not show the visits a failed routine released** (see the note under this table) | both | 1 to 1.5 h |
| 6 | S10 + M4: skip, reschedule, extend. **RESCHEDULE and EXTEND get the salon's hours check (E.3)** | both | 3 h |
| 7 | S11 + M4: pause, resume, cancel, quote. **RESUME and PAUSE get the salon's hours check (E.3)** | both | 3 to 4 h |
| 8 | S12: the job | booking-api | 2 to 3 h |
| 9 | S13, M6: docs and the app team's FE guide. **The guide must say: send `Idempotency-Key` only on the real create, never on `dry_run`** (see the note under this table) | both | 1 h |
| 10 | F2 to F7 (optional, each its own business PR) | booking-api | 2 to 3 h |
|    | **Total** | | **about 19 to 25 hours** |

Notes on later steps (Rafa, 2026-09-26):

- **Step 5, phantom visits.** When a routine create fails part way, the visits
  it had already booked are cancelled again in the same request. The customer
  never had them, so My Bookings must not show them, in either tab or in the
  badges. They are easy to find: never linked (no `series_id`), status
  `cancelled`, and the cancel's history reason is exactly `The routine could not
  be booked in full, so this session was released.` The mobile list leaves those
  out (mobile list only; the desk still sees the rows and their history). Test:
  a failed routine leaves nothing in upcoming, archive or the counts.
- **Step 9, the FE guide.** Send `Idempotency-Key` only on the real create, never
  on a `dry_run`. booking-api stores the answer under the key: a key sent with a
  preview would replay that preview (stale availability) to the next preview,
  and would refuse the real create that followed with `IDEMPOTENCY_KEY_REUSED`.
  customer-api already drops the key from a dry run; the app should not rely on
  that.

- That is **3 to 4 working days** of Claude sessions. With a review stop after every
  step, one deploy per step and the business web check after each deploy, expect
  **5 to 7 working days** on the calendar.
- **Add about 30% for staging surprises.** The local database is empty, so real
  shapes are only seen on staging; `SERVICES_FROM_PLATFORM` and `STAFF_FROM_PLATFORM`
  must be on there or services do not resolve (ROUND-3 §1b); per-salon time zone is
  still open.
- **Not in this estimate**: real payments for "pay as you go" and "upfront" (blocked
  on the payment team; about 1 to 1.5 days of Claude time once their API exists),
  real SMS or push (no transport exists), per-salon time zone (Olu).

### E.7 Risks, and what guards them

| Risk | Guard |
| ---- | ----- |
| The desk presses "materialise" on a mobile series | nothing is `planned` inside the 90 days (booked at create or by the mobile job), so it seats nothing. It does rewrite `materialised_through` to today + 70, which would let the nightly job pick the routine 70 days later: the mobile job puts 9999-12-31 back (E.4 item 6). F1 does not stop staff, by design |
| The desk nightly job picks a mobile series | `materialised_through` = 9999-12-31, so `dueForTopUp` (status active and date before today) never selects it, on any day. Proven by `mobile-series.desk-job.spec.ts` (the real job and query code), by the real job run against Postgres, and by `proof-mobile-series-created.sql` CHECK 4 (3,651 days, 0 picked). The desk panel shows 9999-12-31 as the horizon line of a mobile routine |
| The desk pauses or skips a mobile series with desk tools | allowed (R21); the hub reads rows, so it shows what happened; the desk pause loses sessions, which is the desk's rule |
| A session create fails half way through a series | all or nothing: earlier sessions are cancelled as salon initiated (full refund, nothing paid anyway), same as group releasing its hold. Step 5 hides them from My Bookings |
| The business web drags or reschedules a routine visit | both desk paths move the SAME booking row (new day, time, stylist) and leave `series_occurrence` as planned (K17). The hub reads the booking, so the visit stays in the routine at its new time: specs, and seen live with the desk's own move route (2026-09-26). **Rule for steps 6 to 8: a session's day, time and stylist always come from its booking, never from `series_occurrence.planned_*`, which can be stale.** Mobile writes (skip, move, pause) update both |
| A routine create is slow (6 visits booked one by one) | measured 2.4 to 8.7 s for 6 visits on a laptop (dry_run 0.06 to 0.17 s). customer-api waits `SERIES_BOOKING_CREATE_TIMEOUT` (45 s) on the create only, under gunicorn's `--timeout 60`. The FE guide must tell the app to wait up to 60 s on create |
| Six single creates are slow | one hold + confirm per session, 6 at most; measured on staging in step 3 |
| The customer is promised a stylist who then leaves | the job and the hub show "needs your action" with alternatives; never a silent swap |
| No messages are really sent | stated in the FE guide; the events are ready for the sending team |
| The old series doc in `MOBILE.md` misleads the app team | S13 marks it stale and points to the new section |

### E.8 Deploy order (booking-api)

1. **Run the migration first** (`prisma migrate deploy` against the database).
   It only adds nullable columns and CHECKs, so the booking-api image running now
   does not notice it.
2. **Then start the new booking-api image.**

Why this order: the new image's Prisma client reads the new columns on every
`booking_series` read, the desk's panel and board included. A new image against a
database without them would fail those desk reads ("column does not exist"). The
other way round is safe: an old image never names the new columns.

The flag stays OFF through both steps. Turning `MOBILE_SERIES_BOOKING` on is a
third, separate step, after F1 (security fix) is live and the business web has been
checked by hand.
