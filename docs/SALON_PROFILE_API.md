# Salon Profile API — Mobile Handoff

Status: LOCAL only, not yet deployed to staging
Auth: none required, but send the Bearer token when you have one (see
"User-specific fields" below)
Swagger: `/api/docs/` once deployed

These five endpoints feed the salon profile screen
(`src/app/(salon)/male-profile.tsx`). One for the salon itself, then one
per tab so each tab can be fetched lazily when opened.

| #   | Endpoint                  | Feeds                                  |
| --- | ------------------------- | -------------------------------------- |
| 1   | `GET /salon/:id`          | Story slides, info card, check-in card |
| 2   | `GET /salon/:id/services` | Services tab                           |
| 3   | `GET /salon/:id/stylists` | Stylists tab                           |
| 4   | `GET /salon/:id/packages` | Packages tab                           |
| 5   | `GET /salon/:id/products` | Shop tab                               |

## Read this first: `:id` is a UUID

The original spec used `sal_01j8xk2e9`. That format does not exist
anywhere in the platform. `:id` is the **storefront UUID**, the same
`id` that `/api/v1/discover` already returns in its list. Take it
straight from the discovery response, do not transform it.

A salon that is not publicly discoverable returns 404, same rule as
`/discover/{id}`.

---

## 1. Salon details — `GET /salon/:id`

```json
{
  "id": "33333333-3333-3333-3333-333333333333",
  "slug": "iron-razor-jumeirah",
  "name": "The Iron Razor Barbershop",
  "tagline": "Precision Cuts and Classic Shaves Since 2015",
  "bio": "Redefining the modern grooming experience...",
  "category": "gents",
  "cover_url": null,
  "logo_url": null,
  "gallery": [],
  "rating": null,
  "review_count": null,
  "currency": "AED",
  "price_level": 3,

  "is_open": true,
  "status": "OPEN",
  "hours_today": "10:00 AM - 10:00 PM",

  "location": {
    "address": "Shop 4, Al Wasl Road, Jumeirah 1, Dubai",
    "latitude": 25.2213,
    "longitude": 55.2621,
    "map_url": "https://maps.google.com/?q=25.2213,55.2621"
  },

  "amenities": ["wifi", "refreshments", "parking", "card_payment"],

  "booking_policy": {
    "deposit_required": true,
    "deposit_percentage": 20,
    "deposit_amount": 0,
    "free_cancellation": true,
    "cancel_window_hours": 24
  },

  "social_links": [
    { "platform": "instagram", "url": "https://instagram.com/theironrazor" },
    { "platform": "tiktok", "url": "https://tiktok.com/@theironrazor" },
    { "platform": "whatsapp", "url": "https://wa.me/971501234567" },
    { "platform": "website", "url": "https://ironrazor.ae" }
  ],

  "active_booking": null
}
```

### `status` is new, and `is_open` alone is not enough

A salon manager can set one of five states by hand:

| status          | is_open | Meaning                          |
| --------------- | ------- | -------------------------------- |
| `OPEN`          | true    | Normal opening hours             |
| `BUSY`          | true    | Open but very crowded            |
| `WALK_INS`      | true    | Taking walk-ins right now        |
| `SPECIAL_HOURS` | true    | Open on non-standard hours today |
| `CLOSED`        | false   | Shut, whatever the grid says     |

`is_open` is kept as a boolean so nothing breaks today, but `BUSY` and
`WALK_INS` both collapse to `true` and the salon's actual message is
lost. Render the badge from `status` when you can.

Both can be **null**, which means the salon has published no opening
hours. Null is not "closed"; hide the row instead of showing "Closed".

### `amenities` is a closed set of eight

Only these slugs are ever sent. Anything else is dropped server-side, so
you will never receive an unknown value:

```
wifi              refreshments      parking           prayer_room
card_payment      air_conditioning  kids_corner       wheelchair_access
```

Note `refreshments`, not `coffee`. The spec said `coffee`; the platform
stores a broader concept and there is no separate coffee amenity.

### `social_links` has six platforms, and snapchat is not one

`instagram`, `tiktok`, `youtube`, `facebook`, `whatsapp`, `website`.
Snapchat does not exist in the platform and cannot be added without a
schema change there.

Links the salon has switched off are omitted entirely, even when a
handle is stored for them. Render whatever arrives, in the order it
arrives.

### `price_level` is 1 to 3, but the platform has four tiers

`BUDGET` maps to 1, `MID_RANGE` to 2, and both `UPSCALE` and `PREMIUM`
map to 3. If the app ever grows a fourth dollar sign, tell backend and
the mapping opens up.

### `booking_policy.free_cancellation` loses information

The platform models refunds as a **tiered ladder** ("100% back before
48h, 50% back before 24h, 0% after"), not a yes/no. `free_cancellation`
is true whenever `cancel_window_hours` is above zero.
`cancel_window_hours` is sent alongside so the card can eventually show
the real window instead of a boolean.

`deposit_amount` is always 0: the platform stores a percentage, never a
flat amount. Use `deposit_percentage`.

---

## 2. Services tab — `GET /salon/:id/services`

```json
{
  "service_categories": [
    { "id": "all", "label": "All" },
    {
      "id": "55555555-5555-5555-5555-555555555551",
      "label": "Haircut and Styling",
      "icon": "scissors"
    }
  ],
  "service_groups": [
    {
      "id": "55555555-5555-5555-5555-555555555552",
      "category_id": "55555555-5555-5555-5555-555555555551",
      "name": "Precision Cuts",
      "services": [
        {
          "id": "66666666-6666-6666-6666-666666666660",
          "name": "The Gentleman's Cut",
          "description": "A classic haircut tailored to your preferences.",
          "price": 199.0,
          "duration_min": 30,
          "duration_max": 30
        }
      ]
    }
  ]
}
```

### Category ids are UUIDs, not fixed slugs

The spec assumed `haircut_styling`, `nail_care` and so on were global
slugs the app maps to icons. They are not. Categories are **per-tenant
rows** with tenant-authored names, so Salon A's "Haircut" and Salon B's
"Haircut" are two different UUIDs.

The icon is therefore **sent to you**: `service_categories[].icon` is a
lucide icon name (kebab-case, like `scissors`). Render it and fall back
to a default when it is null or unrecognised.

### Groups and chips

Today most tenants have a flat category list, so each category is both a
chip and a group and `category_id` equals the group's own `id`. When a
tenant fills in parent categories, the chip becomes the parent and the
group stays the child, with **no change to this response shape**. Filter
groups by `category_id` and it works in both worlds.

A service with no category appears under a group named `"Other"` with
`id` and `category_id` both set to the string `"other"`. Do not drop it;
it is a bookable service.

### `duration_min` equals `duration_max` today

The platform stores one duration per service. A real range only exists
when a service has variants with different durations, which the API does
not read yet. Collapse `20 - 20 mins` to `20 mins` on your side.

### `is_popular` was dropped

No flag exists for it and nobody defined what "popular" would mean. The
fire badge cannot be built from this endpoint.

---

## 3. Stylists tab — `GET /salon/:id/stylists`

```json
{
  "stylists": [
    {
      "id": "99999999-9999-9999-9999-999999999990",
      "name": "Liam Johnson",
      "role": "Senior Barber",
      "avatar_url": null,
      "rating": null,
      "review_count": null,
      "years_experience": null,
      "day_off": null
    }
  ]
}
```

**Four fields are permanently null right now.** The keys are present so
the response shape stays stable. Treat null as "hide this element".

| Field              | Why                                                |
| ------------------ | -------------------------------------------------- |
| `rating`           | Reviews attach to the salon; no `staff_id` column   |
| `review_count`     | Same table, same reason                            |
| `years_experience` | No column anywhere, no screen ever collects it      |
| `day_off`          | Shift data exists but a day off is not stored       |

The first two are blocked on a platform ticket to add `staff_id` to the
review table. `day_off` may become derivable from the shift roster
later. `years_experience` needs both a column and a place for salons to
enter it.

Only staff who have actually joined appear here: someone invited but who
never accepted is filtered out.

---

## 4. Packages tab — `GET /salon/:id/packages`

```json
{
  "my_packages": [],
  "bundles": [
    {
      "id": "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaa1",
      "name": "Groom and Go",
      "description": "Leave sharp, stay sharp.",
      "features": [
        "Gentleman's Cut",
        "Hot Towel Shave",
        "Beard oil to take home"
      ],
      "duration_minutes": 65,
      "price": 370.0,
      "price_before": 459.0,
      "save_amount": 89.0,
      "theme": "gold"
    }
  ]
}
```

### `my_packages` is always empty, even when logged in

This is not an auth problem. The platform can **sell** a package but has
no table tracking sessions used against sessions bought, so
`sessions_total` and `sessions_used` cannot be computed by anyone. A
platform ticket is open for a redemption table.

Build the tab shell against the empty array; the shape will not change
when it starts filling.

### `price_before` and `save_amount` may be null

They are computed by summing the member services at their individual
prices. When a bundle is priced at or above the sum of its parts there
is no saving, and both fields come back null rather than 0. Hide the
strikethrough row in that case.

`duration_minutes` is likewise the summed duration of the members.

---

## 5. Shop tab — `GET /salon/:id/products`

```json
{
  "products": [
    {
      "id": "cccccccc-cccc-cccc-cccc-ccccccccccc0",
      "name": "Iron Beard Oil",
      "price": 180.0,
      "image_url": "https://picsum.photos/seed/prod0/400/400"
    }
  ]
}
```

Retail products only. Salon-use stock (developer, bleach, towels) is
filtered out server-side.

**The shop is tenant-wide, not branch-specific.** Products carry no
branch in the schema, so every branch of a multi-branch salon shows the
same list. Stock is tracked per branch elsewhere, so a per-branch shop
is possible later, but it is a different query and not built.

---

## User-specific fields

`active_booking` and `my_packages` are the only two fields that depend
on who is asking. Both are currently hardcoded to null and `[]`.

The reason is structural: your account lives in the consumer schema with
its own id, and the salon's client record lives in the platform schema
scoped to that tenant. **There is no link between them.** Until one
exists, the API cannot answer "does this person have a booking here".

Send the Bearer token anyway. The endpoints accept it today and will
start using it the moment the link lands, with no contract change.

---

## Nullability summary

Assume every field can be null except `id`, `slug`, and the top-level
container keys (`stylists`, `bundles`, `products`, `service_groups`,
`service_categories`, `amenities`, `social_links`, `gallery`), which are
always present and may be empty arrays.

In particular:

- `cover_url`, `logo_url` and `gallery` are null or empty until the
  salon uploads media
- `rating` and `review_count` are null with no published reviews. Show
  "New" rather than 0
- `tagline`, `bio`, `price_level` and every `location` field are null
  until the salon fills that part of its storefront
- a salon that has never published at all returns 200 with almost
  everything null. That is a valid state, not an error

---

## Money and formatting

All prices are plain numbers in the salon's currency, which arrives once
as `currency` on endpoint 1. Never expect a formatted string. The app
owns the dirham symbol, the separators and the decimal places.

Durations are integer minutes.

---

## Known gaps, do not block on these

- `active_booking` and `my_packages`: blocked on the consumer-to-customer
  link (see above)
- Stylist `rating`, `review_count`, `years_experience`, `day_off`
- `chair` and `can_start_session` on `active_booking`: nothing links a
  booking to a physical chair
- `is_popular` on categories
- Snapchat
- Dated opening-hours exceptions (Eid, holidays) are not read yet, so
  `hours_today` shows the normal weekly hours on those days

---

## Also note: `/discover` changed

Two things on the existing discovery endpoint moved while this was
built:

1. `photo_url` was replaced by `cover_url`, `logo_url` and
   `gallery_urls`. The `DISCOVERY_API.md` doc still shows the old field.
2. `hours_today`, `closes_at` and `is_open_now` now come from the
   salon's **published** storefront hours rather than an internal
   onboarding value, and a new `status` field carries the five states
   described above. Salons open past midnight also report correctly now;
   they previously showed as closed all evening.

---

## Who to ping

Backend: Imran (Rafa). Changes to this contract will be announced in the
team channel before deploy.