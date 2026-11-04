# Mobile Discovery API: the complete reference

For: the mobile app team. Date: 2026-11-04.
Screens: Interactive Map, Map Filters (short and full), Search, Smart Search Matrix,
Search Result Salons, Search Result Services.

This is the one reference for every call those screens make: each route, each param,
each field, each error, with real answers taken from live on 2026-11-04. The
screen-by-screen wiring guide is `docs/MOBILE_DISCOVERY_FE.md`.

**Base URL:** `https://api.gostyle.uk/api/v1`
**Auth:** none needed, except the bell count (section 2.10). Send `Authorization: Bearer
<token>` when the customer is signed in; it changes nothing on these routes, and a bad or
expired token is a `401`.

**Live:** every route and field below is on (checked on live 2026-11-04: 40 of 40 checks
passed). Switches on: `MAP_FILTER_V1`, `MAP_COUNTRY_V1`, `MAP_PIN_LOGO_V1`,
`MAP_RATING_FIGMA_V1`, `MAP_FILTER_VERIFIED_V1`, `SEARCH_POPULAR_V1`,
`SERVICE_CURRENCY_V1`, `CARD_NEXT_OPENING_V1`, `SEARCH_RESULTS_V1`, `SEARCH_OPTIONS_V1`,
`SEARCH_SUGGEST_V1`, `NOTIFICATION_CENTER_V1`.

---

## Contents

1. Rules that hold everywhere
2. The routes
   - 2.1 `GET /discover/map`: the map pins
   - 2.2 `GET /me/map/filters`: the map's Filters drawer
   - 2.3 `GET /discover`: salon lists (Popular Near You)
   - 2.4 `GET /discover/{id}`: one salon card
   - 2.5 `GET /services`: services across salons (Popular Services)
   - 2.6 `GET /search/suggest`: suggestions while typing
   - 2.7 `GET /search/options`: the Smart Search lists
   - 2.8 `GET /search/salons`: Results, Salons tab
   - 2.9 `GET /search/services`: Results, Services tab
   - 2.10 `GET /notifications/unread-count`: the bell
3. Shared shapes: the salon card, the pin, the service result
4. The opening line ("Opens tomorrow at 10:00 AM")
5. Errors
6. TypeScript types
7. Which screen calls what
8. Not in this round
9. Try it

---

## 1. Rules that hold everywhere

- **Lists come in one page shape:** `{count, next, previous, results}`. `count` is every
  match, not only this page. `next` and `previous` are full URLs or `null`. Ask for
  `pageSize` 1 to 50 (default 20 on the search routes, 15 on `/discover`).
- **Ids are UUID strings.** Send back the `id` of a picked option, never its label.
- **Money:** `price` is a number in major units (`199.0`), one visit, that salon's own
  price, before VAT. Print it with its `currency` (`"AED"`, `"BDT"`).
- **Distances** are strings ready to print (`"2.7 km"`, `"850 m"`), and `null` when you
  sent no position.
- **Ratings:** a salon card's `rating` is a string (`"4.7"`), a pin's is a number (`4.7`).
  Both are `null` when nobody has reviewed the salon: show "New", never 0.0.
- **Positions:** `latitude` and `longitude` always go together; half a position is an
  error. The phone's own GPS fix on the map goes as `user_latitude` / `user_longitude`,
  rounded to 2 decimals.
- **Booleans** read `true`, `1`, `yes`, `on` and `false`, `0`, `no`, `off`.
- **`any`, an empty value, or a param left out** all mean "no filter".
- **Gender is strict:** `gents` returns gents salons only; a unisex salon appears only under
  `all`.
- **A salon with no data says null, never a made-up value:** no hours, no logo, no rating.

---

## 2. The routes

### 2.1 `GET /discover/map`: the map pins

```
GET /discover/map?latitude=25.2048&longitude=55.2708&latitudeDelta=0.12&longitudeDelta=0.08
    &zoom=12&limit=100&category=all&user_latitude=25.20&user_longitude=55.27
```

| Param | Type | Rule |
| ----- | ---- | ---- |
| `latitude`, `longitude` | number | the map's centre |
| `latitudeDelta`, `longitudeDelta` | number | the visible box around the centre |
| `zoom` | int | the map's zoom (read, not used for filtering) |
| `limit` | int | at most this many pins (the server caps at 500) |
| `radius` | number | **metres** (100 to 50000), a circle around the centre, on top of the box |
| `category` | `all`, `gents`, `ladies`, `unisex` | gender, strict |
| `user_latitude`, `user_longitude` | number, 2 decimals | the phone's GPS: only salons in the customer's country are shown (else the map centre's country) |
| `service_categories` | ids, comma separated | from the drawer (2.2): a salon with a service in any of them. An unknown id is ignored |
| `rating` | `5`, `4`, `3`, `2`, `1` | the salon's average is at least that (`5` = 5.0 only); unrated salons are left out |
| `availability` | `today`, `tomorrow`, `week` | the salon still has a free start in that window |
| `distance` | `5`, `10`, `20`, `100` | km from `origin_latitude`, `origin_longitude` (send all three together) |
| `additional` | `open_now`, `verified`, comma separated | open right now; marked verified by our staff |
| `verified` | `true` | the same as `additional=verified` |

Answer (live):

```json
{
  "mode": "markers",
  "count": 1,
  "venues": [
    {
      "id": "7dd88cda-9a17-4ac8-9f78-41c2903e23c8",
      "lat": 25.205047,
      "lng": 55.274148,
      "rating": null,
      "salon_profile_image": "https://.../95pb503hrvw.jpg",
      "logo_url": "https://.../71y7vby1tm4.png",
      "is_verified": false
    }
  ],
  "country": {"code": "AE", "name": "United Arab Emirates", "has_salons": true}
}
```

- `count`: "12 nearby spots". With drawer filters it is every match in view, before
  `limit`.
- Draw the pin with **`logo_url`**. `salon_profile_image` is the cover photo.
- `country` is `null` when no country could be told (the box only). `has_salons` is false
  when the customer's country has no salon at all: show "Not available in your country yet"
  rather than "No salons here".
- Errors: drawer params (`rating`, `availability`, `distance`, `additional`) with an id not
  offered, or `distance` without its origin: **400**. The map's own params (`latitude`,
  `radius` ...): **422**. Availability unreadable: **503**.

### 2.2 `GET /me/map/filters`: the map's Filters drawer

No params. Answer (live, the categories cut short):

```json
{
  "sections": [
    {"key": "service_categories", "title": "Service Categories", "multiple": true, "default": [],
     "options": [{"id": "45c1807b-f9d6-577e-8bb7-efccfff72e38", "label": "Hair"},
                 {"id": "44036904-c738-5277-bdab-48616c661319", "label": "Men's Grooming"}]},
    {"key": "rating", "title": "Rating", "multiple": false, "default": ["any"],
     "options": [{"id": "any", "label": "Any"}, {"id": "5", "label": "5"}, {"id": "4", "label": "4.0+"},
                 {"id": "3", "label": "3.0+"}, {"id": "2", "label": "2.0+"}, {"id": "1", "label": "1.0+"}]},
    {"key": "availability", "title": "Availability", "multiple": false, "default": ["any"],
     "options": [{"id": "any", "label": "Any"}, {"id": "today", "label": "Today"},
                 {"id": "tomorrow", "label": "Tomorrow"}, {"id": "week", "label": "This week"}]},
    {"key": "distance", "title": "Distance", "multiple": false, "default": ["any"],
     "options": [{"id": "any", "label": "Any"}, {"id": "5", "label": "5 km"}, {"id": "10", "label": "10 km"},
                 {"id": "20", "label": "20 km"}, {"id": "100", "label": "100 km"}]},
    {"key": "additional", "title": "Additional", "multiple": true, "default": ["open_now"],
     "options": [{"id": "open_now", "label": "Open now"}, {"id": "verified", "label": "Verified salons"}]}
  ]
}
```

- Each section's `key` is the param of `/discover/map` (and of the search routes); each
  option's `id` is a value it takes. `multiple: true`: send ids comma separated.
- **Service categories come from the salons' own menus**, not a fixed list: the labels are
  the salons' own words, and the list changes as salons edit their menus.
- **Show Results** = the same `/discover/map` call with the picks; read `count`.
- **Clear Filters** = every section back to its `default` (Additional opens with
  `open_now` on).

### 2.3 `GET /discover`: salon lists (Popular Near You)

| Param | Type | Rule |
| ----- | ---- | ---- |
| `latitude`, `longitude` | number | gives each card its `distance` |
| `radius` | number | **kilometres**, needs the position |
| `category` | `all`, `gents`, `ladies`, `unisex` | gender, strict |
| `search` (or `q`) | text, up to 100 | the salon's name or city |
| `city` | text | exact city, case ignored |
| `sort` | `distance`, `rating` | default `rating`; `distance` needs the position |
| `is_popular` | bool | the most popular first (a weighted rating); wins over `sort` |
| `rating_min` | number 0 to 5 | average at least this |
| `is_top_rated` | bool | 4.5+ with at least 3 reviews |
| `is_open_now` | bool | open right now |
| `hijab_mode` | bool | hijab-certified salons only |
| `verified` | `true` | only verified salons |
| `total_amount` | number | a basket total, to fill `deposit.amount` |
| `page`, `pageSize` | int | `pageSize` 1 to 50 (default 15) |

Answer: the page shape, each result **a salon card** (section 3.1).
Popular Near You: `GET /discover?is_popular=true&latitude=&longitude=&radius=10&pageSize=10`.

### 2.4 `GET /discover/{id}`: one salon card

`{id}` is the salon id from any list. Answer: one salon card (section 3.1). `404` when the
salon is not public.

### 2.5 `GET /services`: services across salons (Popular Services)

Without `tenant_id`, `branch_id` and `category_id` this is the list across every salon.
(With one of them it is one salon's old menu, a different shape: not used by these screens.)

| Param | Type | Rule |
| ----- | ---- | ---- |
| `is_popular` | bool | most booked in the last 30 days first, at most 2 of one salon on a page; else the newest first |
| `search` | text, up to 100 | the service's name or its category label |
| `latitude`, `longitude`, `radius` | number, km | only salons that close |
| `verified` | `true` | only the services of verified salons |
| `page`, `pageSize` | int | `pageSize` 1 to 50, default 20 |

Answer (live):

```json
{
  "count": 14,
  "next": "https://api.gostyle.uk/api/v1/services?is_popular=true&page=2&pageSize=1",
  "previous": null,
  "results": [
    {
      "id": "58c49adf-2781-40b3-8ec2-cb2beabe2575",
      "name": "Hair Cut & Spa",
      "description": null,
      "price": 100.0,
      "currency": "AED",
      "duration_min": 30,
      "duration_max": 30,
      "category": {"id": "710b14fe-cb1a-46ca-9425-5692b4322396", "label": "Hair Treatments"},
      "salon": {"id": "5d6a110f-5633-4bfe-942f-19be062a4373", "name": "Look Change",
                "category": "gents", "is_verified": false}
    }
  ]
}
```

Popular Services: `GET /services?is_popular=true&pageSize=10`.

### 2.6 `GET /search/suggest`: suggestions while typing

| Param | Type | Rule |
| ----- | ---- | ---- |
| `q` | text, 2 to 100 | what the customer typed |
| `latitude`, `longitude` | number | gives each salon its `distance` |
| `verified` | `true` | only verified salons in `salons` |

Answer (live, `q=hair`):

```json
{
  "salons": [],
  "services": [
    {"name": "Hair Cut", "salons": 1},
    {"name": "Hair Cut & Spa", "salons": 1},
    {"name": "Hair Spa Treatment", "salons": 1},
    {"name": "hair treatment", "salons": 1},
    {"name": "Balayage Hair Colour", "salons": 1}
  ],
  "stylists": []
}
```

| List | Each | Tap |
| ---- | ---- | --- |
| `salons` | `{id, name, logo_url, distance, is_verified}` | open the salon |
| `services` | `{name, salons}`: one per name across all salons, and how many offer it | open Results with `q=<name>` |
| `stylists` | `{id, name, photo_url, salon: {id, name}}` | open `/salon/{salon.id}/stylist/{id}` |

Up to 5 of each. Names starting with `q` first. Call it 300 ms after the last key, from 2
characters. Too many calls in a minute from one address: **429**.

### 2.7 `GET /search/options`: the Smart Search lists

No params. Answer (live):

```json
{
  "sections": [
    {"key": "tab", "title": "Search in", "multiple": false, "default": ["near_me"],
     "options": [{"id": "near_me", "label": "Near Me"}, {"id": "packages", "label": "Packages"}]},
    {"key": "quick", "title": "Quick picks", "multiple": false, "default": [],
     "options": [{"id": "cuts_fade", "label": "Cuts & Fade", "icon": null},
                 {"id": "color_styles", "label": "Color & Styles", "icon": null}]},
    {"key": "amenities", "title": "Amenities", "multiple": true, "default": [],
     "options": [{"id": "parking", "label": "Parking"}, {"id": "free_parking", "label": "Free Parking"},
                 {"id": "packages", "label": "Packages Use"}]},
    {"key": "service_tags", "title": "Services", "multiple": true, "default": [],
     "groups": [{"key": "color_treatments", "title": "Color & Treatments",
                 "options": [{"id": "keratin", "label": "Keratin", "icon": null}]}]}
  ]
}
```

- **Only chips with data are sent:** a chip that no salon can match today is left out, and
  so is an empty section or group. Read the list each time the screen opens.
- `service_tags` has `groups` (one row each, with its title) instead of `options`.
- `icon`: an icon name, or `null` (use your own icon for that id).
- `tab=near_me` needs the position: without location, send no `tab`.
- **Apply number** = `GET /search/salons?<picks>&pageSize=1`, read `count`.

### 2.8 `GET /search/salons`: Results, Salons tab

| Param | Type | Rule |
| ----- | ---- | ---- |
| `q` | text, up to 100 | the salon's name or city, **or a service on its menu** (name, category, tag) |
| `latitude`, `longitude` | number | `distance` on each card; needed by `distance`, `tab=near_me`, `sort=distance` |
| `tab` | `near_me`, `packages` | from 2.7 |
| `gender` | `all`, `gents`, `ladies`, `unisex` | strict |
| `service_categories`, `rating`, `availability`, `distance`, `additional` | the drawer's ids (2.2) | as on the map; `distance` measures from `latitude`, `longitude` |
| `verified` | `true` | only verified salons |
| `amenities` | `parking`, `free_parking`, `packages` | from 2.7; with several, all must hold |
| `quick` | one id from 2.7 | a salon with a service that matches the chip |
| `service_tags` | ids from 2.7 | a salon with a service that matches any of them |
| `sort` | `distance`, `rating`, `popular` | default `distance` with a position, else `rating` |
| `page`, `pageSize` | int | 1 to 50, default 20 |

Answer: the page shape, each result **a salon card** (section 3.1). Live, `q=thunder`
finds "Green Wave Salon" through its service "Love and thunder".

### 2.9 `GET /search/services`: Results, Services tab

The same params as 2.8 (the text, the categories, `quick` and `service_tags` match each
service; the other filters pick the salons), plus `sort=price`. Default sort: `distance`
with a position, else `popular`.

Answer (live, `q=cut&sort=price`, first result):

```json
{
  "count": 9,
  "next": "https://api.gostyle.uk/api/v1/search/services?latitude=25.2&longitude=55.27&page=2&pageSize=1&q=cut&sort=price",
  "previous": null,
  "results": [
    {
      "id": "826d7cf5-dbfc-4faf-b7ff-58527bb33d66",
      "name": "Kids Cut",
      "description": null,
      "price": 0.0,
      "currency": "BDT",
      "duration_min": 30,
      "duration_max": 30,
      "images": [],
      "category": {"id": "7461267c-be4c-4a58-bc86-f3cc55c9c322", "label": "Hair · Cut & Style"},
      "salon": {"id": "4aa5a05b-7237-4079-aa27-f006c11270f3", "name": "Romoni Studio",
                "logo_url": "https://.../c11g18u2rz.jpeg", "category": "gents",
                "distance": "3546.5 km", "rating": null, "is_verified": false}
    }
  ]
}
```

- `images`: up to 4 (the service's own photos, then the salon's gallery photos tagged with
  it); `[]` when none.
- `duration_min` and `duration_max` are the same number today: print "30 mins".
- `sort=price` compares the plain numbers; prices in different currencies are not converted.

### 2.10 `GET /notifications/unread-count`: the bell

Needs the token. Answer: `{"total": 3, "orders": 0, "updates": 3, "payments": 0,
"promotions": 0}`. Show `total` on the bell. No token: **401**.

---

## 3. Shared shapes

### 3.1 The salon card

Used by `/discover`, `/discover/{id}` and `/search/salons`. Live example (Romoni Studio,
URLs cut short):

```json
{
  "id": "4aa5a05b-7237-4079-aa27-f006c11270f3",
  "name": "Romoni Studio",
  "category": "gents",
  "logo_url": "https://.../c11g18u2rz.jpeg",
  "rating": null,
  "review_count": "0",
  "distance": null,
  "open": true,
  "opens_at": "9:00 AM",
  "closes_at": "9:00 PM",
  "next_opening": null,
  "has_story": false,
  "gallery": ["https://.../mfjzv8tvgwc.avif", "https://.../nawoeprhv9.avif"],
  "slug": "main-4",
  "branch_id": "b67fad90-6064-446a-8d80-411347c1f929",
  "tenant_id": "3c457f1c-e7d4-4c9b-b877-27146c834608",
  "city": "Dhaka",
  "coordinate": {"latitude": 23.741589, "longitude": 90.373978},
  "status": "OPEN",
  "hours_today": "9:00 AM - 9:00 PM",
  "status_line": "Closes at 9:00 PM",
  "cover_url": "https://.../zth1mwccel.jpg",
  "hijab_certified": false,
  "is_verified": false,
  "deposit": {"required": false, "label": "No Deposit", "percentage": null, "amount": null},
  "free_cancellation": false
}
```

| Field | Type | Meaning |
| ----- | ---- | ------- |
| `id` | string | the salon id, for every other call |
| `name` | string | the name the salon published |
| `category` | `gents`, `ladies`, `unisex`, null | the gender icon |
| `logo_url`, `cover_url` | string or null | |
| `rating` | string or null | `"4.7"`; null: no review yet ("New") |
| `review_count` | string | `"0"`, `"260"` |
| `distance` | string or null | null without a position |
| `open` | bool or null | null: the salon published no hours |
| `opens_at`, `closes_at`, `hours_today` | string or null | today's times, as printed |
| `status` | `OPEN`, `BUSY`, `WALK_INS`, `SPECIAL_HOURS`, `CLOSED`, null | set by the salon by hand, or worked out from the hours |
| `status_line` | string or null | "Closes at 9:00 PM" when open; the next opening when shut (section 4) |
| `next_opening` | ISO time with offset, or null | only when shut (section 4) |
| `gallery` | string[] | the photo strip; `[]` when none |
| `has_story` | bool | show the story ring |
| `hijab_certified` | bool | |
| `is_verified` | bool | our staff marked it verified: show the badge |
| `coordinate` | `{latitude, longitude}` or null | |
| `deposit` | object or null | `amount` is filled when you send `total_amount` |
| `free_cancellation` | bool | |
| `slug`, `branch_id`, `tenant_id`, `city` | | for links and the old menu routes |

### 3.2 The pin

`{id, lat, lng, rating (number or null), salon_profile_image (cover), logo_url, is_verified}`.

### 3.3 The service result

`/services` and `/search/services`: `{id, name, description, price, currency,
duration_min, duration_max, category {id, label}, salon {...}}`. `/search/services` adds
`images` and a fuller `salon`: `{id, name, logo_url, category, distance, rating,
is_verified}`. `/services`' salon is `{id, name, category, is_verified}`.

---

## 4. The opening line ("Opens tomorrow at 10:00 AM")

When `open` is `false`, `next_opening` is the moment the salon next opens, with the salon's
own offset, for example `2026-11-05T10:00:00+04:00`. It looks from now: later today when
today's opening is still ahead, else the next day the salon opens.

Read it in **that offset, not the phone's**, and print:

| `next_opening` falls on | Print |
| ----------------------- | ----- |
| today (in the salon's offset) | "Opens at 10:00 AM" |
| the next day | "Opens tomorrow at 10:00 AM" |
| later | "Opens Sunday at 10:00 AM" |

`status_line` already holds that sentence in English. For Arabic, build it yourself from
`next_opening`. `next_opening` is `null` when the salon is open, or opens on no day of the
coming week.

---

## 5. Errors

One envelope everywhere:

```json
{
  "detail": "Please correct the highlighted fields.",
  "code": "validation_error",
  "errors": [{"field": "rating", "code": "invalid_choice", "message": "Expected one of: any, 5, 4, 3, 2, 1. Got '4.5'."}]
}
```

| Status | When | Routes |
| ------ | ---- | ------ |
| 400 | a filter id the lists do not offer (`invalid_choice`); `distance` without its origin or position, `tab=near_me` without a position (`required`) | map, search salons and services |
| 422 | another param cannot be read: half a position (`required`), `q` too long, `q` under 2 characters on suggest (`too_short`), `pageSize` out of 1 to 50, `sort=distance` without a position, a bad `gender` or `sort` | all |
| 401 | the bell without a token; any route with a bad token | all |
| 404 | `/discover/{id}` of a salon that is not public | 2.4 |
| 429 | suggestions: too many calls in a minute from one address | 2.6 |
| 503 | `availability` sent and the busy times cannot be read right now (`errors[0].code: availability_unavailable`): retry, or drop `availability` | map, search salons and services |

---

## 6. TypeScript types

```ts
type Page<T> = { count: number; next: string | null; previous: string | null; results: T[] };
type Gender = "gents" | "ladies" | "unisex";
type Option = { id: string; label: string; icon?: string | null };
type Section = {
  key: string; title: string; multiple: boolean; default: string[];
  options?: Option[];
  groups?: { key: string; title: string; options: Option[] }[];   // service_tags only
};

type MapVenue = {
  id: string; lat: number; lng: number; rating: number | null;
  salon_profile_image: string | null; logo_url: string | null; is_verified: boolean;
};
type MapAnswer = {
  mode: "markers"; count: number; venues: MapVenue[];
  country: { code: string; name: string; has_salons: boolean } | null;
};

type SalonCard = {
  id: string; name: string; category: Gender | null;
  logo_url: string | null; cover_url: string | null;
  rating: string | null; review_count: string; distance: string | null;
  open: boolean | null; opens_at: string | null; closes_at: string | null;
  hours_today: string | null; next_opening: string | null; status_line: string | null;
  status: "OPEN" | "BUSY" | "WALK_INS" | "SPECIAL_HOURS" | "CLOSED" | null;
  gallery: string[]; has_story: boolean; hijab_certified: boolean; is_verified: boolean;
  coordinate: { latitude: number; longitude: number } | null;
  deposit: { required: boolean; label: string; percentage: number | null; amount: number | null } | null;
  free_cancellation: boolean;
  slug: string; branch_id: string; tenant_id: string; city: string | null;
};

type ServiceResult = {
  id: string; name: string; description: string | null;
  price: number; currency: string; duration_min: number; duration_max: number;
  category: { id: string; label: string };
  salon: { id: string; name: string; category: Gender | null; is_verified: boolean };
};
type SearchServiceResult = Omit<ServiceResult, "salon"> & {
  images: string[];
  salon: {
    id: string; name: string; logo_url: string | null; category: Gender | null;
    distance: string | null; rating: string | null; is_verified: boolean;
  };
};

type Suggestions = {
  salons: { id: string; name: string; logo_url: string | null; distance: string | null; is_verified: boolean }[];
  services: { name: string; salons: number }[];
  stylists: { id: string; name: string; photo_url: string | null; salon: { id: string; name: string } }[];
};

type ApiError = {
  detail: string; code: string;
  errors: { field: string | null; code: string; message: string }[];
};
```

---

## 7. Which screen calls what

| Screen | Action | Call |
| ------ | ------ | ---- |
| Interactive Map | pins, "12 nearby spots", gender chips | 2.1 |
| Interactive Map | bell badge | 2.10 |
| Map Filters | the chips | 2.2 |
| Map Filters | Show Results | 2.1 with the picks, `count` |
| Search | Recent searches, Clear All | on the phone (no call) |
| Search | Popular Services | 2.5 `is_popular=true` |
| Search | Popular Near You | 2.3 `is_popular=true` + position |
| Search | while typing | 2.6 |
| Smart Search | tabs, quick chips, amenities, sub tags | 2.7 |
| Smart Search | Apply number | 2.8 with the picks, `pageSize=1` |
| Results, Salons | list, count, sort, pages | 2.8 |
| Results, Services | list, count, sort, pages | 2.9 |
| any card | tap | 2.4, then the salon profile |

---

## 8. Not in this round

Decided by Sarwar (lead), 2026-11-04. **Hide these chips; no route offers or filters on
them:** UAE Regions / Major Hubs / Neighbourhood, Mall Shops, Home Service, Metro Near,
Membership Card, Offers (20 Pts) and 20pt Deals. Also later: products in the search bar,
and a real "20 to 30 mins" duration range.

Live data, today: no salon has reviews yet (ratings show "New"), no service has photos
(`images` is `[]`), and no salon is marked verified yet (staff do it in the admin).

---

## 9. Try it

```
B=https://api.gostyle.uk/api/v1
curl -s "$B/discover/map?latitude=25.2&longitude=55.27&latitudeDelta=1&longitudeDelta=1"
curl -s "$B/me/map/filters"
curl -s "$B/discover?pageSize=1"
curl -s "$B/services?is_popular=true&pageSize=3"
curl -s "$B/search/suggest?q=hair"
curl -s "$B/search/options"
curl -s "$B/search/salons?q=thunder"
curl -s "$B/search/services?q=cut&sort=price&latitude=25.2&longitude=55.27"
curl -s "$B/search/salons?pageSize=1&quick=cuts_fade"
```
