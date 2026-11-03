# Search: popular services and popular salons

> The mobile team's request, as received 2026-11-03. Kept unchanged. What the
> server really does is docs/SEARCH_POPULAR_API.md.

Base URL: https://api.gostyle.uk/api/v1 Auth: optional on both, as on /discover today. Send the token when signed in.

Date: 2026-11-03. From: the mobile team. For the search over the map (src/components/SearchModal.tsx, Figma 3777:5634).

## 0. What we are asking for

A new GET /services: a list of services from every salon, with two params, is_popular and search, and optionally the customer's position.
GET /discover: one new param, is_popular. Everything else, search included, stays as it is.

## 1. Why

The search opens on three blocks. Two are hard coded in the app today:

| Block | Shows | Needs |
| ----- | ----- | ----- |
| Recent searches | What this customer searched before | Nothing: kept on the phone |
| Popular Services | A row of cards: the service's name, price, two lines of its description | GET /services?is_popular=true |
| Popular Near You | A row of salons: a square photo and the name | GET /discover?is_popular=true |

And while the customer types, the search shows matching services and salons (see §4).

## 2. New: GET /services

### 2.1 Request

```
GET /services?is_popular=true&latitude=25.1972&longitude=55.2744&radius=500&page=1&pageSize=10
GET /services?search=fade&latitude=25.1972&longitude=55.2744&radius=500&page=1&pageSize=20
GET /services?page=1&pageSize=20
```

| Param | Type | Required | Notes |
| ----- | ---- | -------- | ----- |
| is_popular | boolean | no | true: most popular first (2.3). Missing or false: newest first |
| search | string | no | Matches the service's name or its category label, case-insensitive (2.5). Empty is the same as missing |
| latitude, longitude | number | no | The customer's position. Sent: only services at salons within radius of it (2.6) |
| radius | number | no | The same meaning and unit as on /discover, which the app already sends |
| page | number | no | From 1, as on /discover |
| pageSize | number | no | Default 20, at most 50, as on /discover |

All of them can be sent together: services near the customer matching search, most popular first.

### 2.2 Response: 200 OK

The same page shape as /discover, so the app reads it with the paging it already has:

```json
{
  "count": 42,
  "next": "https://api.gostyle.uk/api/v1/services?is_popular=true&page=2&pageSize=10",
  "previous": null,
  "results": [
    {
      "id": "srv_gentlemans_cut",
      "name": "The Gentleman's Cut",
      "description": "A classic haircut tailored to your preferences, including a wash, scalp massage, and styled finish.",
      "price": 180,
      "duration_min": 20,
      "duration_max": 30,
      "category": { "id": "haircut_styling", "label": "Haircut & Styling" },
      "salon": {
        "id": "c6c248ab-f2cd-4f12-a31e-243c6e64b3b5",
        "name": "Del Capo Barber",
        "category": "gents"
      }
    }
  ]
}
```

| Field | Type | Notes |
| ----- | ---- | ----- |
| id | string | The service's id, the same as on GET /salon/:id/services |
| name | string | Bold on the card, at most two lines |
| description | string or null | Two lines on the card, then cut |
| price | number | One visit, in AED, as on GET /salon/:id/services |
| duration_min, duration_max | number or null | Not on the card yet; for the search results list |
| category | object | The salon's category for it, as on GET /salon/:id/services |
| salon.id | string | Tapping a card opens GET /salon/:salon_id/service/:service_id, which needs it |
| salon.name | string | Shown under the service in the search results list |
| salon.category | string | gents, ladies or unisex; the details screen opens in that salon's look |

Nothing matches: 200 with "count": 0 and "results": []. The app hides the row, or shows its "No results found" card while searching.

### 2.3 What is_popular means

Our suggestion; change it if your data says otherwise:

- Popular: the number of bookings of that service in the last 30 days, most first, leaving cancelled bookings out. Ties: the salon's rating, then the newest service.
- At most 2 services from the same salon on a page, so one busy salon does not fill the row.
- Without is_popular: newest first (the service's created_at).

### 2.4 Which services are listed

- Only active services, open to online booking, at salons that are live.
- Each salon's service is its own row: "Haircut" at two salons is two results, each with its own price and salon.

### 2.5 What search matches

- The service's name, and its category.label: "colour" finds "Root Colour" by its name and "Balayage" filed under "Hair Colour".
- Nothing else: not the description, not the salon's name. Salons are searched with /discover?search=.
- A match anywhere in the text, not only at the start, ignoring case.

### 2.6 Why the position

A service is booked at one salon, at that salon's price. A popular cut in another city is no use to someone looking for a cut today, so with latitude and longitude only salons within radius of the customer count, for both is_popular and search. Without them, every salon counts: that is the customer who has not shared their location, and the list is then the whole country rather than empty.

## 3. GET /discover: a new is_popular param

The "Popular Near You" row. Every param /discover takes today stays, search included.

```
GET /discover?is_popular=true&category=all&latitude=25.1972&longitude=55.2744&radius=500&page=1&pageSize=10
```

| Param | Type | Notes |
| ----- | ---- | ----- |
| is_popular | boolean | true: the most popular salons first (3.1). Missing or false: today's order |

The response does not change. The row shows logo_url, else cover_url, and name; a tap opens the salon's profile.

### 3.1 Popular by rating and how many rated it

Sorting by rating alone puts a salon with one 5-star review above one with 4.8 from 300 reviews. To weigh both, we suggest a weighted rating:

```
score = (v / (v + m)) * R + (m / (v + m)) * C
```

| Symbol | Meaning |
| ------ | ------- |
| R | The salon's rating |
| v | How many reviews it has (review_count) |
| C | The average rating across all live salons |
| m | How many reviews before a rating is fully trusted, for example 10 |

- Highest score first. Ties: more reviews first, then distance when latitude and longitude are sent.
- Salons with no reviews yet come after every rated one, so a quiet area is not empty.
- Every other param (category, search, is_open_now, is_top_rated, latitude, longitude, radius) still narrows the list exactly as today; is_popular decides only the order.

## 4. What the app will do

Every call below carries latitude, longitude and radius when the app has the customer's position, and leaves them out when it does not.

- On open:
  - GET /services?is_popular=true&pageSize=10 for Popular Services.
  - GET /discover?is_popular=true&pageSize=10 for Popular Near You.
  - Either row is hidden when its results is empty.
- While typing: after 2 characters, 400 ms after the last key, two calls side by side:
  - GET /services?search=<text>: a "Services" list, each with its salon.
  - GET /discover?search=<text>: a "Salons" list.
  - "No results found" shows only when both come back empty.
- Taps: a service opens its details; a salon opens its profile.
- Recent searches stay on the phone, as today: nothing from you.
- The search bar now reads "Search salons or services...", which is what these two calls cover.

## 5. Errors

- A value that is not a boolean for is_popular, or a pageSize out of range: 422, in the envelope every other route uses.
- latitude without longitude, or the other way round: 422.
- No match is never an error: 200 with an empty results.
