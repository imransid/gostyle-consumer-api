# Booking — Create

`POST /api/v1/booking` · **Bearer token required**

Creates a booking. This service does not own bookings: the request is
forwarded to **gostyle-booking-api**, which holds them in its own database
(`gostyle_booking`), and that service's answer is returned unchanged.

Price it first with `POST /api/v1/booking/quote` (§6): it answers the figures
this create checks. A salon package is booked as one line of `services`
(§7).

---

## 0. `salon_id` is the only id you need

Send the **salon id this service already gave you** — the storefront uuid
from `/discover`, `/salon/<id>` or `/salon/<id>/stylists`. Nothing else:

```jsonc
{ "salon_id": "c6c248ab-f2cd-4f12-a31e-243c6e64b3b5", ... }
```

No `X-Tenant-Id`. No `Idempotency-Key`. Both are resolved here.

**What this spares you.** booking-api's payload calls the field `salon_id`
and then reads it straight into `branchId` — so the field is named for a
storefront and holds a *branch*. Every other endpoint in this service returns
storefront uuids, so an app that booked with the id it had just browsed with
sent the wrong one. Both are uuids, so nothing complained about the id: the
refusal came back three steps later as

> `stylist_unavailable` — "That stylist does not work at this salon."

...because a wrong branch first shows up as an empty roster. One field, two
id spaces, and an error naming neither.

Now `salon_id` accepts **any of the three** and resolves them all to the same
salon:

| You send | Resolves |
| ------------------ | -------- |
| storefront uuid    | ✅ |
| branch uuid        | ✅ (left exactly as sent) |
| storefront slug    | ✅ |

**Nothing is guessed.** A reference matching no salon, or two, is forwarded
untouched with no tenant — booking-api then refuses it, which is where the
refusal was before. A slug is unique per tenant and *not* globally, so two
salons can share one; picking the first would file a real booking, with real
money, against another salon's diary.

**Idempotency comes free.** When you send no `Idempotency-Key`, one is
derived from the customer and the body. The same booking retried produces the
same key and is replayed; a different booking produces a different one. A
double tap on a flaky connection is one booking, not two charges — which
previously depended on every client remembering the header. Send your own if
you do your own retry accounting, and it is used untouched.

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
> envelope, the same as every other route: `detail` (a sentence), `code` (what
> kind of error), and `errors`, a list of `{ field, code, message }`. They are
> the four rows in the table below. The app has
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

---

## 6. Price it first — `POST /api/v1/booking/quote`

> **Added 2026-10-18.** Not behind a switch: it prices any basket, a package
> or not.

`POST /api/v1/booking/quote` · **Bearer token required**

The figures create checks, priced by booking-api (its
`POST /v1/mobile-booking/quote`) with the same computation create checks
yours against. It holds nothing and writes nothing. Ask it once the customer
has picked a time, and build the create from its answer: a create built from
it is accepted, unless a price changes in between.

### Request

```jsonc
{
  "salon_id": "33333333-3333-3333-3333-333333333333", // the id you have (§0)
  "services": [{ "id": "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaa1" }], // service and package ids
  "products": [],                                    // optional, exactly as on create
  "date": "2026-10-20",
  "start_time": "2026-10-20T14:00:00+04:00"
}
```

**Exactly these keys, and `{id}` alone on a line.** booking-api refuses any
other key with `422`, so this is not create's body: no `stylists` (who does
the visit does not change what it costs), no `end_time`, no money, and no
`amount` on a line. Those are what it answers.

### Response — `200 OK`

A package at the seeded salon, "Groom and Go" at 520 (a cut, a scalp
treatment at this branch's 150, a hot towel shave), for a customer with no
tier. Each part carries its share of the package price, in proportion to its
own price, and the shares add up to it:

```json
{
  "salon_id": "22222222-2222-2222-2222-222222222222",
  "date": "2026-10-20",
  "start_time": "2026-10-20T16:00:00+06:00",
  "end_time": "2026-10-20T17:25:00+06:00",
  "duration_min": 85,
  "services": [
    {
      "id": "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaa1",
      "amount": 520
    }
  ],
  "parts": [
    {
      "id": "66666666-6666-6666-6666-666666666660",
      "name": "The Gentleman's Cut",
      "amount": 169.92,
      "duration_min": 30,
      "package_id": "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaa1"
    },
    {
      "id": "66666666-6666-6666-6666-666666666663",
      "name": "Scalp Treatment",
      "amount": 128.08,
      "duration_min": 20,
      "package_id": "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaa1"
    },
    {
      "id": "66666666-6666-6666-6666-666666666662",
      "name": "Hot Towel Shave",
      "amount": 222,
      "duration_min": 35,
      "package_id": "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaa1"
    }
  ],
  "packages": [
    {
      "id": "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaa1",
      "name": "Groom and Go",
      "amount": 520,
      "parts_total": 609,
      "savings": 89,
      "service_ids": [
        "66666666-6666-6666-6666-666666666660",
        "66666666-6666-6666-6666-666666666663",
        "66666666-6666-6666-6666-666666666662"
      ]
    }
  ],
  "products": [],
  "amount_without_tax": 520,
  "discount": 0,
  "tax_amount": 26,
  "total": 546,
  "advance_paid_amount": 0,
  "due_amount": 546,
  "deposit_amount": 0
}
```

| Field | What it is | On create |
| ----- | ---------- | --------- |
| `services` | The lines as you asked for them, each with its `amount`: a package at the package price, a service at its own | send as they are |
| `start_time`, `end_time` | The visit, on booking-api's clock: the same instants as yours, in its offset | send `end_time` |
| `duration_min` | The parts end to end | |
| `parts` | What will be performed, each at its share of any package (`package_id`, null for a hand-picked service) | display only |
| `packages` | Each package: `amount` (its price), `parts_total` (its parts at their own prices here), `savings` (`parts_total - amount`, never below 0), `service_ids` (its parts) | display only |
| `products` | The product lines, priced | as on create |
| `amount_without_tax`, `discount`, `tax_amount`, `total` | The money create checks | send as they are |
| `advance_paid_amount`, `due_amount` | `0`, and `total` | send as they are |
| `deposit_amount` | The deposit the salon asks for this visit | for the pay screen |
| `salon_id`, `date` | booking-api's echo; `salon_id` is the branch it priced at | send create your own `salon_id`: either works |

### Routing and errors

Routed exactly as create (§0, §1): `salon_id` resolved to the branch, the
tenant derived unless you send `X-Tenant-Id`, your own `Authorization`
forwarded, and booking-api's status and body returned unchanged. This
service's own refusals are create's: `401`, `415`, `405`, and `503`
`booking_api_unavailable` (§2).

booking-api's refusals are create's, in its envelope:

```jsonc
// 422
{
  "detail": "Please correct the highlighted fields.",
  "code": "validation_error",
  "errors": [
    {
      "field": "services",
      "code": "unknown_service",
      "message": "Not sold at this salon: aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaa1."
    }
  ]
}
```

For example `unknown_service` (not sold here, or a package booking-api does
not sell here), `date_mismatch` (`date` is not `start_time`'s day at the
salon), `invalid_window`, `products_not_supported`.

**No `Idempotency-Key`.** None is sent, yours or a derived one: booking-api
would store the answer under the key and replay it to the next quote with the
same key and body, which is an old price. A quote has nothing a retry could
do twice. Ask again whenever the basket or the time changes.

---

## 7. Booking a package

> **Added 2026-10-18.** Option A of `PACKAGE_DETAIL_AUDIT.md`: a package is
> booked as one visit, at the package price. Nothing is bought beforehand and
> there are no sessions to count, so `my_packages` stays `[]`.

**On only where both switches are on:** this service's `PACKAGE_BOOKING_V1`
(`can_book`, and the slot search taking a package id) and booking-api's
`PACKAGES_FROM_PLATFORM` (without it booking-api sells no package, and a
package line is `422 unknown_service`).

### The flow

1. **Show Book with this Package only when `can_book` is true** (Package
   Details, and each Packages tab bundle). The key is absent while the switch
   is off: hide the button then, as before.
2. **Find a time with the package's own id.**
   `GET /api/v1/booking/nearest-available/{salon_id}?from=…&to=…&service_ids={package_id}`,
   with `bundles[].id`, not its `includes[].service_id`s. The server searches
   for its parts, each as many times as the package holds it, so
   `duration_min` is the whole visit, and every offer's stylist can do every
   part (`BOOKING_NEAREST_AVAILABLE_API.md`).
3. **Price it.** `POST /api/v1/booking/quote` with
   `services: [{"id": "<package id>"}]` (and any hand-picked services beside
   it), `date`, and the offer's `start` as `start_time` (§6).
4. **Book it.** `POST /api/v1/booking` with the quote's `services`,
   `end_time`, `amount_without_tax`, `discount`, `tax_amount`, `total`,
   `advance_paid_amount` and `due_amount` exactly as it answered them,
   `stylists: ["<the offer's stylist.id>"]`, and the rest as for any booking.

### The line

A package is a line of `services`, like a service:

```jsonc
"services": [
  { "id": "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaa1", "amount": 520 }, // the package, at its price
  { "id": "66666666-6666-6666-6666-666666666661", "amount": 230 }  // a service beside it, at its own
]
```

- `id` is the package's id: `bundles[].id` on the Packages tab, `id` on
  Package Details. Do not list its included services as well: booking-api
  expands the package into them itself.
- There is **no `package_id` field**. booking-api refuses a key it does not
  know with `422`.
- `amount` is the package price, as the quote answered it. booking-api checks
  the totals rather than each line, but send the quote's lines unchanged.
- `stylists`: one for the whole visit. The slot search only offers a stylist
  who can do every part.

### The money

- `amount_without_tax`: the lines added up (a package at its **package
  price**, a service at its own price), plus the products.
- `discount`: the customer's tier discount, and nothing else. There is no
  bundle discount line: the package price is the price.
- `tax_amount`: 5% of `amount_without_tax - discount`. `total`: that plus the
  tax.
- `advance_paid_amount`: `0`. `due_amount`: `total`.
- `end_time`: `start_time` plus the parts end to end.

A package dearer than its parts is sold at its price (`savings` 0), not
refused. Take these figures from the quote rather than working them out:
that is how an `amount_mismatch` is avoided.

### What the booking reads back

booking-api's fields, forwarded untouched:

- **Create's `201` and `GET /api/v1/booking/{id}`** carry
  `packages: [{id, name, amount, service_ids}]`: `amount` is the package
  price before VAT, `service_ids` its parts. `services` lists the parts as
  before, each at its share of the package price; the shares add up to it.
- **Each My Bookings row** (`GET /api/v1/bookings`) carries
  `packages: [{id, name}]`, so the row can say "Groom and Go".
- A booking of hand-picked services has `packages: []`. Treat a missing key
  the same way.

### When it cannot be booked

| Where | The answer |
| ----- | ---------- |
| Package Details, a Packages tab bundle | `can_book: false`: hide the button |
| The slot search, if it changed after the screen was drawn | `422` `package_not_bookable` on `service_ids` |
| The quote or create, if booking-api does not sell it here | `422` `unknown_service` ("Not sold at this salon: …") |
| The quote or create, with a service beside it priced in another currency | `422` `BOOKING_CURRENCY_MIXED`, in booking-api's own shape (`{statusCode, code, message, details, error}`, no `errors`): take one of them off (added 2026-10-18) |

`can_book` is true only when booking-api would sell it: published, online
booking on, sold at this branch, every part on the menu and offered at this
branch (the platform's branch rule, for the service and its category), no
part pinned to a size or length of a service (a variant), a price above 0,
every part priced above 0 at this branch, and every part priced in the
package's own currency here (changed 2026-10-18: booking-api refuses the
whole basket for one part with no price, `409 BOOKING_STATE_INVALID`, and
for a package and a part in two currencies, `422 BOOKING_CURRENCY_MIXED`).

### Not built

- Prepaid sessions and My Packages (option B): nothing is bought ahead,
  nothing is counted, `my_packages` stays `[]`.
- ~~A package in a group booking: `members[].packages` is still `422`
  `packages_not_supported` (`BOOKING_GROUP_API.md`).~~ **Changed
  2026-10-18: built.** A party member books a package the same way, as one
  line of that member's `services` (`BOOKING_GROUP_API.md` §12,
  `MOBILE_GROUP_BOOKING_FE.md` Packages). `members[].packages` is still
  refused.
