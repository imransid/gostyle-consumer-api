# Search suggestions while typing

For the mobile team. `GET /api/v1/search/suggest`: the short list under the search bar
while the customer types. Behind the switch `SEARCH_SUGGEST_V1`: while it is off, it
answers 404.

No token needed.

---

## 1. Call

| Param | Value |
| ----- | ----- |
| `q` | what the customer typed, 2 to 100 characters |
| `latitude`, `longitude` | optional, the phone's position, both or neither: gives each salon its `distance` |
| `verified` | optional, `true`: only verified salons in `salons` (switch `MAP_FILTER_VERIFIED_V1`; each salon then also says `is_verified`) |

```
GET /api/v1/search/suggest?q=fad&latitude=25.2048&longitude=55.2708
```

Wait about 300 ms after the last key before calling, and drop an answer that comes back
for an older `q`.

---

## 2. Answer

Up to 5 of each. Each list can be empty.

```json
{
  "salons": [
    {"id": "33333333-3333-3333-3333-333333333333", "name": "Fade Lab Barbers",
     "logo_url": "https://.../logo.png", "distance": "2.7 km"}
  ],
  "services": [
    {"name": "Skin Fade", "salons": 12},
    {"name": "Fade with Beard Sculpt", "salons": 3}
  ],
  "stylists": [
    {"id": "99999999-9999-9999-9999-999999999990", "name": "Fadi Haddad",
     "photo_url": "https://.../face.jpg",
     "salon": {"id": "33333333-3333-3333-3333-333333333333", "name": "Fade Lab Barbers"}}
  ]
}
```

| List | What it holds | Tap |
| ---- | ------------- | --- |
| `salons` | salons whose name or city holds `q` (as `/discover?search=`). `distance` is null without a position | open the salon (`/discover/{id}` or the profile) |
| `services` | one entry per service **name** across all salons, with how many salons offer it | run the search with that name: `/search/salons?q=<name>` and `/search/services?q=<name>` |
| `stylists` | stylists whose name holds `q`, with their salon. `photo_url` can be null | open the stylist (`/salon/{salon.id}/stylist/{id}`) |

Order inside each list: names that start with `q` first, then names with a word that
starts with `q`, then the rest. Salons: then the nearest. Services: then the most
offered.

Products are not in the suggestions yet.

---

## 3. Errors

| Status | When |
| ------ | ---- |
| 422 | `q` missing (`required`) or 1 character (`too_short`), longer than 100, or half a position |
| 429 | Too many calls from this address in a minute (120 by default). Slow down; the next minute works again |
| 404 | The switch is off |
