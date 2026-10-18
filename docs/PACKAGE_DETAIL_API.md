# Package Details

Base URL: `https://api.gostyle.uk/api/v1`
Auth: none needed. A Bearer token is accepted, and a bad or expired one is `401`.

The agreed contract, 2026-10-15. It is the mobile team's
`package-detail-fe-contract.md` (kept as received) with what the server really
does. The full `200` below was taken from the running code, and a test asks
the code again from the same rows and must get the same answer, so this page
cannot drift from the code. The ids in the examples are test data.

**Changed 2026-10-18:** Book with this Package has a flow behind it now, and
the answer gains `can_book` to show it by (§5, point 12 below).

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
12. **`can_book` is added (2026-10-18)**, last, while the server's
    `PACKAGE_BOOKING_V1` is on, and absent while it is off. True: the
    package can be booked here now as one visit at `price`, so show Book with
    this Package (§5). It is never true when `is_active` is false, and can be
    false when `is_active` is true.

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
| `is_active` | boolean | False: pulled from sale here. Book with this Package goes by `can_book` (§5) |
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
| `can_book` | boolean, or absent | Shows Book with this Package when true. Absent while the server's `PACKAGE_BOOKING_V1` is off: hide the button (§5) |

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
- **`can_book`** (2026-10-18, `PACKAGE_BOOKING_V1` on) is true only when
  booking-api would sell the package as one booking: `is_active`, and every
  included service offered at this branch (the platform's branch rule, for
  the service and its category), no included service pinned to a size or
  length (a variant: booking-api books services, not their variants), a
  `price` above 0, and every included service priced above 0 at this branch
  and in the package's own currency (changed 2026-10-18: booking-api refuses
  the whole booking for one service with no price, and for a package and a
  service in two currencies). A package dearer than its services can be
  booked: it is sold at its price. The Packages tab's `can_book` is the same
  rule once the tab shows the published content too; until then it is
  `false` on every bundle (changed 2026-10-18: the tab shows the price the
  salon last saved, not the published one a booking is charged). This
  screen reads the published content either way, so its `can_book` stands
  on its own.
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

> **Changed 2026-10-18: the button has a flow behind it.**
>
> - **It was** "there is nothing behind this button yet, anywhere": hide it,
>   and send no `package_id`, because booking create had none.
> - **It is** a booking of the package as **one visit at the package price**
>   (the first of the two answers this section used to weigh), shown when
>   `can_book` is true. Your proposed create body,
>   `{ "salon_id", "package_id", "services": [{ "service_id", … }] }`, is
>   replaced by the package as one line of `services`:
>   `{ "id": "<package id>", "amount": <package price> }`. There is no
>   `package_id` field, and the included services are not listed.
> - **Why.** booking-api takes the package as a line, and refuses a key it
>   does not know with `422`, so a `package_id` field would fail the booking.
>   And the package price is the line's price: there is no bundle discount
>   line, so everything that reads the booking's lines (the till, the payment
>   link) charges the package price, as the quote did.

1. **Show the button only when `can_book` is true.** The key is absent while
   the server's switch is off: hide the button then, as before. A guest is
   asked to sign in first (your §6).
2. **Find a time with the package's own id:**
   `GET /booking/nearest-available/{salon_id}?from=…&to=…&service_ids={id}`,
   not its `includes[].service_id`s. The server searches for the included
   services itself, each as many times as the package holds it, with one
   stylist for all of them; `duration_min` is the whole visit.
3. **Price it:** `POST /booking/quote` with
   `services: [{ "id": "<package id>" }]`, `date`, and the chosen offer's
   `start` as `start_time`.
4. **Book it:** `POST /booking` with the quote's `services`, `end_time` and
   money figures exactly as it answered them, and the chosen offer's stylist.

The bodies, the money rules and what the booking reads back (`packages`) are
in `BOOKING_CREATE_API.md`, §6 and §7.

Your three questions, answered again:

- **What booking create expects:** the package as a line of `services`, at
  its price (above). No `package_id`, no session field.
- **Bought at that moment or beforehand:** at that moment, as the booking
  itself: one visit, paid like any booking. Nothing is prepaid and no
  sessions are counted (prepaid sessions are not built), so How it works'
  "Buy and activate instantly" and "Check in, covered" do not describe it.
- **Sold out or no longer active:** `can_book` is false. If that changes
  while the customer is on the way, the slot search answers `422`
  `package_not_bookable`, and the quote or create `422` `unknown_service`.

Do not book the included services by their own ids under the package's name:
picked one by one, each is charged at its own price, not the package price.

---

## 6. What the screen does next

1. Opened from the Packages tab with the salon id and `bundles[].id`.
2. Reads this endpoint once. Nothing to poll, nothing to write.
3. Draws the hero, What's Included, the timeline, How it works (your own
   text), the saving card (when `price_before` is not null) and `terms`.
4. Book with this Package: shown when `can_book` is true, and hidden while it
   is false or absent (§5).

---

## 7. Your open points

1. **A package the customer already owns.** Not possible: nothing records
   ownership or sessions used, and prepaid sessions are not built (a package
   is booked as one visit, §5). `my_packages` on the tab stays `[]`. This
   screen has one state.
2. **The hero theme.** Yes: `theme`, the tab row's own value.
3. **Share.** There is no link for a package (nothing per package to point
   at). Leave the button out for now.
4. **`duration_minutes`.** The included services' minutes added up, each
   times its quantity: the same number as on the tab row. For a usual
   package it is one visit's chair time, and the timeline's last step ends at
   it. The timeline's first step is its own service's minutes, so the two
   only match when the package holds one service.
