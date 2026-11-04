# Mobile Discovery: FE Guide (Map, Filters, Search, Smart Search, Results)

Date: 2026-11-04. For: the mobile app team.
Screens: Interactive Map, Map Filters (short and full), Search, Smart Search Matrix,
Search Result Salons, Search Result Services (the Figma "Discovery" frames).

This is the one page to wire those six screens. It says which call each screen makes and
which fields it reads. The field-by-field references are the guides it links to.

Base URL: `https://api.gostyle.uk/api/v1`. No token is needed on any call here except the
notification count; send it when the customer is signed in.

**Everything below is LIVE** (server switches on, 2026-11-04): `MAP_FILTER_V1`,
`MAP_COUNTRY_V1`, `MAP_PIN_LOGO_V1`, `MAP_RATING_FIGMA_V1`, `SEARCH_POPULAR_V1`,
`SERVICE_CURRENCY_V1`, `CARD_NEXT_OPENING_V1`, `SEARCH_RESULTS_V1`, `SEARCH_OPTIONS_V1`,
`SEARCH_SUGGEST_V1`, `NOTIFICATION_CENTER_V1`.

**Coming with its own switch:** Verified salons (`MAP_FILTER_VERIFIED_V1`, section 3.1). Until
the server team says it is on, hide the Verified chip and the badge.

---

## 0. Read this first

1. **Only show the chips the server sends.** The map drawer (`/me/map/filters`) and the
   Smart Search (`/search/options`) list their own chips. Some Figma chips have no data
   yet and are never sent (section 8). Do not hardcode them.
2. **Ids, not labels.** Send back the `id` of each picked option, never its label.
3. **Recent searches stay on the phone.** There is no server call for them; Clear All
   clears the phone's list.
4. **"Opens tomorrow at 10:00 AM" is built by the app** from `next_opening` (section 6).
5. **Money comes with its currency.** Print `currency` next to every `price` (AED, BDT ...).

---

## 1. Endpoints at a glance

| Screen | Call | Guide |
| ------ | ---- | ----- |
| Interactive Map: pins and "12 nearby spots" | `GET /discover/map` | `MOBILE_MAP_COUNTRY_FE.md`, `MAP_FILTERS_API.md` |
| Interactive Map: bell badge | `GET /notifications/unread-count` (token) | `NOTIFICATIONS_FE_GUIDE.md` |
| Map Filters: the chips | `GET /me/map/filters` | `MAP_FILTERS_API.md` |
| Map Filters: Show Results | `GET /discover/map` with the picks, read `count` | `MAP_FILTERS_API.md` |
| Search: Popular Services | `GET /services?is_popular=true` | `SEARCH_POPULAR_API.md` |
| Search: Popular Near You | `GET /discover?is_popular=true` + position | `SEARCH_POPULAR_API.md` |
| Search: while typing | `GET /search/suggest?q=` | `SEARCH_SUGGEST_API.md` |
| Smart Search: the chips | `GET /search/options` | `SEARCH_OPTIONS_API.md` |
| Smart Search: Apply number | `GET /search/salons` with the picks and `pageSize=1`, read `count` | `SEARCH_RESULTS_API.md` |
| Results: Salons tab | `GET /search/salons` | `SEARCH_RESULTS_API.md` |
| Results: Services tab | `GET /search/services` | `SEARCH_RESULTS_API.md` |

---

## 2. Interactive Map

```
GET /discover/map?latitude=25.2048&longitude=55.2708&latitudeDelta=0.12&longitudeDelta=0.08
    &zoom=12&limit=100&category=gents&user_latitude=25.20&user_longitude=55.27
```

- **Pins:** `venues[]` = `{id, lat, lng, rating, salon_profile_image, logo_url}`. Draw the
  pin with **`logo_url`** (the salon's logo; null: your placeholder). `salon_profile_image`
  is the cover photo, kept for the old build. `rating` is a number (4.5) or null.
- **"12 nearby spots":** `count`.
- **All / Gents / Ladies:** `category=all|gents|ladies`. Strict: a unisex salon shows only
  under All.
- **The "You" pin:** the phone's own GPS. Send it rounded to 2 decimals as
  `user_latitude` / `user_longitude`: the map then shows only the customer's country and
  answers `country` (see `MOBILE_MAP_COUNTRY_FE.md`).
- **The search bar** opens the Search screen (section 4).
- **The bell:** `GET /notifications/unread-count` with the token, read `total`.

---

## 3. Map Filters (short and full)

`GET /me/map/filters` lists the sections. Send each picked id back to `/discover/map` as
`<section key>=<id>[,<id>]`.

| Figma row | Section `key` | Ids now |
| --------- | ------------- | ------- |
| Service category | `service_categories` | from the salons' own menus (uuids). "All Services" = send none |
| Rating | `rating` | `any`, `5`, `4`, `3`, `2`, `1` (labels 5, 4.0+, 3.0+, 2.0+, 1.0+) |
| Availability | `availability` | `any`, `today`, `tomorrow`, `week` |
| Distance | `distance` | `any`, `5`, `10`, `20`, `100` (km), sent with `origin_latitude`, `origin_longitude` |
| Additional | `additional` | `open_now`; `verified` ("Verified salons") once `MAP_FILTER_VERIFIED_V1` is on |

- **Rating changed on 2026-11-04** to the Figma's chips: `4.5` is gone and is now a 400.
- **Show Results:** call `/discover/map` with the picks; `count` is every match, before
  `limit`.
- **Clear Filters:** back to each section's `default` (Additional opens with `open_now` on).

### 3.1 Verified salons (`MAP_FILTER_VERIFIED_V1`)

Our staff mark salons verified in the admin. With the switch on:

- The drawer's Additional section offers `{"id": "verified", "label": "Verified salons"}`.
  Send it back as `additional=verified` (or `verified=true`, the same thing).
- `verified=true` also works on `/discover`, `/services` (across salons),
  `/search/salons`, `/search/services` and `/search/suggest` (its salons list).
  `false`, anything else, or nothing: no filter.
- Every salon says **`is_verified`** (`true` / `false`): the map pins, the salon cards
  (`/discover`, `/discover/{id}`, the Salons tab), the Services tab's and `/services`'
  `salon`, and the suggested salons. Show the badge when it is `true`.

With the switch off none of this is there: no chip, no `is_verified` key, and
`additional=verified` is a 400.

---

## 4. Search

| Block | Call | Read |
| ----- | ---- | ---- |
| Recent searches | none: kept on the phone | |
| Popular Services | `GET /services?is_popular=true&pageSize=10` (+ position) | `name`, `price` **+ `currency`**, `description`, `salon.name` |
| Popular Near You | `GET /discover?is_popular=true&pageSize=10&latitude=&longitude=&radius=` | `logo_url`, `name` |
| While typing | `GET /search/suggest?q=<text>` (+ position) | `salons[]`, `services[]`, `stylists[]` |

**While typing** (new on 2026-11-04, replaces the two full lists of `MOBILE_SEARCH_FE.md`
section 6):

- Start at 2 characters, 300 ms after the last key. Drop an answer for an older text.
- One call, up to 5 of each: salons `{id, name, logo_url, distance}`, service names
  `{name, salons}`, stylists `{id, name, photo_url, salon {id, name}}`.
- Tap a salon: open it. Tap a stylist: `/salon/{salon.id}/stylist/{id}`. Tap a service
  name, or press Search: open the Results screen with `q=<text>` (section 6).
- A 429 means too many calls from this phone's address in a minute: slow down.

---

## 5. Smart Search Matrix

`GET /search/options` lists the chips, in the drawer's shape:

| Figma row | Section `key` | Send as | Ids today |
| --------- | ------------- | ------- | --------- |
| Near Me / Packages tabs | `tab` | `tab=near_me` (needs the position) or `tab=packages` | `near_me`, `packages` |
| Quick chips (Cuts & Fade ...) | `quick` | `quick=<id>` | e.g. `cuts_fade`, `color_styles` |
| Amenities | `amenities` | `amenities=<id>,<id>` | `parking`, `free_parking`, `packages` |
| Cuts & Styling / Beard & Shave / Color & Treatments | `service_tags` (`groups`) | `service_tags=<id>,<id>` | e.g. `fade`, `keratin` |

- The list only holds chips that some salon can match today, so it changes as salons add
  services: read it each time the screen opens. A row with no chip is not sent at all.
- **Apply:** open the Results screen with every pick. The number on the button is
  `GET /search/salons?<picks>&pageSize=1` -> `count`.
- **Clear Filters:** every section back to its `default`.
- Amenity labels: "Parking" (the salon lists parking, or shows the Free Parking badge),
  "Free Parking" (only the badge), "Packages Use" (the salon sells packages).

---

## 6. Search Results

Both tabs take the same params: `q`, `latitude`/`longitude`, `tab`, `gender`, the map
drawer's ids (`service_categories`, `rating`, `availability`, `distance`, `additional`),
the Smart Search's (`amenities`, `quick`, `service_tags`), `sort`, `page`, `pageSize`
(1 to 50, default 20). Page shape: `{count, next, previous, results}`; "17 results
found" is `count`.

### 6.1 Salons tab: `GET /search/salons`

Each result is the `/discover` salon card. `q` also finds a salon by a service on its
menu ("fade" finds the barbers that offer one).

| Figma | Field |
| ----- | ----- |
| logo, name | `logo_url`, `name` |
| rating, review count | `rating` ("4.7" or null: show "New"), `review_count` ("260") |
| distance | `distance` ("2.7 km", null without a position) |
| gender icon | `category`: `gents`, `ladies`, `unisex` or null |
| Open / Closed | `open` (true, false, or null when the salon published no hours) |
| "Closes at 8:00 PM" | `status_line` when open, or build it from `closes_at` |
| "Opens tomorrow at 10:00 AM" | build it from **`next_opening`** (below) |
| photo strip | `gallery` (list of URLs, can be empty) |

**The opening line (rule since 2026-11-04).** When `open` is false, `next_opening` is the
moment the salon next opens, with the salon's own offset (`2026-11-05T10:00:00+04:00`).
It looks from now: later today, else the next day it opens. Read it in the salon's
offset, not the phone's, and print:

- the same date as today in that offset: "Opens at 10:00 AM";
- the next date: "Opens tomorrow at 10:00 AM";
- later: "Opens Sunday at 10:00 AM".

`status_line` already carries the same sentence in English, for a quick start. Build it
yourself for Arabic. `next_opening` is null when the salon is open, or opens on no day of
the week.

Sort: `distance` (default with a position), `rating` (default without), `popular`.

### 6.2 Services tab: `GET /search/services`

```json
{
  "id": "...", "name": "The Gentleman's Cut", "description": "...",
  "price": 199.0, "currency": "AED", "duration_min": 20, "duration_max": 20,
  "images": ["https://...", "https://..."],
  "category": {"id": "...", "label": "Haircut"},
  "salon": {"id": "...", "name": "The Iron Razor Barbershop", "logo_url": "https://...",
            "category": "gents", "distance": "2.7 km", "rating": "4.7"}
}
```

- The collage: `images` (up to 4; `[]` when none: show your placeholder).
- "20 - 30 mins": `duration_min` and `duration_max` are the same number today. Show
  "20 mins" when equal.
- Price: `price` + `currency`, that salon's own price.
- Sort: `distance` (default with a position), `popular` (default without), `rating`,
  `price` (lowest first). Prices in different currencies are compared as plain numbers.

---

## 7. Errors

The usual envelope everywhere: `{detail, code, errors: [{field, code, message}]}`.

| Status | Meaning | What to do |
| ------ | ------- | ---------- |
| 400 | A filter id that the server's lists do not offer (`invalid_choice`), or `distance` / `tab=near_me` without a position (`required`) | reload the lists; drop the pick |
| 422 | Another param is wrong (half a position, `q` too long or too short, `pageSize`, `sort`) | fix the call |
| 429 | Suggestions only: too many calls in a minute | wait, keep typing |
| 503 | `availability` was asked and the busy times cannot be read right now | retry, or drop `availability` |

---

## 8. Hidden: not this round (never sent, do not show)

Decided by Sarwar (lead), 2026-11-04: **hide these 6 chips for now; the API will not
support them this round.** Membership and rewards are future work.

| Figma | Why |
| ----- | --- |
| UAE Regions, Major Hubs, Neighbourhood | not this round (no region list) |
| Mall Shops, Home Service | not this round (no salon records it) |
| Metro Near, Membership Card | not this round (no salon records it) |
| Offers (20 Pts) (and 20pt Deals) | not this round (points are per salon business) |
| Products in the search bar | later |

Verified salons is no longer in this list: see section 3.1.

---

## 9. QA checklist (live)

```
B=https://api.gostyle.uk/api/v1
curl -s "$B/discover/map?latitude=25.2&longitude=55.27&latitudeDelta=1&longitudeDelta=1"   # venues[].logo_url
curl -s "$B/me/map/filters"                                                             # rating: any 5 4 3 2 1
curl -s "$B/services?is_popular=true&pageSize=3"                                        # results[].currency
curl -s "$B/search/suggest?q=hair"                                                      # services: Hair Cut ...
curl -s "$B/search/options"                                                             # tab, quick, amenities, service_tags
curl -s "$B/search/salons?q=thunder"                                                    # Green Wave Salon (by its service)
curl -s "$B/search/services?q=cut&sort=price&latitude=25.2&longitude=55.27"             # cheapest first, currency, distance
curl -s "$B/search/salons?pageSize=1&quick=cuts_fade"                                   # count for the Apply button
```

- [ ] Map pins show the logo; a salon with no logo shows the placeholder.
- [ ] Rating chips are 5, 4.0+, 3.0+, 2.0+, 1.0+; picking one changes `count`.
- [ ] Popular Services prints the currency.
- [ ] Typing "ha" shows suggestions; a tap on a service name opens Results with `q`.
- [ ] Smart Search shows only the chips the server sends; Apply shows `count`.
- [ ] Results: Salons and Services tabs page with `next`; sort works.
- [ ] A closed salon reads "Opens tomorrow at ..." (or today / a day name), in the salon's time.
- [ ] No Verified, Mall, Home Service, Metro, Membership, points or region chip anywhere.
