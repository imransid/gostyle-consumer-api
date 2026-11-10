# Rate your visit API

After a visit, the customer opens **Rate your visit** from the receipt or from the booking. On this screen they rate:

- the salon (stars and tags);
- each stylist who worked on the booking (stars and tags);
- optionally, with a comment and up to 3 photos.

Everything on the screen is hard-coded in the app today. This doc covers:

1. [`GET /booking/:id/review`](#1-get-the-review-form): fills the screen.
2. [`POST /booking/:id/review`](#2-post-the-review): submits the review.
3. [`is_rated` and `can_rate` on bookings](#3-is_rated-and-can_rate-on-bookings): so the booking list and details know whether to offer "Rate".
4. [Storage](#4-storage-required): how reviews **must** be stored.

## Rules for every part

- **Only completed bookings** can be rated.
- **The salon rating is required** (`salon.stars`).
- **Stylist ratings are optional.** A stylist the customer skips or leaves unrated gets the salon rating's stars by default. See [Stylists the customer didn't rate](#stylists-the-customer-didnt-rate).
- **One review per booking.**
- **A review is final.** The customer can't edit or delete it, so there is no customer PATCH, PUT or DELETE.
- **Only the booking's owner** can read the form or submit a review.

---

## 1. GET the review form

```
GET /booking/:id/review
```

- **Auth:** the signed-in customer's access token, as for `GET /booking/:id`.
- **`:id`:** the booking id. This is the same id the app already uses for `/booking/:id`, `/booking/:id/check-in`, etc.

### What the screen shows, and where each piece comes from

| On screen | Field |
|---|---|
| Salon logo and name (top card and salon card) | `salon.logo_url`, `salon.name` |
| "Today · 3 services" | `booking.completed_at`, `booking.service_count` |
| "Completed" chip | `booking.status` |
| Salon tags under "What stood out?" | `tags.salon` |
| One row per stylist: photo, name, the services they did | `stylists[]` |
| "Darius and Marco worked on your visit" | `stylists[].name` |
| Stylist tags under "What stood out?" | `tags.stylist` |
| Up to 3 photos, comment box | `limits` |
| Already rated / can't rate | `can_rate`, `reason`, `review` |

### Response

The JSON below is the payload. Wrap it in the same response envelope that `GET /booking/:id` uses.

```json
{
  "booking": {
    "id": "b7f1c2e4-1d2a-4c3b-9f8e-0a1b2c3d4e5f",
    "status": "completed",
    "completed_at": "2026-11-10T13:45:00+04:00",
    "service_count": 3
  },
  "salon": {
    "id": "5d6a110f-5633-4bfe-942f-19be062a4373",
    "name": "The Iron Razor Barbershop",
    "logo_url": "https://cdn.example.com/salons/iron-razor/logo.png"
  },
  "stylists": [
    {
      "id": "c1a2b3c4-0000-4000-8000-000000000001",
      "name": "Darius Stone",
      "avatar_url": "https://cdn.example.com/stylists/darius.jpg",
      "services": [
        { "id": "svc-1", "name": "Modern Fade" }
      ]
    },
    {
      "id": "c1a2b3c4-0000-4000-8000-000000000002",
      "name": "Marco Bell",
      "avatar_url": null,
      "services": [
        { "id": "svc-2", "name": "Beard trim" },
        { "id": "svc-3", "name": "Grey blending" }
      ]
    }
  ],
  "tags": {
    "salon": [
      { "code": "cleanliness", "label": "Cleanliness" },
      { "code": "ambience", "label": "Ambience" },
      { "code": "music_vibe", "label": "Music vibe" },
      { "code": "comfortable_seats", "label": "Comfortable seats" },
      { "code": "premium_feel", "label": "Premium feel" },
      { "code": "fair_pricing", "label": "Fair pricing" },
      { "code": "parking", "label": "Parking" },
      { "code": "wifi", "label": "Wi-Fi" }
    ],
    "stylist": [
      { "code": "skill", "label": "Skill" },
      { "code": "attention_to_detail", "label": "Attention to detail" },
      { "code": "speed", "label": "Speed" },
      { "code": "professionalism", "label": "Professionalism" },
      { "code": "friendliness", "label": "Friendliness" },
      { "code": "listened", "label": "Listened to me" },
      { "code": "style_advice", "label": "Style advice" },
      { "code": "beard_work", "label": "Beard work" }
    ]
  },
  "limits": {
    "max_photos": 3,
    "comment_max_length": 500
  },
  "can_rate": true,
  "reason": null,
  "review": null
}
```

### `booking`

| Field | Type | Notes |
|---|---|---|
| `id` | string | The booking id. |
| `status` | string | `"completed"` when the visit can be rated. Send the real status otherwise. The app uses it, together with `reason`, to explain why the customer can't rate. |
| `completed_at` | ISO 8601 with offset | When the visit finished. The app turns it into "Today", "Yesterday" or "8 Nov". If you don't keep a completion time, the visit's start time is fine. |
| `service_count` | number | The services actually done on this booking. Cancelled services are not counted. |

### `salon`

| Field | Type | Notes |
|---|---|---|
| `id` | string | |
| `name` | string | |
| `logo_url` | string or `null` | Full URL. When `null`, the app shows the salon's first letter in a circle. |

### `stylists[]`

These are the stylists who **actually worked** on this booking. Rules:

1. **One entry per stylist.** If one stylist did two services, send one entry with both services in `services`. The app shows "Beard trim · Grey blending".
2. **Only services that were done.** Leave out cancelled services. Leave out a stylist whose services were all cancelled.
3. **Use who actually did the service.** If the stylist changed at check-in, or was "any available expert" at booking time, send the stylist who did it, not the one first booked.
4. **Skip services with no stylist.** If a service has no stylist recorded, leave it out of `stylists`, but still count it in `service_count`.
5. **Order by first service.** List stylists by when their first service on the booking started.
6. **Empty is fine.** If no service has a stylist recorded, send `[]`. The app then hides the "Your stylists" card and only the salon is rated.

This list is also the set of stylists the POST gives a rating to: each one either gets the customer's stars, or the salon rating's stars by default.

| Field | Type | Notes |
|---|---|---|
| `id` | string | The stylist's id. The POST sends it back as `stylist_id`. |
| `name` | string | Full name. The app uses the first word for "Darius and Marco worked on your visit". |
| `avatar_url` | string or `null` | Full URL. When `null`, the app shows the name's first letter. |
| `services` | `{ id, name }[]` | The services this stylist did on this booking, at least one. |

### `tags`

These are the tags under "What stood out?". They come from the backend so they can change without an app release.

- **`code`:** stable and never renamed. The POST sends codes.
- **`label`:** what the customer sees.
- **Order:** the app shows the tags in the order you send them.
- **Salon type:** you may tailor the list to the salon type. For example, leave out `beard_work` for a ladies salon.

### `limits`

| Field | Type | Notes |
|---|---|---|
| `max_photos` | number | How many photos the customer can attach. The design uses 3. |
| `comment_max_length` | number | Characters. The app stops typing at this length. |

### `can_rate`, `reason`, `review`

`can_rate` is `true` only when all three hold:
- the booking is completed;
- it has no review yet;
- it is still inside the review window.

Otherwise `can_rate` is `false`, and `reason` says why:

| `reason` | When | What the app shows |
|---|---|---|
| `null` | `can_rate` is `true` | The form |
| `"not_completed"` | The booking isn't completed (upcoming, in progress, cancelled, no-show) | "You can rate this visit once it's done." |
| `"already_reviewed"` | This booking already has a review | The submitted review, read-only |
| `"window_closed"` | The review window has passed | "This visit can no longer be rated." |

`review` is `null` until a review exists. After that, it is the [review object](#the-review-object), so the app can show what the customer submitted.

### Errors

| Status | `code` | When |
|---|---|---|
| `401` | | Not signed in, or the token has expired. |
| `404` | `booking_not_found` | No booking with this id, **or** the booking belongs to another customer. Return 404 in both cases, so the endpoint never confirms that someone else's booking exists. |

Not being able to rate is **not** an error. Return `200` with `can_rate: false` and a `reason`.

---

## 2. POST the review

```
POST /booking/:id/review
Content-Type: application/json
```

- **Auth:** the signed-in customer's access token.
- **`:id`:** the booking id.

### Photos

Photos use the existing **`POST /upload-files`**. The app uploads each photo there first, then sends the returned file `id`s in `photo_ids`. The review endpoint never receives file bytes.

### Request body

In this example the customer rated Darius and skipped Marco:

```json
{
  "salon": {
    "stars": 4,
    "tags": ["cleanliness", "ambience"]
  },
  "stylists": [
    {
      "stylist_id": "c1a2b3c4-0000-4000-8000-000000000001",
      "stars": 5,
      "tags": ["skill", "attention_to_detail"]
    }
  ],
  "comment": "Great fade and they started right on time.",
  "photo_ids": ["f-81c2", "f-81c3"]
}
```

| Field | Type | Required | Rules |
|---|---|---|---|
| `salon` | object | **yes** | The salon rating. |
| `salon.stars` | integer | **yes** | 1 to 5. |
| `salon.tags` | string[] | no | Codes from `tags.salon`. No duplicates. Default `[]`. |
| `stylists` | array | no | Only the stylists the customer rated. A stylist who was skipped or left unrated is not in the array and gets the salon rating's stars by default (see below). Default `[]`. |
| `stylists[].stylist_id` | string | yes | Must be one of the `stylists[].id` that the GET returns for this booking. Each stylist at most once. |
| `stylists[].stars` | integer | yes | 1 to 5. |
| `stylists[].tags` | string[] | no | Codes from `tags.stylist`. No duplicates. Default `[]`. |
| `comment` | string | no | Trim it. Empty means no comment. At most `limits.comment_max_length` characters. |
| `photo_ids` | string[] | no | At most `limits.max_photos`. Each id must come from `POST /upload-files`, be uploaded by this customer, be an `IMAGE`, and not already be attached to another review. |

**One comment and one set of photos per review.** `comment` and `photo_ids` aren't sent per stylist. They belong to the whole review, so the same comment and photos go with the salon rating and with every stylist rating, defaulted or not. Store them once on the review, not copied onto each rating (see [Storage](#4-storage-required)).

**The salon rating is required.** A review without `salon.stars` is rejected, even if it has stylist ratings, a comment or photos. The app only enables Submit once the salon has stars.

### Stylists the customer didn't rate

When the review is saved, look at every stylist the GET returns for this booking (`stylists[]`). Any stylist **not** in the request's `stylists` array still gets a rating:

- **`stars`:** the salon rating's stars, `salon.stars`.
- **`tags`:** none.
- **`defaulted`:** `true`, so a default can always be told apart from a rating the customer gave.

This applies whether the customer skipped one stylist, several, or all of them. In the example above, Marco gets 4 stars (the salon rating) with `defaulted: true`.

Default ratings count like any other: they are included in the stylist's `rating` and `review_count`.

### Success: `201 Created`

The body is the [review object](#the-review-object).

### The review object

`POST` returns this object, and the GET returns it in `review` once the booking is rated.

```json
{
  "id": "r-5f2a",
  "booking_id": "b7f1c2e4-1d2a-4c3b-9f8e-0a1b2c3d4e5f",
  "submitted_at": "2026-11-10T15:02:00+04:00",
  "salon": {
    "stars": 4,
    "tags": [
      { "code": "cleanliness", "label": "Cleanliness" },
      { "code": "ambience", "label": "Ambience" }
    ]
  },
  "stylists": [
    {
      "stylist_id": "c1a2b3c4-0000-4000-8000-000000000001",
      "name": "Darius Stone",
      "avatar_url": "https://cdn.example.com/stylists/darius.jpg",
      "stars": 5,
      "tags": [
        { "code": "skill", "label": "Skill" },
        { "code": "attention_to_detail", "label": "Attention to detail" }
      ],
      "defaulted": false
    },
    {
      "stylist_id": "c1a2b3c4-0000-4000-8000-000000000002",
      "name": "Marco Bell",
      "avatar_url": null,
      "stars": 4,
      "tags": [],
      "defaulted": true
    }
  ],
  "comment": "Great fade and they started right on time.",
  "photos": [
    { "id": "f-81c2", "url": "https://cdn.example.com/reviews/f-81c2.jpg" },
    { "id": "f-81c3", "url": "https://cdn.example.com/reviews/f-81c3.jpg" }
  ]
}
```

- **`salon`** is always present, because the salon rating is required.
- **`stylists`** lists **every** stylist on the booking: those the customer rated (`defaulted: false`) and those who got the salon rating's stars (`defaulted: true`, `tags: []`).
- **`comment`** and **`photos`** sit once on the review and apply to the salon and to every stylist in it. `comment` is `null` when there was no comment, and `photos` is `[]` when there were none.
- **Tags are `{ code, label }` objects here**, so a saved review still displays correctly if a tag's label changes later.

### Errors

Use the existing error format:

```json
{
  "errors": [
    {
      "field": "stylists[1].stylist_id",
      "code": "stylist_not_on_booking",
      "message": "This stylist didn't work on this visit."
    }
  ]
}
```

For errors that aren't about one field, `field` is `null`. The app shows `message` to the customer, so write it for them.

| Status | `code` | When |
|---|---|---|
| `401` | | Not signed in, or the token has expired. |
| `404` | `booking_not_found` | No booking with this id, or it belongs to another customer. |
| `409` | `already_reviewed` | This booking already has a review. Reviews can't be changed. |
| `409` | `not_completed` | The booking isn't completed. |
| `409` | `window_closed` | The review window has passed. |
| `422` | `salon_required` | `salon` or `salon.stars` is missing. The salon rating is required. |
| `422` | `invalid_stars` | `stars` is missing, not an integer, or outside 1 to 5. |
| `422` | `unknown_tag` | A tag code isn't in that kind's list, or the same tag appears twice. |
| `422` | `stylist_not_on_booking` | A `stylist_id` isn't one of this booking's stylists. |
| `422` | `duplicate_stylist` | The same `stylist_id` appears twice. |
| `422` | `comment_too_long` | `comment` is longer than `limits.comment_max_length`. |
| `422` | `too_many_photos` | More than `limits.max_photos` ids. |
| `422` | `invalid_photo` | A photo id is unknown, belongs to someone else, isn't an image, or is already used by another review. |

**One review even on a double tap.** Put a unique constraint on the review's booking id. If two submits race, the second one gets `409 already_reviewed`, and the app treats that as done.

### What else a review changes

Do all of this when the review is saved:

1. **The booking:** `is_rated` becomes `true` and `can_rate` becomes `false` ([part 3](#3-is_rated-and-can_rate-on-bookings)).
2. **The salon:** its `rating` and `review_count`, wherever they appear (salon profile, discover, search), include the salon stars.
3. **Every stylist on the booking:** their `rating` and `review_count` include their stars, whether the customer gave them or they were defaulted. The salon profile's Stylists tab already reads these two fields.
4. **Wherever the review is listed**, it shows the same comment and photos: in the salon's reviews, and in each stylist's reviews, including stylists who got the salon rating by default.

How reviews are listed publicly on the salon's Ratings & Reviews screen will be a separate doc.

---

## 3. `is_rated` and `can_rate` on bookings

Add two booleans to **every booking object** that these endpoints return:

- **`GET /bookings`:** each booking in the list.
- **`GET /booking/:id`:** the booking, and each booking inside it when it answers with the group shape.

```json
{
  "id": "b7f1c2e4-1d2a-4c3b-9f8e-0a1b2c3d4e5f",
  "status": "completed",
  "is_rated": false,
  "can_rate": true
}
```

| Field | Type | Meaning |
|---|---|---|
| `is_rated` | boolean | `true` once a review exists for this booking. It never goes back to `false`, because reviews are final. Always `false` for bookings that aren't completed. |
| `can_rate` | boolean | `true` only when `status` is `completed`, `is_rated` is `false`, and the booking is still inside the review window. This is the same value as `can_rate` in the GET. |

`is_rated` alone isn't enough: a completed, unrated booking can still be past the review window, and only the backend knows the window. `can_rate` saves the app from repeating that rule.

How the app uses them:

| `can_rate` | `is_rated` | App shows |
|---|---|---|
| `true` | `false` | A "Rate your visit" button |
| `false` | `true` | A "Rated" badge. Tapping it opens the submitted review, read-only. |
| `false` | `false` | Nothing about rating (not completed yet, or the window has closed) |

---

## 4. Storage (required)

Reviews **must** be stored in exactly these two models: one `Review` per booking, and one `ReviewStylistRating` row per stylist on that booking. This is not a suggestion. The rest of this doc (defaulted stylist ratings, shared comment and photos, stylist averages, one review per booking) depends on it.

A customer who rates the salon and two stylists produces **1 `Review` row and 2 `ReviewStylistRating` rows**.

### `Review`

There is one `Review` per booking. It holds the salon rating and everything shared by the whole review.

| Column | Type | Rules |
|---|---|---|
| `id` | id | Primary key. |
| `booking_id` | FK → Booking | **Unique.** This is what enforces one review per booking. |
| `customer_id` | FK → Customer | The booking's owner. |
| `salon_id` | FK → Salon | Indexed. Used for the salon's average and its reviews list. |
| `branch_id` | id | **Nullable, optional for now.** Reserved for later. Set it from the booking when the booking has one, otherwise leave it null. |
| `tenant_id` | id | **Nullable, optional for now.** Reserved for later. Set it from the booking when the booking has one, otherwise leave it null. |
| `salon_stars` | integer | **Not null**, 1 to 5. The required salon rating. |
| `salon_tags` | tag codes | Codes from the salon tag list. May be empty. |
| `comment` | text | Nullable. Shared by the salon and every stylist in this review. |
| `submitted_at` | timestamp | |

### `ReviewStylistRating`

There is one row per stylist on the booking. Every stylist in the GET's `stylists[]` gets a row, whether the customer rated them or not.

| Column | Type | Rules |
|---|---|---|
| `id` | id | Primary key. |
| `review_id` | FK → Review | |
| `stylist_id` | FK → Stylist | Indexed. Used for the stylist's average and their reviews list. |
| `branch_id` | id | **Nullable, optional for now.** Reserved for later. Same value as its `Review.branch_id`. |
| `tenant_id` | id | **Nullable, optional for now.** Reserved for later. Same value as its `Review.tenant_id`. |
| `stars` | integer | **Not null**, 1 to 5. Either the customer's stars, or a copy of `Review.salon_stars` when defaulted. |
| `tags` | tag codes | Codes from the stylist tag list. Always empty when defaulted. |
| `defaulted` | boolean | **Not null.** `true` when the customer didn't rate this stylist and the row got the salon stars. |

Add a **unique constraint on (`review_id`, `stylist_id`)**, so a stylist is rated at most once per review.

`branch_id` and `tenant_id` are for the multi-branch salon system: a tenant is the salon business, and a branch is one of its locations.

**For now, just keep the two columns. Nothing uses them yet:**

- They aren't required, aren't validated, and aren't returned by any endpoint.
- **Ratings are global for now.** The salon's `rating` and `review_count` cover all its reviews, and a stylist's cover all theirs, with no split by branch or tenant. Don't filter or group anything by these fields yet.

They're only there so the columns already exist when per-branch ratings are needed. Since every `Review` has `booking_id`, any row saved with them null can be backfilled from its booking then.

### Photos

Photos belong to the `Review`, not to any rating. Link each file row from `POST /upload-files` to the review, keeping the order the customer sent them in. A small link table (`review_id`, `file_id`, `position`) is the cleanest way.

### Not allowed

- **One row or one JSON blob holding all the ratings.** The number of stylists varies, and stylist averages couldn't be indexed.
- **One generic `Rating` model with `target_type` (salon or stylist) and `target_id`.** `target_id` can't be a real foreign key to two tables.
- **The comment or photos copied onto each `ReviewStylistRating`.** They live once, on the `Review`.
- **A separate row for the salon rating.** It is a required, single value, so it lives on the `Review`.

### Saving

The POST must save everything in **one database transaction**: the `Review`, every `ReviewStylistRating` (given and defaulted), the photo links, and the salon and stylist totals. Either all of it is saved or none of it is. A failure halfway must never leave a review without its stylist rows.

### Salon and stylist totals

Reviews are final, so totals only ever go up. Keep them on the `Salon` and `Stylist` rows, and update them in the same transaction:

- **`Salon`:** add `salon_stars` to `rating_sum`, add 1 to `review_count`.
- **`Stylist`:** for each `ReviewStylistRating`, add `stars` to `rating_sum`, add 1 to `review_count`.
- **`rating`** in API responses is `rating_sum / review_count`, rounded to one decimal.

### Reading it back

| What | Query |
|---|---|
| Salon's average | From the totals above, or `AVG(Review.salon_stars) WHERE salon_id = ?` |
| Stylist's average | From the totals above, or `AVG(ReviewStylistRating.stars) WHERE stylist_id = ?` |
| A stylist's reviews | `ReviewStylistRating WHERE stylist_id = ?`, joined to `Review` for the comment, photos and date |
| The review object (GET and POST) | The `Review`, its `ReviewStylistRating` rows (with stylist name and photo), and its photos |

---

## Decisions for the backend

1. **Review window.** How long after a visit can the customer still rate it? We suggest **14 days** after `completed_at`.
2. **Group bookings** (`/booking/group/:groupId`). Should a group visit get one review covering every stylist across the group, or one review per booking in the group? We suggest **one review per booking id**, so each guest rates their own stylists. Tell us if the group owner should rate for everyone instead.
3. **Comment length.** We suggest **500** characters; send whatever you choose in `limits.comment_max_length`.
