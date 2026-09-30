# Expert Profile

Base URL: `https://api.gostyle.uk/api/v1`
Auth: none needed to open the screen. Send the Bearer token when the customer
is signed in: it fills the heart. A bad or expired token is `401`. The heart
itself (`POST /favourite`) needs a token.

The agreed contract, 2026-09-30. It is the app team's
`expert-profile-fe-contract.md` (kept as received) with what the server really
does. Every example below is what the server really answers: the full `200` was
taken from the running code, and a test asks the code again for every JSON
block on this page and must get the same answer, so this page cannot drift
from the code. The ids in the examples come from our test data, so they do not
exist on the live server: take real ids from `GET /salon/:id/stylists`.

One stylist at one salon: who they are, how they rate, the work they have
posted, the salon, and the services they personally do. **One call draws the
whole screen.**

---

## What differs from your draft

1. **The route is public.** No token is needed, like every other
   `/salon/:id/...` route: a guest can open a salon and its stylists, so the
   profile opens too. **Send the token when you have one**: without it
   `is_favorite` is always `false`. A bad or expired token is `401` (§5).
2. **The `404` body has one item in `errors`**, not `[]`. The item repeats
   `detail`. Read `detail` and `code`; do not rely on `errors` being empty (§5).
3. **There are no stylist reviews yet**: `rating` is `null` and `review_count`
   is `0`. Show "No reviews yet" (§7).
4. **`bio`, `years_experience` and `price_level` are `null` for now** (§7).
   Hide the block and the rows, as your draft says for `null`.
5. **`is_network_member` is always `false` for now.** Nothing in our data has
   that meaning yet. We have a question for you about it (§8).
6. **`day_off` can name more than one day**: `"Tuesday"`, or
   `"Friday, Saturday"`. Print it as it is. It is worked out from the salon's
   roster, and it is `null` when it cannot be told (§2).
7. **`has_story` follows `media`**: it is `true` when the stylist has at least
   one photo. There are no separate stylist stories, so the story viewer shows
   `media`, as your open point says.
8. **`media` holds images only today**: `type` is always `image` and
   `thumbnail_url` is always `null`. A salon cannot upload a video yet. When
   it can, a `video` item will carry its `thumbnail_url`; nothing changes in
   the shape.
9. **Three `salon` values can be `null`.** `is_open` is `null` when the salon
   published no opening hours (show neither Open nor Closed): the same value
   `GET /salon/:id` gives. `hours_today` is `null` on a day the salon does not
   open, and when it published no hours. `latitude` and `longitude` are `null`
   when the salon has no pin (show no distance).
10. **`service_groups[].id` is the Services tab's group id**: a UUID, or
    `"other"`. Never a word like `grp_precision_cuts`.
11. **`name` can be `null`**, for a stylist with no name on file. The same as
    in the stylists list.
12. **Photo URLs are plain storage URLs**, with no fixed host
    (`https://<bucket>.s3.<region>.amazonaws.com/...` today). Use them as they
    are; do not build or check a host.
13. **`GET /salon/:id/stylists` is not quite "unchanged"**: it now lists only
    the salon's own stylists, and it fills `day_off` (§9).

Also worth knowing:

- The keys come in your draft's order.
- `price` is a JSON number and can come written as `199.0`. Read it as a number.
- `duration_min` and `duration_max` are the same number today: every service
  has one length.
- The times (`GET /booking/nearest-available/:salon_id`) need a token, and one
  call answers one day (§6).
- A day off is a hint for the customer. What can be booked is decided by the
  Time step, and the booking is checked again when it is created.

---

## 1. Endpoint

| Method | Path                                     | Purpose                      |
| ------ | ---------------------------------------- | ---------------------------- |
| `GET`  | `/salon/{salon_id}/stylist/{stylist_id}` | Everything this screen draws |

```
GET /salon/33333333-3333-3333-3333-333333333333/stylist/99999999-9999-9999-9999-999999999990
```

Both ids are in the path: a stylist is read at one salon, whose prices, hours
and services the screen shows.

Reused:

| Method | Path                                            | Used by                                          |
| ------ | ----------------------------------------------- | ------------------------------------------------ |
| `GET`  | `/salon/:id/stylists?service_ids=`              | The team list, and the booking Expert step (§9)  |
| `POST` | `/favourite`                                    | The heart (§4)                                   |
| `GET`  | `/booking/nearest-available/:salon_id`          | The Time step, with `stylist_id` (§6)            |

---

## 2. Response: `200 OK`

The seeded salon's Liam Johnson, asked as a guest on a Wednesday afternoon,
with seven gallery photos tagged with him:

```json
{
  "id": "99999999-9999-9999-9999-999999999990",
  "salon_id": "33333333-3333-3333-3333-333333333333",
  "name": "Liam Johnson",
  "title": "Senior Barber",
  "role": "Barber and Grooming Expert",
  "bio": null,
  "avatar_url": null,
  "rating": null,
  "review_count": 0,
  "years_experience": null,
  "price_level": null,
  "day_off": "Tuesday",
  "is_network_member": false,
  "is_favorite": false,
  "has_story": true,
  "media": [
    {
      "id": "8e2b1f10-0000-4000-8000-000000000007",
      "type": "image",
      "url": "https://gostyle-media.s3.me-central-1.amazonaws.com/tenants/11111111-1111-1111-1111-111111111111/storefronts/33333333-3333-3333-3333-333333333333/media/kids-cut.jpg",
      "thumbnail_url": null
    },
    {
      "id": "8e2b1f10-0000-4000-8000-000000000006",
      "type": "image",
      "url": "https://gostyle-media.s3.me-central-1.amazonaws.com/tenants/11111111-1111-1111-1111-111111111111/storefronts/33333333-3333-3333-3333-333333333333/media/taper.jpg",
      "thumbnail_url": null
    },
    {
      "id": "8e2b1f10-0000-4000-8000-000000000005",
      "type": "image",
      "url": "https://gostyle-media.s3.me-central-1.amazonaws.com/tenants/11111111-1111-1111-1111-111111111111/storefronts/33333333-3333-3333-3333-333333333333/media/crew-cut.jpg",
      "thumbnail_url": null
    },
    {
      "id": "8e2b1f10-0000-4000-8000-000000000004",
      "type": "image",
      "url": "https://gostyle-media.s3.me-central-1.amazonaws.com/tenants/11111111-1111-1111-1111-111111111111/storefronts/33333333-3333-3333-3333-333333333333/media/classic-side-part.jpg",
      "thumbnail_url": null
    },
    {
      "id": "8e2b1f10-0000-4000-8000-000000000003",
      "type": "image",
      "url": "https://gostyle-media.s3.me-central-1.amazonaws.com/tenants/11111111-1111-1111-1111-111111111111/storefronts/33333333-3333-3333-3333-333333333333/media/hot-towel-shave.jpg",
      "thumbnail_url": null
    }
  ],
  "media_count": 7,
  "salon": {
    "id": "33333333-3333-3333-3333-333333333333",
    "name": "The Iron Razor Barbershop",
    "is_open": true,
    "hours_today": "10:00 AM - 10:00 PM",
    "latitude": 25.2213,
    "longitude": 55.2621
  },
  "service_groups": [
    {
      "id": "55555555-5555-5555-5555-555555555553",
      "name": "Beard Care",
      "services": [
        {
          "id": "66666666-6666-6666-6666-666666666662",
          "name": "Hot Towel Shave",
          "description": "Hot Towel Shave at Iron Razor.",
          "price": 260.0,
          "duration_min": 35,
          "duration_max": 35
        }
      ]
    },
    {
      "id": "55555555-5555-5555-5555-555555555552",
      "name": "Precision Cuts",
      "services": [
        {
          "id": "66666666-6666-6666-6666-666666666660",
          "name": "The Gentleman's Cut",
          "description": "The Gentleman's Cut at Iron Razor.",
          "price": 199.0,
          "duration_min": 30,
          "duration_max": 30
        }
      ]
    }
  ]
}
```

Seven photos: the first five are in `media`, so the app shows "+2". Liam can do
two of the salon's four services, so two of its three groups are here. He never
works on a Tuesday. No token was sent, so `is_favorite` is `false`.

### Fields

| Field               | Type            | Notes |
| ------------------- | --------------- | ----- |
| `id`                | uuid            | The stylist id. Goes to the booking flow as the chosen expert. |
| `salon_id`          | uuid            | The `salon_id` from the path, echoed. |
| `name`              | string \| null | First and last name. `null` when there is none on file. |
| `title`             | string \| null | The job title on the staff record, for example "Master Barber". |
| `role`              | string \| null | The expertise line on the person's account. Either of the two can be `null`; print whichever you have. |
| `bio`               | null            | Always `null` for now (§7). Hides the block. |
| `avatar_url`        | string \| null | `null`: draw the initials. |
| `rating`            | null            | Always `null` for now (§7). |
| `review_count`      | number          | Always `0` for now (§7). Show "No reviews yet". |
| `years_experience`  | null            | Always `null` for now (§7). Hides the row. |
| `price_level`       | null            | Always `null` for now (§7). Hides the row. |
| `day_off`           | string \| null | Full day names, to print as they are: `"Tuesday"`, `"Friday, Saturday"`. `null` hides the row. See below. |
| `is_network_member` | boolean         | Always `false` for now (§8). |
| `is_favorite`       | boolean         | The customer's own heart on this stylist. `false` for a guest (§4). |
| `has_story`         | boolean         | `true` when `media_count` is above 0. |
| `media`             | array           | Their posted work, newest first. At most 5 (below). |
| `media_count`       | number          | All of them. "+N" is `media_count - media.length`; no "+N" when it is 5 or fewer. |
| `salon`             | object          | The salon this stylist is read at (below). |
| `service_groups`    | array           | Only what this stylist does (below). `[]` when nothing. |

### `day_off`

We store no day off, only the salon's roster of dated shifts. So `day_off` is
the weekday, or weekdays, the stylist never works:

- a day the salon is open (a day the salon is closed is not a day off),
- with no shift at this salon in any rostered week of the last 4 roster weeks
  (the current week included),
- and only when there are at least 2 rostered weeks to judge from.

Several days come in week order, joined with a comma: `"Friday, Saturday"`. It
is `null` when it cannot be told: the stylist works every day the salon is
open, the roster is too short, or the salon published no hours. The same text
is in the stylist's row of `GET /salon/:id/stylists`.

### `media`

This salon's gallery photos that the salon tagged with this stylist: only
public, approved ones.

| Field           | Type            | Notes |
| --------------- | --------------- | ----- |
| `id`            | uuid            | The photo's own id. |
| `type`          | string          | `image` or `video`. Always `image` today. |
| `url`           | string          | The image (or the video file). |
| `thumbnail_url` | string \| null | `null` for an image. A video always has one: a video without a thumbnail is not sent. |

Newest first. No photos: `media` is `[]`, `media_count` is `0` and `has_story`
is `false`.

### `salon`

What `GET /salon/:id` says about the same salon, by the same code.

| Field         | Type             | Notes |
| ------------- | ---------------- | ----- |
| `id`          | uuid             | The `salon_id` from the path. |
| `name`        | string           | The salon's published name (else its branch's name). |
| `is_open`     | boolean \| null | Open right now, on the salon's own clock. `null`: the salon published no hours. |
| `hours_today` | string \| null  | To print as it is, for example `"10:00 AM - 9:00 PM"`. `null` on a day the salon does not open (a closed weekday, or closed by hand), and when it published no hours. |
| `latitude`    | number \| null  | The salon's pin. `null`: no pin. |
| `longitude`   | number \| null  | Same. |

The distance is yours to work out from the phone's position.

### `service_groups`

Only the services on the salon's menu that this stylist can do alone. The
groups, their order, the rows and the prices are the Services tab's
(`GET /salon/:id/services`), so one row renderer serves both.

| Field          | Type    | Notes |
| -------------- | ------- | ----- |
| `id`           | string  | The tab's group id (`service_groups[].id` there): a UUID, or `"other"`. |
| `name`         | string  | The group's name. |
| `services`     | array   | Never empty: a group with nothing this stylist does is not sent. |
| ↳ `id`         | uuid    | The service id. Goes to the booking flow as one of `serviceIds`. |
| ↳ `name`       | string  | |
| ↳ `description`| string \| null | |
| ↳ `price`      | number  | One visit, before VAT, at this branch. **The same number as the Services tab**, and the one the booking create checks. |
| ↳ `duration_min`, `duration_max` | number | Minutes. Today always equal. |

---

## 3. Rules the server follows

- **One call draws the whole screen.** No second call for the media, the
  services or the salon's hours.
- **`media` is capped at five**, and `media_count` carries the real number.
- **Distance is the app's job.** We send the salon's pin.
- **The services are this stylist's own.** They come from the same code that
  answers `GET /salon/:id/stylists?service_ids=`, asked the other way round. So
  a service is here exactly when this stylist is in that list for it. A service
  the salon sells but this stylist cannot do is not here.
- **The prices are the Services tab's**, before VAT, from the same code: this
  branch's own price when the salon set one, else the service's. It is the
  number the booking create checks.
- **Durations are a pair of minutes**, never a sentence.
- **Only this salon's stylists.** A stylist who no longer works there is `404`.
  So is a stylist of another salon, even another salon of the same business,
  and even with a real id. Never a read across salons.
- **`is_favorite` is per customer**, read from the token, and `false` when
  there is none.
- **Nothing is booked or held.** It is a read; the booking flow checks prices,
  stylists and times again when it creates the booking.

---

## 4. The heart

The heart uses the favourites route, with the stylist's id. **It needs a
token.**

```
POST /favourite
```

```json
{
  "stylist_id": "99999999-9999-9999-9999-999999999990"
}
```

It toggles, and the answer says which way it went. Draw what comes back. The
first tap:

```json
{
  "is_favorite": true
}
```

The next tap:

```json
{
  "is_favorite": false
}
```

- The salon heart is unchanged: `{ "salon_id": "..." }` works as before.
- Send exactly one of the two ids.
- After a save, the profile answers `is_favorite: true` for that customer.

Both ids at once, `422`:

```json
{
  "detail": "Send salon_id or stylist_id, not both.",
  "code": "validation_error",
  "errors": [
    {
      "field": null,
      "code": "one_id_only",
      "message": "Send salon_id or stylist_id, not both."
    }
  ]
}
```

An id that is not a UUID, `422`:

```json
{
  "detail": "Please correct the highlighted fields.",
  "code": "validation_error",
  "errors": [
    {
      "field": "stylist_id",
      "code": "invalid",
      "message": "Must be a valid UUID."
    }
  ]
}
```

Saving a stylist who is no longer available (one who left, or an id nobody
has), `404`. Removing a heart never answers this, so a heart on a stylist who
left can still be removed:

```json
{
  "detail": "This stylist is no longer available.",
  "code": "not_found",
  "errors": [
    {
      "field": null,
      "code": "not_found",
      "message": "This stylist is no longer available."
    }
  ]
}
```

Signed out, `401`: send the customer to sign in.

```json
{
  "detail": "Authentication credentials were not provided.",
  "code": "not_authenticated",
  "errors": [
    {
      "field": null,
      "code": "not_authenticated",
      "message": "Authentication credentials were not provided."
    }
  ]
}
```

---

## 5. Errors

Same envelope as every other route: `detail` (a sentence), `code` (what kind
of error), and `errors`, a list of `{ field, code, message }`.

| Case                                                              | Status | `code`              | `detail` |
| ----------------------------------------------------------------- | ------ | ------------------- | -------- |
| No such salon, or its id is not a UUID                            | 404    | `not_found`         | `This salon is not available.` |
| No such stylist at this salon, one who left, or its id is not a UUID | 404 | `not_found`         | `This stylist is no longer at this salon.` |
| A bad or expired token                                            | 401    | `not_authenticated` | `Authentication required.` |
| No token                                                          | 200    |                     | The route is public |

`detail` is written for the customer: show it on the empty state with the
"Back to salon" button.

```json
{
  "detail": "This stylist is no longer at this salon.",
  "code": "not_found",
  "errors": [
    {
      "field": null,
      "code": "not_found",
      "message": "This stylist is no longer at this salon."
    }
  ]
}
```

```json
{
  "detail": "This salon is not available.",
  "code": "not_found",
  "errors": [
    {
      "field": null,
      "code": "not_found",
      "message": "This salon is not available."
    }
  ]
}
```

A bad token. The token library adds its own items to `errors`; read `code`,
then log in again:

```json
{
  "detail": "Authentication required.",
  "code": "not_authenticated",
  "errors": [
    {
      "field": null,
      "code": "token_not_valid",
      "message": "Given token not valid for any token type"
    },
    {
      "field": "code",
      "code": "token_not_valid",
      "message": "token_not_valid"
    },
    {
      "field": "token_class",
      "code": "token_not_valid",
      "message": "AccessToken"
    },
    {
      "field": "token_type",
      "code": "token_not_valid",
      "message": "access"
    },
    {
      "field": "message",
      "code": "token_not_valid",
      "message": "Token is invalid"
    }
  ]
}
```

---

## 6. What the screen does next

Ticking services enables Continue, which opens the booking flow past the Expert
step:

| Screen state    | Passed on as |
| --------------- | ------------ |
| `salon_id`      | `salonId` |
| The ticked ids  | `serviceIds` |
| `id`            | The chosen stylist: the flow opens on the Time step |

The flow then calls `GET /booking/nearest-available/{salon_id}` with
`stylist_id` and `service_ids`, exactly as if the stylist had been picked in
the Expert step. Every service on this screen is one this stylist can do, so
any ticked set is accepted there. Two things to know about that route
(`BOOKING_NEAREST_AVAILABLE_API.md`):

- **It needs a token.** A guest can read this screen, and must sign in to see
  times.
- **One call answers one day.** For "the next free day", call it day by day.

---

## 7. Empty for now

| Field | Answer today | Why |
| ----- | ------------ | --- |
| `bio` | `null` | Nothing stores a text about a stylist yet. The salon's own "about" is the salon's. |
| `years_experience` | `null` | Not stored yet. |
| `price_level` | `null` | Nothing prices a stylist: a price level exists for a salon (`GET /salon/:id`), not for a person. Your open point says it can be dropped if it mirrors the salon. |
| `rating` | `null` | Reviews belong to a salon; none is linked to a stylist yet. |
| `review_count` | `0` | Same. |
| `is_network_member` | `false` | See §8. |
| A `video` in `media` | not sent | A salon cannot upload video yet. |
| Stylist stories | `has_story` follows `media` | There are no stylist stories; the salon has its own (`GET /salon/:id/stories`). |
| A price by stylist | the salon's price | Not used when a booking is priced, as your open point says. |

These fill in without a change to the shape: the app can already draw them.

---

## 8. A question for you: `is_network_member`

Your draft draws a "Network Member" badge from `is_network_member`. Nothing in
our data says what that is, so we send `false` for every stylist and the badge
never shows.

**What should it mean?** For example: the stylist works at more than one
salon, the salon is part of a partner programme, or the stylist is verified
in some way. Tell us the rule and who decides it (the salon, or GoStyle), and
we can say whether the data exists or what has to be added first.

---

## 9. `GET /salon/:id/stylists`: two small changes

Your draft says this route "stays as it is". It does, in shape. Two things
changed in what it answers, for the Stylists tab, the booking Expert step and
the service detail's `experts` alike:

1. **Only the salon's own stylists.** A stylist belongs to one salon (their
   home branch). In a business with two salons, each salon now lists its own
   people, never the other's. Those were never bookable at the other salon:
   the booking refused them. Today every business has one salon, so no list
   changed.
2. **`day_off` is filled**, with the same text as on this screen. It was always
   `null` before.

A row of that list, for the stylist in §2:

```json
{
  "id": "99999999-9999-9999-9999-999999999990",
  "tenant_id": "11111111-1111-1111-1111-111111111111",
  "branch_id": "22222222-2222-2222-2222-222222222222",
  "name": "Liam Johnson",
  "title": "Senior Barber",
  "role": "Barber and Grooming Expert",
  "avatar_url": null,
  "rating": null,
  "review_count": null,
  "years_experience": null,
  "day_off": "Tuesday"
}
```
