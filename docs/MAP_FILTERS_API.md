# Map filters

Base URL: `https://api.gostyle.uk/api/v1`
Auth: none needed on either call. A Bearer token is accepted (send it when
signed in, as on `/discover/map` today), and a bad or expired one is `401`.

The agreed contract, 2026-11-02. It is the mobile team's
`map-filter-params-fe-contract.md` (kept as received) with what the server
really does. Two calls:

| Method | Path | What for |
| ------ | ---- | -------- |
| GET | `/me/map/filters` | The Filters drawer's sections and options |
| GET | `/discover/map` | The map, now with the drawer's picks as params |

Behind the switch `MAP_FILTER_V1`. Off, `/me/map/filters` is `404` and
`/discover/map` ignores the new params, exactly as today.

**Update 2026-11-04:** the Rating chips are now the Figma's: `5`, `4`, `3`, `2`, `1` (labels 5, 4.0+, 3.0+, 2.0+, 1.0+), switch `MAP_RATING_FIGMA_V1`, on. `4.5` is gone and is a `400 invalid_choice`. The examples below show the new chips. The whole Discovery flow, screen by screen, is `docs/MOBILE_DISCOVERY_FE.md`.

---

## Notes on your draft

Built as your draft asks. What to know:

1. **Filter errors are `400`, the map's own are still `422`.** The five
   filter params and their origin answer `400` as you asked, in the usual
   envelope (`detail`, `code`, `errors[]`). The params the map took before
   (`latitude`, `radius` ...) keep their `422`, as today. Codes in §4.
2. **A service category id is one category NAME across all salons.** On the
   platform every salon makes its own categories, so salon A's "Hair" and
   salon B's "Hair" are two rows with two ids, and one salon's id means
   nothing at another salon. The drawer lists each name once, and its id is a
   UUID made from the name, so the same name always gets the same id. A
   service is filed the way the salon's Services tab files it: under its
   category's parent when it has one ("Hair > Colouring" is "Hair").
3. **No "Verified" chip.** No salon carries a "verified by Go Style" mark on
   the platform yet, so the drawer leaves it out, as your §7 says, and
   `additional=verified` is refused (`400`, `invalid_choice`).
4. **The `/me/map/filters` shape is ours.** We did not have your
   `map-filters.md`, so §1 is built from this doc: each section's `key` is the
   param, each option's `id` a value. If your `map-filters.md` asks for
   another shape, send it and we match it.
5. **`availability` is per service category**, the full version from your
   §3, not the "any service" fallback from your §7.
6. **With a filter, `count` can be more than the venues sent.** It is every
   salon in view that matches, before `limit`, as your §4 says. A call with
   no filter keeps today's `count`.
7. **One extra answer: `503`** when `availability` is sent and we cannot read
   who is busy. See §4.

---

## 1. `GET /me/map/filters`

```
GET /api/v1/me/map/filters
```

### Response: `200 OK`

```json
{
  "sections": [
    {
      "key": "service_categories",
      "title": "Service Categories",
      "multiple": true,
      "default": [],
      "options": [
        { "id": "45c1807b-f9d6-577e-8bb7-efccfff72e38", "label": "Hair" },
        { "id": "ccee249c-52ab-520a-9705-95c05a9f04a9", "label": "Beard" }
      ]
    },
    {
      "key": "rating",
      "title": "Rating",
      "multiple": false,
      "default": ["any"],
      "options": [
        { "id": "any", "label": "Any" },
        { "id": "5", "label": "5" },
        { "id": "4", "label": "4.0+" },
        { "id": "3", "label": "3.0+" },
        { "id": "2", "label": "2.0+" },
        { "id": "1", "label": "1.0+" }
      ]
    },
    {
      "key": "availability",
      "title": "Availability",
      "multiple": false,
      "default": ["any"],
      "options": [
        { "id": "any", "label": "Any" },
        { "id": "today", "label": "Today" },
        { "id": "tomorrow", "label": "Tomorrow" },
        { "id": "week", "label": "This week" }
      ]
    },
    {
      "key": "distance",
      "title": "Distance",
      "multiple": false,
      "default": ["any"],
      "options": [
        { "id": "any", "label": "Any" },
        { "id": "5", "label": "5 km" },
        { "id": "10", "label": "10 km" },
        { "id": "20", "label": "20 km" },
        { "id": "100", "label": "100 km" }
      ]
    },
    {
      "key": "additional",
      "title": "Additional",
      "multiple": true,
      "default": ["open_now"],
      "options": [{ "id": "open_now", "label": "Open now" }]
    }
  ]
}
```

### Fields

| Field | Meaning |
| ----- | ------- |
| `key` | The param to send on `/discover/map` for this section |
| `title` | The section's heading |
| `multiple` | `true`: pick any number (send them comma separated). `false`: pick one |
| `default` | What the drawer opens with. `any` and `[]` mean "no filter" |
| `options[].id` | A value the param takes. Send it as it is |
| `options[].label` | What the chip says |

- The sections come in the drawer's order: categories, rating,
  availability, distance, additional.
- Single-choice sections start with `any`. Multi-choice ones have no `any`:
  nothing picked is no filter.
- **Service categories** are every category name that at least one public
  salon has a bookable service in, the most offered first. `label` is the
  spelling most salons use. The list follows the salons' menus, so load it
  when the drawer opens (it is cached for about a minute on our side).

---

## 2. `GET /discover/map`: the new params

Every param the map takes today (`category`, `latitude`, `longitude`,
`latitudeDelta`, `longitudeDelta`, `zoom`, `limit`, `radius`) stays as it is.
The response does not change: `mode`, `count`, `venues`.

| Param | From section | Value |
| ----- | ------------ | ----- |
| `service_categories` | `service_categories` | One or more ids, comma separated |
| `rating` | `rating` | One id |
| `availability` | `availability` | One id |
| `distance` | `distance` | One id, with `origin_latitude` and `origin_longitude` |
| `origin_latitude`, `origin_longitude` | | Where `distance` is measured from. Only with `distance` |
| `additional` | `additional` | One or more ids, comma separated |

Leave a param out for "no filter". `any`, and an empty value, are read as not
sent. Ids are read whatever their case.

The drawer as it first opens:

```
GET /api/v1/discover/map?latitude=25.2048&longitude=55.2708&latitudeDelta=0.12
    &longitudeDelta=0.08&zoom=12&limit=100&additional=open_now
```

Everything picked:

```
GET /api/v1/discover/map?category=gents&latitude=25.2048&longitude=55.2708
    &latitudeDelta=0.12&longitudeDelta=0.08&zoom=12&limit=100
    &service_categories=45c1807b-f9d6-577e-8bb7-efccfff72e38,ccee249c-52ab-520a-9705-95c05a9f04a9
    &rating=4
    &availability=today
    &distance=10&origin_latitude=25.1972&origin_longitude=55.2744
    &additional=open_now
```

`count` is every salon in view that matches the filters, before `limit`,
so it can be more than the venues sent. Nothing matches: `200` with
`"count": 0` and `"venues": []`. The order of the venues is as today.

---

## 3. What each param keeps

All params narrow together (AND), and together with the visible area and
`category`. The filters run first, then `limit`.

| Param | Keeps a salon when |
| ----- | ------------------ |
| `service_categories` | It has a service on its menu (published, open to online booking) in any of the categories (OR inside the list) |
| `rating` | Its rating is at least the value: `5` means 5.0 only. Salons with no rating yet are left out |
| `availability` | It has a start a customer could still book, on the salon's own clock: `today` from now to closing, `tomorrow` any time tomorrow, `week` from now to the end of the 7th day, today included. With `service_categories`, the start must be for a service in those categories |
| `distance` | Its straight-line distance from `origin_latitude`, `origin_longitude` is at most the value, in km |
| `additional=open_now` | It is open right now, by the same rule as `is_open_now` on `/discover` (and the card's `open`) |

**`availability` uses the slot picker's own rules**, so the map and the
booking screen agree. A start counts when `GET /booking/nearest-available`
would offer it: the salon open that day (its published hours, not closed by
hand), a stylist rostered there who can do the service alone, their break and
their bookings left out, the service's length and its notice (lead time), on
the 15-minute grid, inside booking-api's bookable day. A booking made a moment
ago shows on the map within about a minute.

**Unknown service category ids are ignored**, never refused: a category can
disappear after the app loaded the drawer. If every id is unknown, it is as
if the param were not sent.

---

## 4. Errors

All in the usual envelope:

```json
{
  "detail": "Please correct the highlighted fields.",
  "code": "validation_error",
  "errors": [
    { "field": "rating", "code": "invalid_choice", "message": "Expected one of: any, 5, 4, 3, 2, 1. Got '7'." }
  ]
}
```

| Case | Status | `errors[].field` | `errors[].code` |
| ---- | ------ | ---------------- | --------------- |
| `rating`, `availability` or `distance` with an id the drawer does not offer | `400` | that param | `invalid_choice` |
| `additional` with an id the drawer does not offer (e.g. `verified`) | `400` | `additional` | `invalid_choice` |
| `distance` without `origin_latitude` or `origin_longitude` | `400` | the missing one | `required` |
| `origin_latitude` or `origin_longitude` without `distance` | `400` | that param | `invalid` |
| An origin that is not a number, or off the globe | `400` | that param | `invalid` |
| `service_categories` with an id we no longer have | `200` | | the id is ignored |
| `any` in any param | `200` | | read as not sent |
| `availability` sent and we cannot read who is busy | `503` | `null` | `availability_unavailable` (top `code`: `service_unavailable`). Retry, or show the map without it |
| `/me/map/filters` with the switch off | `404` | | top `code`: `not_found` |
| One of the map's own params (`latitude`, `radius` ...) cannot be read | `422` | that param | `invalid`, as today |

---

## 5. Your open points (§7)

- **Availability per service category:** done, per category (point 6 above).
- **Verified:** left out until the platform has the mark (point 3 above).
