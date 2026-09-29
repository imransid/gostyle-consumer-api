Replaced by docs/ROUTINE_BOOKING_API.md, kept for history.

# Routine Booking: draft of §3 (response) to §8

Draft from the server team, 2026-09-29. Not agreed yet.

This continues `routine-booking-fe-contract.md`: same base URL, same auth (Bearer
token, the customer is taken from the token), same error envelope as
`booking-create.md` §9. It fills in what the contract leaves open (the create
response, §4 to §7) and adds §8, the routine actions that are already live.

---

## 0. Changes we ask for in §1 to §3

| Where                  | Contract says                     | Server proposes                                                                                                                                                                                              |
| ---------------------- | --------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| §2 `sessions`          | 1 to 6                            | **2 to 6.** A routine is at least two visits. `1` is refused with `invalid_session_count`. The stepper starts at 2.                                                                                          |
| §2 `stylist_id`        | `null`: the salon assigns         | The server picks **one** stylist for the whole plan: one who does all the services and is free on the most sessions. That stylist is shown on every session and becomes the routine's regular stylist.      |
| §2 `alternatives`      | same day first, then next open day | Always **the session's own stylist** (the plan keeps one stylist). Same day first, nearest time first. Then the next days (up to 7 after), the same time first. At most 2 on one day, so there is always a choice of another day. A day another session already has is never offered. |
| §2 `month`             | 31 Jan becomes 28 Feb             | Yes, and then **back to 31 Mar**, 30 Apr, 31 May: every month takes the first session's day, or the month's last day when the month is shorter. The plan never drifts to the 28th.                          |
| §2 far sessions        | nothing said                      | A session more than 90 days away is shown like the others, `available` checked against the calendar as it is today. It is booked for real when it comes within 90 days (§5, `PLANNED`).                    |
| §2 `start_time`        | ISO 8601 with offset              | Any offset is read as that instant, as for a single booking. Every time the server answers is in the salon's offset.                                                                                         |
| §3 new `start_time`    | not in the payload                | **Add it**: the same `start_time` the preview was asked with. The server works out the cadence from it, so it can tell a session on its cadence from one moved to an alternative.                           |
| §3 `sessions[]`        | the resolved times                | Each entry must be its cadence slot or a time the alternatives rule allows (same stylist, same day or up to 7 days after, not another session's day). Anything else is refused (`session_not_offered`). The server never takes a schedule its rule does not allow.                             |
| §3 payment fields      | as in the payload                 | Pay at the salon only, for now (§4).                                                                                                                                                                         |
| §1 "reused as-is"      | `GET` / `PATCH /booking/:id`, the list | They learn the routine id: §4 and §5.                                                                                                                                                                  |

### Known limits today

- **UAE salons: 08:00 to 20:00 salon time.** For now a routine can only use
  times between 08:00 and 20:00 salon time: every session must start and finish
  inside that window. A start outside it is refused with `invalid_time`.
  `nearest-available` never offers one, so this only bites a hand-made time
  (the contract's example start, 20:00, is one). The limit goes when each salon
  gets its own time zone on the server.

---

## 3. Create: `POST /booking/routine` (continued)

### Payload rules

1. **`start_time`** is the preview's own `start_time` (the first session's
   start on the cadence). Required.
2. **`sessions`** has 2 to 6 entries, `index` 0, 1, 2, ... in order. Each
   `start_time` is either that session's cadence slot or a time the
   alternatives rule allows: the same stylist, the session's own day or up
   to 7 days after it, not another session's day. It need not be one of the
   alternatives the preview listed (the list changes as the diary does), but
   it must still be free. Anything else is `422` / `session_not_offered`.
3. **`date`** is the salon's date of that `start_time` (`date_mismatch`), and
   **`end_time`** is `start_time` plus the services' duration (`invalid_window`),
   exactly as for a single booking.
4. **`stylist_id`**: send the stylist the preview showed. `null` lets the server
   pick again with the same rule; it may pick someone else if the diary changed.
5. **`services[].amount`** echoes the price the app showed. It is **not
   checked**, exactly as for a single booking: a line's price can differ by
   stylist or by session date. Only the four totals (rule 6) are checked.
6. **The money is verified, never trusted.** `amount_without_tax`,
   `tax_amount`, `discount` and `total` are for the whole plan (every session
   added up). A figure that disagrees is `422` / `amount_mismatch` with the
   server's figure in `expected`.
7. **All or nothing.** Every session within 90 days is booked in this one call.
   If one cannot be, none are, and the answer names it (`409` / `slot_taken`,
   field `sessions[i]`). Run the preview again and let the customer pick.
8. **Double submits.** Send an `Idempotency-Key` header, one value per attempt,
   as for a single booking. The same key returns the same routine. Never send a
   key with the preview.
9. **It can take a while.** Up to six visits are booked one by one. Wait up to
   60 seconds before giving up; a retry with the same key is safe.

### Response: `201 Created`

The routine, in the shape of §5, so the confirmation screen needs no second call.

```json
{
  "id": "rtn_01j9q4w",
  "booking_type": "ROUTINE",
  "salon_id": "sal_01j8xk2e9",
  "status": "ACTIVE",
  "cadence": "month",
  "date": "2026-09-20",
  "start_time": "2026-09-20T20:00:00+04:00",
  "end_time": "2026-09-20T20:45:00+04:00",
  "services": [
    { "id": "svc_fade", "name": "Signature Fade", "amount": 120 },
    { "id": "svc_beard", "name": "Beard Trim", "amount": 60 }
  ],
  "products": [],
  "stylists": [
    {
      "id": "sty_liam",
      "name": "Liam Johnson",
      "avatar_url": "https://cdn.gostyles.app/stylists/sty_liam.png"
    }
  ],
  "amount_without_tax": 360,
  "tax_amount": 18,
  "discount": 0,
  "promo_code": null,
  "total": 378,
  "advance_paid_amount": 0,
  "due_amount": 378,
  "payment_status": "PAY_AFTER_CHECK_IN",
  "payment_method": null,
  "pass_qr_code": "GS-BKG-01J9M2K-8F3A",
  "counts": { "total": 2, "done": 0, "remaining": 2, "skipped": 0, "cancelled": 0 },
  "pause": null,
  "sessions": [
    {
      "id": "ses_01j9q4x",
      "index": 0,
      "date": "2026-09-20",
      "start_time": "2026-09-20T20:00:00+04:00",
      "end_time": "2026-09-20T20:45:00+04:00",
      "state": "SCHEDULED",
      "stylist": { "id": "sty_liam", "name": "Liam Johnson" },
      "booking_id": "bkg_01j9m2k",
      "pass_qr_code": "GS-BKG-01J9M2K-8F3A",
      "total": 189,
      "locked": false,
      "can_skip": true,
      "can_reschedule": true
    },
    {
      "id": "ses_01j9q4y",
      "index": 1,
      "date": "2026-10-21",
      "start_time": "2026-10-21T20:00:00+04:00",
      "end_time": "2026-10-21T20:45:00+04:00",
      "state": "SCHEDULED",
      "stylist": { "id": "sty_liam", "name": "Liam Johnson" },
      "booking_id": "bkg_01j9m2p",
      "pass_qr_code": "GS-BKG-01J9M2P-2C7D",
      "total": 189,
      "locked": false,
      "can_skip": true,
      "can_reschedule": true
    }
  ],
  "can": {
    "skip": true,
    "reschedule": true,
    "extend": true,
    "pause": true,
    "resume": false,
    "cancel": true
  },
  "salon": { "id": "sal_01j8xk2e9", "name": "Marina Walk", "timezone": "Asia/Dubai" },
  "created_at": "2026-09-18T14:02:11+04:00"
}
```

Every session is an ordinary booking of its own (`booking_id`), confirmed at
once and paid at the salon. The routine ties them together.

---

## 4. Payment: pay at the salon, for now

For now a routine is paid at the salon, visit by visit. Nothing is taken in the
app. Paying in the app (a deposit on each visit, or the whole plan up front)
and products (on the first session only) come later, and will be added here.

### On create

| Field                 | Must be   | Else                                               |
| --------------------- | --------- | -------------------------------------------------- |
| `payment_status`      | `DRAFT`   | `422` / `invalid_payment_status`                   |
| `advance_paid_amount` | `0`       | `422` / `amount_mismatch`, `expected: 0`           |
| `due_amount`          | = `total` | `422` / `amount_mismatch`, `expected` is the total |
| `products`            | `[]`      | `422` / `products_not_supported`                   |
| `promo_code`          | `null`    | `422` / `invalid_promo`                            |
| `status`              | `BOOKED`  | `422` / `invalid_status`                           |
| `booking_type`        | `ROUTINE` | `422` / `invalid_booking_type`                     |

### What comes back

- Every session is **confirmed at once** and held outright. There is no
  payment link and nothing expires, unlike a single `DRAFT` booking.
- The routine answers `payment_status: "PAY_AFTER_CHECK_IN"`,
  `advance_paid_amount: 0` and `due_amount` equal to `total`.
- Each session's own booking reads `status: "CONFIRMED_BY_SALON"` and
  `payment_status: "PAY_AFTER_CHECK_IN"`.

### `PATCH /booking/:id`: skip the payment step

There is nothing to record. A `PATCH` on a routine id answers `409` /
`already_paid`, as it does for a single `PAY_AFTER_CHECK_IN` booking
(`booking-create.md` §11):

> This routine is paid at the salon, visit by visit. There is nothing to record here.

### At the salon

Each visit is paid at the desk on the day, like any pay-on-arrival booking. If
the salon's own rules ask for a deposit, the desk asks for it then.

---

## 5. Reading the routine

### `GET /booking/:id`

```
GET /booking/:id
```

`:id` may be a routine id or a booking id.

- **A routine id** answers the routine, exactly the object of §3.
- **A booking id** answers the booking as today (`booking-create.md` §10), plus
  two fields: `booking_type` (`SINGLE` or `ROUTINE`) and `series_id` (the
  routine it belongs to, or `null`), so a session opened from Upcoming can open
  its routine.

| Field                   | Type    | Notes                                                                                                                                         |
| ----------------------- | ------- | --------------------------------------------------------------------------------------------------------------------------------------------- |
| `id`                    | string  | The routine's id. What §6 and §8 take.                                                                                                         |
| `booking_type`          | enum    | Always `ROUTINE` here.                                                                                                                         |
| `status`                | enum    | The routine's own word: `ACTIVE`, `PAUSED`, `ENDED` (cancelled), `COMPLETED` (every session is over).                                           |
| `cadence`               | enum    | `week`, `fortnight`, `month`.                                                                                                                  |
| `date` / `start_time` / `end_time` | string | The next session still to come. When none is left, the last session.                                                               |
| `services`              | array   | `id`, `name`, `amount`: one session's price of that service, before tax.                                                                       |
| `products`              | array   | Always `[]` for now.                                                                                                                           |
| `stylists`              | array   | The regular stylist, `{ id, name, avatar_url }`, as on a single booking.                                                                       |
| `amount_without_tax` ... `total` | number | The whole plan: the sum of its visits, each exactly as that visit's own booking reads (`GET /booking/:id`), so the routine and its visits never disagree. Skipped, cancelled and missed visits add nothing. A session not booked yet counts at today's price, fully due. |
| `advance_paid_amount`   | number  | The sum of what was taken on its visits (at the desk, for now).                                                                                |
| `due_amount`            | number  | The sum of what is still due on its visits. Skipped, cancelled and missed visits add nothing.                                                  |
| `payment_status`        | enum    | `PAY_AFTER_CHECK_IN` for now (§4).                                                                                                             |
| `pass_qr_code`          | string  | The next session's pass. `null` when none is left.                                                                                              |
| `counts`                | object  | `total`, `done`, `remaining`, `skipped`, `cancelled`: "1 of 6 done, 5 remaining". `total` is `done` plus `remaining`.                          |
| `pause`                 | object  | `until`, `reason`, `note` while `PAUSED`. `null` otherwise.                                                                                    |
| `sessions`              | array   | Every session, in order.                                                                                                                       |
| `↳ id`                  | string  | The session's id. What §6 and §8 take.                                                                                                         |
| `↳ index`               | number  | From `0`.                                                                                                                                      |
| `↳ start`/`end`         | string  | ISO 8601 with the salon's offset. `date` is the salon's date.                                                                                  |
| `↳ state`               | enum    | See the table below.                                                                                                                           |
| `↳ stylist`             | object  | `{ id, name }`. A single session can have another stylist after a move (§6).                                                                   |
| `↳ booking_id`          | string  | The session's own booking. `null` while `PLANNED` or `NEEDS_ACTION`.                                                                           |
| `↳ pass_qr_code`        | string  | That booking's pass. `null` while there is no booking.                                                                                         |
| `↳ total`               | number  | This session, tax included.                                                                                                                    |
| `↳ locked`              | boolean | Inside the 24 hours before it starts: no skip, no move.                                                                                        |
| `↳ can_skip`, `can_reschedule` | boolean | What the customer may do with this session now.                                                                                         |
| `can`                   | object  | `skip`, `reschedule`, `extend`, `pause`, `resume`, `cancel`: the routine's buttons. A button is shown only when the server would accept it.   |
| `salon`                 | object  | The salon card, as on a single booking.                                                                                                        |

| `state`        | Means                                                                                               |
| -------------- | --------------------------------------------------------------------------------------------------- |
| `SCHEDULED`    | Booked, more than 24 hours away. It can still be skipped or moved.                                  |
| `CONFIRMED`    | Booked, inside the last 24 hours. Only the normal cancel applies.                                   |
| `PLANNED`      | More than 90 days away. Booked automatically when it comes within 90 days. A session more than 90 days away keeps the routine's stylist. |
| `NEEDS_ACTION` | Its time was gone when it came within 90 days. The customer picks a new time with §6.               |
| `CHECKED_IN`   | The visit is happening.                                                                             |
| `COMPLETED`    | The visit happened.                                                                                 |
| `MISSED`       | A no-show. Two in a row pause the routine.                                                          |
| `SKIPPED`      | Skipped by the customer (§8).                                                                       |
| `CANCELLED`    | Cancelled any other way (by the salon, for example).                                                |

Rules:

1. **Only the customer who made it.** Anyone else gets `404` / `not_found`, not
   `403`.
2. **States follow the clock.** `SCHEDULED` becomes `CONFIRMED` 24 hours before
   the session, with nothing changed on the server. Read, do not cache.
3. **Nothing is moved without the customer.** A session that cannot be booked
   when its day comes near shows `NEEDS_ACTION`; it is never given another
   time or stylist silently.

### The Recurring tab: `GET /bookings?filter=recurring`

One row per routine: the same routine object as `GET /booking/:id` answers
(every field above, every session included), except `salon`, which is the short
card every tab's rows carry: `id`, `name`, `logo_url`, `city`. The same page
envelope, paging and `counts` as the other tabs. Order: `ACTIVE` and `PAUSED`
first, the soonest next session first; then `ENDED` and `COMPLETED`, the newest
first.

Each booked session also shows in **Upcoming** and **Archive** as its own
booking, with `booking_type: "ROUTINE"` and `series_id`. A routine that could
not be booked in full leaves nothing behind in any tab.

---

## 6. Move one session: `PATCH /booking/:id/sessions/:session_id`

`:id` is the routine id, `:session_id` a `sessions[].id` from §5. Moves that one
session. The routine keeps its cadence, and no other session changes.

### Request

```json
{
  "start_time": "2026-10-22T19:00:00+04:00",
  "stylist_id": null,
  "dry_run": false
}
```

| Field        | Type    | Required | Notes                                                                                           |
| ------------ | ------- | -------- | ----------------------------------------------------------------------------------------------- |
| `start_time` | string  | yes      | ISO 8601 with offset. The new start, for example from `booking-nearest-available.md`.           |
| `stylist_id` | string  | no       | `null` or omitted keeps the session's stylist. Another id moves this session to that stylist.   |
| `dry_run`    | boolean | no       | `true` checks everything and moves nothing. Default `false`.                                    |

### Response: `200 OK`

The whole routine (§5), after the move. With `dry_run: true`, the routine as it
is now: a `200` means the move is allowed and the time is free this moment.

Rules:

1. **Only a session still to come, and not inside its last 24 hours.**
   `session_locked` inside the lock, `session_not_changeable` for one done,
   skipped or cancelled.
2. **The new start is at least 24 hours ahead and within 90 days**
   (`reschedule_out_of_range`), inside the salon's hours on that day and the
   services' notice (`salon_closed`, `outside_hours`, `too_soon`).
3. **One session per day.** A day another session of the routine already has is
   `session_day_taken`.
4. **The same booking moves.** It keeps its id, code and pass. The new time is
   held the moment it is checked; if it is gone, `409` / `slot_taken`.
5. **A session with no booking yet** (`PLANNED` or `NEEDS_ACTION`) is booked at
   the new time.
6. **Only an `ACTIVE` routine** (`routine_not_active`). A paused routine is
   changed with resume (§8).
7. **Double submits.** Send an `Idempotency-Key` with the real move, never with
   `dry_run`.

---

## 7. Error codes

Same envelope as `booking-create.md` §9. A `422` has `code: "validation_error"`
and the field errors in `errors`; a `409` or `404` has the error's own `code` at
the top.

```json
{
  "detail": "Please correct the highlighted fields.",
  "code": "validation_error",
  "errors": [
    {
      "field": "sessions[1]",
      "code": "session_not_offered",
      "message": "Session 2 is neither on its cadence nor at a time the alternatives rule allows (same stylist, same day or up to 7 days after, not another session's day)."
    }
  ]
}
```

```json
{
  "detail": "Session 2 on 2026-10-21 was taken while booking. Nothing was booked.",
  "code": "slot_taken",
  "errors": [
    {
      "field": "sessions[1]",
      "code": "slot_taken",
      "message": "Session 2 on 2026-10-21 was taken while booking. Nothing was booked."
    }
  ]
}
```

### Preview `reason` (§2)

A session with `available: false` carries one of these. When more than one is
true, the first in this table wins.

| `reason`              | Means                                                                             |
| --------------------- | --------------------------------------------------------------------------------- |
| `salon_closed`        | The salon is closed that day. The alternatives are on the next open days.         |
| `outside_hours`       | The visit would not start and finish within the salon's hours that day.           |
| `too_soon`            | The time has passed, or is inside the notice the services ask for.                |
| `stylist_unavailable` | The stylist is off, or already busy, at that time.                                |

### Every code

| Code                     | Status | When                                                                                          | §          |
| ------------------------ | ------ | --------------------------------------------------------------------------------------------- | ---------- |
| `no_services`            | 422    | `service_ids` / `services` is empty.                                                           | 2, 3       |
| `unknown_service`        | 422    | A service is not sold at this salon.                                                           | 2, 3       |
| `foreign_id`             | 422    | The stylist does not work at this salon.                                                       | 2, 3, 6    |
| `stylist_mismatch`       | 422    | The stylist cannot do all of the services.                                                     | 2, 3, 6    |
| `no_stylist_available`   | 422    | Any Available Expert, but nobody at this salon does all of these services.                      | 2, 3       |
| `invalid_cadence`        | 422    | `cadence` is not `week`, `fortnight` or `month`.                                                | 2, 3       |
| `invalid_session_count`  | 422    | Fewer than 2 or more than 6 sessions.                                                           | 2, 3       |
| `invalid_time`           | 422    | `start_time` is not ISO 8601 with an offset, or not on a 5 minute step, or outside the hours the salon can be booked. | 2, 3, 6 |
| `date_out_of_range`      | 422    | The first session is not between today and 90 days ahead.                                       | 2, 3       |
| `invalid_sessions`       | 422    | `sessions[].index` is not 0, 1, 2, ... in order.                                                | 3          |
| `session_not_offered`    | 422    | A session is neither on its cadence nor at a time the alternatives rule allows (same stylist, same day or up to 7 days after, not another session's day). | 2, 3       |
| `session_day_taken`      | 422    | Two sessions on the same day.                                                                   | 3, 6       |
| `date_mismatch`          | 422    | `date` is not the salon's date of `start_time`.                                                 | 3          |
| `invalid_window`         | 422    | `end_time` disagrees with the services' duration.                                               | 3          |
| `amount_mismatch`        | 422    | A money figure disagrees with the server's. `expected` has the right one.                       | 3, 4       |
| `invalid_payment_status` | 422    | `payment_status` is not `DRAFT`.                                                                | 4          |
| `products_not_supported` | 422    | `products` is not empty.                                                                        | 4          |
| `invalid_promo`          | 422    | `promo_code` is not `null`.                                                                     | 4          |
| `invalid_status`         | 422    | `status` is not `BOOKED`.                                                                       | 4          |
| `invalid_booking_type`   | 422    | `booking_type` is not `ROUTINE`.                                                                | 4          |
| `salon_closed`           | 422    | A session (or the new time) is on a day the salon is closed.                                    | 3, 6       |
| `outside_hours`          | 422    | A session would not start and finish within the salon's hours.                                  | 3, 6       |
| `too_soon`               | 422    | A session has passed, or is inside the services' notice.                                        | 3, 6       |
| `session_locked`         | 422    | The session starts in less than 24 hours.                                                       | 6          |
| `session_not_changeable` | 422    | The session is done, skipped or cancelled.                                                      | 6          |
| `reschedule_out_of_range`| 422    | The new start is less than 24 hours ahead or more than 90 days away.                            | 6          |
| `routine_not_active`     | 422    | The routine is paused, ended or completed.                                                      | 6          |
| `slot_taken`             | 409    | A time went between the preview and the create, or the new time of a move is not free.         | 3, 6       |
| `already_paid`           | 409    | `PATCH /booking/:id` on a routine: it is paid at the salon.                                     | 4          |
| `idempotency_key_reused` | 409    | The same `Idempotency-Key` was used for a different request.                                    | 3, 6       |
| `not_found`              | 404    | No such routine, booking or session, or it is not yours.                                        | all        |
| `unsupported_media_type` | 415    | The body is not `application/json`.                                                             | all        |
| `service_unavailable`    | 503    | Booking is down for a moment. Safe to retry with the same `Idempotency-Key`.                    | all        |

---

## 8. Already live, not in this contract

These work today on the routine id and session ids of §5. They still speak the
first routine format: a day as `YYYY-MM-DD` and a time as `HH:MM`, both on the
salon's clock. Each answers the routine hub (the fields of §5 without the
booking-shaped ones). A later version can bring them into this contract's shape.

| Action                  | Route                                     | Body                                                                                     |
| ----------------------- | ----------------------------------------- | ---------------------------------------------------------------------------------------- |
| Read the hub            | `GET /booking/series/:id`                 | none                                                                                     |
| Skip sessions           | `PATCH /booking/series/:id`               | `{ "action": "SKIP", "session_ids": ["ses_01j9q4y"] }`                                   |
| Add visits              | `PATCH /booking/series/:id`               | `{ "action": "EXTEND", "sessions": 2 }`                                                  |
| Pause                   | `PATCH /booking/series/:id`               | `{ "action": "PAUSE", "until": "2026-11-15", "reason": "TRAVEL", "note": "Away" }`        |
| Resume                  | `PATCH /booking/series/:id`               | `{ "action": "RESUME" }`, optional `frequency`, `time`, `stylist_id` ("Customize first") |
| Cancel the routine      | `POST /booking/series/:id/cancel`         | `{ "reason": "TOO_EXPENSIVE" }`, optional                                                 |

Rules they keep:

1. **`dry_run: true`** on every one of them checks and changes nothing; its
   answer shows what would happen (the new sessions for add, pause and resume,
   the refund summary for cancel).
2. **The 24 hour lock.** Skip and move never touch a session in its last 24
   hours. Pause leaves such a session where it is.
3. **Skip** loses the session (the routine gets shorter). **Add visits** adds
   1 to 6, never more than 6 still to come. **Pause** lasts at most 60 days
   (`TRAVEL`, `HEALTH`, `BUSY`, `BUDGET`, `OTHER`, note up to 200 characters);
   the remaining sessions move to after the resume date and the count is kept.
   **Resume** brings them back from the next bookable day.
4. **Cancel** cancels every session still to come under the single booking's
   refund rules (a session less than 24 hours away is a late cancel), and ends
   the routine. Reasons: `NOT_SATISFIED`, `TOO_EXPENSIVE`, `MOVING`, `OTHER`.
5. **A busy new date is never changed silently.** Add, pause and resume answer
   up to 3 alternatives per busy session; the customer picks, and the action is
   sent again with `picks: [{ "index", "date", "time", "stylist_id" }]`.
6. **`Idempotency-Key`** with a real change only.
