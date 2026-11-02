Filtering the map: the params GET /discover/map takes
Base URL: https://api.gostyle.uk/api/v1 Auth: as /discover/map today. Send the token when signed in.

Date: 2026-11-02. From: the mobile team. For the map's Filters drawer (src/components/MapFilters.tsx). The drawer's options come from GET /me/map/filters (map-filters.md); this doc is how the picks come back to you.

0. What we are asking for
GET /discover/map takes one new query param per drawer section, named after that section's key in /me/map/filters, with the picked option ids as its value. The response does not change.

Param	From section	Value
service_categories	service_categories	One or more ids, comma separated
rating	rating	One id
availability	availability	One id
distance	distance	One id, with origin_latitude and origin_longitude
additional	additional	One or more ids, comma separated
Every param the map takes today (category, latitude, longitude, latitudeDelta, longitudeDelta, zoom, limit) stays as it is.

1. Why this shape
Param name = section key, value = option id. You own both ends: an id you send in /me/map/filters is exactly what comes back here, with no mapping table in between, in the app or on your side.
Flat query params, not a JSON blob. Readable in logs, cacheable by URL, and parsed by any framework without extra code.
Comma-separated lists (a,b), not repeated keys (x=a&x=b) or brackets (x[]=a). One key per section, split on ,. Ids never contain a comma.
Nothing is sent for "no filter". The "any" option, and an empty additional, leave the param out. A plain map call stays exactly as today.
Same picks, same URL. The app sorts list values, leaves out empty params, and rounds latitude, longitude and both deltas to 4 decimals (about 11 m), so identical views give identical URLs you can cache.
2. Examples
The drawer as it first opens ("Open now" on, everything else "any"):

GET /discover/map?latitude=25.2048&longitude=55.2708&latitudeDelta=0.12
    &longitudeDelta=0.08&zoom=12&limit=100&additional=open_now
Everything picked:

GET /discover/map?category=gents&latitude=25.2048&longitude=55.2708
    &latitudeDelta=0.12&longitudeDelta=0.08&zoom=12&limit=100
    &service_categories=1d9a7f3c-2e5b-4c8d-a6f1-7b3e9c2d4a18,8b1f6c2e-4a7d-4f0e-9c3b-2d5e7a1f9c40
    &rating=4
    &availability=today
    &distance=10&origin_latitude=25.1972&origin_longitude=55.2744
    &additional=open_now,verified
"Clear Filters": none of the five params.

3. What each param means
All params narrow together (AND), and together with the visible area and category.

Param	Keeps a salon when
service_categories	It has at least one active service in any of the listed categories (OR inside the list)
rating	Its rating is at least the value. 5 means 5.0 only. Salons with no rating yet are left out whenever rating is sent
availability	It has at least one slot a customer could still book, in the salon's own time zone: today from now to closing, tomorrow any time in tomorrow's hours, week from now to the end of the 7th day, today included. With service_categories also sent, the slot must be for a service in those categories
distance	Its straight-line distance from origin_latitude, origin_longitude is at most the value, in km
additional	open_now: open at the time of the request, by the same rule as is_open_now on /discover. verified: verified by Go Style. With both, both must hold
The distance origin
origin_latitude and origin_longitude are where the distance is measured from: the user's position, not the centre of the map, since the map can be panned far from the user. When the phone has no location, the app sends the map's centre instead. They are sent only with distance, always all three together.

4. The response
Unchanged: mode, count and venues, each venue with id, lat, lng, rating and salon_profile_image.

Filter first, then apply limit and decide mode.
count is the number of salons that match the filters in the visible area, not the number before filtering.
Nothing matches: 200 with "count": 0 and "venues": []. The app shows its own empty state.
5. Running it fast
What we suggest; the response is all that matters to the app.

5.1 Narrow in this order, cheapest first
Step	Filter	How
1	Visible area and category	Spatial index on the salon's location (GiST with PostGIS), then the category column
2	rating, additional=verified	Plain columns on the salon row, indexed
3	distance	ST_DWithin(location::geography, origin, km * 1000) on the same index. Without PostGIS: a lat/lng box around the origin first, then the exact distance on what is left
4	additional=open_now	The opening hours check /discover already has
5	service_categories	EXISTS on the salon's active services, with an index on (category_id, salon_id)
6	availability	Last, on the few salons left. See 5.2
5.2 Availability without working out slots on every call
Working out free slots for every salon in view on each map move is the expensive part. Keep a small table instead, one row per salon per day for the next 7 days: whether it has a free slot, and for which service categories. Update a salon's rows when a booking is made or cancelled, or when its hours or team change, and refresh today's rows every few minutes as slots pass. The map then reads a flag.

5.3 Caching
Since the same picks give the same URL (section 1), the full URL can be the cache key. Keep it short, about 60 seconds, because open_now and availability change with the clock. Leave signed-in details out of the cached body: the map response has none today.

6. Errors
Case	Answer
rating, availability, distance or additional with an id not in /me/map/filters	400, with the field errors in the shape other endpoints use
distance without both origin params, or an origin param without distance	400
service_categories with an id you no longer have	Ignore that id and use the rest: the category may have been removed after the app loaded the drawer. All ids unknown: as if the param were not sent
any sent for any param	Treat as not sent
7. Open points
Availability per service category: if 5.2 by category is too much for now, start with any service at the salon and tell us.
Verified: if there is no such flag yet, /me/map/filters leaves the chip out and verified never arrives here.
