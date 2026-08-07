# Salon Discovery API — Mobile Handoff

Status: LIVE on staging (`http://156.67.214.42:3850`)
Auth: none required (public endpoints)
Swagger: `http://156.67.214.42:3850/api/docs/` (see `/api/v1/discover`)

These endpoints replace the old `/api/v1/salons` list for the Explore
screens (Interactive Map, Salon Card, Saloons List View, Hijab Mode list).
The old endpoint keeps working until the app has fully switched; after
that it will be removed.

## Endpoints

### 1. List salons

```
GET /api/v1/discover
```

Query parameters (all optional):

| Param      | Type  | Example  | Notes                                         |
| ---------- | ----- | -------- | --------------------------------------------- |
| lat        | float | 25.19    | User latitude. Send together with lng.        |
| lng        | float | 55.26    | User longitude.                               |
| sort       | str   | distance | `distance` (needs lat+lng) or default rating. |
| rating_min | float | 4.5      | Only salons with avg rating >= value.         |
| city       | str   | Dubai    | Case-insensitive exact match.                 |
| hijab_mode | str   | 1        | `1` = only hijab-certified salons.            |
| open_now   | str   | 1        | `1` = only salons open right now (salon tz).  |

Response is paginated (page size 15):

```json
{
  "count": 1,
  "next": null,
  "previous": null,
  "results": [
    {
      "id": "aaaaaaaa-1111-2222-3333-444444444444",
      "slug": "bella-jumeirah-main",
      "name": "Bella Jumeirah (Main)",
      "city": "Dubai",
      "rating": 4.5,
      "review_count": 2,
      "coordinate": { "latitude": 25.2048, "longitude": 55.2708 },
      "is_open_now": true,
      "hours_today": "13:00 - 22:00",
      "closes_at": "Closes at 10:00 PM",
      "photo_url": "https://picsum.photos/seed/bella/600/400",
      "hijab_certified": false,
      "deposit": { "required": true, "label": "20% Deposit", "percent": 20 },
      "distance_km": 2.0
    }
  ]
}
```

Use the `next` URL directly for infinite scroll; it is a full URL with
all current filters preserved. `?page=2` also works manually.

### 2. Salon detail

```
GET /api/v1/discover/{id}
```

Same card object as above (single object, not paginated). Only returns
salons that are publicly discoverable; hidden salons 404.

## Field mapping from the old endpoint

| Old (`/api/v1/salons`) | New (`/api/v1/discover`)      |
| ---------------------- | ----------------------------- |
| `open`                 | `is_open_now`                 |
| `hours`                | `hours_today` or `closes_at`  |
| `logo`                 | `photo_url`                   |
| `reviews`              | `review_count`                |
| `coordinate`           | `coordinate` (same shape)     |
| `distance_km`          | `distance_km` (same)          |
| `category`             | not available yet (see below) |

## Nullability rules

Every field except `id`, `slug`, `name`, `hijab_certified` can be null:

- `distance_km` is null unless both `lat` and `lng` were sent.
- `coordinate` is null if the branch has no location yet.
- `is_open_now` / `hours_today` / `closes_at` are null if the salon has
  not published opening hours.
- `rating` / `review_count` are null when there are no published reviews
  (treat as "New" in the UI rather than 0).
- `deposit` is null when the salon has no published policy. `deposit.label`
  is display-ready ("No Deposit" / "20% Deposit").

## Known gaps (backend WIP, do not block on these)

- `category` (Gents/Ladies filter) is not exposed yet.
- Gallery thumbnails (multiple photos) not exposed yet; only one
  `photo_url` per salon.
- Favorites endpoints are coming next; heart button can be built with
  local state for now.

## Who to ping

Backend: Imran (Rafa). Changes to this contract will be announced in the
team channel before deploy.
