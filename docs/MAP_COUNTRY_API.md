# The map shows only the customer's country

For: the mobile team. Your brief is docs/map-country-fe-contract.md; this is
what the API does, built to it.

`GET https://api.gostyle.uk/api/v1/discover/map`, auth as today (send the
token when signed in). Behind the switch `MAP_COUNTRY_V1`: until it is on,
both new params are ignored and the answer has no `country` field.

## Request

Everything the map takes today, plus:

| Param | Type | Notes |
|---|---|---|
| `user_latitude` | number | The phone's own GPS fix, -90 to 90. Never the map's centre. |
| `user_longitude` | number | -180 to 180. Both or neither. |

```
GET /discover/map?latitude=25.2048&longitude=55.2708&latitudeDelta=0.12
    &longitudeDelta=0.08&zoom=12&limit=100&user_latitude=25.20&user_longitude=55.27
```

Send them rounded to 2 decimals. We round to 2 decimals as well before we
look anything up, so more decimals give the same answer. The position is used
to pick the country and nothing else: it is not stored, and it is cut out of
our access logs (nginx and gunicorn) before they are written.

`origin_latitude` and `origin_longitude` are unchanged: they are only for the
`distance` filter.

## Which country

First one that answers:

1. `user_latitude`, `user_longitude` sent and in a country: that country.
2. The map's centre (`latitude`, `longitude`) in a country: that country
   (location off).
3. Neither (the centre is out at sea, or not sent): no country filter, the
   box only, as before. `country` is `null`.

"In a country" = inside its borders, or else the nearest country within 25 km
(the coast, the marina, Palm Jumeirah). A phone out at sea, more than 25 km
from land, falls through to step 2.

Then: country, then the box, then your filter params, then `limit`. `count`
and `venues` only ever hold salons of that one country.

## Response: 200

As today, plus `country`:

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
| `country` | object or null | The country the salons were limited to. `null`: no country (step 3). |
| `country.code` | string | ISO 3166-1 alpha-2, upper case (`AE`). |
| `country.name` | string | English, for your empty message ("United Arab Emirates", "Bangladesh"). |
| `country.has_salons` | boolean | Any live salon in the whole country, ignoring the box and every filter. |

The two empty maps:

- `count: 0`, `has_salons: true`: salons exist in this country, not in view or
  not with these filters.
- `count: 0`, `has_salons: false`: GoStyle is not in this country yet.

While the switch is off there is no `country` key at all: treat a missing key
as "show the map as today".

## Your five cases

| The customer | What you get |
|---|---|
| In Dubai, zoomed out over the Gulf | UAE salons only, `country.code: "AE"` |
| In Dubai, pans to Muscat | `count: 0`, `country: {"code": "AE", ..., "has_salons": true}` |
| In Dhaka | `count: 0`, `country: {"code": "BD", "name": "Bangladesh", "has_salons": false}` |
| Phone in Riyadh, map opens on Dubai | Saudi salons, `country.code: "SA"` |
| Location off, map on Dubai | UAE salons, `country.code: "AE"` (from the centre) |

## Errors: 422

In the usual envelope. Only these are new:

```json
{
  "detail": "Please correct the highlighted fields.",
  "code": "validation_error",
  "errors": [
    {"field": "user_longitude", "code": "invalid", "message": "Send user_latitude and user_longitude together."}
  ]
}
```

| When | `field` | `message` |
|---|---|---|
| `user_latitude` without `user_longitude` | `user_longitude` | Send user_latitude and user_longitude together. |
| `user_longitude` without `user_latitude` | `user_latitude` | Send user_latitude and user_longitude together. |
| `user_latitude` outside -90 to 90 | `user_latitude` | Must be 90 or less, got 91.0. (or "-90 or more") |
| `user_longitude` outside -180 to 180 | `user_longitude` | Must be 180 or less, got 181.0. (or "-180 or more") |
| Not a number | that param | Expected a number, got 'abc'. |

No salon in the country or the box is never an error: `200`, `count: 0`,
`country` filled in. The filter params keep their own `400`s.

## Good to know

- **Borders**: Natural Earth 1:10m, held on our server, looked up offline. No
  outside service sees any position. Right to about a kilometre at a land
  border (Al Ain is the UAE, Buraimi next door is Oman), so a salon standing
  right on a land border could, rarely, count for the country next door.
- **Each salon's country** comes from its map pin, never its typed address,
  by the same borders as the customer. A new salon, or one that moves its pin,
  gets its country within a minute.
- **`has_salons`** is counted once a minute, so a brand new salon can take
  up to a minute to turn it to `true`.
- **Names** are Natural Earth's English short names: "United States",
  "United Kingdom", "China", "The Bahamas", "Ivory Coast".
- **`mode`** is still always `"markers"`: this route has no clustering yet,
  so "country before `limit`" is the rule that applies.
- **Older app builds** that do not send `user_latitude` get step 2 (the map's
  centre) once the switch is on.
