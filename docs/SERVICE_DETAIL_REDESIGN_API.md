# Service Details redesign: photos, menu group and what's included

Base URL: `https://api.gostyle.uk/api/v1`
Auth: as today. The route is public; a Bearer token is accepted, and a bad or
expired one is `401`.

Answer to the mobile team's brief "Service Details redesign: photos, menu
group and what's included" (2026-10-30, kept as received in
`service-detail-redesign-fe-contract.md`), for the Service Details screen
(`src/app/(salon)/service-details.tsx`, Figma 3908:16357 and 3908:17110).

| Ask | Where | State |
| --- | ----- | ----- |
| `images`: every photo, in order | `GET /salon/{salon_id}/service/{service_id}` | New, behind `SERVICE_DETAIL_REDESIGN_V1`. We tell you when it is on |
| `group`: the menu group, after the category | same | Same switch |
| `included_items`: each item with its line | same | Same switch |

Nothing is removed and no field changes shape. With the switch off the answer
is exactly as today. Every field in `SERVICE_DETAIL_API.md` stays.

---

## 1. The response after the change

The seeded salon's "The Gentleman's Cut", filed under "Precision Cuts" in
"Haircut and Styling", with seven photos. Only the new fields and their
neighbours are shown; `SERVICE_DETAIL_API.md` §2 has the full answer. A test
asks the code again from the same rows and must get this answer.

```json
{
  "category": {
    "id": "55555555-5555-5555-5555-555555555551",
    "label": "Haircut and Styling"
  },
  "group": {
    "id": "55555555-5555-5555-5555-555555555552",
    "name": "Precision Cuts"
  },
  "gallery_count": 7,
  "images": [
    "https://gostyle-media.s3.me-central-1.amazonaws.com/tenants/11111111-1111-1111-1111-111111111111/services/66666666-6666-6666-6666-666666666660/media/1.jpg",
    "https://gostyle-media.s3.me-central-1.amazonaws.com/tenants/11111111-1111-1111-1111-111111111111/services/66666666-6666-6666-6666-666666666660/media/2.jpg",
    "https://gostyle-media.s3.me-central-1.amazonaws.com/tenants/11111111-1111-1111-1111-111111111111/services/66666666-6666-6666-6666-666666666660/media/3.jpg",
    "https://gostyle-media.s3.me-central-1.amazonaws.com/tenants/11111111-1111-1111-1111-111111111111/services/66666666-6666-6666-6666-666666666660/media/4.jpg",
    "https://gostyle-media.s3.me-central-1.amazonaws.com/tenants/11111111-1111-1111-1111-111111111111/services/66666666-6666-6666-6666-666666666660/media/5.jpg",
    "https://gostyle-media.s3.me-central-1.amazonaws.com/tenants/11111111-1111-1111-1111-111111111111/services/66666666-6666-6666-6666-666666666660/media/6.jpg",
    "https://gostyle-media.s3.me-central-1.amazonaws.com/tenants/11111111-1111-1111-1111-111111111111/storefronts/33333333-3333-3333-3333-333333333333/gallery/fade-side.jpg"
  ],
  "included": [
    "Consultation",
    "Wash",
    "Scalp massage",
    "Hair cut",
    "Styling"
  ],
  "included_items": [
    {
      "title": "Consultation",
      "description": "Tailored style analysis based on face shape and hair type."
    },
    {
      "title": "Wash",
      "description": "A deep cleanse with a gentle shampoo."
    },
    {
      "title": "Scalp massage",
      "description": "A relaxing massage to wake up the scalp."
    },
    {
      "title": "Hair cut",
      "description": "A classic cut with clippers and scissors."
    },
    {
      "title": "Styling",
      "description": null
    }
  ]
}
```

Where the keys sit: `group` right after `category`, `images` right after
`gallery_count`, `included_items` right after `included`.

| Field | Type | Notes |
| ----- | ---- | ----- |
| `images` | array of URLs | Always present, `[]` when there are none. In the salon's order; the first is shown large and is the same as `hero_url`. The same list as the service's `images` on `GET /salon/:id/services` (that list stops at 4; this one goes on to 10) |
| `group` | object or null | `{ id, name }`, the same ids as `service_groups[].id` on `GET /salon/:id/services`, and the same name |
| `included_items` | array | Always present, `[]` hides the block. In display order |
| `included_items[].title` | string | Short, shown in bold. Never ends with a colon |
| `included_items[].description` | string or null | The salon's line. `null` shows the title alone |

---

## 2. The rules

### `images`

1. **Every photo, up to 10.** No cap of 5. The service's own photos first
   (the main one, then the salon's order), then this salon's gallery photos
   tagged with the service (approved and public only).
2. **No repeats, no blanks.** The same URL is never sent twice.
3. `hero_url`, `gallery` and `gallery_count` stay for older builds. `gallery`
   is still at most 5, and `gallery_count` still counts every photo, so it can
   be more than 10.

### `group`

1. **It is the Services tab's group** for this service: the same `id` and
   `name` as the tab's `service_groups[]` entry the service is listed under.
2. **When the salon has groups inside a category** (as above), the path reads
   "Haircut and Styling > Precision Cuts".
3. **When the salon's categories are flat** (no groups inside them, most
   salons today), the category is its own group on the tab, so `group.id` is
   `category.id` and `group.name` is `category.label`. That is the same name
   the profile gives you today.
4. **A service with no category** (or a deleted one) is in the tab's "Other"
   group: `{ "id": "other", "name": "Other" }`, with `category`
   `{ "id": "other", "label": "Other" }`.
5. So `group` is **never `null` today**: every service sits in a tab group.
   A pulled service (`is_active: false`) keeps its group.

### `included_items`

1. **One item for each `included` name, in the same order.** The same stages
   are left out (a blank name, a repeated name, a stage named like the
   service), so the titles are the `included` names.
2. **`title`**: the name with a colon at its end dropped, so your "Title:"
   never shows "Title::". `included` keeps the name as before.
3. **`description`**: the line the salon wrote on that stage of the service,
   on one line (extra spaces and line breaks tidied), or `null` when it wrote
   none.
4. **`included` stays the titles alone**, for the builds already installed.

---

## 3. Photo size

We send the photo exactly as the salon uploaded it. The platform keeps the
original (up to 10 MB a photo) and does not resize yet, so we cannot promise
700 x 840, JPEG or WebP, or under 200 KB. Crop to fill, centred, as you
planned.

## 4. What the screen no longer reads

`rating`, `review_count`, `details`, `preparation`, `products` and `experts`
are still sent. Tell us when they can go.

## 5. Errors

Unchanged: `404` when the service is not on this salon's menu, `401` when
the token has expired (`SERVICE_DETAIL_API.md` §4).

## 6. Your open points (section 6)

1. **Where the lines come from.** Yes, a salon can write them today. Each
   stage of a service on the platform has its own free-text description (up
   to 2,000 characters), set when the stage is added or edited; we send it as
   the item's `description`. A stage with none gives `null`. No new partner
   app field is needed. One thing to know: the platform calls this text the
   stage's note, so some salons may have written it for their staff. We read
   the live lines before switching on.
2. **Photos: is 10 the right limit?** The platform puts no limit on how many
   photos a service can have (only 10 MB each), so a salon can upload more.
   We send the first 10, as you asked; `gallery_count` still counts all of
   them.

---

## Server steps (for Rafa)

No migration. No booking-api or platform change. No extra query: the three
keys come from the rows the screen already reads.

1. Before switching on, read the live stage lines (section A), the photo
   counts (B) and how `group` will read (C). Read-only, ends with ROLLBACK:

   ```bash
   psql -h <DB_HOST> -p <DB_PORT> -U <DB_USER> -d <DB_NAME> \
        -v ON_ERROR_STOP=1 -f scripts/sql/service_detail_redesign_live_checks.sql
   ```

   If a line in A is written for staff, not customers, tell me before step 3.

2. Deploy the image as usual (api service only), with a rollback tag first.
3. Turn it on, on the service and in stack.env the same day, so a stack
   deploy keeps it:

   ```bash
   docker service update --env-add SERVICE_DETAIL_REDESIGN_V1=True gostyle-consumer_api
   cd /root/gostyle-customer && grep -q '^SERVICE_DETAIL_REDESIGN_V1=' stack.env || echo "SERVICE_DETAIL_REDESIGN_V1='True'" >> stack.env
   bash scripts/stack-env-check.sh
   ```

   The check must print only `check done`.

4. Check (a service with photos):

   ```bash
   curl -s "https://api.gostyle.uk/api/v1/salon/<salon_id>/service/<service_id>" | jq '{category, group, hero_url, images, included, included_items}'
   ```

   `group` is there, `images[0]` is `hero_url`, and `included_items` has one
   item per `included` name.

5. Off again: `--env-add SERVICE_DETAIL_REDESIGN_V1=False` and the same line
   in stack.env. The answer goes back to today's shape.
