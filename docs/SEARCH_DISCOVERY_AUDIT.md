# Mobile Discovery screens (Map, Filters, Search, Smart Search, Results): audit and plan

Date: 2026-11-04. Status: **built and LIVE: P1, P2, P3 and P5 (2026-11-04), all switches on and proven on live. P4 and P7 wait on the client (D2 to D5). P6 skipped (D1). Progress: section 6.**

Repos read:

- **customer-api**: this repo (Django). The app talks only to this. Read at `main` 3d61252
  plus `feat/map-filter-parking` 5f5777a (the parking chip, not pushed). `imransid/main`
  5bd1ea7 adds only the display name (PR #60), nothing in search.
- **booking-api**: `../gostyle-booking-api`, `main` at 1a79d71. Owns bookings only.
- **gostyle-platform**: `../gostyle-platform`, at 9daf3921. Not in the brief, but it owns the
  salon, service, review and media tables customer-api reads (`apps/platform_data/models.py`
  is its inspectdb copy), so the "is there a field for it" answers come from there.

Target: the Figma screenshot (8 frames: Interactive Map, Map Filters short and full,
Search, Smart Search Matrix x2, Search Result Salons, Search Result Services).

Legend: **AVAILABLE** the endpoint exists and returns what the screen needs. **PARTIAL** it
exists but misses fields, filters or sorting. **MISSING** nothing exists. **DATA MISSING** no
endpoint and no field or table for it.

Switches that matter here, as they are on live (from the deploy notes): `MAP_FILTER_V1` on,
`SEARCH_POPULAR_V1` on, `MAP_COUNTRY_V1` on, `NOTIFICATION_CENTER_V1` on,
`MAP_FILTER_PARKING_V1` off (and its code is not pushed).

---

## 0. Short version

1. **Most of the Map, Map Filters, Search and Results screens already work** from earlier
   rounds (map filters PR #54, search popular PR #55, map country PR #57): 16 of 42
   actions are AVAILABLE, 12 PARTIAL.
2. **booking-api has nothing to do with discovery** and needs no change. It has no salons,
   no coordinates and no ratings. customer-api reads its database read-only for two things
   only: who is busy (availability filter) and booking counts (popular services). So the
   business web cannot be touched by this work.
3. **The Smart Search Matrix is the big gap.** No route takes all its filters at once, no
   route serves its option lists, and 7 of its chips have **no data anywhere**: Mall Shops,
   Home Service, Membership Card, Metro Near, "20 Pts" offers and deals, and the UAE
   region > hub > neighbourhood tree. Service sub tags (Fade, Buzz, Keratin ...) have only
   free-text `service.tags` per salon.
4. **Small PARTIALs that are cheap to close**: the map pin sends the salon's cover, not its
   logo; the service results have no currency, no images, no salon logo or distance; the
   rating chips differ from the Figma (no 2.0+ and 1.0+); "Verified" has no data.
5. **One bug found**: after closing time, the card's `status_line` says "Opens at 9:00 AM"
   (today's opening, already past) instead of tomorrow's. `next_opening` is right.
6. **Plan**: new mobile-only routes under `/api/v1/search/...` (options, salons, services,
   suggest), each behind its own switch, OFF by default. The existing `/discover`,
   `/discover/map`, `/me/map/filters` and `/services` keep their answers; they get only
   small additive fields behind switches. New data lives in customer-api's own `consumer`
   tables (areas, curated tags), like `salon_country` did. **About 15 to 18 hours of
   Claude work** in 6 phases, each testable alone, plus a blocked phase that waits on data
   decisions.

---

## 1. Summary table

| # | Screen | Action | Status |
| - | ------ | ------ | ------ |
| 1.1 | Interactive Map | Salons near the user as pins (lat, lng, logo, rating) | PARTIAL |
| 1.2 | Interactive Map | "12 nearby spots" count | AVAILABLE |
| 1.3 | Interactive Map | Gender chips All / Gents / Ladies | AVAILABLE |
| 1.4 | Interactive Map | Search bar: salons, stylists, products | PARTIAL |
| 1.5 | Interactive Map | Bell with unread badge | AVAILABLE |
| 1.6 | Interactive Map | Centre on the user ("You" pin) | AVAILABLE |
| 2.1 | Map Filters | Service category list | AVAILABLE |
| 2.2 | Map Filters | Rating: Any, 5, 4.0+, 3.0+, 2.0+, 1.0+ | PARTIAL |
| 2.3 | Map Filters | Availability: Any, Today, Tomorrow, This Week | AVAILABLE |
| 2.4 | Map Filters | Distance: Default, 5, 10, 20, 100 km | AVAILABLE |
| 2.5 | Map Filters | Additional: Open now, Verified salons | PARTIAL |
| 2.6 | Map Filters | Show Results count | AVAILABLE |
| 2.7 | Map Filters | Clear Filters | AVAILABLE |
| 3.1 | Search | Recent searches list | MISSING (on purpose) |
| 3.2 | Search | Clear All recent searches | MISSING (on purpose) |
| 3.3 | Search | Popular Services cards | PARTIAL |
| 3.4 | Search | Popular Near You salons | AVAILABLE |
| 3.5 | Search | Live search as the user types | PARTIAL |
| 4.1a | Smart Search | Tab: Near Me | AVAILABLE |
| 4.1b | Smart Search | Tab: Mall Shops | DATA MISSING |
| 4.1c | Smart Search | Tab: Packages | MISSING |
| 4.1d | Smart Search | Tab: Home Service | DATA MISSING |
| 4.2a | Smart Search | Quick: Offers (20 Pts) | DATA MISSING |
| 4.2b | Smart Search | Quick: Cuts & Fade, Beard & Shave, Color & Styles | PARTIAL |
| 4.3 | Smart Search | Location tree: Regions > Hubs > Neighbourhoods | DATA MISSING |
| 4.4a | Smart Search | Amenity: Membership Card | DATA MISSING |
| 4.4b | Smart Search | Amenity: Packages Use | MISSING |
| 4.4c | Smart Search | Amenity: 20pt Deals | DATA MISSING |
| 4.4d | Smart Search | Amenity: Free Parking | PARTIAL |
| 4.4e | Smart Search | Amenity: Metro Near | DATA MISSING |
| 4.5 | Smart Search | Service sub tags (Fade, Buzz, Scissor, Beard Trim, Hot Towel, Shave, Grey Blend, Highlights, Keratin) | MISSING |
| 4.6 | Smart Search | Apply (all combined) and Clear Filters | MISSING |
| 4.7 | Smart Search | Where the option lists come from | MISSING |
| 5.1 | Results, Salons | Result count | AVAILABLE |
| 5.2 | Results, Salons | Logo, name, rating, review count, distance, gender icon | AVAILABLE |
| 5.3 | Results, Salons | Open / Closed, "Closes at", "Opens tomorrow at" | PARTIAL |
| 5.4 | Results, Salons | Gallery strip | AVAILABLE |
| 5.5 | Results, Salons | Pagination and sorting | AVAILABLE |
| 6.1 | Results, Services | Result count | AVAILABLE |
| 6.2 | Results, Services | Images, name, description, duration range, price | PARTIAL |
| 6.3 | Results, Services | Salon that offers it: logo, name, distance | PARTIAL |
| 6.4 | Results, Services | Same filters as the Salons tab | PARTIAL |

Totals: **16 AVAILABLE, 12 PARTIAL, 7 MISSING, 7 DATA MISSING** (42 rows; the Smart Search
tabs, quick chips and amenities are split one row per chip because their answers differ).

### 1.1 Each action in detail

All paths are customer-api unless they start with `platform:` (`../gostyle-platform`) or
`booking-api:`.

#### Screen 1: Interactive Map

| # | Status | Existing endpoint | What is missing | Proof |
| - | ------ | ----------------- | --------------- | ----- |
| 1.1 | PARTIAL | `GET /api/v1/discover/map` (`apps/salons/views.py:1106`); pin shape `MapVenueSerializer` (`apps/salons/serializers.py:295`): `id, lat, lng, rating, salon_profile_image` | The pin image is the salon's **cover** photo (`kind="COVER"`, `apps/salons/selectors.py:1595-1606`), not its logo. No `logo_url` on the pin. | `views.py`, `serializers.py`, `selectors.py` `map_venue_queryset` (:1565) |
| 1.2 | AVAILABLE | same, field `count` | Nothing. Plain call: `count` = pins sent (`views.py:1312`), at most 500 (`selectors.py:1468`). With drawer filters: every match in view before the cap (`views.py:1293`, `apps/salons/map_filters.py:399-440`). It counts the map box, not a radius around the user. | same |
| 1.3 | AVAILABLE | `category=all\|gents\|ladies\|unisex` on `/discover/map` and `/discover` (`apps/salons/params.py:30`, `selectors.py:86`) | Nothing. Strict on purpose: a unisex salon shows only under All (`selectors.py:93-99`). | `params.py`, `selectors.py` |
| 1.4 | PARTIAL | Salons: `GET /api/v1/discover?search=` (salon name, branch name, city, `selectors.py:1292-1322`). Services: `GET /api/v1/services?search=` (`apps/salons/service_search_views.py:31`) | No stylist search across salons: `GET /api/v1/stylists` needs `tenant_id` and `branch_id` (`views.py:645-664`). No product search across salons: only `GET /api/v1/salon/{id}/products` (`views.py:997`) and `GET /api/v1/products/{id}` (`views.py:959`). The salon search does not look at the services a salon offers (`selectors.py:1304-1309`). | `views.py`, `selectors.py`, `service_search_views.py` |
| 1.5 | AVAILABLE | `GET /api/v1/notifications/unread-count` (`apps/notifications/views.py:201`, `apps/notifications/urls.py:16`), answers `{total, <tab>: n}` (`views.py:52-62`) | Nothing. Needs a token. `NOTIFICATION_CENTER_V1` (on). | `apps/notifications/` |
| 1.6 | AVAILABLE | No server part: the phone's GPS. `user_latitude`, `user_longitude` on `/discover/map` only pick the country (`views.py:1255`, `MAP_COUNTRY_V1`) | Nothing. | `views.py` |

#### Screen 2: Map Filters (short and full)

| # | Status | Existing endpoint | What is missing | Proof |
| - | ------ | ----------------- | --------------- | ----- |
| 2.1 | AVAILABLE | `GET /api/v1/me/map/filters` (`apps/salons/map_views.py:88`), section `service_categories` (`map_filters.py:199-215`) | **From the DB, not static**: every category name on a public salon's bookable menu, filed parent first, id = uuid5 of the name, cached 60 s (`map_filters.py:120-191`). The labels are the salons' own words, so "Haircut" or "Beard & Shave" show only if salons named them so. "All Services" = send none. | `map_filters.py`, `map_views.py`, `menu.py:61` |
| 2.2 | PARTIAL | `rating` on `/discover/map` (`map_filters.py:54`, `:479-481`) | Served: 3.0+, 4.0+, 4.5+, 5.0. Figma: 5, 4.0+, 3.0+, 2.0+, 1.0+. Missing **2.0+ and 1.0+**; 4.5+ is not in the Figma (D7). Live: no salon has a review yet. | `map_filters.py` |
| 2.3 | AVAILABLE | `availability=today\|tomorrow\|week` (`map_filters.py:55`, `apps/salons/map_availability.py:90`) | Nothing. **It does not call booking-api's engine.** It is customer-api's copy of the slot rules of `GET /booking/nearest-available` (`views._offers`, `views.py:1843`): platform shifts and published hours, plus busy rows read read-only from booking-api's database (`map_availability.py:224-250`), with copies of booking-api's `BLOCKING_STATUSES` and its 10:00 to 22:00 day (`map_availability.py:55`, `:69`). 503 `availability_unavailable` when that database cannot be read. Labels are "Today / Tomorrow / This week"; the app may print its own. | `map_availability.py`, `views.py` |
| 2.4 | AVAILABLE | `distance=5\|10\|20\|100` with `origin_latitude`, `origin_longitude` (`map_filters.py:56`, `:483-489`) | Nothing. "Default" = `any`. | `map_filters.py` |
| 2.5 | PARTIAL | `additional=open_now` (`map_filters.py:60`, `:450-453`) | Open now: done. **Verified: DATA MISSING.** No "verified by Go Style" mark exists; the chip is left out on purpose and `additional=verified` is a 400 (`map_filters.py:57-59`). Things that could mean it: `storefront_certification` (hijab, with `on_site_verified_at`, `apps/platform_data/models.py:3157-3188`) or a custom badge reviewed `APPROVED` (`storefront_badge.state`, platform: `packages/nest-storefront/src/domain/services/badge-rules.ts:187`). D2. | `map_filters.py`, `models.py`, platform badge rules |
| 2.6 | AVAILABLE | the same `/discover/map` call with the picks: `count` before `limit` (`map_filters.py:399-440`) | Nothing. There is no count-only call, so the app gets the pins too. Fine at today's size. | `map_filters.py` |
| 2.7 | AVAILABLE | each section's `default` in `/me/map/filters` (`map_filters.py:213-243`) | App side. Note: the Additional default is `["open_now"]` (the FE doc), so Clear puts Open now back on. | `map_filters.py` |

#### Screen 3: Search

| # | Status | Existing endpoint | What is missing | Proof |
| - | ------ | ----------------- | --------------- | ----- |
| 3.1 | MISSING (on purpose) | none | Nothing on the server. The mobile team's own contract keeps recent searches on the phone (`docs/search-popular-fe-contract.md:21`, `:159`). D1. | grep `recent` over `apps/`: nothing |
| 3.2 | MISSING (on purpose) | none | Same as 3.1. | same |
| 3.3 | PARTIAL | `GET /api/v1/services?is_popular=true` (`views.py:730`, handed to `service_search_views.py:31`) | Has name, description, price, durations, category and salon `{id, name, category}`. Order: most booked in 30 days, read-only from booking-api's database (`service_search.py:23-45`, `selectors.py:2107`). **No currency** (`menu.py:88-97`): the card prints "AED 180", and prod also has Dhaka salons. No image. | `service_search.py`, `menu.py`, `selectors.py` |
| 3.4 | AVAILABLE | `GET /api/v1/discover?is_popular=true&latitude=&longitude=&radius=` (`views.py:208`, weighted rating `selectors.py:2162`) | Nothing: `logo_url` and `name` are on the card. | `views.py`, `selectors.py` |
| 3.5 | PARTIAL | the two lists above with `search` | No suggest route. Typing would call two full lists per key (a salon card is about ten subqueries per row, `config/pagination.py:19-24`). ILIKE only, no stylists, no products, no typo tolerance. These public routes have no throttle (`config/settings/base.py:523` throttles login only). | `selectors.py:1292`, `config/` |

#### Screen 4: Smart Search Matrix

| # | Status | Existing endpoint | What is missing | Proof |
| - | ------ | ----------------- | --------------- | ----- |
| 4.1a | AVAILABLE | `GET /api/v1/discover?latitude=&longitude=&sort=distance` (`views.py:314-315`) | Nothing. | `views.py` |
| 4.1b | DATA MISSING | none | No "in a mall" field. Branch has only free-text `address_line`, `city`, `region` (`models.py:222-238`). The salon build captures free-text `building` and `shopUnit` inside `salon.captured_data` (platform: `packages/nest-build/src/domain/review/review-document.ts:806-821`), never on the storefront. | `models.py`, platform build review |
| 4.1c | MISSING | per salon only: `GET /api/v1/salon/{id}/packages` (`views.py:868`) | Data exists (`service_package`, `selectors.py:229-318`). No list of "salons with packages" or "packages across salons". Live rules change with `PACKAGES_LIVE_RULES_V1` (off). D14. | `views.py`, `selectors.py` |
| 4.1d | DATA MISSING | none | No home-service field on service, storefront, branch or salon. Grep of the platform schema and code: nothing. | platform `apps/gostyle-api/prisma/schema/` |
| 4.2a | DATA MISSING | none | Points are per business: every platform `LoyaltyEarningRule` has its own `tenantId` (platform: `apps/gostyle-api/prisma/schema/loyalty.prisma:26-54`), so there is no app-wide "20 Pts". Dated offers do exist (`storefront_offer`: text, scope, `discount_percent`, `starts_on`, `ends_on`, `models.py:3248-3266`), but customer-api never reads them. D5. | `models.py`, platform `loyalty.prisma`, `storefront.prisma:745` |
| 4.2b | PARTIAL | `service_categories` on the map drawer only | They can be matched to salons' category names, but those are each salon's own words: nothing ties "Cuts & Fade" to a set of categories. Not on `/discover` or `/services`. D6. | `map_filters.py` |
| 4.3 | DATA MISSING | none | No hierarchy table. Only free text: `branch.city`, `branch.region`, `branch.country_code` (`models.py:227-233`), `salon.city`, `salon.area` (`models.py:2305-2306`), and build `region` / `area` in `salon.captured_data`. `/discover?city=` matches the city exactly (`views.py:245-246`). Our `consumer.salon_country` gives the country only (`apps/salons/models.py:29`). D3. | `models.py`, `apps/salons/models.py` |
| 4.4a | DATA MISSING | none | Nothing. The platform's "membership" tables are staff logins (platform: `membership.prisma`). | platform schema |
| 4.4b | MISSING | none | Same data as 4.1c. | as 4.1c |
| 4.4c | DATA MISSING | none | As 4.2a. | as 4.2a |
| 4.4d | PARTIAL | `additional=parking` on `/discover/map` only: branch `feat/map-filter-parking` 5f5777a, not pushed, `MAP_FILTER_PARKING_V1` off (`map_filters.py:61-67`, `:384-396`) | Uses the amenity `PARKING`, which does not say free. The platform also has a manual badge **`FREE_PARKING`** (`BADGES.manualBadges`, platform: `packages/nest-storefront/src/domain/services/publish-blockers.ts:114`) that does say free. Neither is on the search lists. D8. | `map_filters.py`, platform `amenities.ts`, `publish-blockers.ts` |
| 4.4e | DATA MISSING | none | Only the build's free-text `nearestMetro` inside `salon.captured_data` (platform: `review-document.ts:819`), not on the storefront and not checked by anyone. | platform build review |
| 4.5 | MISSING | none | Platform `service.tags` exists (free-form `text[]`, per salon, platform: `apps/gostyle-api/prisma/schema/service-menu.prisma:187-193`, `models.py:2353`), and a seeded `badge` table (`service-menu.prisma:1040`). Nothing reads them, and free text cannot back a fixed chip list without our own word list. D6. | `models.py`, platform `service-menu.prisma` |
| 4.6 | MISSING | none | No route takes these together. `/discover` takes text, gender, `rating_min`, open now, city, radius, top rated, hijab (`params.py:232-290`); not service categories, availability, amenities, areas, tags or tabs. The map drawer's filters live on `/discover/map` only. | `params.py`, `map_filters.py` |
| 4.7 | MISSING | only `/me/map/filters` for the map drawer | No options route for the Smart Search: today the app would have to hardcode every list. | `apps/salons/urls.py` |

#### Screen 5: Search Results, Salons tab

| # | Status | Existing endpoint | What is missing | Proof |
| - | ------ | ----------------- | --------------- | ----- |
| 5.1 | AVAILABLE | `GET /api/v1/discover`, page shape `{count, next, previous, results}` (`config/pagination.py:14`) | Nothing. | `config/pagination.py` |
| 5.2 | AVAILABLE | `SalonCardSerializer` (`serializers.py:38-81`): `logo_url`, `name`, `rating` "4.7", `review_count` "260", `distance` "2.7 km" (with latitude/longitude), `category` gents/ladies/unisex | Nothing. | `serializers.py` |
| 5.3 | PARTIAL | `open`, `closes_at`, `status_line`, `next_opening` (`serializers.py:62-65`, `:111-135`), worked out in `apps/salons/hours.py:129-173` on `branch.timezone`, else Asia/Dubai (`apps/salons/timezones.py:32`) | **Bug**: after closing time on an open day, `status_line` says "Opens at 9:00 AM", today's opening, already past (`hours.py:150-153` and `:59-62`). `next_opening` (full timestamp with offset) is right, so the app must build "Opens tomorrow at 10:00 AM" from it. Dated holiday exceptions are not read on the card (`serializers.py:97-100`). D10. | `hours.py`, `serializers.py`, `timezones.py` |
| 5.4 | AVAILABLE | `gallery`: list of URLs (`selectors.py:1446`, `serializers.py:183`) | Nothing (public, approved, not deleted only). | `selectors.py` |
| 5.5 | AVAILABLE | `page`, `pageSize` 1 to 50 (or old `page_size`), `sort=distance\|rating`, `is_popular` (`views.py:281-328`, `params.py:232-290`) | Nothing on `/discover`. Default order is rating. | `views.py`, `params.py` |

Note on screen 5: the Figma searches "Fade with Bread Sculpt" and lists 17 salons. Today
`/discover?search=` matches only salon names and city, so a salon that offers a fade but is
not called "Fade" is not found (D11).

#### Screen 6: Search Results, Services tab

| # | Status | Existing endpoint | What is missing | Proof |
| - | ------ | ----------------- | --------------- | ----- |
| 6.1 | AVAILABLE | `GET /api/v1/services` without salon ids, `{count, next, previous, results}` (`service_search_views.py:31-68`) | Nothing. | `service_search_views.py` |
| 6.2 | PARTIAL | one result: `service_search.py:186-213`, `menu.py:79-97` | **No images** (the data exists: `service_media` and gallery photos linked to the service, read for Service Detail by `selectors.py:1113`). `duration_min` = `duration_max` always (one number, `menu.py:84-87`; a real range needs `service_variant`, D15). **No `currency`.** | `service_search.py`, `menu.py`, `selectors.py` |
| 6.3 | PARTIAL | `salon: {id, name, category}` (`service_search.py:206-212`) | No `logo_url`, no `distance`: not selected (`selectors.py:2040-2049`). | `selectors.py` |
| 6.4 | PARTIAL | `search`, `latitude`, `longitude`, `radius`, `is_popular`, `page`, `pageSize` (`params.py:190-213`) | No gender, rating, availability, open now, categories, amenities, areas, tags, or sort by distance. | `params.py` |

---

## 2. Existing endpoints found

All in customer-api, all mobile only (the business web never calls customer-api).

| Method + path | File | Returns | Switch |
| ------------- | ---- | ------- | ------ |
| `GET /api/v1/discover` | `apps/salons/views.py:208` | page of salon cards (`SalonCardSerializer`: id, name, category, logo_url, rating, review_count, distance, open, opens_at, closes_at, next_opening, has_story, gallery, slug, branch_id, tenant_id, city, coordinate, status, hours_today, status_line, cover_url, hijab_certified, deposit, free_cancellation). Params: latitude, longitude, radius (km), category, search / q, city, sort, rating_min, is_top_rated, is_open_now, hijab_mode, page, page_size, is_popular, pageSize | `SEARCH_POPULAR_V1` for is_popular and pageSize (on) |
| `GET /api/v1/discover/{id}` | `views.py:356` | one salon card | none |
| `GET /api/v1/discover/map` | `views.py:1106` | `{mode, count, venues: [{id, lat, lng, rating, salon_profile_image}], country}`. Params: latitude, longitude, latitudeDelta, longitudeDelta, radius (metres), limit, zoom, category, service_categories, rating, availability, distance, origin_latitude, origin_longitude, additional, user_latitude, user_longitude | `MAP_FILTER_V1` (on), `MAP_COUNTRY_V1` (on), `MAP_FILTER_PARKING_V1` (branch only, off) |
| `GET /api/v1/me/map/filters` | `apps/salons/map_views.py:88` | `{sections: [{key, title, multiple, default, options: [{id, label}]}]}`: service_categories, rating, availability, distance, additional | `MAP_FILTER_V1` (on) |
| `GET /api/v1/services` (no tenant_id, branch_id, category_id) | `views.py:730` then `apps/salons/service_search_views.py:31` | page of services across salons: id, name, description, price, duration_min, duration_max, category `{id, label}`, salon `{id, name, category}`. Params: search, latitude, longitude, radius (km), is_popular, page, pageSize | `SEARCH_POPULAR_V1` (on) |
| `GET /api/v1/services?tenant_id=` | `views.py:730` | one salon's menu (old route, same path) | none |
| `GET /api/v1/services-details?service_ids=` | `views.py:825` | services by id, with `image_url` | none |
| `GET /api/v1/stylists?tenant_id=&branch_id=` | `views.py:645` | one salon's stylists | none |
| `GET /api/v1/salon/{id}/packages` | `views.py:868` | one salon's packages | `PACKAGES_LIVE_RULES_V1` (off) changes the rules |
| `GET /api/v1/salon/{id}/products`, `GET /api/v1/products/{id}` | `views.py:997`, `views.py:959` | one salon's products, one product | none |
| `GET /api/v1/booking/nearest-available/{salon_id}` | `views.py:1631` | free starts; its rules (`_offers`, `views.py:1843`) are what the map's availability filter copies | none |
| `GET /api/v1/notifications/unread-count` | `apps/notifications/views.py:201` | `{total, <tab>: n}` | `NOTIFICATION_CENTER_V1` (on) |

booking-api: **no discovery, search, salon or geo route, and no salon, coordinate or rating
table** (`prisma/schema.prisma` models: Booking, BookingItem, Hold, reservations, series,
groups, check-in, outbox ...). customer-api reads two of its tables read-only through the
`booking` database alias (`BOOKING_DB_NAME`, `config/settings/base.py:155`):
`booking_item` + `booking` for busy rows (`map_availability.py:236`) and for popular counts
(`selectors.py:2133`).

---

## 3. Gaps

### 3.1 Missing endpoints

| Gap | Rows |
| --- | ---- |
| One route that takes every Smart Search filter at once, for salons | 4.6, 5.x, D11 |
| The same for services | 6.4 |
| An options route for the Smart Search lists (tabs, quick chips, areas, amenities, tags) | 4.7 |
| A typeahead (suggest) route, light and throttled | 3.5 |
| Stylist search across salons | 1.4 |
| Product search across salons | 1.4 |
| Salons with packages / packages across salons | 4.1c, 4.4b |
| Recent searches on the server (only if D1 says so) | 3.1, 3.2 |

### 3.2 Missing fields

| Field | Where | Rows |
| ----- | ----- | ---- |
| `logo_url` on each map pin | `/discover/map` venues | 1.1 |
| Rating chips 2.0+ and 1.0+ (and drop 4.5+?) | `/me/map/filters` | 2.2 |
| `currency` on each service result | `/services` search | 3.3, 6.2 |
| `images` (up to 4) on each service result | `/services` search | 6.2 |
| A real `duration_min` / `duration_max` range | every service row | 6.2 |
| `salon.logo_url`, `salon.distance` on each service result | `/services` search | 6.3 |
| A correct "next opening" sentence after closing time | card `status_line` | 5.3 |
| `is_verified` | needs data first | 2.5 |

### 3.3 Missing data or tables

| Data | What exists today | Rows |
| ---- | ----------------- | ---- |
| Location tree (region > hub > neighbourhood) | free text `branch.city`, `branch.region`, `salon.area`; country from our `salon_country` | 4.3 |
| In a mall | free text in build data only | 4.1b |
| Home service | nothing | 4.1d |
| Metro near | free text `nearestMetro` in build data only | 4.4e |
| Membership card | nothing | 4.4a |
| App-wide points ("20 Pts", "20pt Deals") | loyalty per business only; dated `storefront_offer` exists | 4.2a, 4.4c |
| Verified mark | hijab certification and approved custom badges only | 2.5 |
| A fixed word list for sub tags and quick chips | free-form `service.tags` and category names per salon | 4.2b, 4.5 |
| Free parking (truly free) | amenity `PARKING` (not "free") and manual badge `FREE_PARKING` | 4.4d |

### 3.4 Found on the way

- `status_line` after closing (row 5.3) affects every screen that shows a salon card
  (`/discover`, `/discover/{id}`, favourites).
- Distance is plain trigonometry over subqueries on platform's `branch` (no PostGIS, no
  index we may add there: `selectors.py:1272-1277`). Fine today (live has 4 public salons);
  it needs a prefilter on a table of ours when the catalogue grows (4.4 below).
- The public list routes are not throttled. A typeahead makes that matter.

---

## 4. Plan

### 4.1 The idea

- **New mobile-only routes under `/api/v1/search/`** for the Smart Search and the Results
  screens. Each behind its own switch, OFF = 404. Rafa's rule: every app screen gets its own
  endpoint. They share the selectors on purpose (`discoverable_salons`, the card,
  `map_filters`, `map_availability`, `service_search`), so a salon is described the same way
  everywhere, but the routes stay separate.
- **The existing routes keep their answers.** `/discover`, `/discover/map`,
  `/me/map/filters` and `/services` get only small additive fields, each behind a switch;
  off, byte for byte today's answer. They are app routes already (the business web never
  calls customer-api), so this breaks nothing on the business side.
- **Same ids as the map drawer.** The new routes read `service_categories`, `rating`,
  `availability`, `distance` and `additional` with the same ids `/me/map/filters` serves
  (the same parser), so the map and the search cannot drift.
- **New data lives in our own `consumer` tables** (Django migrations), like
  `salon_country`. Platform tables stay read-only.
- **No booking-api change.** Availability and popular counts keep reading its database
  read-only, as they do today.
- **A chip with no data is not served** (as Verified on the map today). The app shows what
  the options route sends, so a chip appears the day its data does, with no app release.

### 4.2 New mobile endpoints (customer-api)

| # | Method + path | Switch | Does |
| - | ------------- | ------ | ---- |
| N1 | `GET /api/v1/search/options` | `SEARCH_OPTIONS_V1` | Every Smart Search list, in the map drawer's `sections` shape |
| N2 | `GET /api/v1/search/salons` | `SEARCH_RESULTS_V1` | Salons tab: every filter at once |
| N3 | `GET /api/v1/search/services` | `SEARCH_RESULTS_V1` | Services tab: the same filters |
| N4 | `GET /api/v1/search/suggest` | `SEARCH_SUGGEST_V1` | Typeahead: salons, service names, stylists |
| N5 | `GET`, `POST`, `DELETE /api/v1/me/search/recent`, `DELETE /api/v1/me/search/recent/{id}` | `RECENT_SEARCH_V1` | Only if D1 = server |

All are `AllowAny` except N5 (token). Errors in the usual envelope; a bad filter id is 400
as on the map (`MapFilterInvalid`), so one error reader serves both.

**N1. `GET /api/v1/search/options`**

Query: optional `latitude`, `longitude` (the phone's): picks the country, so the area tree
is that country's. Answer, the drawer's shape plus a tree for areas:

```json
{
  "sections": [
    {"key": "tab", "title": "", "multiple": false, "default": ["near_me"],
     "options": [{"id": "near_me", "label": "Near Me"}, {"id": "packages", "label": "Packages"}]},
    {"key": "quick", "title": "", "multiple": false, "default": [],
     "options": [{"id": "cuts_fade", "label": "Cuts & Fade", "icon": "scissors"}]},
    {"key": "area", "title": "UAE Regions", "multiple": false, "default": [],
     "tree": [{"id": "<uuid>", "label": "Dubai", "level": "region",
               "children": [{"id": "<uuid>", "label": "Downtown", "level": "hub",
                             "children": [{"id": "<uuid>", "label": "JVC", "level": "neighbourhood"}]}]}]},
    {"key": "amenities", "title": "", "multiple": true, "default": [],
     "options": [{"id": "free_parking", "label": "Free Parking"}, {"id": "packages", "label": "Packages Use"}]},
    {"key": "service_tags", "title": "", "multiple": true, "default": [],
     "groups": [{"title": "Cuts & Styling", "options": [{"id": "fade", "label": "Fade"}]}]}
  ]
}
```

Only options with data are sent (4.1). Arabic labels can follow the same way later.

**N2. `GET /api/v1/search/salons`**

| Param | Rule |
| ----- | ---- |
| `q` | up to 100 characters. Matches the salon's name, branch name, city, **and** (D11) a service on its menu by name, category label or tag |
| `latitude`, `longitude` | the phone's position: distance on each card, needed by `near_me` and `sort=distance` |
| `tab` | `near_me` (sort by distance, needs a position), `packages` (D14); later `mall`, `home` |
| `gender` | `all`, `gents`, `ladies` (strict, as `/discover`, D13) |
| `service_categories`, `rating`, `availability`, `distance`, `additional` | exactly the map drawer's ids and rules (`map_filters.py`), `distance` measured from `latitude`, `longitude` |
| `quick` | one id from N1 (D5, D6) |
| `amenities` | ids from N1 (`free_parking`, `packages`; others when data exists) |
| `area` | one area id from N1, any level: a region keeps all its hubs and neighbourhoods |
| `service_tags` | ids from N1: a salon with at least one service that matches any of them |
| `sort` | `distance`, `rating`, `popular` (the `/discover` weighted rating). Default: `distance` with a position, else `rating` |
| `page`, `pageSize` | 1 to 50, default 20 |

Answer: `{count, next, previous, results}`, each result **the same card as `/discover`**
(`SalonCardSerializer`), so 5.2 to 5.4 come for free. `count` is every match, which also
serves the Smart Search "Apply" preview: call with `pageSize=1` and read `count`.

**N3. `GET /api/v1/search/services`**

Same params as N2 (the salon filters narrow which salons' services are listed;
`service_tags` and `quick` match the service itself), plus `sort=popular` (most booked in
30 days, today's `is_popular` rule) and `sort=price`. One result:

```json
{
  "id": "<uuid>", "name": "The Gentleman's Cut", "description": "A classic haircut ...",
  "price": "199.00", "currency": "AED", "duration_min": 20, "duration_max": 30,
  "images": ["https://...", "https://...", "https://...", "https://..."],
  "category": {"id": "<uuid>", "label": "Haircut"},
  "salon": {"id": "<uuid>", "name": "The Iron Razor Barbershop", "logo_url": "https://...",
            "category": "gents", "distance": "2.7 km", "rating": "4.7"}
}
```

`price`, `duration_*` and `category` by the Services tab's own rules (`menu.service_row`),
so every screen shows the same numbers. `images`: the service's own photos then the
salon's gallery photos linked to it (the Service Detail order, `selectors.py:1113`), at
most 4.

**N4. `GET /api/v1/search/suggest`**

Query: `q` (2 to 100 characters), optional `latitude`, `longitude`. Throttled (for example
60 per minute per address). Light: names only, no card subqueries.

```json
{
  "salons":   [{"id": "<uuid>", "name": "Del Capo Barber", "logo_url": "https://...", "distance": "3.1 km"}],
  "services": [{"name": "Fade", "salons": 12}],
  "stylists": [{"id": "<uuid>", "name": "Omar", "photo_url": "https://...", "salon": {"id": "<uuid>", "name": "Del Capo Barber"}}]
}
```

At most 5 of each. A service suggestion is a name; tapping it runs N2 / N3 with that `q`.
Products wait (D12).

**N5. Recent searches (only if D1 = server)**

`GET` answers the last 10 `{id, query, created_at}`, newest first. `POST {"query"}` adds one
(the same text again moves it to the top). `DELETE /me/search/recent` is Clear All,
`DELETE /me/search/recent/{id}` removes one. The account deletion purge must clear them.

### 4.3 Small changes on existing app routes (additive, each its own switch)

| # | Route | Change | Switch |
| - | ----- | ------ | ------ |
| X1 | `GET /discover/map` | add `logo_url` to each venue (the card's LOGO rule); `salon_profile_image` stays | `MAP_PIN_LOGO_V1` |
| X2 | `GET /me/map/filters` + `/discover/map` | rating chips as the Figma: 5, 4, 3, 2, 1 (D7) | `MAP_RATING_FIGMA_V1` |
| X3 | `GET /services` (search answer) | add `currency` to each result, for the Popular Services cards | `SERVICE_CURRENCY_V1` |
| X4 | every salon card | `status_line` after closing: "Opens at <next opening time>" from `next_opening`, never a past time (D10) | `CARD_NEXT_OPENING_V1` |
| X5 | `feat/map-filter-parking` | finish or rework by D8 (badge `FREE_PARKING`, amenity `PARKING`, or both) before it is pushed | `MAP_FILTER_PARKING_V1` |

### 4.4 Database changes (customer-api `consumer` schema only)

| Table | Columns | Why | Phase |
| ----- | ------- | --- | ----- |
| `search_area` | `id` uuid pk, `parent_id` (self, null for a region), `level` (`region`, `hub`, `neighbourhood`, CHECK), `country_code` char 2, `slug`, `name_en`, `name_ar` null, `centre_lat`, `centre_lng`, `radius_km` null, `boundary` jsonb null (a polygon, when we have one), `sort_order`, `active`. Unique (`country_code`, `level`, `slug`). Index `parent_id` | the location tree (D3) | P4 |
| `salon_area` | `branch_id` pk, `lat`, `lng` (what it was worked out from), `region_id`, `hub_id`, `neighbourhood_id` (null each), `manual` bool, `updated_at`. Index on each area id | each salon's place in the tree. Refreshed like `salon_country` (missing row or the pin moved), never over a `manual` row (admin override) | P4 |
| `search_tag` | `id`, `group` (`quick`, `cuts_styling`, `beard_shave`, `color_treatments`), `slug`, `label_en`, `label_ar` null, `icon` null, `match_terms` text[] (for example fade: `fade`, `skin fade`, `taper`), `category_names` text[] (for quick chips: the category names it stands for), `sort_order`, `active`. Unique (`group`, `slug`) | the curated word list behind quick chips and sub tags (D6). A service matches when its name, its category label or one of its `tags` holds a term, case ignored | P3 |
| `recent_search` | `id`, `account_id` (FK `consumer_account`, cascade), `query` (100), `created_at`. Unique (`account_id`, lower(`query`)). Index (`account_id`, `created_at` desc) | only if D1 = server. Kept to the last 10 per account | P6 |
| `salon_attribute` | `branch_id`, `key` (`mall`, `home_service`, `metro_near`, `membership_card`, `verified`, CHECK), `value` text null, `set_by`, `set_at`. Unique (`branch_id`, `key`) | only if D2 / D4 say "we mark it ourselves" (admin page) | P7 |

Indexes and geo: no PostGIS anywhere, and we cannot index platform's `branch`. Keep
today's box then exact distance. When the public salons pass a few thousand, prefilter on a
btree index (`lat`, `lng`) on `salon_area` (it holds every pin) instead of the branch
subqueries. Not needed now.

### 4.5 Which repo does what

| Repo | Work |
| ---- | ---- |
| customer-api | everything in 4.2 to 4.4: routes, parsers, selectors, tables, seed data, admin pages, tests, FE guide |
| booking-api | **nothing.** No route, no migration, no flag. Its database stays read-only for availability and popular counts. The business web cannot be affected |
| gostyle-platform | nothing in v1. Only if D2 / D4 choose "the salon sets it": new amenity keys (`IN_MALL`, `HOME_SERVICE`, `NEAR_METRO`, `MEMBERSHIP_CARD`) or a verified mark. That is the platform's own work and console change |

### 4.6 Order of work (small phases, each testable alone)

| Phase | What | Switch | How to test it alone | Claude time |
| ----- | ---- | ------ | -------------------- | ----------- |
| P0 | Rafa answers D1 to D15 | none | | 0 |
| P1 | X1 to X4 (pin logo, rating chips, currency, next opening line) | one each | tests with each switch off (answer identical) and on; one curl each on live after deploy | 2 h |
| P2 | N2 + N3 with the data we have: `q` (with services, D11), gender, service categories, rating, availability, distance, open now, parking (D8), tabs `near_me` and `packages` (D14), sorts, paging; service images, currency, salon logo and distance | `SEARCH_RESULTS_V1` | tests; on live, the same params on `/discover` and N2 give the same salons when `q` matches no service; `count` equals the list length | 4 to 5 h |
| P3 | `search_tag` table + seed (the Figma's 4 quick chips and 9 sub tags) + admin page; N1 without areas; `quick` and `service_tags` on N2 / N3 | `SEARCH_OPTIONS_V1` | tests on seeded services; on live, a read-only command lists which salons each tag finds, Rafa checks | 3 h |
| P4 | `search_area` + seed (by D3), `salon_area` + `manage.py salon_areas [--apply]` (dry run first), `area` on N2 / N3, the tree in N1 | `SEARCH_AREAS_V1` | dry run on live lists every salon with its area; Rafa checks against the address | 3 to 4 h |
| P5 | N4 suggest + throttle | `SEARCH_SUGGEST_V1` | tests; on live, timing of one call | 2 h |
| P6 | N5 recent searches + purge on account deletion (only if D1 = server) | `RECENT_SEARCH_V1` | tests; add, list, clear on live with a test account | 1.5 h |
| P7 | Verified, Mall, Home Service, Metro Near, Membership Card, points deals | per chip | **blocked** on D2, D4, D5 (data first) | later |
| P8 | FE guide `docs/SEARCH_DISCOVERY_FE.md` + server steps `docs/SEARCH_DISCOVERY_SERVER.md` | none | Rafa reads it | 1 h |
| | **Total (P1 to P6, P8)** | | | **about 15 to 18 hours** |

Each phase: its own branch from `main`, its own PR (base main), tests before every commit,
deploy only by Rafa. Add about 30% for live surprises: the local database is nearly empty,
so real shapes (category names, tags, hours) are only seen on live.

### 4.7 Risks

| Risk | Guard |
| ---- | ----- |
| Open now and availability run in Python over every match (as on the map) | same code and cap as the map; fine at today's size; measure on live in P2 |
| Category names are the salons' own words, so "Cuts & Fade" can miss a salon that calls it "Barbering" | the curated `category_names` and `match_terms` in `search_tag` are edited in the admin without a release |
| A salon put in the wrong neighbourhood | `salon_area.manual` override in the admin; the refresh never overwrites it |
| "Free Parking" shown for a salon whose parking is paid | D8: only the `FREE_PARKING` badge says free |
| The typeahead hammers the database | throttle, names only, at most 5 of each |
| Two answers for one salon (map pin vs search card) | the new routes reuse the card and the drawer parser, no copies |

---

## 5. Open questions (D1 to D15)

| # | Question | Recommended |
| - | -------- | ----------- |
| D1 | Recent searches: on the phone (as the FE contract says today, `docs/search-popular-fe-contract.md:159`) or on the server (same list on every phone, needs N5 + a table)? | **Phone.** Skip P6 unless the app team asks |
| D2 | What does "Verified salons" mean? (a) hijab certification `CERTIFIED`; (b) at least one custom badge reviewed `APPROVED`; (c) our own admin mark; (d) the platform adds a real "verified by Go Style" mark | **Keep the chip hidden until (d)**, or (c) if the business wants it now |
| D3 | Where does the location tree come from? (a) our own list (regions, hubs, neighbourhoods with a centre point, polygons later from OpenStreetMap, free, no signup), each salon placed from its pin; (b) the free-text `branch.region` / `salon.area`; (c) the platform adds area fields | **(a).** Note: the Figma's "Major Hubs" lists "Dubai" beside Downtown and Business Bay; please confirm the real list |
| D4 | Mall Shops, Home Service, Metro Near, Membership Card: who says it is true? (a) the salon, through new platform amenity keys; (b) our admin table; (c) hide the chips until there is data | **(c) now, (a) later** |
| D5 | "Offers (20 Pts)" and "20pt Deals": what are they? Points are per business today. Should "Offers" mean a live `storefront_offer` (a dated discount the salon posts)? | **"Offers" = a live `storefront_offer`; hide the points chips** until there is an app-wide points rule |
| D6 | Sub tags (Fade, Buzz ...) and quick chips: our curated word list matched on service name, category and tags (`search_tag`), or ask salons to pick fixed tags on the platform? | **Our curated list** |
| D7 | Rating chips: the Figma (5, 4.0+, 3.0+, 2.0+, 1.0+) or today's (3.0+, 4.0+, 4.5+, 5.0)? | **The Figma** (X2) |
| D8 | Free Parking: the badge `FREE_PARKING` (says free), the amenity `PARKING` (does not), or either? This also decides the unpushed parking chip | **Badge or amenity, label "Parking"; "Free Parking" only from the badge** |
| D9 | New `/search/...` routes, or more params on `/discover` and `/services`? | **New routes** (your rule; the old ones stay as they are) |
| D10 | `status_line` after closing: fix the server sentence (X4), or tell the app to use `next_opening` only? | **Both:** fix behind a switch, and the FE guide says "Opens tomorrow at ..." is built from `next_opening` |
| D11 | Should a text like "Fade" find salons that offer a fade on the Salons tab? | **Yes, on N2 only**; `/discover` unchanged |
| D12 | Stylists and products in the search bar: in v1? | **Stylists yes** (N4), **products later** (no list of products across salons yet) |
| D13 | Gender chips stay strict (a unisex salon only under All, as today), or does Gents also show unisex? | **Keep strict**, same as the map and `/discover` |
| D14 | Packages tab: salons with at least one bookable package by the Packages tab's rules. Which rules, today's or `PACKAGES_LIVE_RULES_V1`? | **The same rules the salon's Packages tab uses at that moment** (one switch decides both) |
| D15 | Duration range "20 to 30 mins": take min and max over the service's variants? It would then differ from the Services tab and Service Detail, which send one number twice | **Change it everywhere at once, later**, behind one switch; v1 keeps one number |

---

## 6. Progress (2026-11-04)

### 6.1 Rafa's answers

| # | Answer |
| - | ------ |
| D1 | Recent searches on the phone. P6 skipped |
| D2, D3, D4, D5 | **Wait on the client.** Not built; their chips are never served |
| D6 | Our curated list (`search_tag`) |
| D7 | The Figma's rating chips, no 4.5 |
| D8 | "Parking" = amenity or badge; "Free Parking" only from the `FREE_PARKING` badge |
| D9 | New `/search/...` routes; old routes only got switched additive fields |
| D10 | Both: the server line fixed (`CARD_NEXT_OPENING_V1`), and the FE guide builds "Opens tomorrow at" from `next_opening` |
| D11 | Yes, on N2 only |
| D12 | Stylists yes, products later |
| D13 | Keep strict |
| D14 | The salon's Packages tab rules (one switch decides both) |
| D15 | One number in v1 |

### 6.2 What was built

| Phase | PR (merge) | Switch | Live |
| ----- | ---------- | ------ | ---- |
| P1: X1 pin logo, X2 Figma rating chips, X3 service currency, X4 next opening line | #61 (f98c771) | `MAP_PIN_LOGO_V1`, `MAP_RATING_FIGMA_V1`, `SERVICE_CURRENCY_V1`, `CARD_NEXT_OPENING_V1` | on, proven (X4's closed-salon line still to be seen after closing time) |
| P2: N2 `/search/salons`, N3 `/search/services` | #62 (2806d92) | `SEARCH_RESULTS_V1` | on, proven |
| P3: N1 `/search/options`, `quick` + `service_tags`, table `search_tag` (salons 0003, 0004) | #63 (4a8faea) | `SEARCH_OPTIONS_V1` | on, proven |
| P5: N4 `/search/suggest`, throttled | #64 (d1e88b0) | `SEARCH_SUGGEST_V1` | on, proven |
| P8: FE guides | this docs PR | none | `docs/MOBILE_DISCOVERY_FE.md` (all screens), `SEARCH_RESULTS_API.md`, `SEARCH_OPTIONS_API.md`, `SEARCH_SUGGEST_API.md`; older guides updated |
| P4: regions | not built | | waits on D3 |
| P6: recent searches on the server | not built | | skipped (D1) |
| P7: Verified, Mall, Home Service, Metro, Membership, points | not built | | waits on D2, D4, D5 |
| X5: the parking chip on the map drawer (`feat/map-filter-parking`, 5f5777a, never pushed) | not needed | | the Smart Search's parking now comes from `/search/options` amenities (D8); the branch can be dropped |

Found on the way and fixed under X4: `next_opening` always started from tomorrow, so
before today's opening it named tomorrow.

### 6.3 The summary table, now

| Status | Then | Now |
| ------ | ---- | --- |
| AVAILABLE | 16 | 30 |
| PARTIAL | 12 | 3: 1.4 products (D12, later), 2.5 Verified (D2), 6.2 duration range (D15, later) |
| MISSING | 7 | 2: 3.1 and 3.2, recent searches, on the phone by D1 |
| DATA MISSING | 7 | 7 (wait on D2 to D5) |

### 6.4 Seen in the live data (for the business, not code)

- No salon has a review yet, so ratings show "New" and rating filters keep nothing.
- No live service has a photo, so the Services tab's `images` are all `[]`.
- Look Change is in Dhaka but its prices are saved in AED.
- "staff man nhl" at Demo Salon & Spa looks like a test account, shown to customers.
- `availability=today` found no salon with a start left on the day it was tested (the map
  says the same): check the rosters.

### 6.5 Left for Rafa

- After a salon's closing time, run the card curl once: a closed salon should read
  "Opens tomorrow at ...".
- `bash scripts/stack-env-check.sh` on the server: it should print only `check done`.
- `_grpc` and `_worker` still run an older image (fine for this work; update them with the
  next deploy so the purge from PR #60 is current).
