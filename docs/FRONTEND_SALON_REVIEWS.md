# Salon Ratings & Reviews: FE integration guide

Date: 2026-11-16. For: the mobile app team.

This guide describes what the server **does today**, read from the code. It
is the one to build from. Your request, `docs/salon-reviews-fe-contract.md`,
was built as written. The answers to its open points and the few additions
are in [section 4](#4-answers-to-your-open-points) and
[section 5.3](#53-what-is-added-to-salon-reviews-fe-contractmd).

Base URL: `https://api.gostyle.uk/api/v1`

**Status: live on production since 2026-11-16, behind the server switch
`SALON_REVIEWS_V1`, which is on.** What the app sees if it is ever turned
off is in [section 5.5](#55-status-and-the-server-switch).

Ids, names, hosts and paths in the examples are made up. Their shape is
real. Always use the values the server sends.

---

## Contents

1. [Opening the screen: `GET /salon/{salon_id}/reviews`](#1-opening-the-screen-get-salonsalon_idreviews)
2. [Helpful: `PUT` and `DELETE /review/{review_id}/helpful`](#2-helpful-put-and-delete-reviewreview_idhelpful)
3. [Errors](#3-errors)
4. [Answers to your open points](#4-answers-to-your-open-points)
5. [Practical notes](#5-practical-notes): auth, worked example, additions,
   checklist, status

---

## 1. Opening the screen: `GET /salon/{salon_id}/reviews`

### 1.1 The request

```
GET /salon/5d6a110f-5633-4bfe-942f-19be062a4373/reviews?sort=top&page=1&pageSize=20
Authorization: Bearer <access token>   (optional)
```

`{salon_id}` is the id `GET /salon/{id}` takes. Pass the same id the salon
profile was opened with.

| Query | Type | Default | What the server does |
|---|---|---|---|
| `sort` | string | `top` | `top`, `newest`, `highest` or `lowest`. Absent or empty is `top`. Anything else is a `422` ([section 3](#3-errors)). |
| `page` | int | `1` | 1-based. Not a whole number above 0 (`0`, `-1`, `abc`): page 1. |
| `pageSize` | int | `20` | Not a whole number above 0: 20. Above 50: 50. `page_size` works too, as on `GET /bookings`. |
| `has_photos` | string | (off) | Only the exact value `true` turns it on: only the reviews that have photos. See [section 1.7](#17-with-photos-has_photostrue). |

Reading is public: with no token, the answer is the same, with `is_helpful`
and `is_mine` always `false`. Anyone who can open the salon profile can open
its reviews; a salon the profile answers `404` for is a `404` here too.

### 1.2 The answer: `200 OK`

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
      "is_mine": false,
      "photos": [
        { "id": "5e0a1c9b-81c2-4d7e-9f30-6b2a8d4c1e01", "url": "https://cdn.example.com/reviews/1.jpg" }
      ],
      "tags": [
        { "code": "cleanliness", "label": "Cleanliness" },
        { "code": "ambience", "label": "Ambience" }
      ]
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
      "is_mine": true,
      "photos": [],
      "tags": []
    }
  ]
}
```

The keys come in this order, but do not depend on the order.

### 1.3 `summary`: the card and the bars

| Field | Type | Null? | Notes |
|---|---|---|---|
| `rating` | number, 1 decimal | can be `null` | Exactly the salon profile's `rating`, read the same way from the same place. `null` when nobody has reviewed the salon. |
| `review_count` | integer | never | Exactly the salon profile's `review_count`. `0` for a salon nobody has reviewed. |
| `distribution` | array | never | Always 5 entries, `stars` 5 down to 1, `count: 0` where there are none. The counts add up to `review_count`. |

- `summary` is the same on every page, for every `sort`, and with
  `has_photos=true`. Draw the card from the first page you have and keep it.
- Draw each bar as `count / review_count` of its width. When `review_count`
  is `0`, draw every bar empty (do not divide by 0).
- `review_count: 0` is the empty screen.

### 1.4 `results[]`: one card per review

| Field | Type | Null? | Notes |
|---|---|---|---|
| `id` | string (uuid) | never | The review's id. Helpful uses it. |
| `stars` | integer, 1 to 5 | never | The stars the customer gave the salon. |
| `comment` | string | can be `null` | As the customer wrote it (trimmed, up to 500 characters). `null` when they wrote none: show the card without text. |
| `submitted_at` | string | never | UTC, to the second, always ending in `Z`: `2026-11-12T09:41:07Z`. Word it yourself ("Just now", "3 hours ago", "1 day ago"). |
| `author.name` | string | can be `null` | Ready to print, already shortened ([section 4.2](#42-the-reviewers-name)). `null`: show "Go Style member". |
| `author.avatar_url` | string (full URL) | can be `null` | The customer's profile photo. `null`: show their initial, or a plain avatar when `name` is `null` too. |
| `services` | `{ id, name }[]` | never | The services done on the reviewed visit, in the order they were done. Show the first `name` on the chip, with "+1" and so on for the rest. `[]`: hide the chip. |
| `helpful_count` | integer | never | How many customers marked it helpful. |
| `is_helpful` | boolean | never | This customer has marked it. `false` signed out. |
| `is_mine` | boolean | never | This customer wrote it. `false` signed out. On their own review show the count, but no Helpful button. |
| `photos` | `{ id, url }[]` | never | Added for a later build: the review's photos (up to 3), in the order the customer added them. `url` is a full URL. `[]` when none. |
| `tags` | `{ code, label }[]` | never | Added for a later build: the salon tags the customer picked, with the server's labels, in the order picked. `[]` when none. |

Every review of the salon is listed, with or without a comment. Stylist
ratings are not on this screen.

`services` can be `[]` on a review that did have services, in one rare case:
the server could not read the visit from the booking service at that moment.
The rest of the card is still right. Hide the chip; the next load fills it.

### 1.5 Order

| `sort` | Chip | Order |
|---|---|---|
| `top` | Top Reviews | `helpful_count` high to low, then reviews with a comment first, then newest first. |
| `newest` | Newest | `submitted_at` newest first. |
| `highest` | Highest Rating | `stars` high to low, then newest first. |
| `lowest` | Lowest Rating | `stars` low to high, then newest first. |

Every order ends on the review id, so a page never repeats or skips a review
when two tie.

`top` moves when customers tap Helpful. If someone marks a review between
your page 1 and page 2, a review can move across the page edge. Pull down to
reload starts again from page 1.

### 1.6 Paging

`count`, `next`, `previous` and `results` page like `GET /bookings`:

- `count`: how many reviews this list has, all pages together.
- `next`: the full URL of the next page, or `null` on the last page. It keeps
  your `sort`, `has_photos=true` (when you sent it) and `pageSize`. Load more
  by calling it as it is.
- `previous`: the full URL of the page before, or `null` on page 1.
- A page past the end is a `200` with `results: []` and `next: null`.

Stop loading more when `next` is `null`. Do not stop on
`count == summary.review_count`: the two are equal for the four sorts today,
but not with `has_photos=true` (see below), and
[section 5.3](#53-what-is-added-to-salon-reviews-fe-contractmd) has the one
other case where they can differ.

### 1.7 With photos: `has_photos=true`

Your suggestion in place of Featured Products ([section 4.1](#41-featured-products)).

```
GET /salon/5d6a110f-5633-4bfe-942f-19be062a4373/reviews?sort=newest&has_photos=true
```

- `results` has only the reviews with at least one photo, in the order of
  `sort` (any of the four works).
- `count`, `next` and `previous` cover only those.
- `summary` stays the whole salon's.

---

## 2. Helpful: `PUT` and `DELETE /review/{review_id}/helpful`

```
PUT /review/3e9b6c1d-8a24-4f70-b5d2-7c1e0f9a6b43/helpful
DELETE /review/3e9b6c1d-8a24-4f70-b5d2-7c1e0f9a6b43/helpful
Authorization: Bearer <access token>
```

No body. `PUT` marks the review helpful for this customer, `DELETE` takes the
mark back. Both answer `200 OK` with the saved state:

```json
{
  "is_helpful": true,
  "helpful_count": 14
}
```

- **Safe to repeat.** `PUT` on a review already marked, or `DELETE` on one
  not marked, changes nothing and answers the current state with `200`. A
  double tap or a retry never counts twice.
- **Take the numbers from the answer.** Tick the button at once, then set
  `is_helpful` and `helpful_count` from the answer. Other customers may have
  tapped meanwhile, so the count can jump by more than one.
- **Signed in only.** A guest gets `401`. Ask them to sign in first.
- **Not on your own review.** `PUT` on a review the customer wrote is a `403`
  `own_review`. `is_mine: true` means: no button. (`DELETE` on your own
  review is a harmless `200` with `is_helpful: false`.)
- **If the call fails**, put the button back as it was. Nothing was saved.
- There is no rate limit on these two calls.

When a customer deletes their account, their Helpful marks go with it, and
each review they marked counts one less.

---

## 3. Errors

Every error uses the usual envelope (`detail`, `code`, `errors[]` with
`field`, `code`, `message`), as in `auth-error-response.md`. Branch on
`errors[0].code`. Show `message` as it is.

| Call | Status | `errors[0].code` | `field` | `message` | When | App does |
|---|---|---|---|---|---|---|
| `GET` | `404` | `not_found` | `null` | `Salon not found` | No such salon, or one the profile does not show. | "This salon isn't available" with Go back. |
| `GET` | `422` | `invalid_sort` | `sort` | `Please choose top, newest, highest or lowest.` | `sort` is not one of the four. | App bug. Fall back to `top`. |
| `GET` | `401` | (auth codes) | | | A token was sent and it is bad or expired. No token at all is fine. | Refresh the token and retry, as on `GET /salon/{id}`. |
| `PUT`, `DELETE` | `401` | (auth codes) | | | No token, or it has expired. | Refresh the token and retry, or ask the guest to sign in. |
| `PUT`, `DELETE` | `404` | `review_not_found` | `null` | `We couldn't find this review.` | No such review (also an id that is not a uuid). | Put the button back and reload the list. |
| `PUT` | `403` | `own_review` | `null` | `You can't mark your own review as helpful.` | The customer wrote this review. | App bug. Put the button back. |

The top-level `code` is the usual one for the status (`not_found`,
`validation_error`, `permission_denied`, `not_authenticated`). Examples:

```json
{
  "detail": "Please correct the highlighted fields.",
  "code": "validation_error",
  "errors": [
    { "field": "sort", "code": "invalid_sort", "message": "Please choose top, newest, highest or lowest." }
  ]
}
```

```json
{
  "detail": "You can't mark your own review as helpful.",
  "code": "permission_denied",
  "errors": [
    { "field": null, "code": "own_review", "message": "You can't mark your own review as helpful." }
  ]
}
```

A `{salon_id}` that is not a uuid at all gets a plain `404` page with no
envelope, as `GET /salon/{id}` does. Always pass the id the profile used.

---

## 4. Answers to your open points

### 4.1 Featured Products

Built as you suggested: **With photos**, as `?has_photos=true`, with `count`
covering only those ([section 1.7](#17-with-photos-has_photostrue)).
Show the chip as "With photos" in place of Featured Products.

### 4.2 The reviewer's name

The server sends the **first name and the last name's initial**, as you
suggested. The last name is everything after the first word, so:

| Name on the account | `author.name` |
|---|---|
| `Omar Al Fahim` | `Omar A.` |
| `Layla Hassan` | `Layla H.` |
| `Sara` (one word) | `Sara` |
| no name at all | `null` |

The privacy switch "Show my name to salons" (`show_name_to_salons`) does not
change this name: the name here is always short, whatever the switch says.
Print `author.name` as it comes; do not shorten it again.

### 4.3 Reviews from deleted accounts

Kept, as you suggested: they still count in the rating, the bars and the
list, with `author.name: null` and `author.avatar_url: null`. This happens
once the account is deleted for good (at the end of its 30 days). Show
"Go Style member".

### 4.4 Photos and tags

Sent now on every review, as `photos` (`{ id, url }[]`) and `tags`
(`{ code, label }[]`) ([section 1.4](#14-results-one-card-per-review)). The
design does not show them yet; a later build can, with no API change.

### 4.5 A stylist's reviews

Not built (your doc: not needed for this release). There is no
`GET /stylist/{stylist_id}/reviews` yet.

### 4.6 Reporting a review

Not built. It needs its own doc. There is also no hiding of reviews yet, so
today `review_not_found` only means "no such review".

---

## 5. Practical notes

### 5.1 Base URL and auth

- Base URL: `https://api.gostyle.uk/api/v1`.
- `GET /salon/{salon_id}/reviews`: token optional. Send it when the customer
  is signed in, so `is_helpful` and `is_mine` are theirs. A bad or expired
  token is a `401`, not "signed out": refresh it as on every other call.
- `PUT` / `DELETE /review/{review_id}/helpful`: token required.

### 5.2 Worked example

1. The customer taps "4.8 (320 reviews)" on the salon profile.
   `GET /salon/5d6a110f-.../reviews?sort=top` (20 a time by default).
   Draw the card from `summary` and the list from `results`.
2. They scroll to the end: call `next` as it is. Stop when `next` is `null`.
3. They tap Newest: `GET /salon/5d6a110f-.../reviews?sort=newest`, page 1.
   Keep each chip's pages, so coming back to Top is instant.
4. They tap Helpful on Omar's review (`is_mine: false`, `is_helpful: false`,
   `helpful_count: 13`). Tick it at once, then
   `PUT /review/3e9b6c1d-.../helpful`. The answer
   `{ "is_helpful": true, "helpful_count": 14 }` sets the button.
5. They tap it again: untick at once, then `DELETE` the same URL. The answer
   `{ "is_helpful": false, "helpful_count": 13 }` sets the button.
6. On Layla's review (`is_mine: true`) show "Helpful (0)" with no button.
7. Signed out: the same list with every `is_helpful` and `is_mine` `false`.
   A tap on Helpful asks them to sign in first.

### 5.3 What is added to `salon-reviews-fe-contract.md`

Everything in your doc is built as written. On top of it:

- `photos` and `tags` on each review ([section 4.4](#44-photos-and-tags)).
- `has_photos=true` ([section 4.1](#41-featured-products)).
- `page_size` is accepted as well as `pageSize`, as on `GET /bookings`.

One thing to know about `count` and `review_count`: the salon profile's
rating also counts reviews published on the salon's own website (the
storefront's reviews), and so do `summary` and the bars. Those are not app
reviews, so they are not in `results`. On production today there are none,
so for the four sorts `count` equals `summary.review_count`. If a salon ever
has website reviews, its `count` is lower than `review_count` by that many.
This is why the list should end on `next: null`
([section 1.6](#16-paging)).

### 5.4 Checklist for the app

- [ ] Pass the salon's id from the profile into the screen.
- [ ] First page: `GET /salon/{salon_id}/reviews?sort=top`, with the token
      when signed in.
- [ ] Card from `summary`; bars as `count / review_count`, empty when
      `review_count` is `0`.
- [ ] Empty screen when `summary.review_count` is `0`.
- [ ] Load more with `next` as it is; stop on `next: null`.
- [ ] Chips send `sort`; "With photos" sends `has_photos=true`. Featured
      Products is gone.
- [ ] Card: `author.name` (`null`: "Go Style member"), `avatar_url`
      (`null`: initial), `submitted_at` worded on the phone, `stars`,
      `comment` (`null`: no text), chip from `services[0]` with "+N".
- [ ] Helpful: hidden on `is_mine`; ticked from `is_helpful`; tick at once,
      then take both numbers from the answer; put it back on failure.
- [ ] Guest taps Helpful: ask to sign in, no call.
- [ ] `404 review_not_found`: put the button back and reload the list.
- [ ] `404 not_found` on the list: "This salon isn't available".

### 5.5 Status and the server switch

All three calls are behind the server switch `SALON_REVIEWS_V1`, **on in
production since 2026-11-16**. If it is ever turned off, all three answer
`404` with `code: "not_found"` (`detail: "Not found"`), and the app shows
"This salon isn't available". Nothing else in the app changes with it.
