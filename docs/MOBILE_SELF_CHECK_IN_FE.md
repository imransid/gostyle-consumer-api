# Mobile Self Check-in: FE Guide

Date: 2026-10-08. For: the mobile app team.

The customer arrives at the salon, opens their booking and taps **"I am
here"**. The salon's desk sees them on its reception list and approves or
rejects. Approving checks the booking in, exactly as if the desk had pressed
its own check-in button.

**Everything here is behind the server switch `SELF_CHECK_IN_V1`, and it is
OFF today.** Until it is on, both routes answer `404`. See §10.

---

## 0. Read this first

1. **Base URL:** `https://api.gostyle.uk/api/v1`. Every path below is relative to it.
2. **Auth:** `Authorization: Bearer <app login token>` on every call. The customer is always the signed-in user.
3. **No body.** "I am here" sends nothing but the token.
4. **Keys are `camelCase` here**, not `snake_case`. These answers are booking-api's own, passed through as they came, like the single cancel and reschedule answers.
5. **The server decides.** The app shows the button by the rule in §3, but the server is the judge. Every refusal has a `code` to switch on (§8). Never show a refusal as a crash.
6. **The pass QR is unchanged** (§9).

---

## 1. Endpoints at a glance

| Method | Path | What it does |
| ------ | ---- | ------------ |
| `POST` | `/booking/{id}/check-in` | "I am here": ask the desk to check me in |
| `GET`  | `/booking/{id}/check-in` | Has the desk answered? |

`{id}` is the id on the My Bookings row: a `SINGLE` row's id, or a `ROUTINE`
session row's id. **Not a `GROUP` row** (§9).

---

## 2. The flow

1. The customer opens an upcoming booking, from 30 minutes before its start.
2. They tap **"I am here"**: `POST /booking/{id}/check-in`. Answer: `WAITING`.
3. The app shows "The salon knows you are here", and reads the answer every 10 seconds while the screen is open (§7).
4. The desk answers.
   - **`APPROVED`:** the customer is checked in. Refresh the booking; its `status` is now `CHECKED_IN`.
   - **`REJECTED`:** "Please speak to the desk." There is no second try for this booking.
5. If nobody answers before the booking's end time, the request becomes `EXPIRED`. The booking is **not** marked a no-show; the desk sorts it out by hand.

---

## 3. When to show "I am here"

Show it on a My Bookings row (or the booking screen) when **all** of these are true:

- `status` is `CONFIRMED_BY_SALON`;
- `booking_type` is `SINGLE` or `ROUTINE`;
- now is between **30 minutes before `start_time`** and the booking's **end time**;
- `GET /booking/{id}/check-in` does not already say `REJECTED`.

The server checks every one of these again. If the app's clock or the row is
out of date, the answer is a `409` with a `code` (§8), and the app shows that
message instead.

Hide it once the request is `APPROVED`, or the booking is `CHECKED_IN`.

---

## 4. TypeScript types (copy these)

```ts
export type CheckInState =
  | 'WAITING'   // raised, the desk has not answered yet
  | 'APPROVED'  // the desk said yes: the booking is CHECKED_IN
  | 'REJECTED'  // the desk said no: speak to the desk, no second try
  | 'EXPIRED'   // nobody answered before the booking's end time
  | 'CLOSED';   // the booking moved on another way (checked in at the desk, cancelled, moved)

export interface CheckInRequest {
  requestId: string;
  bookingId: string;
  state: CheckInState;
  raisedAt: string;          // ISO 8601, UTC
  decidedAt: string | null;  // null while WAITING
}

export interface CheckInAnswer {
  request: CheckInRequest | null;  // null: never raised
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
  };
  error: string;
}
```

---

## 5. "I am here": `POST /booking/{id}/check-in`

No body. Send the token only.

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

**`200 OK`**: `{ "request": CheckInRequest }`, or `{ "request": null }` if the
customer never tapped "I am here" for this booking.

| `state` | Show | Button |
| ------- | ---- | ------ |
| `WAITING` | "The salon knows you are here. Please take a seat." | hidden |
| `APPROVED` | "You are checked in." Refresh the booking. | hidden |
| `REJECTED` | "The salon could not confirm your arrival. Please speak to the desk." | hidden for good |
| `EXPIRED` | "The salon did not answer in time. Please speak to the desk." | hidden |
| `CLOSED` | Nothing special: show the booking as it now is. | as §3 |

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

- **Shape A**, this service's envelope `{detail, code, errors[]}`: `401`, `503` (booking-api unreachable), and `404` while this service's switch is off.
- **Shape B**, booking-api's `{statusCode, code, message, details?}`: every refusal below.

| Status | `code` | When | What the app does |
| ------ | ------ | ---- | ----------------- |
| `409` | `BOOKING_CHECKIN_WINDOW` with `details.windowOpensAt` | Too early | "Check-in opens at {time}." Show the time in the salon's zone. |
| `409` | `BOOKING_CHECKIN_WINDOW` with `details.windowClosed` | After the end time | "Check-in for this booking has closed." |
| `409` | `BOOKING_STATE_INVALID` with `details.status` | Not confirmed (already checked in, cancelled, unpaid, ...) | Refresh the booking. If `CHECKED_IN`, show "You are checked in." |
| `409` | `BOOKING_CHECKIN_REJECTED` | The desk said no before | "Please speak to the desk." No retry. |
| `404` | `BOOKING_NOT_FOUND` | Not the customer's booking, a party's id, or booking-api's switch is off | Hide the button. |
| `404` | `not_found` (shape A) | This service's switch is off | Hide the button. |
| `401` | `not_authenticated` (shape A) | No token, or it expired | Refresh the token, as everywhere. |
| `503` | `booking_api_unavailable` (shape A) | Booking is down | "Please try again." The desk can still check you in by hand. |

---

## 9. Pass QR, parties, and what is not here yet

- **Pass QR.** The pass keeps showing `pass_qr_code` from `GET /booking/{id}`, the booking's own code (for example `GS-1403`). The desk may scan it. Nothing new to scan in the app.
- **Parties (`GROUP` rows).** No "I am here" in v1. A party's id is `404` here; the party checks in at the desk.
- **Not here yet:**
  - a push when the desk answers (read it, §7);
  - a chair QR (later).

---

## 10. Rollout: `SELF_CHECK_IN_V1`

The switch exists twice, on customer-api and on booking-api, and both must be
on. Until then both routes answer `404`. The app can ship the button early, as
long as it hides the button on a `404` (§8).

---

## 11. QA checklist

- [ ] Tap 31 minutes before the start: `409 BOOKING_CHECKIN_WINDOW` with `windowOpensAt`; the message shows the salon's time.
- [ ] Tap 10 minutes before: `201 WAITING`. Tap again: `200`, the same `requestId`.
- [ ] The desk approves: within 10 seconds the screen says checked in, and the booking is `CHECKED_IN`.
- [ ] The desk rejects: the screen says speak to the desk; tapping again gives `409 BOOKING_CHECKIN_REJECTED`.
- [ ] Nobody answers until the end time: `EXPIRED`; the booking is not shown as a no-show.
- [ ] A `GROUP` row shows no button.
- [ ] Airplane mode on tap: an error message, no crash; tapping again later works (`200` if it went through).
- [ ] Switch off: `404`, no button.
