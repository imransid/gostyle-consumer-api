# Search: popular services and popular salons

Base URL: `https://api.gostyle.uk/api/v1`
Auth: optional on both calls, as on `/discover` today. Send the token when
signed in. A bad or expired token is `401`, as on `/discover`.

Built from your doc of 2026-11-03 (`docs/search-popular-fe-contract.md`, kept
as received), as you asked it. Two calls:

| Method | Path | What for |
| ------ | ---- | -------- |
| GET | `/services` | Popular Services row, and the "Services" list while typing |
| GET | `/discover` | Popular Near You row (`is_popular`), and the "Salons" list (`search`, as today) |

Behind the switch `SEARCH_POPULAR_V1`. Off, `GET /services` without
`tenant_id` is the `422` it is today, and `/discover` ignores `is_popular` and
`pageSize`. We tell you when it is on.

**Update 2026-11-04:** each `/services` result also sends `currency` right after `price` ("AED", "BDT"), switch `SERVICE_CURRENCY_V1`, on. The "while typing" lists now have their own light call, `GET /search/suggest` (`docs/SEARCH_SUGGEST_API.md`). The whole Discovery flow, screen by screen, is `docs/MOBILE_DISCOVERY_FE.md`.

---

## Notes on your doc

Everything is as you wrote it. Six small things to know:

1. **`category.id` is a UUID, not a slug.** It is the same id as on
   `GET /salon/:id/services` (the chip): `"5f1c..."`, or `"other"` for a
   service with no category. Your example `"haircut_styling"` was only an
   example. `category` is never null.
2. **`radius` is in KILOMETRES**, as on `/discover`. Your examples send
   `radius=500`: that is 500 km, so about the whole UAE. Send the same number
   you send to `/discover`.
3. **`/discover` without `pageSize` still gives 15 a page** (its old default;
   your doc says 20). `/services` without `pageSize` gives 20. Send `pageSize`
   on both and you get exactly that many.
4. **`salon.category` can be null**: a salon that has not said gents, ladies
   or unisex. The salon card on `/discover` is null for the same salon. Use
   your default look.
5. **`duration_min` and `duration_max` are the same number today.** A service
   has one duration on the platform. Show "30 min" when they are equal.
6. **`is_popular=true` on `/discover` wins over `sort`.** You do not send
   `sort` there, so nothing changes for you.

---

## 1. `GET /services`

```
GET /services?is_popular=true&latitude=25.1972&longitude=55.2744&radius=500&page=1&pageSize=10
GET /services?search=fade&latitude=25.1972&longitude=55.2744&radius=500&page=1&pageSize=20
GET /services?page=1&pageSize=20
```

| Param | Type | Notes |
| ----- | ---- | ----- |
| `is_popular` | boolean | `true`: most popular first (1.3). Missing or `false`: newest first |
| `search` | string | The service's name or its `category.label` holds it, case ignored (1.4). Empty is the same as missing. At most 100 characters |
| `latitude`, `longitude` | number | Send both or neither. Sent: only salons within `radius` of it |
| `radius` | number | Kilometres, as on `/discover`. Needs `latitude` and `longitude` |
| `page` | number | From 1 |
| `pageSize` | number | 1 to 50. Default 20 |

All can be sent together.

**Do not send `tenant_id`, `branch_id` or `category_id` here.** With any of
them, `GET /services` is one salon's menu, the old answer, as before.

### 1.1 Response: 200

```json
{
  "count": 42,
  "next": "https://api.gostyle.uk/api/v1/services?is_popular=true&page=2&pageSize=10",
  "previous": null,
  "results": [
    {
      "id": "66666666-6666-6666-6666-666666666660",
      "name": "The Gentleman's Cut",
      "description": "A classic haircut tailored to your preferences, including a wash.",
      "price": 180.0,
      "currency": "AED",
      "duration_min": 30,
      "duration_max": 30,
      "category": { "id": "55555555-5555-5555-5555-555555555551", "label": "Haircut & Styling" },
      "salon": {
        "id": "c6c248ab-f2cd-4f12-a31e-243c6e64b3b5",
        "name": "Del Capo Barber",
        "category": "gents"
      }
    }
  ]
}
```

| Field | Type | Notes |
| ----- | ---- | ----- |
| `id` | string | The service's id, as on `GET /salon/:id/services` |
| `name` | string | |
| `description` | string or null | |
| `price` | number | One visit, the salon's own price, in `currency`: the same number as its Services tab and the Service Detail screen |
| `currency` | string | The currency of `price` ("AED", "BDT"): the branch's own when set, else the service's |
| `duration_min`, `duration_max` | number | Minutes. The same number today (note 5) |
| `category` | `{ id, label }` | The chip the salon's Services tab files it under. Never null |
| `salon.id` | string | For `GET /salon/:salon_id/service/:service_id` |
| `salon.name` | string | The name on the salon's card |
| `salon.category` | string or null | `gents`, `ladies`, `unisex`, or null (note 4) |

Nothing matches, or no salon near: `200` with `"count": 0` and
`"results": []`.

### 1.2 Which services

- Every service on a public salon's menu: published, open to online booking.
  The same services the salon's Services tab shows.
- One result per salon and service: "Haircut" at two salons of one business
  is two results, each with its own salon and price.
- With `latitude` and `longitude`: only salons within `radius` km of them (a
  salon with no map pin is left out, as on `/discover`). Without them: every
  salon.

### 1.3 `is_popular=true`

- **Most booked first**: bookings of that service at that salon made in the
  last 30 days. Cancelled ones are left out, and so are holds that were
  never booked (expired) and routine visits that were skipped or moved.
  A no-show counts: it was booked.
- **Ties**: the salon's rating (higher first, a salon nobody rated after the
  rated ones), then the newest service.
- **At most 2 of one salon on a page.** The rest move to the next page. When
  too few other salons are left to fill a page, it is filled with the next
  ones in order, so every page but the last is full.

Without `is_popular`: newest service first.

### 1.4 `search`

- The service's `name`, or its `category.label`, holds the text, anywhere in
  it, case ignored: "colour" finds "Root Colour" by its name and "Balayage"
  filed under "Hair Colour".
- Not the description, not the salon's name (use `/discover?search=`).

---

## 2. `GET /discover?is_popular=true`

```
GET /discover?is_popular=true&category=all&latitude=25.1972&longitude=55.2744&radius=500&page=1&pageSize=10
```

| Param | Type | Notes |
| ----- | ---- | ----- |
| `is_popular` | boolean | `true`: most popular salons first. Missing or `false`: today's order |
| `pageSize` | number | New here: 1 to 50. Without it, 15 a page as today (note 3) |

The response does not change. Every other param narrows exactly as today.

**The order**: your weighted rating, highest first.

```
score = (v / (v + m)) * R + (m / (v + m)) * C
```

`R` the salon's rating, `v` its review count, `m` = 10, `C` the average
rating of every rated public salon. A salon with no reviews comes after
every rated one. Ties: more reviews first, then the nearest (when a position
is sent).

---

## 3. Errors

`422`, in the usual envelope, naming the param:

```json
{
  "detail": "Please correct the highlighted fields.",
  "code": "validation_error",
  "errors": [
    { "field": "pageSize", "code": "invalid", "message": "Must be a whole number from 1 to 50, got '51'." }
  ]
}
```

| When | `field` |
| ---- | ------- |
| `is_popular` is not true/false (`1`, `0`, `yes`, `no` are fine) | `is_popular` |
| `pageSize` is not a whole number from 1 to 50 | `pageSize` |
| `latitude` without `longitude` | `longitude` |
| `longitude` without `latitude` | `latitude` |
| `radius` without a position, or `radius` 0 or less | `radius` |
| `search` longer than 100 characters | `search` |

A `page` after the last one is `404`, as on `/discover`. No match is never an
error.

---

## 4. Your calls, checked

| Your call | Answer |
| --------- | ------ |
| `GET /services?is_popular=true&pageSize=10` (+ position) | Popular Services, up to 10 |
| `GET /discover?is_popular=true&pageSize=10` (+ position) | Popular Near You, up to 10 |
| `GET /services?search=<text>` (+ position) | Services list, 20 a page |
| `GET /discover?search=<text>` (+ position) | Salons list, 15 a page (send `pageSize` for another size) |
