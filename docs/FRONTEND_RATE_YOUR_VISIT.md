# Rate your visit: FE integration guide

Date: 2026-11-10. For: the mobile app team.

This guide describes what the server **does today**, read from the code. It
is the one to build from. The older file `docs/rate-your-visit-fe-contract.md`
was the request we built from. The build differs from it in a few places,
listed in [section 5.3](#53-where-the-build-differs-from-rate-your-visit-fe-contractmd).

Base URL: `https://api.gostyle.uk/api/v1`

**Status: live on production since 2026-11-10, behind the server switch
`RATE_VISIT_V1`, which is on.** What the app sees if it is ever turned off is
in [section 5.5](#55-status-and-the-server-switch).

Ids, names, hosts and paths in the examples are made up. Their shape is
real. Always use the values the server sends.

---

## Contents

1. [When to show "Rate your visit"](#1-when-to-show-rate-your-visit)
2. [Opening the form](#2-opening-the-form)
3. [Sending the review](#3-sending-the-review)
4. [After a review is sent](#4-after-a-review-is-sent)
5. [Practical notes](#5-practical-notes): auth, worked example,
   differences from the old contract, checklist, status

---

## 1. When to show "Rate your visit"

### 1.1 The two booleans

Every booking object in these two answers carries `is_rated` and `can_rate`:

| Endpoint | Where |
|---|---|
| `GET /bookings` (all three tabs) | On every row of `results`. |
| `GET /booking/{id}` | On the booking. For a party (`booking_type: "GROUP"`), also on every row of `members`. |

They are only added to a `200` answer. An error answer from these endpoints
is unchanged.

| Field | Type | Meaning |
|---|---|---|
| `is_rated` | boolean | A review exists for this booking. |
| `can_rate` | boolean | The signed-in customer can rate this booking now. Exactly the same rule as `can_rate` in `GET /booking/{id}/review`. |

`can_rate` is `true` only when all of these hold:

- the booking is completed,
- it has no review yet,
- it is still inside the review window (14 days after the visit was completed),
- the booking is the signed-in customer's own.

What the app shows:

| `can_rate` | `is_rated` | Show |
|---|---|---|
| `true` | `false` | A "Rate your visit" button. Tapping it opens the form ([section 2](#2-opening-the-form)). |
| `false` | `true` | A "Rated" badge. Tapping it opens the same form, which then shows the review read-only. |
| `false` | `false` | Nothing about rating. Not completed yet, cancelled, the window has closed, or not the customer's own booking. |
| `true` | `true` | Never sent. |

### 1.2 Do not work it out on the phone

Use the two booleans as they come. Do not compute "completed and not rated"
from `status` yourself, because the app does not have what the rule needs:

- **The window counts from the completion time**, which no booking answer
  carries. The visit's `end_time` is the planned end, not when the salon
  closed the visit.
- **Ownership.** In a party, the row you see may stand for someone else's
  booking. Only the server knows whose booking each row is.
- **The review itself.** Only the server knows whether a review was already
  sent, maybe from another phone.
- The window length may change. The app should not hold a copy of it.

### 1.3 Which statuses can be rated

In `GET /bookings` and `GET /booking/{id}`, `status` is in **upper case**:

| `status` | Can be rated? |
|---|---|
| `COMPLETED` | Yes, inside the window, once. |
| `BOOKED` | No |
| `CONFIRMED_BY_SALON` | No |
| `CHECKED_IN` | No (this includes "in service") |
| `CANCELLED` | No (this includes no-shows and expired bookings) |

In the review form (`GET /booking/{id}/review`), `booking.status` is the
**same word in lower case**: `completed`, `booked`, `confirmed_by_salon`,
`checked_in`, `cancelled`. Compare without caring about case if you compare
at all. Better: do not compare, use `can_rate` and `reason`.

### 1.4 Parties (group bookings)

In a party, each member has their own booking inside it. A review is about
**one member's own booking**. Each member with an account rates their own
visit. Nobody rates for someone else.

- **The party's row** in `GET /bookings`, and the party itself in
  `GET /booking/{party id}`: `is_rated` and `can_rate` are about the
  **signed-in customer's own booking in that party**. If the customer has
  no booking of their own in the party, both are `false`.
- **Each row of `members`** in `GET /booking/{party id}`: the two booleans
  are about **that member's** booking. `can_rate` can only be `true` on the
  signed-in customer's own row. On a friend's row, `is_rated` says whether
  that friend has rated their visit, and `can_rate` is always `false`.
- **Guests** (members with no account) cannot rate. Their row is always
  `false` / `false`.

```json
{
  "id": "41c7e0b9-2a5d-4f86-9e13-7b0d4c8a6f52",
  "booking_type": "GROUP",
  "is_rated": false,
  "can_rate": true,
  "members": [
    {
      "id": "a3e5b7d1-0c24-4f69-8b1e-5d7a9c2e4f60",
      "kind": "self",
      "name": "Omar Khalid",
      "status": "COMPLETED",
      "is_me": true,
      "booking_id": "6d2f8a14-b9e3-4c07-a5d6-0e1b3f7c9a28",
      "is_rated": false,
      "can_rate": true
    },
    {
      "id": "f09c4e6a-7d31-4b28-9a5f-2c8e0b6d1a47",
      "kind": "registered",
      "name": "Sami Nasser",
      "status": "COMPLETED",
      "is_me": false,
      "booking_id": null,
      "is_rated": true,
      "can_rate": false
    },
    {
      "id": "0e8b2d5f-4a96-4c13-b7e0-9f1a6c3d8b25",
      "kind": "guest",
      "name": "Adam",
      "status": "COMPLETED",
      "is_me": false,
      "booking_id": null,
      "is_rated": false,
      "can_rate": false
    }
  ]
}
```

(Only the keys that matter here are shown. `is_me` and `booking_id` on a
member come from the party read's own feature and may be missing. Rating
does not need them.)

To open the form for a party, call `GET /booking/{party id}/review` with
the **party's id**, the same id the booking screen uses. The server opens
the signed-in customer's own booking in that party. See
[2.1](#21-get-bookingidreview).

### 1.5 Routines

- **Recurring tab** rows (a routine as a whole) and the routine read
  `GET /booking/{routine id}`: always `false` / `false`. A routine is not
  rated as a whole.
- **Each visit of a routine** is its own row on the Upcoming and Archive
  tabs (`booking_type: "ROUTINE"`, and its `id` is that visit's booking id).
  Those rows carry real values and can be rated like a single booking.
- The visits listed **inside** the routine read do not carry the two
  booleans. Rate a routine visit from its own row.

### 1.6 When the booking data cannot be read

If the server cannot read the booking data for a moment, `GET /bookings`
and `GET /booking/{id}` still answer `200`. Then `can_rate` is `false` on
every booking. `is_rated` stays right for single and routine bookings, but
a party and its members show `is_rated: false` even when rated. Both come
back right on the next refresh.

---

## 2. Opening the form

### 2.1 `GET /booking/{id}/review`

| | |
|---|---|
| **When** | The customer taps "Rate your visit" or "Rated". |
| **Headers** | `Authorization: Bearer <access token>` |
| **Body** | None |
| **`{id}`** | The id the app uses for `GET /booking/{id}`: a booking's id, or a party's id. A party's id opens the signed-in customer's own booking in that party. |
| **Success** | `200` with the form below. Also `200` when the customer can't rate (see [2.6](#26-can_rate-reason-review)). |
| **Errors** | `401`, `404`, `503`. See [2.7](#27-errors-on-the-form). |

Response, for a completed booking with two stylists, not rated yet:

```json
{
  "booking": {
    "id": "3c9e2f71-5b4a-4d0e-8f12-7a6b9c0d1e23",
    "status": "completed",
    "completed_at": "2026-11-09T18:58:41+04:00",
    "service_count": 3
  },
  "salon": {
    "id": "8a41d6b2-0f3c-4e57-9b18-2c5d7e9f0a64",
    "name": "Oud & Oak Gents Salon",
    "logo_url": "https://gostyle-media.s3.me-central-1.amazonaws.com/tenants/0c5e9a12-7b3f-4d68-a1e4-9f2b6c8d0a35/logo.png"
  },
  "stylists": [
    {
      "id": "5f0b8e21-9c47-4a6d-b3e2-0d1c7a8f6b95",
      "name": "Karim Haddad",
      "avatar_url": "https://gostyle-media.s3.me-central-1.amazonaws.com/tenants/0c5e9a12-7b3f-4d68-a1e4-9f2b6c8d0a35/staff/karim.jpg",
      "services": [
        { "id": "1d7e4b20-6a93-4f18-9c25-8b0e3f7a1d46", "name": "Skin fade" },
        { "id": "c4e81f07-3d92-4b6a-9e05-2f7b8a1c6d39", "name": "Hot towel shave" }
      ]
    },
    {
      "id": "b6d3a9f4-2e81-4c05-8f7a-3e9b1d0c5a72",
      "name": "Yusuf Rahman",
      "avatar_url": null,
      "services": [
        { "id": "7a2c9e51-0b64-4d3f-a8e7-5c1f2b9d0e83", "name": "Beard shape-up" }
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

The keys always come in this order and are always all present.

### 2.2 Field by field

| Field | Type | Null? | Fills on screen |
|---|---|---|---|
| `booking.id` | string (uuid) | never | Nothing. For a party it is the customer's **own** booking id inside the party, not the party id. Keep using the id you opened the form with. |
| `booking.status` | string | never | The status chip. Lower case: `completed`, `booked`, `confirmed_by_salon`, `checked_in`, `cancelled`. |
| `booking.completed_at` | string, ISO 8601 with the salon's offset | never | "Today", "Yesterday", "8 Nov". When the visit was completed. If the salon's system kept no completion time, this is the visit's start time. |
| `booking.service_count` | integer | never | "3 services". Every service on the booking, also one whose stylist is not shown in `stylists`. |
| `salon.id` | string (uuid) | never | Nothing. The salon id the app uses for `GET /salon/{id}`. |
| `salon.name` | string | never | Salon name on the top card. |
| `salon.logo_url` | string (full URL) | can be `null` | Salon logo. `null`: show the first letter of the name. |
| `stylists` | array | never (can be `[]`) | "Your stylists" card. See [2.3](#23-stylists). |
| `tags.salon` | array of `{ code, label }` | never | Salon tags under "What stood out?". See [2.4](#24-tags). |
| `tags.stylist` | array of `{ code, label }` | never | Each stylist's tags. |
| `limits.max_photos` | integer | never | Photo slots (today `3`). |
| `limits.comment_max_length` | integer | never | Comment counter and typing limit (today `500`). |
| `can_rate` | boolean | never | Form editable or not. |
| `reason` | string | `null` when `can_rate` is `true` | Why the customer can't rate. |
| `review` | object | `null` until a review exists | The sent review, for the read-only view. |

### 2.3 `stylists`

The stylists who did the services on this booking.

- **One entry per stylist.** A stylist who did two services is one entry
  with both in `services` ("Skin fade · Hot towel shave").
- **The stylist who really did the service**, also when the stylist was
  changed after booking or the booking was for "any available expert".
- **Order:** by when each stylist's first service started. Show them in
  this order.
- **A stylist the server can't name is left out.** If a service has no
  stylist recorded, or the stylist's profile is gone from the app, that
  service is not under any stylist, but it is still counted in
  `booking.service_count`.
- **`[]` is normal.** Hide the "Your stylists" card. The customer rates
  only the salon.

| Field | Type | Null? | Notes |
|---|---|---|---|
| `id` | string (uuid) | never | Send it back as `stylist_id` in the POST. |
| `name` | string | can be `null` | Full name. For "Karim and Yusuf worked on your visit", use the first word. `null`: show a neutral placeholder. |
| `avatar_url` | string (full URL) | can be `null` | `null`: show the first letter of the name. |
| `services` | array of `{ id, name }` | never, at least one | The services this stylist did on this visit. |

This list is also the set of stylists the review covers: every stylist in
it gets a rating when the review is saved (see [3.3](#33-stylists-the-customer-did-not-rate)).

### 2.4 Tags

- **Draw them from the server.** The app must not keep its own list.
- **Order matters.** Show them in the order sent.
- **`code` is stable.** It is never renamed. The POST sends codes.
- **`label` is display text only.** It can change at any time. Never store
  or compare labels.
- **The lists can differ by salon.** Today, a ladies salon does not get
  `beard_work` in `tags.stylist`. Sending a code that is not in this
  salon's list is a `422 unknown_tag`.
- Labels are English only today. There is no translated label.

### 2.5 `limits`

Use these numbers, not constants in the app. The server checks the same
numbers on POST.

`comment_max_length` counts characters after the server trims spaces at
both ends. The server counts Unicode characters. Dart and JavaScript string
`length` counts UTF-16 units, where one emoji can count as 2. That is
stricter than the server, so limiting on `length` is safe.

### 2.6 `can_rate`, `reason`, `review`

Not being able to rate is **not an error**. The answer is `200` with
`can_rate: false` and a `reason`.

| `can_rate` | `reason` | `review` | Show |
|---|---|---|---|
| `true` | `null` | `null` | The form. |
| `false` | `"already_reviewed"` | the review object | The sent review, read-only. |
| `false` | `"not_completed"` | `null` | A message, no form. Suggested text: "You can rate this visit once it's done." |
| `false` | `"window_closed"` | `null` | A message, no form. Suggested text: "This visit can no longer be rated." |

The suggested texts are the server's own messages for the same cases on
POST ([3.5](#35-errors-on-send)).

`already_reviewed` wins over the other two: a rated visit always comes back
with its review.

`review` is the same object the POST returns. See [3.4](#34-success-201-created).
The review's `stylists` entries have no `services`. For the read-only view,
take the services from this answer's `stylists` (match `stylist_id` to
`id`).

### 2.7 Errors on the form

Every error uses the API's usual envelope:

```json
{
  "detail": "We couldn't find this booking.",
  "code": "not_found",
  "errors": [
    {
      "field": null,
      "code": "booking_not_found",
      "message": "We couldn't find this booking."
    }
  ]
}
```

The top-level `code` is the general kind (`not_found`, `conflict`,
`validation_error`, ...). **The specific code is `errors[i].code`.** Branch
on that one.

| Status | `errors[0].code` | `message` | When | App does |
|---|---|---|---|---|
| `401` | (auth codes) | | No token, or it has expired. | As on every other endpoint: refresh the token and retry, or sign in. |
| `404` | `booking_not_found` | "We couldn't find this booking." | No booking with this id, the id is not a uuid, the booking is another customer's, or the salon is no longer in the app. | Close the screen. Refresh the booking. Do not retry. |
| `404` | `not_found` | "Not found" | The server switch is off ([5.5](#55-status-and-the-server-switch)). | Close the screen. Hide rating everywhere. |
| `503` | `booking_api_unavailable` | "Booking is temporarily unavailable. Please try again." | The booking data could not be read. | Show the message with a Retry button. Safe to retry. |

The server never tells the app that a booking exists but belongs to someone
else. Both are the same `404`.

---

## 3. Sending the review

The order is: upload the photos (if any), then send the review with the
photo ids.

### 3.1 Photos: `POST /upload-files`

The review endpoint never takes file bytes. Upload each photo first, then
send the ids that come back.

| | |
|---|---|
| **When** | When the customer picks a photo, or right before sending the review. Your choice. |
| **Headers** | `Authorization: Bearer <access token>`, `Content-Type: multipart/form-data` |
| **Body** | Multipart field `files`, one file or several. |
| **Success** | `201`, always a JSON **array**, one item per file, in the order sent. |
| **Auth** | The account must be **verified** (phone or email confirmed with a code). Otherwise `403`. |

Limits of the upload endpoint:

| Rule | Value |
|---|---|
| Files per request | 1 to 5 |
| Size per file | 10 MB |
| Accepted types | Images: JPEG, PNG, GIF, WebP, HEIC, AVIF. Also videos, audio, PDF and office files, but **a review only takes images**. |
| How the type is decided | From the file's content, not its name or the `Content-Type` the app sends. |

Response:

```json
[
  {
    "id": "e8a3f1c6-47b2-4d90-8c5e-1b6f0a9d2e74",
    "file_url": "https://gostyle-media.s3.me-central-1.amazonaws.com/customers/uploads/2026/11/e8a3f1c6-47b2-4d90-8c5e-1b6f0a9d2e74.jpg",
    "file_type": "IMAGE"
  }
]
```

- Put only the `id` values in the review's `photo_ids`. Never a URL.
- Only use files with `file_type: "IMAGE"`. Any other type is refused by
  the review (`invalid_photo`), after the upload already succeeded.
- A photo id can be used on **one** review only, and only by the account
  that uploaded it.
- An uploaded photo that is never sent with a review does no harm.
- If the review POST fails with a `422` for another reason, the uploaded
  ids are still good. Fix the problem and send them again. Do not upload
  again.

Upload errors:

| Status | `errors[0].code` | When | App does |
|---|---|---|---|
| `403` | `permission_denied` | Account not verified. Message: "Verify your account to use this feature." | Ask the customer to verify, or let them send the review without photos. |
| `413` | | The whole request is over the server's size limit. The body is **HTML, not JSON**. The exact limit is set outside this code and not stated here. | Send fewer or smaller photos per request. One photo per request keeps requests small. |
| `415` | `unsupported_media_type` | The body is not multipart. | App bug. |
| `422` | `required` | No `files` field. | App bug. |
| `422` | `max_length` | More than 5 files. | Split into several requests. |
| `422` | `empty_file` | A file is empty. | Ask the customer to pick the photo again. |
| `422` | `file_too_large` | A file is over 10 MB. | Compress it on the phone, or ask for another photo. |
| `422` | `unsupported_file_type` | Not a type the server accepts. | Ask for another photo. |
| `503` | `storage_unavailable` | The file store failed. | Retry. |

All upload `422` errors have `field: "files"`. Nothing is saved when an
upload request fails.

### 3.2 `POST /booking/{id}/review`

| | |
|---|---|
| **When** | The customer taps Submit. Only enable Submit once the salon has stars. |
| **Headers** | `Authorization: Bearer <access token>`, `Content-Type: application/json` |
| **`{id}`** | The same id you used for the GET. |
| **Success** | `201` with the review object ([3.4](#34-success-201-created)). |
| **Errors** | `401`, `404`, `409`, `422`, `415`, `503`. See [3.5](#35-errors-on-send). |

Request body. In this example the customer rated the salon and Karim, and
did not rate Yusuf:

```json
{
  "salon": {
    "stars": 4,
    "tags": ["cleanliness", "fair_pricing"]
  },
  "stylists": [
    {
      "stylist_id": "5f0b8e21-9c47-4a6d-b3e2-0d1c7a8f6b95",
      "stars": 5,
      "tags": ["skill", "listened"]
    }
  ],
  "comment": "Sharp fade, and the hot towel finish was a nice touch.",
  "photo_ids": ["e8a3f1c6-47b2-4d90-8c5e-1b6f0a9d2e74"]
}
```

| Field | Type | Required | Rules |
|---|---|---|---|
| `salon` | object | **yes** | |
| `salon.stars` | integer | **yes** | A whole number from 1 to 5. `4.0`, `"4"` and `true` are refused. |
| `salon.tags` | array of strings | no | Codes from `tags.salon` of the form. Each code once. Missing or `null` means none. |
| `stylists` | array | no | Only the stylists the customer rated. Missing, `null` or `[]` means none. |
| `stylists[].stylist_id` | string (uuid) | yes | One of the form's `stylists[].id`. Each stylist once. |
| `stylists[].stars` | integer | yes | A whole number from 1 to 5. |
| `stylists[].tags` | array of strings | no | Codes from `tags.stylist` of the form. Each code once. |
| `comment` | string | no | The server trims spaces at both ends. Blank after trimming means no comment. At most `limits.comment_max_length` characters after trimming. |
| `photo_ids` | array of strings | no | At most `limits.max_photos`. Ids from `POST /upload-files`, uploaded by this customer, `file_type: "IMAGE"`, not on another review, each once. Their order is kept. |

**`comment` and `photo_ids` belong to the whole review**, not to one
stylist. There is one comment and one set of photos per review. They are
not sent per stylist.

The server ignores any other key in the body.

### 3.3 Stylists the customer did not rate

Every stylist in the form's `stylists` gets a rating when the review is
saved. A stylist the customer did not rate (not in the request's
`stylists`) gets:

- `stars`: the salon's stars,
- `tags`: none,
- `defaulted: true`.

This counts in that stylist's rating like any other.

What the app shows:

- **On the form:** make it clear that a stylist left unrated will get the
  salon's stars. For example, show the salon's stars greyed out on that
  stylist's row once the salon is rated.
- **In the read-only review:** a stylist with `defaulted: true` was not
  rated by the customer. Show their stars as "from your salon rating", or
  greyed out, not as stars the customer picked.

### 3.4 Success: `201 Created`

The body is the **review object**. `GET /booking/{id}/review` returns the
same object in `review` once the booking is rated.

```json
{
  "id": "2b9f6d04-8e31-4a7c-b5d2-6f0e3a1c9b87",
  "booking_id": "3c9e2f71-5b4a-4d0e-8f12-7a6b9c0d1e23",
  "submitted_at": "2026-11-10T10:15:27+04:00",
  "salon": {
    "stars": 4,
    "tags": [
      { "code": "cleanliness", "label": "Cleanliness" },
      { "code": "fair_pricing", "label": "Fair pricing" }
    ]
  },
  "stylists": [
    {
      "stylist_id": "5f0b8e21-9c47-4a6d-b3e2-0d1c7a8f6b95",
      "name": "Karim Haddad",
      "avatar_url": "https://gostyle-media.s3.me-central-1.amazonaws.com/tenants/0c5e9a12-7b3f-4d68-a1e4-9f2b6c8d0a35/staff/karim.jpg",
      "stars": 5,
      "tags": [
        { "code": "skill", "label": "Skill" },
        { "code": "listened", "label": "Listened to me" }
      ],
      "defaulted": false
    },
    {
      "stylist_id": "b6d3a9f4-2e81-4c05-8f7a-3e9b1d0c5a72",
      "name": "Yusuf Rahman",
      "avatar_url": null,
      "stars": 4,
      "tags": [],
      "defaulted": true
    }
  ],
  "comment": "Sharp fade, and the hot towel finish was a nice touch.",
  "photos": [
    {
      "id": "e8a3f1c6-47b2-4d90-8c5e-1b6f0a9d2e74",
      "url": "https://gostyle-media.s3.me-central-1.amazonaws.com/customers/uploads/2026/11/e8a3f1c6-47b2-4d90-8c5e-1b6f0a9d2e74.jpg"
    }
  ]
}
```

| Field | Type | Null? | Notes |
|---|---|---|---|
| `id` | string (uuid) | never | The review's id. |
| `booking_id` | string (uuid) | never | The booking that was rated. For a party, the customer's own booking id inside the party, not the party id. |
| `submitted_at` | string, ISO 8601 with the salon's offset | never | |
| `salon.stars` | integer 1 to 5 | never | |
| `salon.tags` | array of `{ code, label }` | never (can be `[]`) | In the order the customer sent them. |
| `stylists` | array | never (can be `[]`) | **Every** stylist of the visit: rated (`defaulted: false`) and not rated (`defaulted: true`, `tags: []`). Same order as the form's `stylists`. |
| `stylists[].name` | string | can be `null` | |
| `stylists[].avatar_url` | string | can be `null` | |
| `stylists[].tags` | array of `{ code, label }` | never (can be `[]`) | |
| `comment` | string | can be `null` | `null` when there was no comment. |
| `photos` | array of `{ id, url }` | never (can be `[]`) | In the order sent. `url` is a full URL. |

Tags here carry today's `label`. A saved review keeps its codes, so it
still shows correctly if a label changes later.

### 3.5 Errors on send

The server checks in this order and stops at the first that fails:

1. Signed in? (`401`)
2. The booking exists, is the customer's, and its salon is in the app? (`404`)
3. Can this booking take a review now? (`409`)
4. Is the body right? (`422`, every problem at once)

So a second submit with a broken body still gets `409 already_reviewed`,
not a `422`.

**`401`, `404`, `503`:** as on the form ([2.7](#27-errors-on-the-form)).

**`409`: the booking can't take a review.** One error, `field: null`.

| `errors[0].code` | `message` | App does |
|---|---|---|
| `already_reviewed` | "You've already rated this visit. Reviews can't be changed." | **Treat as success.** See [3.6](#36-double-submit). |
| `not_completed` | "You can rate this visit once it's done." | Show the message, close the form, refresh the booking. |
| `window_closed` | "This visit can no longer be rated." | Show the message, close the form, refresh the booking. |

**`422`: the body is wrong.** Top-level `code` is `validation_error`,
`detail` is "Please correct the highlighted fields.", and `errors` lists
**every** problem found, in order. Each has a `field` path:

```json
{
  "detail": "Please correct the highlighted fields.",
  "code": "validation_error",
  "errors": [
    {
      "field": "salon.stars",
      "code": "salon_required",
      "message": "Please rate the salon."
    },
    {
      "field": "comment",
      "code": "comment_too_long",
      "message": "Your comment can be up to 500 characters."
    }
  ]
}
```

| `errors[i].code` | `field` | `message` | Customer can fix it? | App does |
|---|---|---|---|---|
| `salon_required` | `salon` or `salon.stars` | "Please rate the salon." | Yes | Highlight the salon stars. |
| `invalid_stars` | `salon.stars` or `stylists[i].stars` | "Please choose 1 to 5 stars." | No, app bug | Stars must be a whole number 1 to 5. A stylist entry without `stars` gets this too. |
| `unknown_tag` | `salon.tags[i]`, `stylists[i].tags[j]` | "This tag can't be picked here." | No | The code is not in this salon's list. Reload the form (`GET`), keep the customer's choices that are still in the new lists. |
| `unknown_tag` | same | "You picked this tag twice." | No, app bug | Send each code once. |
| `unknown_tag` | `salon.tags` or `stylists[i].tags` | "Please pick the tags from the list." | No, app bug | `tags` was not an array. |
| `stylist_not_on_booking` | `stylists[i].stylist_id` | "This stylist didn't work on this visit." | No | A missing or unknown `stylist_id`. Reload the form. Also sent with `field: "stylists"` when `stylists` is not an array. |
| `duplicate_stylist` | `stylists[i].stylist_id` | "You rated this stylist twice." | No, app bug | Send each stylist once. Ids are compared without case. |
| `invalid` | `comment` | "Your comment must be text." | No, app bug | `comment` must be a string or left out. |
| `comment_too_long` | `comment` | "Your comment can be up to 500 characters." | Yes | Highlight the comment. The number in the message follows `limits.comment_max_length`. |
| `too_many_photos` | `photo_ids` | "You can add up to 3 photos." | Yes | Ask the customer to remove photos. The number follows `limits.max_photos`. |
| `invalid_photo` | `photo_ids[i]` | "This photo can't be used. Please add it again." | Yes | Remove that photo from the form and ask the customer to add it again (upload again). Unknown id, someone else's, not an image, already on another review, or the same id twice. |
| `invalid_photo` | `photo_ids` | same | Yes | `photo_ids` was not an array, or a photo was taken by another review at the same moment. Ask the customer to add the photos again. |

Show `message` as it is. It is written for the customer.

Other statuses on send:

| Status | `errors[0].code` | When | App does |
|---|---|---|---|
| `415` | `unsupported_media_type` | The body was not sent as JSON. | App bug. Send `Content-Type: application/json`. |
| `422` | `parse_error` | The body is not valid JSON. | App bug. |
| `503` | `booking_api_unavailable` | The booking data could not be read. | Keep everything the customer entered. Retry. Nothing was saved. |

Summary for the app:

| Kind | Errors | What to do |
|---|---|---|
| Fix in place | `salon_required`, `comment_too_long`, `too_many_photos`, `invalid_photo` | Keep the form open, highlight the field, show `message`. |
| Form is stale | `unknown_tag` (can't be picked here), `stylist_not_on_booking` | Reload the form, keep what still fits. |
| Close the screen | `404 booking_not_found`, `409 not_completed`, `409 window_closed` | Show `message`, close, refresh the booking. |
| Success | `201`, `409 already_reviewed` | Go to the "after" state ([section 4](#4-after-a-review-is-sent)). |
| Retry | `503`, `401` after a token refresh, network errors | Keep the input, try again. |
| App bug | `invalid_stars`, the duplicate and not-an-array cases, `invalid`, `415`, `parse_error` | Log it. Should never reach a customer. |

Nothing is saved when the POST fails. The review is all or nothing.

### 3.6 Double submit

Only one review can exist per booking. If the customer taps Submit twice,
or the phone retries after a timeout, the second request gets
`409 already_reviewed` and nothing is saved twice.

Treat `409 already_reviewed` as **success**: the review is saved (by the
first request, or earlier from another phone). Call
`GET /booking/{id}/review` to get the saved review and show it read-only.
Also disable Submit while the request is in flight.

### 3.7 A review is final

There is no edit, no delete and no second review. There is no `PATCH`,
`PUT` or `DELETE` for reviews. Once sent, the form only shows the review
read-only. Say this on the screen before Submit.

---

## 4. After a review is sent

### 4.1 The booking

After a `201` (or `409 already_reviewed`), this booking's values are:

- `is_rated`: `true`
- `can_rate`: `false`

The screen the customer came from (the booking list row, or the booking
details) must change from the "Rate your visit" button to the "Rated"
badge. Either set the two values on the cached booking right away, or
fetch `GET /booking/{id}` and `GET /bookings` again. The server has the new
values at once.

For a party, the change is on the party's row (it stands for the
customer's own booking) and on the customer's own `members` row.

### 4.2 Where the new rating shows up

The server counts the review at once. There is no delay. Refetch these
when they are next shown:

| Where | Field | What changes |
|---|---|---|
| Salon cards (discover, search, favourites), salon profile | `rating`, `review_count` | The salon's stars are counted together with the salon's existing published reviews. |
| Map | `rating` | The same salon rating. |
| Salon profile, Stylists tab (`GET /salon/{id}/stylists`) | each stylist's `rating`, `review_count` | Every stylist of the visit, rated or defaulted. Rated stylists are listed first, highest rating first. |
| Service details, the experts list | each expert's `rating`, `review_count` | As on the Stylists tab. |
| Expert profile (`GET /salon/{id}/stylist/{id}`) | `rating`, `review_count` | As on the Stylists tab. |

`rating` is the average, rounded to one decimal. A stylist with no review
yet has `rating: null`. On the Stylists tab and in the experts list their
`review_count` is also `null`. On the expert profile it is `0`.

**Not built yet:** there is no endpoint that lists reviews (a salon's or a
stylist's reviews, comments, photos). Only the numbers above change. A
public "Ratings & Reviews" list needs its own server work and guide.

---

## 5. Practical notes

### 5.1 Base URL and auth

Base URL: `https://api.gostyle.uk/api/v1`

| Endpoint | Token | Extra |
|---|---|---|
| `GET /bookings` | yes | |
| `GET /booking/{id}` | yes | |
| `GET /booking/{id}/review` | yes | |
| `POST /booking/{id}/review` | yes | Body is JSON. |
| `POST /upload-files` | yes | Account must be verified. Body is multipart. |

Token header: `Authorization: Bearer <access token>`. No endpoint here
works without one.

### 5.2 Worked example

Karim Haddad and Yusuf Rahman worked on the customer's visit at Oud & Oak
on 9 November. The next morning the customer rates it.

**Step 1. The customer opens the Archive tab.**

```
GET /bookings?filter=archive
Authorization: Bearer <access token>
```

`200`. The row (only the keys that matter here are shown):

```json
{
  "id": "3c9e2f71-5b4a-4d0e-8f12-7a6b9c0d1e23",
  "status": "COMPLETED",
  "booking_type": "SINGLE",
  "start_time": "2026-11-09T18:00:00+04:00",
  "end_time": "2026-11-09T19:00:00+04:00",
  "is_rated": false,
  "can_rate": true
}
```

`can_rate: true`, `is_rated: false`: the row shows "Rate your visit".

**Step 2. The customer opens the booking.**

```
GET /booking/3c9e2f71-5b4a-4d0e-8f12-7a6b9c0d1e23
Authorization: Bearer <access token>
```

`200`. Same two values as the row:

```json
{
  "id": "3c9e2f71-5b4a-4d0e-8f12-7a6b9c0d1e23",
  "status": "COMPLETED",
  "booking_type": "SINGLE",
  "is_rated": false,
  "can_rate": true
}
```

**Step 3. The customer taps "Rate your visit".**

```
GET /booking/3c9e2f71-5b4a-4d0e-8f12-7a6b9c0d1e23/review
Authorization: Bearer <access token>
```

`200` with the form shown in [2.1](#21-get-bookingidreview): two stylists,
Karim first (his first service started first), `can_rate: true`.

**Step 4. The customer adds one photo.**

```
POST /upload-files
Authorization: Bearer <access token>
Content-Type: multipart/form-data; boundary=...

files = <the photo, JPEG>
```

`201`:

```json
[
  {
    "id": "e8a3f1c6-47b2-4d90-8c5e-1b6f0a9d2e74",
    "file_url": "https://gostyle-media.s3.me-central-1.amazonaws.com/customers/uploads/2026/11/e8a3f1c6-47b2-4d90-8c5e-1b6f0a9d2e74.jpg",
    "file_type": "IMAGE"
  }
]
```

**Step 5. The customer gives the salon 4 stars, Karim 5 stars, skips
Yusuf, writes a comment and submits.**

```
POST /booking/3c9e2f71-5b4a-4d0e-8f12-7a6b9c0d1e23/review
Authorization: Bearer <access token>
Content-Type: application/json
```

```json
{
  "salon": {
    "stars": 4,
    "tags": ["cleanliness", "fair_pricing"]
  },
  "stylists": [
    {
      "stylist_id": "5f0b8e21-9c47-4a6d-b3e2-0d1c7a8f6b95",
      "stars": 5,
      "tags": ["skill", "listened"]
    }
  ],
  "comment": "Sharp fade, and the hot towel finish was a nice touch.",
  "photo_ids": ["e8a3f1c6-47b2-4d90-8c5e-1b6f0a9d2e74"]
}
```

`201` with the review object shown in [3.4](#34-success-201-created).
Yusuf is in it with the salon's 4 stars and `defaulted: true`.

**Step 6. Back on the booking.**

```
GET /booking/3c9e2f71-5b4a-4d0e-8f12-7a6b9c0d1e23
Authorization: Bearer <access token>
```

```json
{
  "id": "3c9e2f71-5b4a-4d0e-8f12-7a6b9c0d1e23",
  "status": "COMPLETED",
  "booking_type": "SINGLE",
  "is_rated": true,
  "can_rate": false
}
```

The button is now the "Rated" badge. Tapping it:

```
GET /booking/3c9e2f71-5b4a-4d0e-8f12-7a6b9c0d1e23/review
Authorization: Bearer <access token>
```

`200`. The same form as in step 3, except the last three keys:

```json
{
  "can_rate": false,
  "reason": "already_reviewed",
  "review": {
    "id": "2b9f6d04-8e31-4a7c-b5d2-6f0e3a1c9b87",
    "booking_id": "3c9e2f71-5b4a-4d0e-8f12-7a6b9c0d1e23",
    "submitted_at": "2026-11-10T10:15:27+04:00",
    "salon": {
      "stars": 4,
      "tags": [
        { "code": "cleanliness", "label": "Cleanliness" },
        { "code": "fair_pricing", "label": "Fair pricing" }
      ]
    },
    "stylists": [
      {
        "stylist_id": "5f0b8e21-9c47-4a6d-b3e2-0d1c7a8f6b95",
        "name": "Karim Haddad",
        "avatar_url": "https://gostyle-media.s3.me-central-1.amazonaws.com/tenants/0c5e9a12-7b3f-4d68-a1e4-9f2b6c8d0a35/staff/karim.jpg",
        "stars": 5,
        "tags": [
          { "code": "skill", "label": "Skill" },
          { "code": "listened", "label": "Listened to me" }
        ],
        "defaulted": false
      },
      {
        "stylist_id": "b6d3a9f4-2e81-4c05-8f7a-3e9b1d0c5a72",
        "name": "Yusuf Rahman",
        "avatar_url": null,
        "stars": 4,
        "tags": [],
        "defaulted": true
      }
    ],
    "comment": "Sharp fade, and the hot towel finish was a nice touch.",
    "photos": [
      {
        "id": "e8a3f1c6-47b2-4d90-8c5e-1b6f0a9d2e74",
        "url": "https://gostyle-media.s3.me-central-1.amazonaws.com/customers/uploads/2026/11/e8a3f1c6-47b2-4d90-8c5e-1b6f0a9d2e74.jpg"
      }
    ]
  }
}
```

The window for this visit would have closed at
`2026-11-23T18:58:41+04:00`, 14 days after `completed_at`.

### 5.3 Where the build differs from `rate-your-visit-fe-contract.md`

If you read the old contract first, these are the surprises:

1. **Error shape.** Errors have `detail`, `code` and `errors`. The top-level
   `code` is general (`not_found`, `conflict`, `validation_error`). The
   specific code (`booking_not_found`, `already_reviewed`, ...) is in
   `errors[i].code`.
2. **New `503 booking_api_unavailable`** on both review routes, when the
   booking data can't be read. Safe to retry.
3. **`404 booking_not_found` has two more causes:** an id that is not a
   uuid, and a salon that is no longer in the app.
4. **`booking.status` is lower case for every status**, not only
   `completed`: `booked`, `confirmed_by_salon`, `checked_in`, `cancelled`.
   A no-show is `cancelled`. `GET /bookings` and `GET /booking/{id}` keep
   their upper case `COMPLETED`.
5. **Parties: one review per member's own booking.** A party id opens the
   signed-in customer's own booking. `booking.id` and `review.booking_id`
   are that booking's id, not the party id. Guests can't rate. Nobody
   rates for someone else.
6. **`can_rate` is also `false` on bookings that are not the customer's
   own** (a friend's row in a party).
7. **A stylist the server can't name is left out** of `stylists` and gets
   no rating, also no default rating. Their services still count in
   `service_count`.
8. **More `422` cases** with no code in the old contract: `invalid` for a
   comment that is not text, `parse_error` for a body that is not JSON,
   and the not-an-array cases listed in [3.5](#35-errors-on-send). A
   duplicate tag is `unknown_tag` with its own message.
9. **All `422` problems come back at once**, not just the first.
10. **Order of checks:** `404`, then `409`, then `422`.
11. **The form is not wrapped** in any envelope. It is the plain object.
12. **Ids are uuids** (review id, photo ids). Photo `url` is the file
    store's full URL.
13. **Photos need a verified account** for the upload (`403` otherwise).
    The review itself only needs a signed-in account.
14. **The salon's `rating` and `review_count` mix both sources:** the
    salon's existing published reviews and the app's reviews.
15. **No list of reviews exists yet.** The old contract's "wherever the
    review is listed" has nothing to show it in today.
16. Decisions taken as the old contract suggested: a **14-day** window from
    `completed_at`, **one review per booking**, comments up to **500**
    characters. `beard_work` is left out for ladies salons.

### 5.4 Checklist for the app

Everything below is app work. None of it is done on the app side yet.

- [ ] Booking list (all tabs): read `is_rated` / `can_rate` per row; show
      the button or the badge as in [1.1](#11-the-two-booleans).
- [ ] Booking details: the same, from `GET /booking/{id}`.
- [ ] Party details: the same on the party and on each `members` row; the
      button only on the customer's own row.
- [ ] Treat a missing `is_rated` / `can_rate` as `false`.
- [ ] Never decide "can rate" from `status` on the phone.
- [ ] Replace the hard-coded Rate your visit screen with
      `GET /booking/{id}/review`.
- [ ] Draw tags from `tags.salon` / `tags.stylist` in the order sent; send
      `code`, show `label`.
- [ ] Use `limits.max_photos` and `limits.comment_max_length`, not
      constants.
- [ ] `stylists: []`: hide "Your stylists", rate the salon only.
- [ ] `null` `logo_url`, `avatar_url` and stylist `name`: show a letter or
      a placeholder.
- [ ] `can_rate: false`: show the read-only review or the message for
      `reason`.
- [ ] Submit is enabled only when the salon has stars, and disabled while
      sending.
- [ ] Show that an unrated stylist gets the salon's stars; show
      `defaulted: true` ratings differently in the read-only view.
- [ ] Photos: upload with `POST /upload-files`, images only, then send the
      `id` values in `photo_ids`. Handle the `403` for unverified accounts.
- [ ] On a `422`, keep uploaded photo ids and resend them.
- [ ] Handle every error in [2.7](#27-errors-on-the-form) and
      [3.5](#35-errors-on-send); branch on `errors[i].code`, show `message`.
- [ ] `409 already_reviewed` is success: load the saved review.
- [ ] After success: flip the booking to "Rated" and refetch the salon and
      stylist screens when next shown.
- [ ] Tell the customer before Submit that a review can't be changed.

### 5.5 Status and the server switch

**Live on production since 2026-11-10, behind `RATE_VISIT_V1`, which is
on.**

If the switch is ever turned off:

- `GET` and `POST /booking/{id}/review` answer `404`, with
  `errors[0].code: "not_found"` (not `booking_not_found`).
- `GET /bookings` and `GET /booking/{id}` **no longer have** the
  `is_rated` and `can_rate` keys. The keys are missing, not `false`. Treat
  missing as `false`, so no button shows.
- Salon ratings count only the salon's published reviews again, and every
  stylist's `rating` is `null`. Saved reviews are kept and come back when
  the switch is turned on again.
