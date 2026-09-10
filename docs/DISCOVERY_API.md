# Salon Discovery API — Mobile Handoff

Status: LIVE on staging (`https://api.gostyle.uk`)
Auth: none required (public endpoints)
Swagger: `https://api.gostyle.uk/api/docs/` (see `/api/v1/discover`)

These endpoints back the Explore screens: Interactive Map, Salon Card,
Salons List View and Hijab Mode.

---

## 1. List salons

```
GET /api/v1/discover
```

### Query parameters — all optional

| Param          | Type   | Example  | Notes                                                          |
| -------------- | ------ | -------- | -------------------------------------------------------------- |
| `latitude`     | float  | 25.19    | Send together with `longitude`.                                |
| `longitude`    | float  | 55.26    | Send together with `latitude`.                                 |
| `radius`       | float  | 5        | **KILOMETRES.** Needs latitude + longitude. Must be > 0.       |
| `category`     | enum   | gents    | `all` \| `gents` \| `ladies` \| `unisex`. `all` = no filter.   |
| `search`       | string | iron     | Salon name (the published one) or city. Max 100 chars.         |
| `is_top_rated` | bool   | true     | 4.5+ average with at least 3 reviews.                          |
| `is_open_now`  | bool   | true     | Open right now, in the salon's own timezone.                   |
| `hijab_mode`   | bool   | true     | Hijab-certified salons only.                                   |
| `page`         | int    | 2        | Page number.                                                   |
| `page_size`    | int    | 30       | Cards per page. Default 15, **capped at 50**.                  |
| `sort`         | enum   | distance | `distance` (needs a location) or `rating`. Default `rating`.   |
| `city`         | string | Dubai    | Exact match, case-insensitive.                                 |
| `rating_min`   | float  | 4.5      | Average rating floor, 0–5.                                     |
| `total_amount` | float  | 250      | Basket total, used to fill in `deposit.amount`.                |

Booleans accept `true`/`1`/`yes`/`on` and `false`/`0`/`no`/`off`.

**`category` is strict.** `gents` returns gents salons and nothing else — a
unisex salon is not folded in, and shows up only under `all`. That is why the
`category` field in the response can say `"unisex"` even though the filter
offers three choices.

### Response

The envelope is unchanged. The **object inside `results` is new** — see the
breaking changes below.

```json
{
  "count": 1,
  "next": null,
  "previous": null,
  "results": [
    {
      "id": "33333333-3333-3333-3333-333333333333",
      "name": "The Iron Razor Barbershop",
      "category": "gents",
      "logo_url": "https://…/logo.png",
      "rating": "4.5",
      "review_count": "12",
      "distance": "3.5 km",
      "open": true,
      "opens_at": "10:00 AM",
      "closes_at": "10:00 PM",
      "has_story": false,
      "gallery": ["https://…/1.jpg", "https://…/2.jpg"],

      "slug": "iron-razor-jumeirah",
      "city": "Dubai",
      "coordinate": { "latitude": 25.2213, "longitude": 55.2621 },
      "status": "OPEN",
      "hours_today": "10:00 AM - 10:00 PM",
      "status_line": "Closes at 10:00 PM",
      "cover_url": "https://…/cover.jpg",
      "hijab_certified": false,
      "deposit": { "required": true, "label": "20% Deposit", "percentage": 20, "amount": 50 },
      "free_cancellation": true
    }
  ]
}
```

Use the `next` URL directly for infinite scroll; it is a full URL with every
current filter preserved, `page_size` included.

### TypeScript

```ts
type Paginated<T> = { count: number; next: string | null; previous: string | null; results: T[] };

type Salon = {
  id: string;
  name: string;
  category: "gents" | "ladies" | "unisex" | null;
  logo_url: string | null;
  rating: string | null;         // "4.5" — null when nobody has reviewed it
  review_count: string;          // "0", "12" — always present
  distance: string | null;       // "3.5 km" / "850 m" — null without latitude/longitude
  open: boolean | null;          // null when the salon published no hours
  opens_at: string | null;       // "10:00 AM" — TODAY's opening time
  closes_at: string | null;      // "10:00 PM" — TODAY's closing time
  has_story: boolean;
  gallery: string[];             // always an array, possibly empty
};
```

`GET /api/v1/discover` returns `Paginated<Salon>`, not `Salon[]`.

---

## Breaking changes in this release

Three fields changed meaning or type.

| Field          | Was                       | Now                                     |
| -------------- | ------------------------- | --------------------------------------- |
| `closes_at`    | `"Closes at 10:00 PM"`    | `"10:00 PM"` — the time only            |
| `rating`       | `4.5` (number)            | `"4.5"` (string)                        |
| `review_count` | `2` (number) or `null`    | `"2"` (string), never null              |

The old `closes_at` sentence now lives in **`status_line`**, which also
carries `"Opens at 9:00 AM"` when the salon is shut.

`name` is now the name the salon **published**, falling back to the branch
name. The profile screen always used the published name; the card used the
branch name, so one salon could appear under two names one tap apart.

### Removed on 2026-09-10

Deprecated in the previous release. No longer sent, no longer read.

| Removed               | Kind           | Use instead              |
| --------------------- | -------------- | ------------------------ |
| `is_open_now`         | response field | `open`                   |
| `distance_km`         | response field | `distance`               |
| `gallery_urls`        | response field | `gallery`                |
| `lat` / `lng` / `lon` | query param    | `latitude` / `longitude` |
| `open_now`            | query param    | `is_open_now`            |

`is_open_now` is still the **query parameter**; only the response field of that
name is gone. `lat`, `lng` and `lon` are gone from `/discover/map` as well.

Sending a removed parameter is a **422** naming its replacement, not a silent
no-op. Sent empty (`?lat=`) it counts as absent, like any other parameter.

```json
{ "field": "lat", "code": "invalid", "message": "lat was renamed to latitude." }
```

---

## Bad parameters now fail loudly

A parameter that is **absent or empty is ignored** — `?latitude=&longitude=`
is a plain list request, which is what the app sends when the customer denies
location permission.

A parameter that **was sent and cannot be read is a 422** in the standard
error envelope, naming the parameter:

```json
{
  "detail": "Please correct the highlighted fields.",
  "code": "validation_error",
  "errors": [
    { "field": "radius", "code": "invalid", "message": "Needs latitude and longitude to measure from." }
  ]
}
```

This is a behaviour change. Previously `?latitude=25,19` (comma decimal) silently
dropped the location and returned the national list with a `200`. Cases that
now 422:

- a latitude, longitude, radius or rating that is not a number, or is `nan`/`inf`
- one of `latitude`/`longitude` without the other
- `radius` without a location, or `radius=0`
- `category` outside the four allowed values
- a boolean that is neither true-ish nor false-ish (`is_open_now=maybe`)
- `sort=distance` without a location
- a removed parameter: `lat`, `lng`, `lon` or `open_now` (see above)

---

## Nullability

Only `id`, `slug`, `review_count`, `has_story`, `hijab_certified`,
`free_cancellation` and `gallery` are guaranteed non-null.

- `rating` — null until someone reviews the salon. **Show "New", not 0.0.**
  `review_count` is `"0"` beside it, which is the real fact.
- `distance` — null unless both `latitude` and `longitude` were sent.
- `coordinate` — null if the branch has no pin yet.
- `open` — `null` means "this salon has published no hours", which is NOT the
  same as `false` ("shut right now"). Hide the badge on null; do not print
  "Closed".
- `opens_at` / `closes_at` / `hours_today` / `status_line` — null when the
  salon published no hours, or when a manager has manually set the salon to
  CLOSED for the day.
- `logo_url` — null when the salon uploaded no logo. It is **strictly** the
  LOGO asset; if you want a fallback, use `cover_url` on your side. The API
  will not serve a cover photo under the name `logo_url`.
- `category` — null when the salon has not declared one on the platform.
- `deposit` — null when the salon has no published policy. `deposit.label` is
  display-ready.

---

## Things worth knowing

**`opens_at` is TODAY's opening time.** At 11pm on a salon that shut at 10,
`opens_at` still reads `10:00 AM` — this morning's, not tomorrow's. A real
"next opening" has to cross midnight, skip closed days and honour dated
exceptions, and this schema version cannot read exceptions yet. Render off
`open` + `status_line` rather than treating `opens_at` as a countdown.

**`has_story` has no content endpoint yet.** The flag is real — it means the
salon has an unexpired, undeleted story — but there is no
`GET /salon/:id/stories` to open. Build the ring, leave the tap inert, and
ping backend when you need the content.

**`is_open_now` is computed in Python, not SQL**, because it depends on each
salon's own timezone and a JSONB hours grid. It is the one filter whose cost
grows with the catalogue rather than with `page_size`. Combine it with
`radius` or `category` where you can.

---

## 2. Salon detail

```
GET /api/v1/discover/{id}
```

The same card object, as a single object rather than a page. Salons that are
not publicly discoverable 404.

## 3. Map markers

```
GET /api/v1/discover/map?latitude=…&longitude=…&latitudeDelta=…&longitudeDelta=…[&category=…]
```

Lightweight markers for a viewport: `{ mode, count, venues[] }`. All params
optional; the bounding box replaces pagination and a hard `LIMIT 500` inside
the selector is the safety valve. `category` accepts the same four values.

---

## Still not available

- Favorites endpoints. Build the heart with local state for now.
- Story content (see above).
- Per-stylist ratings, years of experience, day off — no column holds them.

## Who to ping

Backend: Imran (Rafa). Changes to this contract are announced in the team
channel before deploy.
