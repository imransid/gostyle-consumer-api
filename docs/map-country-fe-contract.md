# The map shows only the customer's country: GET /discover/map

The mobile team's brief, as received on 2026-11-03. What we built from it is
docs/MAP_COUNTRY_API.md.

Base URL: `https://api.gostyle.uk/api/v1`. Auth: as /discover/map today. Send the token when signed in.

Date: 2026-11-03. From: the mobile team. For the map on the home tab (`src/app/(tabs)/index.tsx`). Builds on discover-map-filtering.md; nothing in it changes.

## 0. What we are asking for

Two new optional params on `GET /discover/map`: `user_latitude` and `user_longitude`, the phone's own position.

From them, work out the customer's country, on your side, and return only salons in that country and inside the map's box (`latitude`, `longitude`, `latitudeDelta`, `longitudeDelta`).

One new field in the response: `country`.

Every param the map takes today stays, the filter params included.

## 1. Why

Today the map returns every salon inside the box. Zoomed out, the box crosses borders, and salons from other countries show. A customer can only book where they are, so the map should never mix countries:

| The customer | Today | After |
|---|---|---|
| In Dubai, zoomed out over the Gulf | UAE, Oman and Qatar salons | UAE salons only |
| In Dubai, pans the map to Muscat | Muscat salons | Nothing, `has_salons: true` (5) |
| In Dhaka, where GoStyle has no salons | Whatever is in the box | Nothing, `has_salons: false` (5) |
| Flies to Riyadh and opens the app | Salons around the map's box | Saudi salons: their phone is in Riyadh |
| Location off, map opens on Dubai | Salons around the map's box | UAE salons, from the map's centre (3) |

## 2. Request

```
GET /discover/map?latitude=25.2048&longitude=55.2708&latitudeDelta=0.12
    &longitudeDelta=0.08&zoom=12&limit=100&user_latitude=25.20&user_longitude=55.27
```

| Param | Type | Required | Notes |
|---|---|---|---|
| `user_latitude` | number | no | The phone's GPS fix, rounded to 2 decimals (about 1 km) |
| `user_longitude` | number | no | Both or neither. Missing: the customer shared no location |

Always the phone's fix, never the map's centre. The app sends them on every map call when it has a fix, however far the map has moved.

Why not `origin_latitude` and `origin_longitude`. Those stay as they are, for distance only. When the phone has no fix the app fills them with the map's centre, so they cannot say where the customer is.

2 decimals is enough for a country and keeps the exact position off the wire. Use it to pick the country only; do not store or log it.

## 3. Which country

In this order, first one that answers:

1. `user_latitude` and `user_longitude` are sent and fall in a country: that country.
2. The map's centre (`latitude`, `longitude`) falls in a country: that country. This is the customer with location off. The app opens on Dubai, so they see the UAE.
3. Neither does (the centre is out at sea): no country filter, the box only, as today. `country` is null.

"Falls in a country": the point is inside its borders, or, if no country contains it, the nearest country within 25 km. The second part catches the coast, the marina and reclaimed land such as Palm Jumeirah, which land-only border data often leaves out.

### 3.1 Doing the lookup

Offline, on your server, against country borders you hold: for example a countries table in PostGIS (Natural Earth admin-0 or geoBoundaries) with a GiST index, `ST_Covers` for "inside" and `ST_DWithin` plus `ORDER BY geom <-> point` for "nearest within 25 km".

Not a geocoding API per request (Google and the like): slower, paid, rate limited, and it sends customers' positions to a third party.

Cache it by the rounded position. Borders do not move: a day or more is fine.

## 4. The query

### 4.1 Every salon gets a country

A `country_code` column on salons: ISO 3166-1 alpha-2, upper case (`AE`).

Set whenever a salon's location is saved, from its coordinates, with the same lookup as section 3. The customer and the salon are then judged by the same borders, and a mistyped address cannot hide a salon.

Backfill every existing salon once, the same way.

Index it together with whatever the box query already uses.

### 4.2 Order of work

1. Resolve the country (3).
2. `country_code = :country` and inside the box you already compute from `latitude`, `longitude` and both deltas.
3. The filter params, as in discover-map-filtering.md.
4. Clustering (`mode: "clusters"`), then `limit`.

The country comes before clustering and before `limit`, so a cluster never counts a salon across the border and `limit` is never spent on one.

### 4.3 Caching

If you cache map responses (discover-map-filtering.md suggests 60 s), key them on the country code, the box and the filters, never on `user_latitude` and `user_longitude`. Everyone in the UAE looking at the same box shares one entry.

## 5. Response: 200 OK

Unchanged, plus `country`:

```json
{
  "mode": "markers",
  "count": 12,
  "venues": ["...as today..."],
  "country": {
    "code": "AE",
    "name": "United Arab Emirates",
    "has_salons": true
  }
}
```

| Field | Type | Notes |
|---|---|---|
| `country` | object or null | The country the results were limited to. null: no filter (3, step 3) |
| `country.code` | string | ISO 3166-1 alpha-2 |
| `country.name` | string | In English, for the app's empty message |
| `country.has_salons` | boolean | Any live salon in the whole country, ignoring the box and the filters |

`has_salons` lets the app tell two empty maps apart:

- `count: 0`, `has_salons: true`: there are salons, just not in view or not with these filters. The app says so and offers to go back to the customer.
- `count: 0`, `has_salons: false`: GoStyle is not in this country yet. The app says "GoStyle isn't in Bangladesh yet" instead of an empty map.

A per-country count of live salons, cached, is all it needs.

## 6. What the app will do

- Send `user_latitude` and `user_longitude`, rounded to 2 decimals, on every /discover/map call while it has a fix. Leave them out when it has none (permission off, GPS off, a guest who declined).
- Keep sending everything else exactly as today.
- Read `country` for the two empty messages in section 5. Pins are drawn from `venues` as today.

## 7. Errors

In the envelope every other route uses:

- `user_latitude` without `user_longitude`, or the other way round: 422.
- `user_latitude` outside -90 to 90, or `user_longitude` outside -180 to 180: 422.
- No salon in the country or the box is never an error: 200 with `"count": 0` and `country` filled in.
