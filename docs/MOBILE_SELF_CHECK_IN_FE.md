# Mobile Self Check-in: FE Guide

Date: 2026-10-08, updated 2026-10-09 (chair check-in, §12; Cancel Request, §13; the welcome, §14) and 2026-10-10 (`can_check_in` and `check_in_opens_at` on the booking read, §3; `can_scan_chair` and the server's chair switch, §12.5; **`offersWaitForStaff` changed, §12.3**). For: the mobile app team.

The customer arrives at the salon, opens their booking and taps **"I am
here"**. The salon's desk sees them on its reception list and approves or
rejects. Approving checks the booking in, exactly as if the desk had pressed
its own check-in button.

**Everything here is behind the server switch `SELF_CHECK_IN_V1`, and it is
OFF today.** Until it is on, every route here answers `404`. See §10.

---

## 0. Read this first

1. **Base URL:** `https://api.gostyle.uk/api/v1`. Every path below is relative to it.
2. **Auth:** `Authorization: Bearer <app login token>` on every call. The customer is always the signed-in user.
3. **No body** for "I am here" (Wait for Staff): it sends nothing but the token. **At a chair**, one field: `chair_token` (§12).
4. **Keys are `camelCase` here**, not `snake_case`. These answers are booking-api's own, passed through as they came, like the single cancel and reschedule answers.
5. **Buttons come from the server.** Show "I am here" only when the row's `can_check_in` is `true` (§3), and Scan the chair only when `can_scan_chair` is `true` (§12.1), as with `can_cancel` and `can_reschedule`. Never work the rule out in the app. Every refusal has a `code` to switch on (§8). Never show a refusal as a crash.
6. **The pass QR is unchanged** (§9).
7. **Two 503s mean different things** (§12.3). Offer Wait for Staff only when `details.fallback` is `"WAIT_FOR_STAFF"`, whatever the status (**changed 2026-10-10**, §12.3).
8. **Cancel Request** takes a waiting request back (§13). Its route says `withdraw`, never `cancel`: `POST /booking/{id}/cancel` cancels the whole visit.

---

## 1. Endpoints at a glance

| Method | Path | What it does |
| ------ | ---- | ------------ |
| `POST` | `/booking/{id}/check-in` | "I am here": ask the desk to check me in |
| `GET`  | `/booking/{id}/check-in` | Has the desk answered? |
| `POST` | `/booking/{id}/check-in/withdraw` | Cancel Request: take back a request that is still waiting (§13) |

`{id}` is the id on the My Bookings row: a `SINGLE` row's id, or a `ROUTINE`
session row's id. **Not a `GROUP` row** (§9).

---

## 2. The flow

1. The customer opens an upcoming booking whose row has `can_check_in: true` (from 30 minutes before its start).
2. They tap **"I am here"**: `POST /booking/{id}/check-in`. Answer: `WAITING`. At a chair, they scan the chair's card instead (§12); the rest of the flow is the same.
3. The app shows "The salon knows you are here", and reads the answer every 10 seconds while the screen is open (§7).
4. The desk answers.
   - **`APPROVED`:** the customer is checked in. Refresh the booking; its `status` is now `CHECKED_IN`.
   - **`REJECTED`:** "Please speak to the desk." There is no second try for this booking.
5. If nobody answers before the booking's end time, the request becomes `EXPIRED`. The booking is **not** marked a no-show; the desk sorts it out by hand.

---

## 3. When to show "I am here"

**Show it only when `can_check_in` is `true`**: on the My Bookings row, and on
the booking's own screen from `GET /booking/{id}`, which carries the same
field worked out by the same function, so the two can never disagree. The
server works it out (docs/BOOKING_LIST_API.md §5), the same way it does
`can_cancel` and `can_reschedule`. For reference only, never to compute in the
app, it is `true` when:

- the switch `SELF_CHECK_IN_V1` is on;
- `booking_type` is `SINGLE` or `ROUTINE` (never `GROUP`);
- `status` is `CONFIRMED_BY_SALON`;
- now is between 30 minutes before `start_time` and `end_time`.

The field is a snapshot from when it was read. **Refresh My Bookings, or the
booking, when the screen comes back to the foreground**, so a booking that was
40 minutes away gets its button once the window opens.

**While `SELF_CHECK_IN_V1` is off,** the row still says `can_check_in: false`
(it always has), and `GET /booking/{id}` has no `can_check_in` key at all.
Treat a missing key as `false`.

**`check_in_opens_at`: when "I am here" opens.** On the row and on
`GET /booking/{id}`, the same value, while the switch is on.
- It is UTC, written like every other check-in time
  (`"2026-09-18T11:40:00.000Z"`). Show it in the salon's time zone:
  "Check-in opens at 15:40."
- It is `null` when self check-in does not apply to this booking: a party, a
  booking that is not `CONFIRMED_BY_SALON`, or one with no start time.
- **It is a fact, not the button.** A booking whose window has closed still
  has the time it opened. Whether to show "I am here" now is `can_check_in`
  alone; never work the button out from this time.
- Use it to know when to read again: a booking whose time is still ahead
  gets its button once that time passes, so refresh then (or when the screen
  comes back to the foreground, as above).
- No key while the switch is off.

**`can_scan_chair`: when to show Scan the chair (§12).** On the row and on
`GET /booking/{id}`, the same value, while the switch is on. It is
`can_check_in` with the server's chair switch (`CHAIR_SCAN_V1`, §12.5) on:
- `false` wherever `can_check_in` is `false`;
- `false` on every booking while chair scanning is off, even where
  `can_check_in` is `true`;
- no key while `SELF_CHECK_IN_V1` is off. Treat a missing key as `false`.

Wait for Staff follows `can_check_in`, never `can_scan_chair`: it is "I am
here" without a chair, and it works with chair scanning off.

**The row cannot know one thing:** whether the desk already said no for this
booking. So on the booking screen, also `GET /booking/{id}/check-in` once (§6):
if it says `REJECTED`, hide the button even though `can_check_in` is `true`.
A tap in that case answers `409 BOOKING_CHECKIN_REJECTED` anyway (§8).

The server checks everything again on the tap. If the list is out of date,
the answer is a `409` with a `code` (§8), and the app shows that message.

Once the request is `APPROVED`, the booking is `CHECKED_IN` and the next list load has `can_check_in: false`.

---

## 4. TypeScript types (copy these)

```ts
export type CheckInState =
  | 'WAITING'   // raised, the desk has not answered yet
  | 'APPROVED'  // the desk said yes: the booking is CHECKED_IN
  | 'REJECTED'  // the desk said no: speak to the desk, no second try
  | 'EXPIRED'   // nobody answered before the booking's end time
  | 'CLOSED'    // the booking moved on another way (checked in at the desk, cancelled, moved)
  | 'WITHDRAWN'; // the customer took it back with Cancel Request (§13); they may ask again

export interface CheckInChair {
  number: string;            // "7": what the customer and the desk read
  zoneName: string | null;   // "Window section", or null
}

export interface CheckInRequest {
  requestId: string;
  bookingId: string;
  state: CheckInState;
  raisedAt: string;          // ISO 8601, UTC
  decidedAt: string | null;  // null while WAITING
  chair?: CheckInChair | null;  // null or absent: no chair (Wait for Staff) (§12)
}

// The POST body (§12). Leave it out entirely for Wait for Staff.
export interface RaiseCheckInBody {
  chair_token?: string;  // snake_case: customer-api's own field
}

export interface CheckInAnswer {
  request: CheckInRequest | null;  // null: never raised
}

// The welcome (§14): the check-in that stands on the booking.
export interface CheckInWelcome {
  at: string;                     // ISO 8601, UTC: when they were checked in
  via: 'SELF' | 'STAFF' | null;   // always present; null: checked in before this was recorded
  byName: string | null;          // "Layla R.", best effort: may be null (§14)
}

// GET /booking/{id}/check-in (§6). The POST answers carry no checkIn.
export interface CheckInRead extends CheckInAnswer {
  checkIn: CheckInWelcome | null;  // null while WAITING, and when none stands (§14)
}

// The SAME three facts on GET /booking/{id}, as `check_in` (§14.3), in that
// booking's snake_case. A DIFFERENT TYPE ON PURPOSE: reuse CheckInWelcome
// there and `byName` reads undefined, and the screen shows a blank name.
export interface BookingCheckIn {
  at: string;                     // ISO 8601, UTC
  via: 'SELF' | 'STAFF' | null;   // as CheckInWelcome.via
  by_name: string | null;         // snake_case here: "Layla R.", best effort
}

// GET /booking/{id} gains, while SELF_CHECK_IN_V1 is on (absent while off):
export interface BookingWithCheckIn {
  check_in?: BookingCheckIn | null;  // absent: off; null: nobody checked in; object: checked in (§14.3)
  can_check_in?: boolean;            // the row's rule, the same function (§3)
  check_in_opens_at?: string | null; // as on the row (§3)
  can_scan_chair?: boolean;          // as on the row (§3, §12.1)
}

// The My Bookings row (GET /bookings) gains these fields (§3).
export interface MyBookingsRowCheckIn {
  can_check_in: boolean;              // snake_case: this one is customer-api's own field
  check_in_opens_at?: string | null;  // UTC ISO; null: does not apply; absent: switch off (§3)
  can_scan_chair?: boolean;           // Scan the chair (§12.1); false: chairs off (§12.5); absent: switch off
}

// booking-api's refusal shape (§8, shape B).
export interface BookingApiError {
  statusCode: number;
  code: string;
  message: string;
  details?: {
    windowOpensAt?: string;  // BOOKING_CHECKIN_WINDOW, too early
    windowClosed?: true;     // BOOKING_CHECKIN_WINDOW, too late
    status?: string;         // BOOKING_STATE_INVALID, e.g. "CHECKED_IN"
    reason?: string;         // BOOKING_CHAIR_REFUSED, or CHAIR_CHECK_UNAVAILABLE on the 503 (§12)
    fallback?: 'WAIT_FOR_STAFF';  // "use Wait for Staff": the 503 of §12.3, or 409 BOOKING_CHAIR_SCAN_OFF (§12.5)
    request?: CheckInState | null;  // BOOKING_STATE_INVALID on Cancel Request: the state now; null, none raised (§13)
  };
  error: string;
}
```

---

## 5. "I am here": `POST /booking/{id}/check-in`

No body: this is Wait for Staff. Send the token only. At a chair, see §12.

**`201 Created`**: a new request.

```json
{
  "request": {
    "requestId": "01a11b4c-aa95-75f5-8e65-b815eccc7204",
    "bookingId": "aaaaaaaa-0000-4000-8000-0000000000a1",
    "state": "WAITING",
    "raisedAt": "2026-10-11T03:50:00.000Z",
    "decidedAt": null
  }
}
```

**`200 OK`**: one was already waiting. Same body, same `requestId`. A double
tap, or a tap after the app was closed, is harmless. Treat 200 and 201 the same.

---

## 6. Read the answer: `GET /booking/{id}/check-in`

**`200 OK`**: `{ "request": CheckInRequest | null, "checkIn": CheckInWelcome | null }`
(`CheckInRead`, §4). `request` is null if the customer never tapped "I am here"
for this booking. `checkIn` is the welcome: §14.

| `state` | Show | Button |
| ------- | ---- | ------ |
| `WAITING` | "The salon knows you are here. Please take a seat." | hidden |
| `APPROVED` | The welcome, from `checkIn` (§14). Refresh the booking. | hidden |
| `REJECTED` | "The salon could not confirm your arrival. Please speak to the desk." | hidden for good |
| `EXPIRED` | "The salon did not answer in time. Please speak to the desk." | hidden |
| `CLOSED` | Nothing special: show the booking as it now is. | as §3 |
| `WITHDRAWN` | Nothing special: they took it back. They may tap "I am here" again, or scan another chair. | as §3 |

The desk's own reason for a rejection is never sent to the app. Use your own words.

---

## 7. Reading the answer while the customer waits

- While the screen is open and the state is `WAITING`, `GET` every **10 seconds**.
- Stop as soon as the state is anything but `WAITING`, or the screen closes.
- Also `GET` once when the booking screen opens, to draw the right state.
- There is **no push** when the desk answers (yet).

---

## 8. Errors

Two shapes, as on the single cancel and reschedule:

- **Shape A**, this service's envelope `{detail, code, errors[]}`: `401`, `422` (a broken chair scan), `503` (booking-api unreachable), and `404` while this service's switch is off.
- **Shape B**, booking-api's `{statusCode, code, message, details?}`: every refusal below. `BOOKING_CHAIR_SCAN_OFF` is customer-api's own, written in this shape so every `409` here reads one way.

| Status | `code` | When | What the app does |
| ------ | ------ | ---- | ----------------- |
| `409` | `BOOKING_CHECKIN_WINDOW` with `details.windowOpensAt` | Too early | "Check-in opens at {time}." Show the time in the salon's zone. |
| `409` | `BOOKING_CHECKIN_WINDOW` with `details.windowClosed` | After the end time | "Check-in for this booking has closed." |
| `409` | `BOOKING_STATE_INVALID` with `details.status` | Not confirmed (already checked in, cancelled, unpaid, ...) | Refresh the booking. If `CHECKED_IN`, show "You are checked in." |
| `409` | `BOOKING_CHECKIN_REJECTED` | The desk said no before | "Please speak to the desk." No retry. |
| `409` | `BOOKING_CHAIR_REFUSED` with `details.reason` | At a chair: not with that chair | Show `message` (§12.2). |
| `409` | `BOOKING_CHAIR_SCAN_OFF` with `details.fallback: "WAIT_FOR_STAFF"` | A scan while chair scanning is off. Nothing was raised. The app should not have sent it: `can_scan_chair` was `false` (§12.5) | Show `message`, offer Wait for Staff (§12.3), hide Scan the chair. |
| `409` | `BOOKING_STATE_INVALID` with `details.request` | Cancel Request: nothing is waiting | Show the screen for `details.request` (§13). |
| `422` | `validation_error` (shape A), `errors[0].field` `chair_token` | At a chair, scanning on: the scan was empty or not text | "Please scan the card again." Offer Wait for Staff. |
| `404` | `BOOKING_NOT_FOUND` | Not the customer's booking, a party's id, or booking-api's switch is off | Hide the button. |
| `404` | `not_found` (shape A) | This service's switch is off | Hide the button. |
| `401` | `not_authenticated` (shape A) | No token, or it expired | Refresh the token, as everywhere. |
| `503` | `DEPENDENCY_UNAVAILABLE` with `details.fallback: "WAIT_FOR_STAFF"` (shape B) | At a chair: the chair could not be checked. The desk still works. | Show `message`, offer Wait for Staff (§12.3). |
| `503` | `booking_api_unavailable` (shape A) | Booking is down, and so is the desk's screen | "Something went wrong on our side. Please try again in a moment." **No** Wait for Staff, and do not say the desk will check them in (§12.3). |
| `503` | `DEPENDENCY_UNAVAILABLE` with **no** `details.fallback` (shape B) | Booking could not check the customer's sign-in | As `booking_api_unavailable`. |

---

## 9. Pass QR, parties, and what is not here yet

- **Pass QR.** The pass keeps showing `pass_qr_code` from `GET /booking/{id}`, the booking's own code (for example `GS-1403`). The desk may scan it. The app does not scan the pass.
- **Chair QR.** The app scans the card on the chair: §12.
- **Parties (`GROUP` rows).** No "I am here" in v1. A party's id is `404` here; the party checks in at the desk.
- **Not here yet:** a push when the desk answers (read it, §7).

---

## 10. Rollout: `SELF_CHECK_IN_V1`

The switch exists twice, on customer-api and on booking-api, and both must be
on. Until then both routes answer `404`. The app can ship the button early:
while customer-api's switch is off, `can_check_in` is `false` on every row, so
the button never shows.

Chair scanning has its own order: §12.5.

**Cancel Request and the welcome screen (§13) have a fixed deploy order:**

1. booking-api's branch `feat/check-in-gaps` is deployed.
2. Then customer-api's branch `feat/check-in-gaps` is deployed.
3. Then customer-api's `SELF_CHECK_IN_V1` is turned on. booking-api's switch has to be on too, as above.

**Do not build against them until the server team says all three are done.** Before step 2, Cancel Request answers `404`.
Until step 3, `GET /booking/{id}` carries no `check_in` key, whatever booking-api's switch says (§14.3).

**`can_check_in` on `GET /booking/{id}` (§3) needs booking-api first:**
booking-api's `feat/single-read-booking-type` (the single read always carries
`booking_type`), then customer-api's `feat/check-in-fe-gaps`. The rule needs
the booking's type: in the other order, the booking screen would say `false`
where the list says `true`.

---

## 11. QA checklist

- [ ] Tap 31 minutes before the start: `409 BOOKING_CHECKIN_WINDOW` with `windowOpensAt`; the message shows the salon's time.
- [ ] Tap 10 minutes before: `201 WAITING`. Tap again: `200`, the same `requestId`.
- [ ] The desk approves: within 10 seconds the screen says checked in, and the booking is `CHECKED_IN`.
- [ ] The desk rejects: the screen says speak to the desk; tapping again gives `409 BOOKING_CHECKIN_REJECTED`.
- [ ] Nobody answers until the end time: `EXPIRED`; the booking is not shown as a no-show.
- [ ] A `GROUP` row has `can_check_in: false` and shows no button.
- [ ] A row 40 minutes away: `can_check_in: false`. Bring the app back to the foreground 10 minutes later: the list reloads and it is `true`.
- [ ] After a rejection: `can_check_in` may still be `true`, and the booking screen hides the button because `GET` says `REJECTED`.
- [ ] Airplane mode on tap: an error message, no crash; tapping again later works (`200` if it went through).
- [ ] Switch off: `can_check_in` is `false` on every row, no button, and the route answers `404`.

---

## 12. At a chair: scan the card

Every chair has a QR card. The customer sits down, scans it, and the app says
"I am here" **with that chair**. The desk approves as before and sees which
chair they are in. booking-api's side: gostyle-booking-api
`docs/chair-check-in.md`.

### 12.1 Two buttons, one route

- **Wait for Staff** shows when `can_check_in` is `true`: the rule of "I am here" (§3).
- **Scan the chair** shows only when `can_scan_chair` is `true`: `can_check_in`
  with the server's chair switch on (§12.5). With chairs off, the screen has
  Wait for Staff alone.

| Button | Call |
| ------ | ---- |
| **Scan the chair** | `POST /booking/{id}/check-in` with `{"chair_token": "<the QR text>"}` |
| **Wait for Staff** | The same `POST` with **no body**: the "I am here" of §5, unchanged |

```json
{ "chair_token": "q7Xk2mP9rT4vW8yZ1aB3cD" }
```

- `chair_token` is `snake_case` (customer-api's own field). The answer is `camelCase` (booking-api's), as in §5.
- Send the scanned text **exactly as it came**. Do not trim it, change its case or try to read it. The server works out which chair it is.
- Never send an empty `chair_token`. An empty or non-text one is a `422` (shape A, `errors[0].field` `chair_token`, code `blank` or `invalid`), and nothing is raised. Ask the customer to scan again, or offer Wait for Staff.
- The app's normal `User-Agent` header goes along to the salon's scan record. Nothing to do.
- **Never retry a scan by itself.** Every answered scan is a real scan in the salon's record. A second scan is the customer's choice.

### 12.2 The answers

**`201` / `200`**: as §5, with the chair on the request:

```json
{
  "request": {
    "requestId": "01a11b4c-aa95-75f5-8e65-b815eccc7204",
    "bookingId": "aaaaaaaa-0000-4000-8000-0000000000a1",
    "state": "WAITING",
    "raisedAt": "2026-10-11T03:50:00.000Z",
    "decidedAt": null,
    "chair": { "number": "7", "zoneName": "Window section" }
  }
}
```

Show "The salon knows you are here, in chair 7." `zoneName` may be `null`.
`chair` is `null` on a Wait for Staff request. `GET` (§6) returns the same
`chair`. **Draw the chair from the answer, never from what was just scanned**
(see the `200` below).

**One request at a time, and its chair stays.** While a request waits, a scan
of any chair, or a tap on Wait for Staff, answers `200` with that same request
and the chair it was raised with (or `null`). The new chair is not recorded.
To change chairs, the customer tells the desk.

**`409 BOOKING_CHAIR_REFUSED`**: not with that chair. **Show `message` as it
came**; it is written for the customer. `details.reason` says which:

| `details.reason` | `message` | Buttons |
| ---------------- | --------- | ------- |
| `CARD_OUT_OF_DATE` | "This card is out of date. Please see the desk." | Wait for Staff |
| `OTHER_SALON` | "This chair is not at the salon of your booking. Please see the desk." | Wait for Staff |
| `CHAIR_NOT_AVAILABLE` | "That chair is not available. Please take another or see the desk." | Scan another chair, Wait for Staff |
| `UNKNOWN_CARD` | "This is not a chair card we know. Please scan the card on your chair, or see the desk." | Scan again, Wait for Staff |
| anything else | as it came | Scan again, Wait for Staff |

`CHAIR_NOT_AVAILABLE` covers both "this chair cannot take a booking" and
"somebody is in it". The app is never told which, or who.

The booking's own refusals (§8: too early, closed, not confirmed, rejected
before) come before the chair rules: a live card scanned 40 minutes early is
`BOOKING_CHECKIN_WINDOW`, not a chair answer. Two answers can come before
them, because the card is read before the booking is looked at:
`UNKNOWN_CARD`, and the `503` of §12.3. And while the server's chair switch
is off, `409 BOOKING_CHAIR_SCAN_OFF` (§12.5) comes before all of them:
nothing is read.

### 12.3 The two 503s

**This is the most important part of this section.**

| What came back | What it means | What the app does |
| -------------- | ------------- | ----------------- |
| `503`, shape B, `details.fallback: "WAIT_FOR_STAFF"` (`code` `DEPENDENCY_UNAVAILABLE`) | **Scanning is off right now. The desk still works.** | Show `message` and the **Wait for Staff** button. |
| `503`, shape A, `code` `booking_api_unavailable` | **Booking itself is down. Nothing works**, the desk's screen too. | "Something went wrong on our side. Please try again in a moment." **No** Wait for Staff button. |
| `503`, shape B, `code` `DEPENDENCY_UNAVAILABLE`, **no** `details.fallback` | Booking could not check the customer's sign-in. Wait for Staff fails the same way. | As `booking_api_unavailable`. |

**The rule: offer Wait for Staff only when `details.fallback` is
`"WAIT_FOR_STAFF"`.** Never on the `503` alone, and never on the `code` alone.
If the app treats them the same, it tells a customer to see the desk when the
desk's screen is dead too. The same rule covers `409 BOOKING_CHAIR_SCAN_OFF`
(§12.5): it carries the same `details.fallback`.

```ts
export function offersWaitForStaff(body: unknown): boolean {
  return (body as BookingApiError | null)?.details?.fallback === 'WAIT_FOR_STAFF';
}
```

> **Changed 2026-10-10: `offersWaitForStaff` no longer checks the status.**
>
> - **It was** `status === 503 && details?.fallback === 'WAIT_FOR_STAFF'`,
>   taking `(status, body)`.
> - **It is** `details?.fallback === 'WAIT_FOR_STAFF'` alone, taking `(body)`.
> - **Why.** A deliberate switch is not an outage, so a scan while chair
>   scanning is off answers `409 BOOKING_CHAIR_SCAN_OFF` (§12.5), not a
>   `503`. The status no longer decides the button; `details.fallback` alone
>   does, as the rule above already said. The old sample contradicted its own
>   rule.
> - **Until the app changes it:** an app on the old helper shows that `409`'s
>   `message` with **no** Wait for Staff button, on that one refusal only.
>   That is small: the app should not have sent the scan, because
>   `can_scan_chair` was `false`, and Wait for Staff is its own button on the
>   same screen (§12.1). Every `503` behaves as before.

Show the button; let the customer tap it. Do not send Wait for Staff for them.

### 12.4 What the app shows, step by step

1. The booking screen shows **Wait for Staff** when `can_check_in` is `true` (§3), and **Scan the chair** beside it when `can_scan_chair` is `true` (§12.1).
2. Scan: `POST` with `chair_token`.
   - `201` / `200`: the state as in §6, and "in chair {number}" when `request.chair` is set (from the answer, §12.2). Then §7, as for any request.
   - `409 BOOKING_CHAIR_REFUSED`: `message`, and the buttons from §12.2.
   - `409 BOOKING_CHAIR_SCAN_OFF`: `message`, and Wait for Staff; hide Scan the chair (§12.5).
   - `503` with `details.fallback`: `message`, and Wait for Staff.
   - Anything else: §8.
3. Wait for Staff: `POST` with no body. Exactly §5.

### 12.5 Rollout

The order is fixed:

1. platform (the chair cards),
2. booking-api (reads the chair),
3. customer-api with the chair switch (`feat/check-in-fe-gaps`): it passes
   `chair_token` on while `CHAIR_SCAN_V1` is on, and refuses it while off,
4. customer-api's `CHAIR_SCAN_V1` turned on: `can_scan_chair` becomes `true`
   where `can_check_in` is,
5. then the app shows Scan the chair on `can_scan_chair`.

**Do not ship scanning before the server team says 1 to 4 are done.** A
customer-api older than step 3 ignores `chair_token` without an error, and the
customer is raised with no chair. The sign: a `201` whose `request.chair` is
`null` or missing after a scan. If QA sees that, stop and report it. From
step 3 on, a scan is never raised without its chair: while the switch is off
it is refused (below).

**The server's chair switch: `CHAIR_SCAN_V1`** (customer-api, off by
default). It turns chair scanning on and off without an app release.
`SELF_CHECK_IN_V1` still turns both buttons off.
- **Off:** `can_scan_chair` is `false` on every row and booking, so the app
  draws no Scan the chair button. Wait for Staff still shows on
  `can_check_in` and still works.
- **On:** `can_scan_chair` is `can_check_in`.

An app-side switch (remote config) is no longer needed to turn scanning off.
If the app keeps one, show Scan the chair only when both say yes.

**`409 BOOKING_CHAIR_SCAN_OFF`: a scan sent while the switch is off.**

```json
{
  "statusCode": 409,
  "code": "BOOKING_CHAIR_SCAN_OFF",
  "message": "Scanning the chair is not available right now. Please use Wait for Staff and the desk will check you in.",
  "details": { "fallback": "WAIT_FOR_STAFF" },
  "error": "Conflict"
}
```

- **The app should not have sent it.** While the switch is off,
  `can_scan_chair` was `false`, so there was no Scan the chair button to tap.
  It comes back to an app that ignores `can_scan_chair`, or to a screen drawn
  before the switch went off. It is the server's backstop, not a path to
  design a screen around.
- **Nothing was raised**, and booking-api was not asked. The customer is never
  queued without the chair they scanned.
- **What the app does:** show `message` and the Wait for Staff button (it has
  `details.fallback`, §12.3), hide Scan the chair, and read the booking again
  (its `can_scan_chair` is `false` now).
- **Any `chair_token` gets it, an empty one too.** While scanning is off it is
  this `409`, never the `422` "scan again" (§8), which would send the customer
  back to a scanner that is off.
- **A `409`, not a `503`:** a switch turned off on purpose is not an outage.
  And not `BOOKING_CHAIR_REFUSED`, which means "not with that chair": no chair
  was looked at.

**For the server team, on deploy.** `CHAIR_SCAN_V1` is off unless the server
sets it. If `SELF_CHECK_IN_V1` is already on when step 3 deploys, and an app
build already sends `chair_token`, deploying without `CHAIR_SCAN_V1=true`
switches scanning off for that build: its scans get this `409` and the Wait
for Staff offer. Set `CHAIR_SCAN_V1` first if scanning is meant to stay on.
On 2026-10-10 `SELF_CHECK_IN_V1` was set on no production service, so the
whole route answered `404` and no app could be sending `chair_token`. Check
again at deploy time: the switch may be on by then.

### 12.6 QA checklist (chairs)

- [ ] Scan a live card in the booking's own salon, 10 minutes before the start: `201`, and the screen shows the chair number.
- [ ] While it waits, scan another chair: `200`, the same `requestId`, and the screen still shows the first chair.
- [ ] Scan a card from another salon: `409 BOOKING_CHAIR_REFUSED`, `OTHER_SALON`, its `message` shown.
- [ ] Scan the pass QR, or any other QR: `409 BOOKING_CHAIR_REFUSED`, `UNKNOWN_CARD`.
- [ ] Scan the chair of a customer who is checked in there: `409 BOOKING_CHAIR_REFUSED`, `CHAIR_NOT_AVAILABLE`.
- [ ] Scan a live card 40 minutes before the start: `409 BOOKING_CHECKIN_WINDOW`, not a chair answer.
- [ ] An empty scan (simulate it): `422`, field `chair_token`; no crash, nothing raised.
- [ ] Platform down (ask the server team): `503` with `details.fallback: "WAIT_FOR_STAFF"`, its `message` and the Wait for Staff button. Tap it: `201`, `chair: null`.
- [ ] booking-api down (ask the server team): `503 booking_api_unavailable`, **no** Wait for Staff button, "try again".
- [ ] App-side switch off: no Scan the chair button; Wait for Staff still works.
- [ ] Server chair switch off (`CHAIR_SCAN_V1`, ask the server team), 10 minutes before the start: `can_check_in: true` and `can_scan_chair: false` on the row and the booking; no Scan the chair button; Wait for Staff answers `201`, `chair: null`.
- [ ] Chair switch off, a scan forced anyway (simulate it): `409 BOOKING_CHAIR_SCAN_OFF`, its `message` and the Wait for Staff button; `GET /booking/{id}/check-in` shows nothing new was raised.
- [ ] Chair switch on: `can_scan_chair: true` exactly where `can_check_in` is `true`.

---

## 13. Cancel Request: `POST /booking/{id}/check-in/withdraw`

While the request is still `WAITING`, the customer can take it back: they
scanned the wrong chair, or want Wait for Staff instead. The request becomes
`WITHDRAWN`. Unlike after a rejection, they may tap "I am here" (or scan a
chair) again straight away.

- **Show the button only while the latest request is `WAITING`** (§6).
- **The path says `withdraw`, never `cancel`.** `POST /booking/{id}/cancel`
  cancels the whole visit, one segment away. The button can still say
  "Cancel Request".
- **No body.** The token only, as for every route here.
- **The booking is untouched.** It is still never marked a no-show
  automatically: someone who took it back to scan again is still in the salon.

### 13.1 The answers

**`200 OK`**: `{ "request": CheckInRequest }`, with `state: "WITHDRAWN"`. A
second tap answers the same request. Treat both the same.

```json
{
  "request": {
    "requestId": "01a11b4c-aa95-75f5-8e65-b815eccc7204",
    "bookingId": "aaaaaaaa-0000-4000-8000-0000000000a1",
    "state": "WITHDRAWN",
    "raisedAt": "2026-10-11T03:50:00.000Z",
    "decidedAt": "2026-10-11T03:52:00.000Z",
    "chair": null
  }
}
```

**`409 BOOKING_STATE_INVALID`**: nothing is waiting to take back. Shape B
(§8). `details.request` is the request's state **as it is now**, so the app
can move on without another `GET`:

| `details.request` | `message` | What happened | Show |
| ----------------- | --------- | ------------- | ---- |
| `APPROVED` | "This request has already ended." | The desk approved it a moment before the tap. | The `APPROVED` screen (§6). |
| `CLOSED` | "This request has already ended." | The desk checked them in with its own button. This very call recorded it. | Refresh the booking; as §6. |
| `EXPIRED` | "This request has already ended." | The end time passed. This very call recorded it. | As §6. |
| `REJECTED` | "This request has already ended." | The desk said no. | As §6. |
| `null` | "There is no check-in request to cancel." | None was ever raised. An app bug: the button should not have been showing. | Hide Cancel Request. |

A `GET` straight after a `409` answers the same state: the request already
holds it.

Everything else is as §8: `404` (not their booking, or a switch is off),
`401`, and `503 booking_api_unavailable` (shape A). After that `503`, the
request may still be waiting: `GET` again before showing anything.

### 13.2 QA checklist (Cancel Request)

- [ ] "I am here", then Cancel Request: `200 WITHDRAWN`. Tap again: `200`, the same `requestId`.
- [ ] Then "I am here" again: `201 WAITING`, a new `requestId`.
- [ ] Scan chair 7, Cancel Request, scan chair 8: the new request shows chair 8.
- [ ] The desk approves while the screen still says `WAITING`; tap Cancel Request: `409` with `details.request: "APPROVED"`, and the app shows checked in.
- [ ] The desk checks them in with its own button while the request waits; tap Cancel Request: `409` with `details.request: "CLOSED"`, and a `GET` straight after says `CLOSED` too.
- [ ] Cancel Request on a booking with no request: `409`, `details.request: null`, "There is no check-in request to cancel."
- [ ] Switch off: `404`.

---

## 14. The welcome screen

Once the desk has checked the customer in, the app can greet them: "Checked
in by Layla R. at 14:24". The same three facts (when, how, by whom) come on
two reads, both booking-api's, passed through as they came:

| Read | Field | Type (§4) | When to read it |
| ---- | ----- | --------- | --------------- |
| `GET /booking/{id}/check-in` | `checkIn: { at, via, byName }` | `CheckInWelcome` | After "I am here": the app is already polling it (§7). |
| `GET /booking/{id}` | `check_in: { at, via, by_name }` | `BookingCheckIn` | The desk scanned the pass: no request was ever raised, so only the booking knows (§14.3). |

**Two types, not one: the casing differs, and it cannot be made to match.**
Each field comes from its own booking-api read (the check-in read is
camelCase, the booking read snake_case), and this service passes both
through untouched. Reuse one type for both and `byName` reads `undefined`
on the booking screen: a blank name nobody notices until a customer does.

A `ROUTINE` row works the same: its `id` is that session's own booking id
(§1), so both reads, with the session's id, carry the session's welcome.
**Read a session's welcome from its own booking** (`GET /booking/{session_id}`),
never from the routine's session list (`GET /booking/{routine_id}`): the
routine's read carries no check-in for its sessions.

### 14.1 When `checkIn` is null

- While the request is `WAITING`. Polling a waiting request never looks
  anything up, so it costs nothing to keep polling (§7).
- When no check-in stands: none yet, or the desk undid it.
- When no request was ever raised (`request` is null too).
- Rarely, the booking is `CHECKED_IN` with no check-in behind it: the salon
  set the status by hand. Show "You are checked in." with no time and no
  name.

### 14.2 The fields

**`at`**: when they were checked in. Show it in the salon's time zone.

**`via`**: which welcome to draw. It is always present.

| `via` | What happened | Draw |
| ----- | ------------- | ---- |
| `SELF` | The customer asked first (at a chair, or with Wait for Staff), and the desk approved it. | The self welcome. |
| `STAFF` | The desk checked them in on its own: their pass scanned, or the booking found on the calendar. The server cannot tell those two apart. | The desk welcome. |
| `null` | Checked in before the server recorded how (before this release). | The plain welcome: "You are checked in at 14:24", with the name if there is one. |

**Draw from `checkIn.via`, never from the request's state.** An approval the
desk undid and then redid with its own button leaves an `APPROVED` request
behind a `STAFF` check-in.

**SELF never means nobody at the salon touched it.** The desk approves every
self check-in. There is no path where a customer is checked in on their own
word. SELF means "the customer asked first". The design's **"Welcome -
Self-approved"** screen describes a path that was deliberately not built:
check its words with the design team before building it.

**`byName`: best effort. The screen must work without it.**

- It is "Layla R.": the first name and the initial of the last, as the
  salon's staff records have them now.
- It is **null** for three different reasons:
  1. the desk member has no staff profile (an owner's account, say);
  2. the staff records did not answer in time (the server never waits more
     than about a second for a name, and after a failure stops asking for 30
     seconds);
  3. the profile has no first name.
- **When it is null, say "Checked in at 14:24".** Never "Checked in by"
  followed by nothing.

### 14.3 The booking read: `check_in`

`GET /booking/{id}` carries `check_in` while `SELF_CHECK_IN_V1` is on. Its
three shapes mean three different things:

| `check_in` | Means | Show |
| ---------- | ----- | ---- |
| no key at all | Self check-in is off. | Nothing from this feature. |
| `null` | On, and nobody is checked in: none yet, or the desk undid it. | No welcome. |
| `{ at, via, by_name }` | Checked in. | The welcome, as §14.2, with `by_name` for `byName`. |

- **This is the staff path's read.** The desk scanned the pass, so no
  request exists, and `GET /booking/{id}/check-in` answers
  `{ "request": null, "checkIn": null }`. Only the booking knows.
- **Never read a missing key as null, or null as "off".** No key says
  nothing about the visit.
- **The fields are §14.2's**, `by_name` for `byName`: best effort, null for
  the same three reasons, and "Checked in at 14:24" without it.
- **A `ROUTINE` session:** `GET /booking/{session_id}` carries it;
  `GET /booking/{routine_id}` does not.

### 14.4 QA checklist (the welcome)

- [ ] "I am here", the desk approves: `checkIn.via` is `SELF`, `at` is the approval time, and `byName` names the receptionist.
- [ ] While `WAITING`: `checkIn` is `null` on every poll.
- [ ] The desk approves, undoes it within five minutes, then checks them in with its own button: `request.state` is still `APPROVED`, and `checkIn.via` is `STAFF`. The app draws the desk welcome.
- [ ] Ask the server team to show a receptionist with no staff profile: `byName` is `null`, and the screen says "Checked in at 14:24".
- [ ] The desk scans the pass, with no "I am here": `GET /booking/{id}` has `check_in.via` `STAFF` and `by_name`; the check-in read has `request: null`. The app draws the desk welcome from `check_in`.
- [ ] Before anyone checks in: `GET /booking/{id}` has `check_in: null`.
- [ ] customer-api's switch off: `GET /booking/{id}` has no `check_in` key at all, and the booking screen draws as before.
- [ ] A `ROUTINE` session checked in at the desk: `GET /booking/{session_id}` has `check_in`.
