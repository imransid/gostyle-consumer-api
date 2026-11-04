# Search Results: the Salons and Services tabs

For the mobile team. Two new routes for the Search Results screen and the Smart Search
"Apply" button. Behind the switch `SEARCH_RESULTS_V1`: while it is off, both answer 404.

| Route | Tab |
| ----- | --- |
| `GET /api/v1/search/salons` | Salons |
| `GET /api/v1/search/services` | Services |

No token needed (send it when you have one, it changes nothing here).
`/discover`, `/discover/map` and `/services` are not changed by these routes.

---

## 1. Params (both routes)

All optional. Ids are the same ones `GET /api/v1/me/map/filters` serves, so the map
drawer's picks can be sent here as they are.

| Param | Value | Meaning |
| ----- | ----- | ------- |
| `q` | text, up to 100 characters | Salons tab: the salon's name or city, **or a service on its menu** (its name, its category label, or one of its tags). Services tab: the service's name, category label or tag. Case is ignored |
| `latitude`, `longitude` | the phone's position, both or neither | Gives each result its `distance`. Needed by `distance`, `tab=near_me` and `sort=distance` |
| `tab` | `near_me`, `packages` | `near_me`: nearest first (needs the position). `packages`: salons whose Packages tab lists at least one package |
| `gender` | `all`, `gents`, `ladies` (`unisex` also works) | Strict, as on the map: a unisex salon shows only under `all` |
| `service_categories` | category ids from `/me/map/filters`, comma separated | A salon with a service in any of them. On the Services tab, only the services in them. An id that is gone is ignored |
| `rating` | an id of the drawer's Rating section | The salon's average is at least that (`5` = 5.0 only). Salons with no reviews are left out |
| `availability` | `today`, `tomorrow`, `week` | The salon still has a free start in that window, as on the map |
| `distance` | `5`, `10`, `20`, `100` (km) | Within that many km of `latitude`, `longitude` |
| `additional` | `open_now`; `verified` with `MAP_FILTER_VERIFIED_V1` on | Open right now, on the salon's own clock; marked verified by our staff |
| `verified` | `true` (switch `MAP_FILTER_VERIFIED_V1`) | Only verified salons. `false` or anything else: no filter. Each card and each result's `salon` then also says `is_verified` |
| `amenities` | `parking`, `free_parking`, `packages`, comma separated | `parking`: the salon lists parking or shows the Free Parking badge. `free_parking`: only the badge (the only place that says free). `packages`: as `tab=packages`. With several, all must hold |
| `quick`, `service_tags` | ids from `GET /search/options` | The Smart Search's word chips (switch `SEARCH_OPTIONS_V1`). See `docs/SEARCH_OPTIONS_API.md` |
| `sort` | Salons: `distance`, `rating`, `popular`. Services: also `price` | See section 4 |
| `page`, `pageSize` | `page` from 1; `pageSize` 1 to 50, default 20 | |

`any` (or an empty value) means "no filter" for `tab`, `rating`, `availability` and
`distance`, so you can send the drawer's defaults as they are.

The filters add up: every one sent must hold.

---

## 2. `GET /api/v1/search/salons`

Answer: `{count, next, previous, results}`, the same page shape as `/discover`.
Each result is **the same salon card as `/discover`** (logo, name, rating, review count,
distance, `category` for the gender icon, `open`, `closes_at`, `status_line`,
`next_opening`, `gallery`, and the rest).

```
GET /api/v1/search/salons?q=fade&latitude=25.2048&longitude=55.2708&gender=gents&rating=4&pageSize=20
```

`count` is every salon that matches, not only this page. For the Smart Search
**Apply** button ("17 results"), call with `pageSize=1` and read `count`.

---

## 3. `GET /api/v1/search/services`

Answer: `{count, next, previous, results}`. The services of the salons the same filters
keep. `q` and `service_categories` are matched on each service.

```
GET /api/v1/search/services?q=fade&latitude=25.2048&longitude=55.2708&pageSize=10
```

One result:

```json
{
  "id": "66666666-6666-6666-6666-666666666660",
  "name": "The Gentleman's Cut",
  "description": "A classic haircut tailored to your preferences.",
  "price": 199.0,
  "currency": "AED",
  "duration_min": 20,
  "duration_max": 20,
  "images": ["https://.../1.jpg", "https://.../2.jpg"],
  "category": {"id": "55555555-5555-5555-5555-555555555551", "label": "Haircut"},
  "salon": {
    "id": "33333333-3333-3333-3333-333333333333",
    "name": "The Iron Razor Barbershop",
    "logo_url": "https://.../logo.png",
    "category": "gents",
    "distance": "2.7 km",
    "rating": "4.7"
  }
}
```

- `price` is that salon's price, before VAT, the same number as on the salon's Services
  tab. `currency` is the currency of that price.
- `duration_min` and `duration_max` are the same number for now (one duration per
  service). Show "20 mins" when they are equal.
- `images`: up to 4 photos, the service's own first, then the salon's gallery photos
  tagged with it. `[]` when there are none.
- `salon.distance` is null without a position. `salon.rating` is null when nobody has
  reviewed the salon yet (never "0.0").

---

## 4. Order

| `sort` | Salons tab | Services tab |
| ------ | ---------- | ------------ |
| `distance` | nearest first (needs the position) | the salon's distance, nearest first |
| `rating` | best rated first, unrated last | the salon's rating |
| `popular` | the weighted rating of `/discover?is_popular=true` | most booked in the last 30 days, at most 2 services of one salon on a page |
| `price` | (not offered) | lowest price first |

Default when `sort` is not sent: `distance` with a position. Without one: `rating`
(Salons) and `popular` (Services). The order never changes between pages.

---

## 5. Errors

Same envelope as every other route: `{detail, code, errors: [{field, code, message}]}`.

| Status | When |
| ------ | ---- |
| 400 | An id the lists do not offer, in `tab`, `rating`, `availability`, `distance`, `additional` or `amenities` (`invalid_choice`). Or `distance` / `tab=near_me` without a position (`required`). The same 400 as the map |
| 422 | Any other param cannot be read: `latitude` without `longitude` (`required`), `q` too long, `gender`, `sort` (also `sort=distance` without a position), `pageSize` |
| 503 | `availability` was sent and the busy times cannot be read right now (`errors[0].code: availability_unavailable`). Try again, or drop `availability` |
| 404 | The switch is off |

---

## 6. Not here yet

- The Smart Search option lists (tabs, quick chips, sub tags, amenities, labels) are
  `GET /api/v1/search/options`: see `docs/SEARCH_OPTIONS_API.md`.
- Regions, hubs and neighbourhoods, Mall Shops, Home Service, Metro Near, Membership
  Card, points offers and "Verified" have no data yet. Do not show those chips.
- Recent searches stay on the phone.
