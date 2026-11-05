# Photos on services and bundles for the salon profile

Base URL: `https://api.gostyle.uk/api/v1`

Answer to the mobile team's brief "Photos on services and bundles for the
salon profile" (2026-10-30, `salon-services-packages.md`), for the Services
and Packages tabs of `src/app/(salon)/salon-profile.tsx`.

| Ask | Where | State |
| --- | ----- | ----- |
| `images` on every service | `GET /salon/{id}/services` | Live since 2026-11-05 (`SERVICE_IMAGES_V1`, on) |
| `quantity` on each bundle service | `GET /salon/{id}/packages` | Live already |
| `image_url` on every bundle | `GET /salon/{id}/packages` | New, behind `PACKAGE_IMAGE_V1`. We tell you when it is on |

Nothing else changes: same URLs, same auth, same statuses, no new errors.
Every other field stays as it is (`theme` and `features` too).

---

## 1. `GET /salon/{id}/services`: `images`

Each service row has `images`, right after `duration_max`.

```json
{
  "id": "6e0b3c1a-6f1e-4c47-9d1a-1d7f3f0e9a11",
  "name": "The Gentleman's Cut",
  "description": "A classic haircut tailored to your preferences.",
  "price": 199.0,
  "duration_min": 20,
  "duration_max": 30,
  "images": [
    "https://cdn.gostyle.uk/services/cut-1.jpg",
    "https://cdn.gostyle.uk/services/cut-2.jpg",
    "https://cdn.gostyle.uk/services/cut-3.jpg",
    "https://cdn.gostyle.uk/services/cut-4.jpg"
  ]
}
```

| Field | Type | Notes |
| ----- | ---- | ----- |
| `images` | array of URLs | Always present. In the order the salon set. `[]` when the service has none. Absolute `https` URLs, as `logo_url`. Up to 4. |

- **Order:** the service's own photos first (the main one first, then the
  order the salon set), then this salon's gallery photos tagged with the
  service. No blanks, no repeats.
- **`/services-details`:** its `image_url` is the first of `images` (the
  same rule and the same order), so the two screens agree. When a service
  has only tagged gallery photos, `image_url` stays `null` there.
- **`images` is shipped**, so you can switch the empty case to your plain
  placeholder now (your section 5.1).

## 2. `GET /salon/{id}/packages`: `image_url` and `quantity`

Each bundle has `image_url`, right after `theme`. Each service in its
`services` has `quantity`. `my_packages` is unchanged.

```json
{
  "my_packages": [],
  "bundles": [
    {
      "id": "4f7e2a10-8c1d-4b6e-9a3f-2d5c7e9b1a40",
      "name": "Gold Grooming Package",
      "description": "The complete reset cut, beard, hot towel.",
      "features": [],
      "duration_minutes": 325,
      "price": 800.0,
      "price_before": 1080.0,
      "save_amount": 280.0,
      "theme": "gold",
      "image_url": "https://cdn.gostyle.uk/bundles/gold-grooming.jpg",
      "services": [
        { "id": "a1c0...", "name": "Sultan Haircut", "description": null, "price": 90.0, "duration_min": 30, "duration_max": 30, "images": [], "quantity": 6 },
        { "id": "b2d1...", "name": "Beard Sculpt", "description": null, "price": 60.0, "duration_min": 20, "duration_max": 20, "images": [], "quantity": 6 },
        { "id": "c3e2...", "name": "Hot Towel Shave", "description": null, "price": 180.0, "duration_min": 25, "duration_max": 25, "images": [], "quantity": 1 }
      ],
      "can_book": true
    }
  ]
}
```

| Field | Type | Notes |
| ----- | ---- | ----- |
| `image_url` | string or null | Always present. `null` when the bundle has no photo. Absolute `https` URL. |
| `services[].quantity` | integer, >= 1 | How many of that service the bundle covers. `1` shows the name alone, `6` shows "6x Name". |

- **Which photo:** the bundle's own main photo, as the salon uploaded it
  to the package. The first photo uploaded is the main one; when the salon
  reorders, the new first one is.
- **`services`** is on every bundle, with `id` and `name` as before (book a
  bundle with those ids, as today). Each row also has the Services tab's
  fields and `images`.
- **`theme`** stays in the answer. Ignore it if you no longer use it.

## 3. Photo size

We send the photo exactly as the salon uploaded it. The platform keeps the
original and does not resize yet, so we cannot promise 264 x 360 (service)
or 716 x 796 (bundle), or the file size. Crop to fill, centred, as you
planned.

## 4. Your open points (section 6)

1. **Where the photos come from.** Yes, the salon uploads them on the
   platform today:
   - service photos: the service editor (`service_media`), plus gallery
     photos the salon tags with a service;
   - bundle photos: the package's own photos
     (`POST /v1/catalog/packages/{packageId}/media` on the platform,
     `service_package_media`).
   A salon that has not uploaded any shows `[]` / `null`.
2. **`quantity` is stored.** Every item of a package has its own quantity
   (the platform defaults to 1 and never stores less), and the published
   package keeps it. Each service is listed once, with its quantity.

---

## Server steps (for Rafa)

No migration. No booking-api or platform change. One small read per call
(the listed bundles' photos), none when the tab lists no bundle.

1. Deploy the image as usual (api service only), with a rollback tag first.
2. Turn it on, on the service and in stack.env the same day, so a stack
   deploy keeps it:

   ```bash
   docker service update --env-add PACKAGE_IMAGE_V1=True gostyle-consumer_api
   cd /root/gostyle-customer && grep -q '^PACKAGE_IMAGE_V1=' stack.env || echo "PACKAGE_IMAGE_V1='True'" >> stack.env
   bash scripts/stack-env-check.sh
   ```

   The check must print only `check done`.

3. Check (a salon with bundles):

   ```bash
   curl -s "https://api.gostyle.uk/api/v1/salon/<salon_id>/packages" | jq '.bundles[] | {name, image_url, services: [.services[] | {name, quantity}]}'
   ```

   Every bundle has `image_url` (a URL, or null when it has no photo) and
   every service has `quantity`.

4. Off again: `--env-add PACKAGE_IMAGE_V1=False` and the same line in
   stack.env. The bundles go back to today's shape.
