# Service Detail

Base URL: `https://api.gostyle.uk/api/v1`
Auth: none needed. A Bearer token is accepted, and a bad or expired one is `401`.

The agreed contract, 2026-09-30. It is the app team's
`service-detail-fe-contract.md` (kept as received) with what the server really
does. Every example below is what the server really answers: the full `200` was
taken from the running code, and a test asks the code again from the same rows
and must get the same answer, so this page cannot drift from the code.

The screen a customer opens from a salon's Services tab: the hero, the price
and duration, the gallery, what the visit includes, how to prepare, the
products, and the experts who can do it. **One call draws the whole screen.**

---

## What differs from your draft

1. **The route is public.** No token is needed, like every other
   `/salon/:id/...` route: a guest can open a salon, its services and its
   stylists, so the detail opens too. A token is accepted, and a bad or
   expired one is `401` (§4).
2. **The `404` body has one item in `errors`**, not `[]`. The item repeats
   `detail`. Read `detail` and `code`; do not rely on `errors` being empty (§4).
3. **`category.id` is a UUID**, never a word like `haircut_styling`. It is the
   same chip id the Services tab uses (`service_categories[].id`,
   `service_groups[].category_id`). A service with no category is
   `{ "id": "other", "label": "Other" }`, as on the tab; `category` is never
   `null`.
4. **One service id works at every branch of a salon.** A service belongs to
   the salon business, not to one branch, so the same `service_id` opens under
   each branch's `salon_id`. The price is that branch's own when the salon
   set one for it (else the service's own price), so the same service can
   cost differently at two branches, exactly as each branch's Services tab
   shows it.
5. **Photo URLs are plain storage URLs**, with no fixed host
   (`https://<bucket>.s3.<region>.amazonaws.com/...` today). Use them as they
   are; do not build or check a host. The host in the example is only an
   example.
6. **`experts: []` means nobody here can do it** (or the service is pulled),
   so keep Book Appointment disabled. It does **not** mean "the salon
   assigns": booking one visit always needs a stylist (`stylist_required`).
7. **`experts[].rating` and `experts[].review_count` are `null`.** There are
   no stylist reviews yet. Show no stars rather than 0.
8. **`rating`, `review_count` and `products` stay empty until that data
   exists**: `rating: null`, `review_count: 0`, `products: []` (§6).

Also worth knowing:

- `price` is a JSON number and can come written as `199.0`. Read it as a number.
- `duration_min` and `duration_max` are the same number today: every service
  has one length.
- Each `experts` row is exactly a row of `GET /salon/:id/stylists?service_ids=`,
  so it has that route's other keys too (`tenant_id`, `branch_id`,
  `years_experience`, `day_off`, `service_ids`). Ignore what you do not use.
  `day_off` is the stylist's steady day off, worked out from the roster
  (`"Tuesday"`, `"Friday, Saturday"`), or `null` when it cannot be told
  (`SALON_PROFILE_API.md` §3).
- `details` has no hair-type "Suitability" row: that is not in the data. It has
  a "Suitable for" row instead (Men, Women, Everyone, Kids), see §2.

---

## 1. Endpoint

| Method | Path                                         | Purpose                      |
| ------ | -------------------------------------------- | ---------------------------- |
| `GET`  | `/salon/{salon_id}/service/{service_id}` | Everything this screen draws |

```
GET /salon/33333333-3333-3333-3333-333333333333/service/66666666-6666-6666-6666-666666666660
```

Reused, unchanged:

| Method | Path                               | Used by                                             |
| ------ | ---------------------------------- | --------------------------------------------------- |
| `GET`  | `/salon/:id/stylists?service_ids=` | The same experts, in the booking flow's Expert step |
| `GET`  | `/salon/:id/products`              | The Shop tab, for a product's `variant_id`          |
| `GET`  | `/services-details?service_ids=`   | Several ids at once, basic fields (Review screen)   |

---

## 2. Response: `200 OK`

The seeded salon's "The Gentleman's Cut", with six service photos, one gallery
photo tagged with the service, the salon's stages and some care text:

```json
{
  "id": "66666666-6666-6666-6666-666666666660",
  "salon_id": "33333333-3333-3333-3333-333333333333",
  "name": "The Gentleman's Cut",
  "description": "A classic cut tailored to you, with a wash, a scalp massage and a styled finish.",
  "price": 199.0,
  "duration_min": 30,
  "duration_max": 30,
  "category": {
    "id": "55555555-5555-5555-5555-555555555551",
    "label": "Haircut and Styling"
  },
  "rating": null,
  "review_count": 0,
  "is_active": true,
  "hero_url": "https://gostyle-media.s3.me-central-1.amazonaws.com/tenants/11111111-1111-1111-1111-111111111111/services/66666666-6666-6666-6666-666666666660/media/1.jpg",
  "gallery": [
    "https://gostyle-media.s3.me-central-1.amazonaws.com/tenants/11111111-1111-1111-1111-111111111111/services/66666666-6666-6666-6666-666666666660/media/1.jpg",
    "https://gostyle-media.s3.me-central-1.amazonaws.com/tenants/11111111-1111-1111-1111-111111111111/services/66666666-6666-6666-6666-666666666660/media/2.jpg",
    "https://gostyle-media.s3.me-central-1.amazonaws.com/tenants/11111111-1111-1111-1111-111111111111/services/66666666-6666-6666-6666-666666666660/media/3.jpg",
    "https://gostyle-media.s3.me-central-1.amazonaws.com/tenants/11111111-1111-1111-1111-111111111111/services/66666666-6666-6666-6666-666666666660/media/4.jpg",
    "https://gostyle-media.s3.me-central-1.amazonaws.com/tenants/11111111-1111-1111-1111-111111111111/services/66666666-6666-6666-6666-666666666660/media/5.jpg"
  ],
  "gallery_count": 7,
  "included": [
    "Consultation",
    "Wash",
    "Scalp massage",
    "Hair cut",
    "Styling"
  ],
  "details": [
    {
      "label": "Time Duration",
      "value": "30 min",
      "icon": "clock"
    },
    {
      "label": "Suitable for",
      "value": "Men",
      "icon": "scissors"
    },
    {
      "label": "Consultation",
      "value": "Needed before this service.",
      "icon": "sparkles"
    }
  ],
  "preparation": [
    "Arrive with clean, dry hair.",
    "Ask your stylist which products suit your hair."
  ],
  "products": [],
  "experts": [
    {
      "id": "99999999-9999-9999-9999-999999999991",
      "tenant_id": "11111111-1111-1111-1111-111111111111",
      "branch_id": "22222222-2222-2222-2222-222222222222",
      "name": "Darius Stone",
      "title": "Master Barber",
      "role": "Haircut and Styling Expert",
      "avatar_url": null,
      "rating": null,
      "review_count": null,
      "years_experience": null,
      "day_off": null,
      "service_ids": [
        "66666666-6666-6666-6666-666666666660"
      ]
    },
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
      "day_off": "Tuesday",
      "service_ids": [
        "66666666-6666-6666-6666-666666666660"
      ]
    }
  ]
}
```

Seven photos: the first five are in `gallery`, so the app shows "+2". The
salon named one stage like the service itself ("The Gentleman's Cut"), so it is
not in `included`: it would only repeat the title.

### Fields

| Field           | Type            | Notes |
| --------------- | --------------- | ----- |
| `id`            | uuid            | The service id. Goes to the booking flow as `serviceIds`. |
| `salon_id`      | uuid            | The `salon_id` from the path, echoed. |
| `name`          | string          | The title. |
| `description`   | string \| null | As the salon wrote it; `null` when it wrote none. The same text as the Services tab. |
| `price`         | number          | One visit, before VAT, in the salon's currency, at this branch: the branch's own price when the salon set one, else the service's. **The same number as the Services tab**, and the one the booking create checks. |
| `duration_min`  | number          | Minutes. |
| `duration_max`  | number          | Minutes. Today always equal to `duration_min`. |
| `category`      | object          | `{ id, label }`: the Services tab's chip (the parent category when there is one). `id` is a UUID, or `"other"`. Never `null`. |
| `rating`        | null            | Always `null` for now (§6). |
| `review_count`  | number          | Always `0` for now (§6). Show "No reviews yet". |
| `is_active`     | boolean         | `false`: the service is pulled from sale (hidden, archived, deleted, or online booking off). Hide Book Appointment. |
| `hero_url`      | string \| null | The first photo. `null`: no photo, use the salon's cover. |
| `gallery`       | string[]        | At most 5, in display order. The hero is the first. |
| `gallery_count` | number          | All the photos. "+N" is `gallery_count - gallery.length`; no "+N" when it is 5 or fewer. |
| `included`      | string[]        | The service's stage names, in order. `[]` hides the block. |
| `details`       | array           | The Key Details rows, in order (below). |
| `preparation`   | string[]        | Preparation & Aftercare bullets. `[]` hides the block. |
| `products`      | array           | Always `[]` for now (§6), which hides the block. |
| `experts`       | array           | Who can do this service (below). `[]`: keep Book disabled. |

**Photos.** The service's own photos come first (the primary one, then in the
salon's order), then the salon's gallery photos tagged with this service (only
public, approved ones from this branch). The same photo twice is shown once.

**`included`.** The stage names as the salon built the service, in stage
order. A blank name is left out, a repeated name is shown once, and a stage
named like the service is left out.

**`preparation`.** The salon's pre-care lines first, then its after-care lines,
one bullet per line. A bullet mark or number the salon typed ("- ", "* ", "• ",
"1. ") is removed, so your own bullets do not show twice.

### `details` rows

In this order. The first two are always there; the others only when the salon
set them.

| `label`         | `value`, for example                                    | `icon`     | When |
| --------------- | ------------------------------------------------------- | ---------- | ---- |
| `Time Duration` | `30 min`, `1 hr`, `1 hr 30 min`                         | `clock`    | Always |
| `Suitable for`  | `Men`, `Women`, `Everyone`, `Kids`                      | `scissors` | Always |
| `Consultation`  | `Needed before this service.`                           | `sparkles` | The salon asks for a consultation first |
| `Patch test`    | `An allergy test, at least 48 hours before your visit.` (no hours set: `An allergy test, before your visit.`) | `drop` | The service needs an allergy patch test |
| `Minimum age`   | `16 years`                                              | `scissors` | The salon set a minimum age |

`icon` is always one of `clock`, `scissors`, `sparkles`, `drop`.

### `experts`

Exactly the list `GET /salon/{salon_id}/stylists?service_ids={service_id}`
answers: the same people, in the same order (best rated first; all ratings are
`null` today, so it is by name), in the same shape. The Expert step keeps
using that route; this one saves the screen a second call.

| Field          | Type            | Notes |
| -------------- | --------------- | ----- |
| `id`           | uuid            | The stylist id the booking flow sends. |
| `name`         | string \| null | First and last name. |
| `title`        | string \| null | The job title on the staff record. |
| `role`         | string \| null | The expertise line on the person's account. Print "title · role"; either can be `null`. |
| `avatar_url`   | string \| null | `null`: draw the initials circle. |
| `rating`       | null            | Always `null` for now. |
| `review_count` | null            | Always `null` for now. |

`[]` when nobody here holds the skills the service needs, when the service has
no stages, or when it is pulled. For example, a live service nobody at the
salon is trained for (only the fields that matter here):

```json
{
  "name": "Scalp Treatment",
  "category": {
    "id": "other",
    "label": "Other"
  },
  "included": [],
  "is_active": true,
  "experts": []
}
```

---

## 3. Rules the server follows

- **One call draws the whole screen.** Nothing below the fold needs a second
  call.
- **The gallery is capped at five**, and `gallery_count` carries the real
  number.
- **`experts` is the stylists route's list**, computed by the same code.
- **The price is the Services tab's**, before VAT, from the same code: this
  branch's own price when the salon set one (0 included), else the service's.
  It is the number booking-api checks when the booking is created. A price shown
  here and the price in the booking payload agree.
- **Duration is a pair of minutes, never a sentence.** `details` spells it out
  for the Key Details row.
- **A pulled service still answers `200`**, with `is_active: false` and
  `experts: []`, so a deep link or an old booking still opens (only the fields
  that matter here):

```json
{
  "id": "66666666-6666-6666-6666-666666666663",
  "name": "Scalp Treatment",
  "price": 180.0,
  "is_active": false,
  "experts": []
}
```

- **Only this salon business's services.** A service of another business is
  `404`, never a read across salons. A service that was never on sale (a draft)
  is `404` too.
- **Nothing is booked or held.** It is a read; the booking flow checks prices,
  stylists and times again when it creates the booking.

---

## 4. Errors

Same envelope as every other route: `detail` (a sentence), `code` (what kind
of error), and `errors`, a list of `{ field, code, message }`.

| Case                                                          | Status | `code`              | `detail` |
| ------------------------------------------------------------- | ------ | ------------------- | -------- |
| No such salon, or its id is not a UUID                        | 404    | `not_found`         | `This salon is not available.` |
| No such service here, never on sale, or its id is not a UUID  | 404    | `not_found`         | `This service is no longer on the menu.` |
| A bad or expired token                                        | 401    | `not_authenticated` | `Authentication required.` |
| No token                                                      | 200    |                     | The route is public |

`detail` is written for the customer: show it on the empty state with the
"Back to salon" button.

```json
{
  "detail": "This service is no longer on the menu.",
  "code": "not_found",
  "errors": [
    {
      "field": null,
      "code": "not_found",
      "message": "This service is no longer on the menu."
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

## 5. What the screen does next

Picking an expert enables Book Appointment, which opens the booking flow with:

| Screen state      | Passed on as |
| ----------------- | ------------ |
| `salon_id`        | `salonId`, the salon every later call is scoped to |
| `id`              | `serviceIds` (a list of one) |
| The chosen expert | The Expert step's pre-selection. One booking always needs a stylist: with `experts: []`, Book stays disabled |
| Ticked products   | None today: `products` is `[]` |

---

## 6. Empty for now

| Field | Answer today | Why |
| ----- | ------------ | --- |
| `rating` | `null` | Reviews belong to a salon; none is linked to a service yet. |
| `review_count` | `0` | Same. |
| `products` | `[]` | Nothing yet says which shop products a service uses. When it does, each item is the Shop tab's card (`id`, `variant_id`, `name`, `price`, `image_url`), and a booking sends `variant_id`. |
| `experts[].rating`, `experts[].review_count` | `null` | No stylist reviews yet. |
| A hair-type "Suitability" row | not sent | Not in the data. "Suitable for" (from who the service is for) is sent instead. |

These fill in without a change to the shape: the app can already draw them.
