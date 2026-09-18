# Booking — Create

`POST /api/v1/booking` · **Bearer token required**

Creates a booking. This service does not own bookings: the request is
forwarded to **gostyle-booking-api**, which holds them in its own database
(`gostyle_booking`), and that service's answer is returned unchanged.

---

## 1. What this endpoint does, exactly

```
app ──POST /api/v1/booking──▶ customer-api ──POST /v1/booking──▶ booking-api
                                            (body byte-for-byte)
app ◀────── status + body ───── customer-api ◀──── status + body ──┘
```

Three headers cross, and nothing else:

| Header            | Source                                          |
| ----------------- | ----------------------------------------------- |
| `Authorization`   | the caller's own, verbatim                      |
| `Content-Type`    | always `application/json`                       |
| `Idempotency-Key` | the caller's, **only when sent**                |
| `X-Tenant-Id`     | the caller's, **only when sent**                |

Neither optional header is ever invented here. A made-up `Idempotency-Key`
would turn every retry into a second booking; a made-up tenant would stamp
another salon's rows.

**The body is forwarded as raw bytes.** It is never parsed and re-serialised,
because booking-api hashes the body to recognise a retry — so `216.25` must
arrive as `216.25`, with the keys in the order they were sent.

**Who is booking is decided over there.** booking-api resolves the customer
from the bearer token; this service does not read, check, or override
`customer_id` in the payload.

---

## 2. Responses

**Whatever booking-api says.** Its status code and its JSON body reach the app
untouched — `201` on success, and equally `409` and `422`:

```jsonc
// 409 — the slot went to someone else between picking and pressing Book
{ "code": "slot_taken", "message": "That time just went." }

// 422 — booking-api rejected the payload
{ "code": "validation_error", "errors": [ { "field": "services", … } ] }
```

> **Two error shapes live on this endpoint.** Refusals from booking-api arrive
> in *its* shape, above. Refusals from this service arrive in this project's
> envelope (`AUTH_GUIDE.md`) — the four rows in the table below. The app has
> to handle both here, which is the price of not translating: a translation
> layer is one more contract to keep in step, and it would have to invent a
> `code` for every code booking-api ever adds.

Errors raised **before** the request leaves this service:

| Case                                          | Status | `code` (top level)      | `errors[0].code`          |
| --------------------------------------------- | ------ | ----------------------- | ------------------------- |
| No bearer token                               | 401    | `not_authenticated`     | `not_authenticated`       |
| Not `Content-Type: application/json`          | 415    | `unsupported_media_type`| `unsupported_media_type`  |
| Any method but POST                           | 405    | `method_not_allowed`    | `method_not_allowed`      |
| booking-api unreachable, timed out, or non-JSON | 503  | `service_unavailable`   | `booking_api_unavailable` |

```json
{
  "detail": "Booking is temporarily unavailable. Please try again.",
  "code": "service_unavailable",
  "errors": [
    {
      "field": null,
      "code": "booking_api_unavailable",
      "message": "Booking is temporarily unavailable. Please try again."
    }
  ]
}
```

`booking_api_unavailable` is the one to branch on: it means the request never
reached booking-api, so **nothing was created and a retry is safe**. A `5xx`
that came *from* booking-api is forwarded as itself and carries no such
promise.

---

## 3. Retries

Send an `Idempotency-Key` on every create. booking-api stores the key with a
hash of the body and returns the original response to a repeat, so a customer
who taps twice, or a phone that loses signal mid-request, gets one booking.

Generate it per booking attempt (a UUID), not per app session, and reuse the
same value for the retry of that attempt.

---

## 4. Configuration

| Setting               | Default                            | Notes                                     |
| --------------------- | ---------------------------------- | ----------------------------------------- |
| `BOOKING_API_URL`     | `http://gostyle-booking_api:3851`  | No `/v1` — the client appends it per path. |
| `BOOKING_API_TIMEOUT` | `10` seconds                       | A customer is waiting; fail fast.          |

The default is the Swarm service name, so production needs no entry. Set it
locally to point somewhere real.

---

## 5. Known gaps

- **Customer identity depends on a service that is currently down.**
  booking-api validates a customer token by calling the consumer gRPC service
  (`CONSUMER_GRPC_ADDR`). On this server `gostyle-consumer_grpc` is
  crash-looping on an empty `SECRET_KEY`, so every create will be refused by
  booking-api until that is fixed. The refusal is forwarded correctly; it is
  just not a refusal about the booking.
- **No payload validation here, by design.** booking-api owns that contract.
  Validating in two places guarantees the two drift, and its `422` is already
  the answer the app needs.
- **The 503 has no retry budget.** One attempt, one timeout, one answer.
  Retrying a create automatically is only safe with an idempotency key, and
  whether to retry belongs to the app, which knows whether the customer is
  still looking at the screen.
- **Nothing here reads the booking back.** `GET /booking/:id` and payment are
  booking-api's endpoints; if the app needs them through this service, they
  are the same shape of forwarder as this one.
