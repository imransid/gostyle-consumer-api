# Routine Booking

Base URL: `https://api.gostyle.uk/api/v1`
Auth: Bearer token on every request. The customer is taken from the token, never
from the payload.

A routine booking is one recurring plan at one salon: the same services, with
the same stylist, at the same time of day, repeated on a fixed cadence for a
fixed number of sessions. It is created in a single call so the whole run is
held at once: the customer never books six visits by hand.

Only **two new endpoints** are needed, plus one for moving a single session.
Everything else the flow uses already exists (see §1).

---

## 1. Endpoints

| Method  | Path                                | Purpose                            |
| ------- | ----------------------------------- | ---------------------------------- |
| `POST`  | `/booking/routine-preview`          | The dates the plan would take (§2) |
| `POST`  | `/booking/routine`                  | Create the whole plan (§3)         |
| `PATCH` | `/booking/:id/sessions/:session_id` | Move one session (§6)              |

Already built, reused as-is:

| Method  | Path                                   | Used by                                      |
| ------- | -------------------------------------- | -------------------------------------------- |
| `GET`   | `/salon/:id/services`                  | Step 3, picking the services                 |
| `GET`   | `/salon/:id/stylists?service_ids=`     | Step 4, stylists who can do those services   |
| `GET`   | `/booking/nearest-available/:salon_id` | Step 5, the **first** session's start        |
| `GET`   | `/booking/:id`                         | The pass, and the plan after booking         |
| `PATCH` | `/booking/:id`                         | Record the payment (`booking-create.md` §11) |
| `GET`   | `/bookings?filter=recurring`           | My Bookings, Recurring tab                   |

A routine booking is an ordinary booking with `booking_type: "ROUTINE"` and a
`sessions` array, so the read, list and payment endpoints need no new shapes:
only the extra array in their response (§5).

---

## 2. Preview the plan: `POST /booking/routine-preview`

The customer picks one start (step 5, from `booking-nearest-available.md`); the
server works out the other dates from the cadence and says which ones actually
fit. Nothing is held.

### Request

```json
{
  "salon_id": "sal_01j8xk2e9",
  "service_ids": ["svc_fade", "svc_beard"],
  "stylist_id": "sty_liam",
  "start_time": "2026-09-20T20:00:00+04:00",
  "cadence": "month",
  "sessions": 5
}
```

| Field         | Type     | Required | Notes                                                            |
| ------------- | -------- | -------- | ---------------------------------------------------------------- |
| `salon_id`    | string   | yes      | Salon the plan runs at.                                          |
| `service_ids` | string[] | yes      | At least one. The same services repeat every session.            |
| `stylist_id`  | string   | no       | `null` or omitted is "Any Available Expert": the salon assigns.  |
| `start_time`  | string   | yes      | ISO 8601 with offset. The **first** session, an offered start.   |
| `cadence`     | enum     | yes      | `week`, `fortnight`, `month` (see the table below).              |
| `sessions`    | number   | yes      | 1 to 6. The app's stepper enforces the same range.               |

| `cadence`   | Step              | The app's label |
| ----------- | ----------------- | --------------- |
| `week`      | +7 days           | Every week      |
| `fortnight` | +14 days          | Every 2 weeks   |
| `month`     | +1 calendar month | Monthly         |

`month` steps the calendar month, not 30 days, and clamps a date that does not
exist in the next month: a 31 Jan start becomes 28 Feb (29 in a leap year).

### Response: `200 OK`

```json
{
  "cadence": "month",
  "sessions": [
    {
      "index": 0,
      "date": "2026-09-20",
      "start_time": "2026-09-20T20:00:00+04:00",
      "end_time": "2026-09-20T20:45:00+04:00",
      "available": true,
      "stylist": { "id": "sty_liam", "name": "Liam Johnson" },
      "alternatives": []
    },
    {
      "index": 1,
      "date": "2026-10-20",
      "start_time": "2026-10-20T20:00:00+04:00",
      "end_time": "2026-10-20T20:45:00+04:00",
      "available": false,
      "reason": "stylist_unavailable",
      "stylist": null,
      "alternatives": ["2026-10-20T19:00:00+04:00", "2026-10-21T20:00:00+04:00"]
    }
  ],
  "per_session_total": 236.25,
  "plan_total": 1181.25
}
```

| Field               | Type    | Notes                                                                       |
| ------------------- | ------- | --------------------------------------------------------------------------- |
| `sessions`          | array   | One entry per session, in order, `index` starting at `0`.                   |
| `↳ start`/`end`     | string  | ISO 8601 with the salon's offset, from the services' duration.              |
| `↳ available`       | boolean | `false` is drawn struck through with its `alternatives` offered underneath. |
| `↳ reason`          | enum    | Only when `available` is `false` (see §7's codes).                          |
| `↳ stylist`         | object  | Who would take it, even when the request sent `null`. `null` when unfit.    |
| `↳ alternatives`    | array   | Up to 3 nearby starts, same day first, then the next open day.              |
| `per_session_total` | number  | One session, tax included.                                                  |
| `plan_total`        | number  | Every session added up, tax included.                                       |

Rules:

1. **A busy session does not block the plan.** The app lets the customer book
   anyway, and the session is created on its alternative (§3 sends the resolved
   times, so the server never guesses).
2. **Nothing is held.** A preview is a look; a start that goes in between fails
   at create with `slot_taken` (§7).
3. **Closed days move, they do not vanish.** A session landing on a salon
   holiday comes back `available: false` with `reason: "salon_closed"` and the
   nearest open day in `alternatives`.

---

## 3. Create: `POST /booking/routine`

One call books every session, or none.

### Payload

```json
{
  "salon_id": "sal_01j8xk2e9",
  "cadence": "month",
  "services": [
    { "id": "svc_fade", "amount": 120 },
    { "id": "svc_beard", "amount": 60 }
  ],
  "products": [],
  "stylist_id": "sty_liam",
  "sessions": [
    {
      "index": 0,
      "date": "2026-09-20",
      "start_time": "2026-09-20T20:00:00+04:00",
      "end_time": "2026-09-20T20:45:00+04:00"
    },
    {
      "index": 1,
      "date": "2026-10-21",
      "start_time": "2026-10-21T20:00:00+04:00",
      "end_time": "2026-10-21T20:45:00+04:00"
    }
  ],
  "amount_without_tax": 360,
  "tax_amount": 18,
  "discount": 0,
  "promo_code": null,
  "total": 378,
  "advance_paid_amount": 0,
  "due_amount": 378,
  "payment_status": "DRAFT",
  "status": "BOOKED",
  "booking_type": "ROUTINE"
}
```
