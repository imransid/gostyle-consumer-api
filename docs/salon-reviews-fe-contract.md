# Salon Ratings & Reviews API

Base URL: https://api.gostyle.uk/api/v1 Auth: optional for reading, as for GET /salon/{id}. Required for Helpful.

Date: 2026-11-13. From: the mobile team.

Ratings & Reviews (src/app/(salon)/ratings.tsx) opens from the salon profile when the customer taps "4.8 (320 reviews)" under the salon's name. The screen has:

- a header with Back, the title and a Share button;
- a summary card: the average ("4.8"), five stars, "(320 reviews)", and one bar per star count from 5 down to 1, each with its count;
- sort chips: Top Reviews, Newest, Highest Rating, Lowest Rating, Featured Products;
- the reviews, one card each: the customer's photo and name, "1 day ago", the stars they gave, their comment, a chip with the service ("Modern Fade"), and Helpful (13), which the customer can tap to mark the review helpful.

Everything on it is demo data today: the score, the bars, four made-up reviews, and Helpful, which only counts in memory. The screen is not even told which salon it is for; the app will pass the salon's id once this API exists.

The reviews themselves already exist: they are what customers send with Rate your visit (docs/api/rate-your-visit.md). This doc asks for a way to read them back for a salon, and for the Helpful button.

| Method | Endpoint | What for |
|---|---|---|
| GET | /salon/{salon_id}/reviews | Fills the screen, one page at a time. |
| PUT | /review/{review_id}/helpful | The customer marks a review helpful. |
| DELETE | /review/{review_id}/helpful | The customer takes their mark back. |

Ids, names and URLs in the examples are made up. Their shape is what we need.

## Rules for every part

- One source for the numbers. The salon profile's rating and review_count and this screen's summary must always agree. A customer who taps "4.8 (320 reviews)" must land on 4.8 and 320.
- A salon's review is the Review row from Rate your visit: its salon_stars, its comment, its date. Stylist ratings are not shown on this screen.
- Every review is listed, with or without a comment, so the list adds up to review_count. A review with no comment shows without text.
- Reading is public. Anyone who can open the salon profile can read its reviews, signed in or not. With a token, is_helpful and is_mine are about that customer; without one they are false.
- Errors use the usual envelope (detail, code, errors[] with field, code, message), as in auth-error-response.md. The app branches on errors[i].code and shows message as it is.

## 1. GET /salon/{salon_id}/reviews

```
GET /salon/5d6a110f-5633-4bfe-942f-19be062a4373/reviews?sort=top&page=1&pageSize=20
Authorization: Bearer <access token>   (optional)
```

{salon_id} is the id GET /salon/{id} takes.

| Query | Type | Required | Default | Notes |
|---|---|---|---|---|
| sort | string | no | top | top, newest, highest or lowest. See [Order](#order). |
| page | int | no | 1 | As on GET /bookings. |
| pageSize | int | no | 20 | Capped at 50, as on GET /bookings. |

### Where each part of the screen comes from

| On screen | Field |
|---|---|
| "4.8" and the five stars | summary.rating |
| "(320 reviews)" | summary.review_count |
| The bars and their counts, 5 down to 1 | summary.distribution[] |
| The sort chips | the app's own, sent as sort |
| Photo and name on a card | results[].author |
| "1 day ago" | results[].submitted_at (the app words it) |
| The stars on a card | results[].stars |
| The comment | results[].comment |
| The chip ("Modern Fade") | results[].services |
| "Helpful (13)", and whether it is ticked | results[].helpful_count, results[].is_helpful |
| Helpful not offered on the customer's own review | results[].is_mine |
| The empty message | summary.review_count: 0 |

200 OK

```json
{
  "summary": {
    "rating": 4.8,
    "review_count": 320,
    "distribution": [
      { "stars": 5, "count": 280 },
      { "stars": 4, "count": 26 },
      { "stars": 3, "count": 8 },
      { "stars": 2, "count": 4 },
      { "stars": 1, "count": 2 }
    ]
  },
  "count": 320,
  "next": "https://api.gostyle.uk/api/v1/salon/5d6a110f-5633-4bfe-942f-19be062a4373/reviews?sort=top&page=2&pageSize=20",
  "previous": null,
  "results": [
    {
      "id": "3e9b6c1d-8a24-4f70-b5d2-7c1e0f9a6b43",
      "stars": 5,
      "comment": "Best fade I have had in Dubai. In and out in forty minutes.",
      "submitted_at": "2026-11-12T09:41:07Z",
      "author": {
        "name": "Omar A.",
        "avatar_url": "https://cdn.example.com/customers/omar.jpg"
      },
      "services": [
        { "id": "55a98876-efdc-4d04-b4bc-f783ced6a8fb", "name": "Modern Fade" },
        { "id": "7a7fafa5-bc8d-485e-b1ef-15a8f1634906", "name": "Beard Trim" }
      ],
      "helpful_count": 13,
      "is_helpful": false,
      "is_mine": false
    },
    {
      "id": "c40f2e87-1b5a-4d39-9e6c-0a8d7f3b2e15",
      "stars": 4,
      "comment": null,
      "submitted_at": "2026-11-11T17:20:44Z",
      "author": {
        "name": "Layla H.",
        "avatar_url": null
      },
      "services": [
        { "id": "58c49adf-2781-40b3-8ec2-cb2beabe2575", "name": "Hair Cut & Spa" }
      ],
      "helpful_count": 0,
      "is_helpful": false,
      "is_mine": true
    }
  ]
}
```

count, next, previous and results page exactly like GET /bookings. summary comes on every page, whatever the sort, so the card never depends on the chip. While all four sorts list every review, count equals summary.review_count.

### summary

| Field | Type | Null? | Notes |
|---|---|---|---|
| rating | number, 1 decimal | can be null | Exactly the salon profile's rating: rating_sum / review_count, rounded to one decimal. null when there are no reviews. |
| review_count | integer | never | Exactly the salon profile's review_count. 0 for a salon nobody has reviewed yet. |
| distribution | array | never | Always five entries, stars 5 down to 1, with count: 0 where there are none. The counts add up to review_count. |

The app draws each bar as count / review_count of its width, so the server sends counts only.

### results[]

| Field | Type | Null? | Notes |
|---|---|---|---|
| id | string (uuid) | never | The Review id. Helpful uses it. |
| stars | integer, 1 to 5 | never | The review's salon_stars. |
| comment | string | can be null | As the customer wrote it. null when they wrote none. |
| submitted_at | ISO 8601, UTC | never | When the review was sent. The app shows "Just now", "3 hours ago", "1 day ago" and so on. |
| author.name | string | can be null | The name to print, already shortened the way it should appear (we suggest first name and last initial). null when the account is gone: the app shows "Go Style member". |
| author.avatar_url | string (full URL) | can be null | The customer's profile photo. null: the app shows their initial. |
| services | { id, name }[] | never | The services done on the reviewed visit, in the order they were done; cancelled ones left out. The app shows the first name on the chip, with "+1" and so on for the rest. [] hides the chip. |
| helpful_count | integer | never | How many customers marked it helpful. |
| is_helpful | boolean | never | Whether this customer has marked it. false signed out. |
| is_mine | boolean | never | Whether this customer wrote it. false signed out. On their own review the app shows the count but no button. |

### Order

| sort | Chip | Order |
|---|---|---|
| top | Top Reviews | helpful_count high to low, then reviews with a comment first, then newest first. |
| newest | Newest | submitted_at newest first. |
| highest | Highest Rating | stars high to low, then newest first. |
| lowest | Lowest Rating | stars low to high, then newest first. |

Every order ends on the review id, so a page never repeats or skips a review when two tie.

Featured Products has no sort yet: see [Open points](#6-open-points). Until it does, the app leaves that chip out.

## 2. Helpful

```
PUT /review/3e9b6c1d-8a24-4f70-b5d2-7c1e0f9a6b43/helpful
DELETE /review/3e9b6c1d-8a24-4f70-b5d2-7c1e0f9a6b43/helpful
Authorization: Bearer <access token>
```

No body. PUT marks the review helpful for this customer, DELETE takes the mark back. Both answer 200 OK with the new state, never the one the app guessed, as PUT and DELETE /salon/{id}/follow do:

```json
{
  "is_helpful": true,
  "helpful_count": 14
}
```

- Both are idempotent. PUT on a review already marked, or DELETE on one not marked, changes nothing and answers the current state with 200. A double tap or a retry can never count twice.
- Signed in only. A guest who taps Helpful is asked to sign in first.
- Not on your own review. PUT on a review the customer wrote is a 403 (see [Errors](#4-errors)). The app never offers it (is_mine).

The app ticks the button at once, then takes the numbers from the answer. If the call fails it puts the button back.

## 3. Storage

Reviews are stored as Rate your visit's [Storage](rate-your-visit.md#4-storage-required) says. This doc adds:

- Per-star counts. distribution must come from the same reviews as rating_sum and review_count. Either keep five counters on the Salon row next to them, updated in the same transaction, or count Review.salon_stars grouped by star for the salon. Both are fine; the numbers must add up.
- Helpful marks: a ReviewHelpful table with review_id, customer_id and created_at, and a unique constraint on (review_id, customer_id), so a customer counts once. Keep helpful_count on the Review, updated in the same transaction as the mark, so top can sort on it with an index on (salon_id, helpful_count).
- Indexes for the sorts: (salon_id, submitted_at) and (salon_id, salon_stars, submitted_at) on Review.
- Deleting an account removes that customer's Helpful marks (each helpful_count goes down). Their reviews stay and still count in the rating, with author.name: null and author.avatar_url: null, unless you decide otherwise (see [Open points](#6-open-points)).
- Hiding a review (if moderation ever needs it) takes it out of the list and out of rating, review_count and distribution together, so the bars always add up.

## 4. Errors

| Call | Status | errors[0].code | field | When | App does |
|---|---|---|---|---|---|
| GET | 404 | not_found | null | No such salon, as GET /salon/{id} answers today. | Shows "This salon isn't available" with Go back. |
| GET | 422 | invalid_sort | sort | Not one of top, newest, highest, lowest. | App bug. Falls back to top. |
| PUT, DELETE | 401 | (auth codes) | | No token, or it has expired. | Refreshes the token and retries, or signs in. |
| PUT, DELETE | 404 | review_not_found | null | No such review, or it has been hidden. | Puts the button back and reloads the list. |
| PUT | 403 | own_review | null | The customer wrote this review. | App bug. Puts the button back. |

Nothing is saved when a call fails.

## 5. What the app does with it

So you can see why each field is asked for:

1. The salon profile's "4.8 (320 reviews)" opens the screen with the salon's id. The screen asks for GET /salon/{salon_id}/reviews?sort=top, 20 at a time, and loads more as the customer scrolls. A skeleton shaped like the card and three reviews shows while the first page loads; pull down reloads.
2. Draws the summary card from summary and the bars from distribution.
3. A sort chip asks for page 1 again with its sort. Each chip keeps its own pages, so going back to one already seen is instant.
4. Draws each card from results[], the chip from the first of services.
5. Helpful: PUT or DELETE, ticking the button at once and taking is_helpful and helpful_count from the answer. Signed out, it asks the customer to sign in.
6. Share: shares the salon's own link, as the salon profile does. It needs nothing from this API.

## 6. Open points

1. What is "Featured Products"? It reads like reviews of products the salon sells, but Rate your visit rates the salon and its stylists, not products, so there is nothing to filter on. Our suggestion: replace it with With photos (reviews that have photos, which customers can already attach), as ?has_photos=true with count then covering only those. Tell us which you want; until then the app leaves the chip out.
2. The reviewer's name. The design prints full names ("Omar Al Fahim"). We suggest first name and last initial ("Omar A."), since these reviews are public. Privacy has a "Show my name to salons" switch (show_name_to_salons); should it also hide the name here? The app prints whatever author.name says, so this is the server's call.
3. Reviews from deleted accounts. We suggest keeping them (they are part of the salon's rating) with no name or photo. If they should go, they must leave the totals and distribution too.
4. Photos and tags. A review can have up to 3 photos and salon tags ("Cleanliness", "Ambience"), but the design shows neither on this screen. If you send photos ({ id, url }[]) and tags ({ code, label }[]) on each review now, a later build can show them without another API change.
5. A stylist's reviews. The expert profile shows a rating too. The same shape at GET /stylist/{stylist_id}/reviews, built on ReviewStylistRating, would let this screen serve stylists later. Not needed for this release.
6. Reporting a review is not in the design. If the salon or customers should be able to flag one, it needs its own doc.
