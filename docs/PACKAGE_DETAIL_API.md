# Package Details

Base URL: `https://api.gostyle.uk/api/v1`
Auth: none needed. A Bearer token is accepted, and a bad or expired one is `401`.

The agreed contract, 2026-10-15. It is the mobile team's
`package-detail-fe-contract.md` (kept as received) with what the server really
does. The full `200` below was taken from the running code, and a test asks
the code again from the same rows and must get the same answer, so this page
cannot drift from the code. The ids in the examples are test data.

The screen a customer opens from a salon's Packages tab by tapping a bundle.
**One call draws the whole screen.** The list call
(`GET /salon/:id/packages`) stays as it is.

---

## What differs from your draft

1. **The route is public.** No token is needed, like every other
   `/salon/:id/...` route, so a guest can read the screen, as you asked. A
   token is accepted, and a bad or expired one is `401` (§4).
2. **The `404` body** has `detail` written for the customer and one item in
   `errors` with `field: null`. It is not "Please correct the highlighted
   fields." (that is the `422` wording). Read `detail` and `code` (§4).
3. **One package id works at every branch of a salon business.** A package
   belongs to the business, like a service, not to one branch. The same
   `package_id` opens under each branch's `salon_id`, at the **same `price`**:
   there is no per-branch package price. Only `price_before`, `save_amount`
   and `save_percent` can differ by branch, because a service can have its own
   price at a branch.
4. **`save_amount` is always `price_before - price`.** `price_before` is not a
   price the salon types: it is the included services booked separately at
   this branch, each times its quantity. That is exactly "what the services
   cost separately", so the saving card and the struck-through price always
   agree. Your design's 540 and 103 cannot both happen.
5. **`price_before`, `save_amount` and `save_percent` are `null` together**
   when the package saves nothing (it costs the same as its services, or
   more). Your table has `save_amount` and `save_percent` as numbers; treat
   them as `number | null`. Null hides the chip, the struck-through price and
   the saving card.
6. **`includes[]` has `quantity`** (1 almost always). `price` is ONE of that
   service, here. A row with `quantity: 2` is "2 x The Gentleman's Cut".
7. **The timeline is built from the same services as `includes`**, in the
   same order, so the names and minutes always match. Your design's "Beard
   Trim & Shave / Shape" and the 75-minute first step are mock data.
8. **`terms` is the salon's own cancellation policy for this package**, as
   they typed it, or `null`. It is not "valid 90 days, sessions deduct,
   refund to wallet": none of that exists anywhere yet (§5). It can hold line
   breaks if the salon typed them.
9. **`theme` is added** (your §7, point 2): the same value as the Packages tab
   row.
10. **`is_active` is also `false` when a service in the package cannot be
    booked here** (the salon hid it, archived it, or turned off online
    booking for it). A package pulled from sale still opens with `200` and
    `is_active: false`, so an old link or a My Packages row never breaks.
11. **No `503` from this route for another service being down.** It reads
    the salon's menu directly and calls nothing else.

---

## 1. Endpoint

| Method | Path                                     | Purpose                      |
| ------ | ---------------------------------------- | ---------------------------- |
| `GET`  | `/salon/{salon_id}/package/{package_id}` | Everything this screen draws |

```
GET /salon/33333333-3333-3333-3333-333333333333/package/aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaa1
```

`package_id` is `bundles[].id` from the list call.

Reused, unchanged:

| Method | Path                  | Used by                                    |
| ------ | --------------------- | ------------------------------------------ |
| `GET`  | `/salon/:id/packages` | The Packages tab, which opens this screen  |

---

## 2. Response: `200 OK`

The seeded salon's "Groom and Go", published with three services. The scalp
treatment has this branch's own price (150, against 180 elsewhere), which is
why `price_before` is 199 + 150 + 260 = 609:

```json
{
  "id": "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaa1",
  "salon_id": "33333333-3333-3333-3333-333333333333",
  "name": "Groom and Go",
  "description": "Leave sharp, stay sharp: a cut, a scalp treatment and a hot towel shave.",
  "price": 520.0,
  "price_before": 609.0,
  "save_amount": 89.0,
  "save_percent": 15,
  "duration_minutes": 85,
  "is_active": true,
  "includes": [
    {
      "service_id": "66666666-6666-6666-6666-666666666660",
      "name": "The Gentleman's Cut",
      "price": 199.0,
      "quantity": 1
    },
    {
      "service_id": "66666666-6666-6666-6666-666666666663",
      "name": "Scalp Treatment",
      "price": 150.0,
      "quantity": 1
    },
    {
      "service_id": "66666666-6666-6666-6666-666666666662",
      "name": "Hot Towel Shave",
      "price": 260.0,
      "quantity": 1
    }
  ],
  "timeline": [
    {
      "service_id": "66666666-6666-6666-6666-666666666660",
      "name": "The Gentleman's Cut",
      "offset_minutes": 0,
      "duration_minutes": 30
    },
    {
      "service_id": "66666666-6666-6666-6666-666666666663",
      "name": "Scalp Treatment",
      "offset_minutes": 30,
      "duration_minutes": 20
    },
    {
      "service_id": "66666666-6666-6666-6666-666666666662",
      "name": "Hot Towel Shave",
      "offset_minutes": 50,
      "duration_minutes": 35
    }
  ],
  "terms": "Free to cancel up to 24 hours before your visit. After that, the deposit is kept.",
  "theme": "gold"
}
```

### Fields

| Field | Type | Where it lands on the screen |
| ----- | ---- | ---------------------------- |
| `id`, `salon_id` | uuid | Not drawn. `salon_id` is the one you asked with |
| `name` | string | The hero title |
| `description` | string \| null | The line under it. Null hides the line |
| `price` | number | The big price beside the dirham mark |
| `price_before` | number \| null | The struck-through price. Null hides it, the SAVE chip and the saving card |
| `save_amount` | number \| null | The saving card's amount. Null with `price_before` |
| `save_percent` | integer \| null | "SAVE 15%" and "(15%)". Null with `price_before` |
| `duration_minutes` | integer \| null | "Time: ~85 mins". Null only for a package with no services |
| `is_active` | boolean | False hides Book with this Package (see §5 for today) |
| `includes[].service_id` | uuid | Not drawn |
| `includes[].name` | string | The left of each What's Included row |
| `includes[].price` | number | The right of it: ONE of this service, here |
| `includes[].quantity` | integer | "2 x" before the name when above 1 |
| `timeline[].service_id` | uuid | Not drawn |
| `timeline[].name` | string | The step's title |
| `timeline[].offset_minutes` | integer | 0 prints "Start", the rest "+30 min" |
| `timeline[].duration_minutes` | integer | "~20 mins" under it |
| `terms` | string \| null | The grey footnote. Null hides it |
| `theme` | string \| null | The hero colour, as on the tab row. Null keeps it black |

All money is a number in the salon's own currency, before VAT, as everywhere
else. Minutes are whole numbers.

---

## 3. Rules the server follows

- **What the customer sees is the live version.** When the salon edits a
  published package, the app keeps showing what is live until the salon
  publishes again. The screen never shows a half-finished edit.
- **`price_before`** = each included service at this branch's price (the
  price the Services tab shows) times its quantity, added up. If a package
  names a size or length of a service (a variant), that variant's price and
  minutes are used.
- **`save_percent`** = `save_amount / price_before`, rounded to the nearest
  whole number (half up), and at least 1, so the chip never says "SAVE 0%".
- **`duration_minutes`** = the included services' minutes added up, each
  times its quantity. It is the same number as `duration_minutes` on the
  tab row.
- **The timeline** has one step per included service, in the package's
  order. Each step starts when the ones before it end, so for a usual package
  the last step ends at `duration_minutes`. When the salon set the package to
  run its services at the same time, every step starts at 0. A service with
  `quantity` 2 is still one step (a timeline is one visit).
- **`is_active`** is true only when the package is on sale at this branch now
  and every included service can be booked here.
- **Plain text only.** `description` and `terms` are sent as the salon typed
  them, trimmed. Blank is `null`.
- **Nothing is booked or held.** It is a read.

---

## 4. Errors

Same envelope as every other route: `detail` (a sentence), `code` (what kind
of error), and `errors`, a list of `{ field, code, message }`.

| Case | Status | `code` | `detail` |
| ---- | ------ | ------ | -------- |
| No such salon, or its id is not a UUID | 404 | `not_found` | `This salon is not available.` |
| No such package at this salon's business, never on sale, or its id is not a UUID | 404 | `not_found` | `This package is no longer available.` |
| A bad or expired token | 401 | `not_authenticated` | `Authentication required.` |
| No token | 200 | | The route is public |

`detail` is written for the customer: show it on your "We couldn't load this
package" state.

```json
{
  "detail": "This package is no longer available.",
  "code": "not_found",
  "errors": [
    {
      "field": null,
      "code": "not_found",
      "message": "This package is no longer available."
    }
  ]
}
```

```json
{
  "detail": "This salon is not available.",
  "code": "not_found",
  "errors": [
    {
      "field": null,
      "code": "not_found",
      "message": "This salon is not available."
    }
  ]
}
```

For a bad token, read `code` (`not_authenticated`), refresh, and try again.
The token library adds its own items to `errors`; do not rely on them.

---

## 5. Book with this Package

**There is nothing behind this button yet, anywhere.** Please hide it (or
show it disabled) until package booking is built. Answers to your three
questions:

- **What booking create expects:** nothing about packages. `POST /booking`
  has no `package_id` and no session field, and the booking records have
  nowhere to keep one. Do not send one.
- **Bought at that moment or beforehand:** neither, today. A customer cannot
  buy a package in the app, and nothing records that a customer owns one or
  how many sessions are left. A salon can sell a package only at its own
  till, and even that records just the sale.
- **Sold out or no longer active:** no such answer exists yet.

Please do not book the included services as an ordinary booking under the
package's name. Booking create prices every service itself, at its normal
price, and refuses any lower total (`amount_mismatch`), so the customer
would pay the full price, not the package price.

Package booking is its own piece of work, and needs one product decision
first: is a package **one visit at a bundle price** (book all its services
together, once), or **prepaid sessions** (buy first, then book included
services one by one, as your How it works describes)? We will come back with
a plan and a contract.

---

## 6. What the screen does next

1. Opened from the Packages tab with the salon id and `bundles[].id`.
2. Reads this endpoint once. Nothing to poll, nothing to write.
3. Draws the hero, What's Included, the timeline, How it works (your own
   text), the saving card (when `price_before` is not null) and `terms`.
4. Book with this Package: hidden for now (§5).

---

## 7. Your open points

1. **A package the customer already owns.** Not possible yet: nothing
   records ownership or sessions used (§5). `my_packages` on the tab stays
   `[]`. This screen has one state.
2. **The hero theme.** Yes: `theme`, the tab row's own value.
3. **Share.** There is no link for a package (nothing per package to point
   at). Leave the button out for now.
4. **`duration_minutes`.** The included services' minutes added up, each
   times its quantity: the same number as on the tab row. For a usual
   package it is one visit's chair time, and the timeline's last step ends at
   it. The timeline's first step is its own service's minutes, so the two
   only match when the package holds one service.
