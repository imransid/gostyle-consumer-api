# Booking — Create

`POST /api/v1/booking` · **Bearer token required**

Creates a booking. This service does not own bookings: the request is
forwarded to **gostyle-booking-api**, which holds them in its own database
(`gostyle_booking`), and that service's answer is returned unchanged.

---

## 1. What this endpoint does, exactly

```
app ──POST /api/v1/booking──▶ customer-api ──POST /v1/mobile-booking──▶ booking-api
                                            (body byte-for-byte)
app ◀────── status + body ───── customer-api ◀──────── status + body ────┘
```

Four headers cross, and nothing else:

| Header            | Source                                                  |
| ----------------- | ------------------------------------------------------- |
| `Authorization`   | the caller's own, verbatim                              |
| `Content-Type`    | always `application/json`                               |
| `Idempotency-Key` | the caller's, **only when sent**                        |
| `X-Tenant-Id`     | the caller's, else **the payload's salon's tenant**     |

`Idempotency-Key` is never invented: a made-up one would turn every retry
into a second booking.

### Why `X-Tenant-Id` is the exception

booking-api needs it to resolve the services in the payload. `ListServices` is
tenant-scoped, and its `TenantMiddleware` reads this header and nothing else —
deliberately, since the middleware runs before the guard and the token's
`tenantId` claim is not available to it. With no header it resolves no
services and refuses the booking:

```
Cannot resolve 1 platform service(s): no X-Tenant-Id on this request,
and ListServices is tenant-scoped.
```

which reaches the app as `422 unknown_service`. The customer app does not send
the header, so this service supplies it.

**Derived, not invented.** When the caller sends no `X-Tenant-Id`, the payload
is read — READ, never rewritten — for its `salon_id`, and that salon's own
`tenant_id` is sent. It is the tenant the salon actually belongs to, from the
same table `/salon/<id>` serves, not a guess about which one was meant.

`salon_id` is matched against all three things it can hold, because this
service and booking-api disagree about what a salon is and each is right in
its own vocabulary: a **storefront uuid** (what every salon endpoint here
returns), a **branch uuid** (what booking-api means — its mobile handler reads
`salon_id` straight into `branchId`), or a **storefront slug** (what its
contract examples show). The two uuid columns are unique, so matching either
is a lookup. `slug` is unique only per tenant, so one that lands on two
tenants resolves to nothing.

Three rules hold it to that:

1. **The caller's header wins**, verbatim and unexamined. An app that knows
   its tenant is the better source, and the salon is not even looked up.
2. **Ambiguity sends nothing.** An unresolvable `salon_id`, a payload without
   one, a body that is not JSON, or a slug that two live storefronts share all
   send no header at all. booking-api then refuses the booking exactly as it
   does today — which is the safe end of the trade, because a tenant chosen at
   random would file a real booking against another salon's rows.
3. **The forwarded bytes do not change.** The parsed copy is only read from;
   what crosses the network is still the caller's body, byte for byte, so
   booking-api's retry hash still matches.

**The body is forwarded as raw bytes.** It is never parsed and re-serialised,
because booking-api hashes the body to recognise a retry — so `216.25` must
arrive as `216.25`, with the keys in the order they were sent. It is *parsed*
for one read-only purpose, the tenant lookup above; the bytes that go out are
still the ones that came in.

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
- **The tenant comes from the salon, not from the app.** Until the app sends
  `X-Tenant-Id` itself, every booking's tenant is resolved from `salon_id`
  here. A booking whose payload names a salon this service cannot resolve
  still fails with `unknown_service`, and the customer-api log says so
  (`No X-Tenant-Id sent and salon_id … resolved to no tenant`) rather than
  leaving the reason in booking-api's logs only.
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
