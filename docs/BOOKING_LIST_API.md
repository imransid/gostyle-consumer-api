# Bookings — List, Read, Update

```
GET   /api/v1/bookings        one shelf of the caller's own bookings   §1
GET   /api/v1/booking/<id>    the whole booking                        §6
PATCH /api/v1/booking/<id>    record the payment after the gateway     §7
```

Bearer token on every request. `<id>` is always the **booking** id, never a
salon id. Creation is `POST /api/v1/booking`, in `BOOKING_CREATE_API.md`.

**Whose bookings is never a parameter.** The customer comes from the token.
An endpoint that took a customer id would be an enumeration of every booking
in the system behind one valid login, and no amount of checking afterwards
makes that a safe shape to expose.

---

## 1. Where the answer comes from

```
app ──GET /api/v1/bookings──▶ customer-api ──GET /v1/mobile-booking──▶ booking-api
                                    │
                                    └─ salon, can_cancel, can_reschedule
                                       ← platform tables, read directly
```

Bookings live in gostyle-booking-api and in ITS database. This service has no
connection to it and no business writing there, so the page — which bookings,
which shelf, what order, the counts and the money — is entirely booking-api's
answer and is not re-decided here.

Three fields on each row are the exception, and they are the reason this is
not a bare proxy:

| Field                           | Why it is filled in here                                                                                                                                            |
| ------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `salon`                         | A booking stores `branch_id` and nothing else. No proto exposes a branch's name, logo or city to booking-api — but this service reads the platform tables directly.  |
| `can_cancel` / `can_reschedule` | Both answer the salon's cancellation policy, which the salon publishes into `storefront_policy` here. booking-api cannot see it, and a hardcoded `true` would be a promise nobody can keep. |

See booking-api's own `docs/booking-list.md` §9, which records these three as
the parts it cannot answer.

---

## 2. Request

```
GET /api/v1/bookings?filter=upcoming&page=1&pageSize=20
```

| Param      | Type | Required | Default    | Notes                                                              |
| ---------- | ---- | -------- | ---------- | ------------------------------------------------------------------ |
| `filter`   | enum | no       | `upcoming` | `upcoming`, `recurring` or `archive`. See §3.                      |
| `page`     | int  | no       | 1          | 1-based.                                                           |
| `pageSize` | int  | no       | 20         | Capped at 50. `page_size` is accepted as an alias.                 |

**`filter` is the one thing that is refused** (422 `invalid_filter`). It
changes WHICH bookings come back, and a typo quietly answered with `upcoming`
is a tab that has never once shown what its label claims. `page` and
`pageSize` only change how many, so a nonsense value falls back to the default
rather than costing the customer their booking history over a typo.

**Both services cap `pageSize`.** This one trims before the round trip;
booking-api clamps again on arrival. Neither trusts the other to have done it,
and the cap is not decoration — each row upstream costs a price quote.

---

## 3. The three shelves

| `filter`    | Contains                                                                                           | Order                  |
| ----------- | -------------------------------------------------------------------------------------------------- | ---------------------- |
| `upcoming`  | Still ahead of the customer and still live.                                                        | `start_time` ascending |
| `recurring` | Routines with a session still to come. **Always empty today** — series are not wired yet.          | Next session first     |
| `archive`   | Finished or dead: past, `COMPLETED`, `CANCELLED`, `NO_SHOW`.                                       | `start_time` descending |

Decided in booking-api by `domain/booking/booking-shelf.ts`, which is worth
reading — the rules that matter are there and are tested there:

1. **Every booking is on exactly one shelf**, so the three never double-count.
2. **A cancelled booking is archive immediately**, whatever its `start_time`.
   Time alone would show it under a heading meaning "what is coming", and
   someone turns up for an appointment that is not there.
3. **A visit in progress stays on `upcoming` until it ends**, not until it
   starts — measuring from the start moves it to history mid-haircut.
4. **A live `DRAFT` checkout IS listed**, so an interrupted payment can be
   found and resumed. An abandoned one — window run out — stays hidden: that
   is litter, not history.
5. **`recurring` answers an empty page, not a 422.** "You have no routines"
   and "there is no such tab" are different sentences, and only the first one
   is true.

---

## 4. Response — `200 OK`

```json
{
  "count": 14,
  "next": "https://api.gostyle.uk/api/v1/bookings?filter=upcoming&page=2",
  "previous": null,
  "counts": { "upcoming": 3, "recurring": 0, "archive": 10 },
  "results": [
    {
      "id": "9f1c0f4e-3a2b-4d55-9a71-2c8e5b0d7a11",
      "salon_id": "marina-walk",
      "status": "CONFIRMED_BY_SALON",
      "payment_status": "FULLY_PAID",
      "booking_type": "SINGLE",
      "date": "2026-09-20",
      "start_time": "2026-09-20T20:00:00+04:00",
      "end_time": "2026-09-20T20:45:00+04:00",
      "salon": {
        "id": "3f6a1d2c-88b4-4f0e-9a3d-51c7e2b40f91",
        "name": "The Iron Razor Barbershop",
        "logo_url": "https://cdn.gostyles.app/logo.png",
        "city": "Dubai"
      },
      "services": [{ "id": "svc_fade", "name": "Signature Fade" }],
      "stylists": [
        { "id": "maya", "name": "Maya", "avatar_url": null }
      ],
      "total": 216.25,
      "due_amount": 0,
      "can_cancel": true,
      "can_reschedule": true,
      "created_at": "2026-09-18T14:02:11+04:00"
    }
  ]
}
```

| Field               | Type           | Notes                                                                             |
| ------------------- | -------------- | --------------------------------------------------------------------------------- |
| `count`             | number         | Rows on this shelf, not the page size.                                            |
| `next` / `previous` | string \| null | Absolute, as in `/discover`, and built from the caller's own url so `filter` and `pageSize` survive into the link. |
| `counts`            | object         | All three tab badges, so the app does not make three requests for numbers it draws at once. |
| `↳ id`              | string         | uuid. What `GET /booking/<id>` takes.                                             |
| `↳ salon_id`        | string         | The salon as booking-api spells it. Kept so a row can be resolved even when `salon` is null. |
| `↳ status`          | enum           | `BOOKED`, `CONFIRMED_BY_SALON`, `CHECKED_IN`, `COMPLETED`, `CANCELLED`.           |
| `↳ payment_status`  | enum           | `DRAFT`, `PARTIALLY`, `FULLY_PAID`, `PAY_AFTER_CHECK_IN`.                         |
| `↳ start_time`      | string         | ISO 8601 with the **salon's** offset, not the caller's.                           |
| `↳ salon`           | object \| null | `{ id, name, logo_url, city }`. See §5.                                           |
| `↳ services`        | array          | `{ id, name }` in booked order. Amounts are the drawer's job.                     |
| `↳ stylists`        | array          | `{ id, name, avatar_url }`.                                                       |
| `↳ total`           | number         | Through the SAME quote `GET /booking/<id>` runs, so the row and the drawer cannot show two prices for one haircut. |
| `↳ due_amount`      | number         | Still to pay. `0` when settled.                                                   |
| `↳ can_cancel`      | boolean        | See §5.                                                                           |
| `↳ can_reschedule`  | boolean        | See §5.                                                                           |

Rows are summaries. Products, the tax breakdown, the promo code and the QR
pass come from `GET /booking/<id>`.

---

## 5. The three fields this service fills in

### `salon`

Resolved in one query for the whole page (`selectors.salon_cards_for_refs`),
not one per row — a list is ten to fifty rows and usually two or three
distinct salons.

**A salon that cannot be resolved is `null`, not an object with holes in it.**
`null` says "we could not find it"; a card with a name-shaped hole in it says
the salon has no name. The booking is still listed either way — a salon
lookup that fails must not empty a customer's booking history.

A reference that lands on two storefronts is also dropped, with a warning.
`slug` is unique per tenant and not globally, and picking the first row would
put another salon's name on a real booking.

### `can_cancel` and `can_reschedule`

Both read `storefront_policy.cancel_window_hours`, and both require two
answers:

* the booking is still live — `BOOKED`, `CONFIRMED_BY_SALON` or `CHECKED_IN`.
  A `COMPLETED` or `CANCELLED` booking cannot be cancelled again, whatever the
  window says, and offering the button is a request the salon will refuse;
* the window is still open — `now < start_time - cancel_window_hours`, compared
  against the salon's clock, which is the offset carried in `start_time`. A
  customer abroad sees the same answer as one standing outside.

**A salon that publishes no window is treated as zero hours** — cancellable
until the appointment starts. `false` is the cautious-*looking* default and is
the wrong one: it would tell customers of every salon that has not filled the
field in that they may never cancel, which is a refusal the salon never made.

They are two fields rather than one because they are two questions. Today one
window answers both; the day a salon publishes a reschedule rule of its own,
only one of them changes.

---

## 6. Errors

| Case                             | Status | `code`                     | Envelope     |
| -------------------------------- | ------ | -------------------------- | ------------ |
| No bearer token                  | 401    | `not_authenticated`        | ours         |
| `filter` is not one of the three | 422    | `invalid_filter`           | booking-api's |
| booking-api unreachable          | 503    | `booking_api_unavailable`  | ours         |
| `page` beyond the last page      | 200    | —                          | empty array  |
| No bookings at all               | 200    | —                          | empty array  |

**An empty list is a `200`, never a `404`.** A customer with no bookings is an
ordinary state, not a missing resource.

A refusal from booking-api reaches the app in **booking-api's** shape, not
this project's envelope, and is not enriched — there are no bookings on it to
enrich. That is the same arrangement `POST /booking` uses, and for the same
reason: the app branches on that service's codes, and a translation layer here
would be one more thing to keep in step.

`503 booking_api_unavailable` is the one to branch on. It means the request
never arrived, so nothing changed and a retry is safe. A 5xx that came FROM
booking-api is forwarded as itself and carries no such promise.

---

## 7. Read one booking — `GET /api/v1/booking/<id>`

**The booking id and the bearer token are the whole request** — no
`X-Tenant-Id`, no salon id. A booking already records which salon and tenant
it belongs to.

Forwarded to booking-api and returned unchanged. The whole booking, whatever
state it is in, so the confirmation screen, the pass and the booking history
all read one shape. The shape is `BOOKING_CREATE_API.md` §8.

1. **Only the customer who owns it**, or staff of the salon it belongs to.
   Anyone else gets `404`, never `403` — a 403 confirms that a booking id
   exists, which is exactly what an enumerator is trying to learn. "No such
   booking" and "not yours" have to be indistinguishable from outside.
2. **No query parameters.** Services, products and stylists always come
   expanded, never as bare ids.
3. **`DRAFT` bookings are readable**, so an interrupted checkout can be
   resumed. `expires_at` is present while the hold window is running and gone
   once the booking is paid.
4. **Money is echoed, never recomputed.** This reports what was agreed at
   creation.

| Case                                 | Status | `code`      |
| ------------------------------------ | ------ | ----------- |
| No such booking, or not the caller's | 404    | `not_found` |

---

## 8. Record the payment — `PATCH /api/v1/booking/<id>`

Called once the payment gateway answers. **Only the booking id and the
bearer token are needed**; `Idempotency-Key` is derived from the body when
not sent, so a callback delivered twice records one payment.

The body is forwarded as raw bytes —
never parsed and re-serialised, because re-encoding rounds every figure
through a Python float. Responds `200` with the full booking, same shape as
§7.

```json
{
  "payment_status": "FULLY_PAID",
  "payment_method": "CARD",
  "advance_paid_amount": 216.25,
  "due_amount": 0,
  "payment_reference": "pi_3Qk2xLJ8n"
}
```

| Field                 | Required | Notes                                                                    |
| --------------------- | -------- | ------------------------------------------------------------------------ |
| `payment_status`      | yes      | `PARTIALLY`, `FULLY_PAID` or `PAY_AFTER_CHECK_IN`. Never back to `DRAFT`. |
| `payment_method`      | cond.    | `WALLET`, `CARD`, `GOOGLE`, `APPLE`, `OTHERS`. Required unless `PAY_AFTER_CHECK_IN`. |
| `advance_paid_amount` | yes      | What the gateway actually took. `0` for `PAY_AFTER_CHECK_IN`.            |
| `due_amount`          | no       | Derived as `total - advance_paid_amount`; verified when sent.            |
| `payment_reference`   | cond.    | The gateway's own id. Required whenever money moved.                     |

1. **Only from `DRAFT`.** Anything else is `409` `already_paid`; refunds and
   top-ups are their own endpoints.
2. **The amount is checked against the booking**, never accepted on trust.
3. **`payment_reference` is unique.** The same one twice returns the same
   booking rather than recording a second payment.
4. **A successful patch clears the hold** — the slot is firmly booked and
   `expires_at` disappears.
5. **A failed payment is not a patch.** Leave the booking in `DRAFT` and let
   the hold expire. Do not invent a `FAILED` status the rest of the app then
   has to handle.
6. **Totals and `status` are immutable here.** A different price is a new
   booking; the salon moves `status` through its own endpoints.

| Case                                         | Status | `code`                      |
| -------------------------------------------- | ------ | --------------------------- |
| Not `Content-Type: application/json`         | 415    | `unsupported_media_type`    |
| Booking is not in `DRAFT`                    | 409    | `already_paid`              |
| The draft hold already expired               | 409    | `booking_expired`           |
| `payment_status` sent as `DRAFT`             | 422    | `invalid_payment_status`    |
| `advance_paid_amount` disagrees with `total` | 422    | `amount_mismatch`           |
| Deposit below the salon's minimum            | 422    | `deposit_too_low`           |
| `payment_reference` missing when money moved | 422    | `missing_payment_reference` |
| No such booking, or not the caller's         | 404    | `not_found`                 |

The 415 is refused **here, before dialling out**, so the app hears about the
header rather than getting an answer about the payload after a round trip.

---

## 9. Order of calls

```
POST  /booking        →  payment_status: DRAFT, slot held
  ↓ gateway authorises
PATCH /booking/<id>   →  FULLY_PAID | PARTIALLY
  ↓
GET   /booking/<id>   →  confirmation screen, pass, history
GET   /bookings       →  upcoming / recurring / archive
```

A booking that never reaches the PATCH stays `DRAFT` and is released when its
hold expires, so it appears on none of the three shelves. A
`PAY_AFTER_CHECK_IN` booking skips the PATCH entirely — it is confirmed at
creation and pays at the desk.
