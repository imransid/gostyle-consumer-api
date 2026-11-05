# Service photos on every service list

Base URL: `https://api.gostyle.uk/api/v1`

A salon adds photos to a service in the service editor. Until now the app got
them only on Service Detail (`hero_url`, `gallery`), on `/services-details`
(`image_url`) and on `/search/services` (`images`). Now every list of
services sends them too, as one new key on each service row: `images`.

Behind the switch `SERVICE_IMAGES_V1`. Off, every list is exactly as today
(no `images` key). We tell you when it is on.

---

## 1. Where

| Method | Path | Where the rows are |
| ------ | ---- | ------------------ |
| GET | `/salon/{salon_id}/services` | `service_groups[].services[]` (Services tab) |
| GET | `/services?tenant_id=...` | `service_groups[].services[]` (the old menu call, same rows) |
| GET | `/services` (no `tenant_id`) | `results[]` (all salons, Popular Services) |
| GET | `/salon/{salon_id}/stylist/{stylist_id}` | `service_groups[].services[]` (Expert Profile) |
| GET | `/salon/{salon_id}/packages` | `bundles[].services[]` (Packages tab) |

`/search/services` already sends `images` the same way: no change there.

## 2. The field

```json
{
  "id": "6e0b3c1a-6f1e-4c47-9d1a-1d7f3f0e9a11",
  "name": "The Gentleman's Cut",
  "description": "Cut, wash and style.",
  "price": 199.0,
  "duration_min": 30,
  "duration_max": 30,
  "images": [
    "https://cdn.gostyle.uk/services/cut-1.jpg",
    "https://cdn.gostyle.uk/services/cut-2.jpg"
  ]
}
```

| Field | Type | Meaning |
| ----- | ---- | ------- |
| `images` | string[] | Photo URLs, up to 4. Always a list: `[]` when the service has no photo. Right after `duration_max` (on the Packages tab, before `quantity`). |

Rules:

1. **Order is Service Detail's order.** The service's own photos first (the
   main one first, then the order the salon set), then this salon's gallery
   photos tagged with the service (approved and public only). So
   `images[0]` is the same picture as Service Detail's `hero_url`.
2. **Up to 4.** For a list row show `images[0]`. For all photos open
   Service Detail (`gallery`, `gallery_count`).
3. **No repeats, no blanks.** The same URL is never sent twice.
4. **This salon's photos.** A tagged gallery photo shows only at the salon
   (branch) that posted it. The service's own photos show at every branch of
   the business.
5. **`[]` means no photo.** Show your placeholder. The key is never null.

Nothing else on these rows changes.

---

## Server steps (for Rafa)

No migration. No booking-api change. Two small reads per call (photos of
the services on that list), none when the list is empty.

1. Deploy the image as usual (api service only), with a rollback tag first.
2. Turn it on, on the service and in stack.env the same day, so a stack
   deploy keeps it:

   ```bash
   docker service update --env-add SERVICE_IMAGES_V1=True gostyle-consumer_api
   cd /root/gostyle-customer && grep -q '^SERVICE_IMAGES_V1=' stack.env || echo "SERVICE_IMAGES_V1='True'" >> stack.env
   bash scripts/stack-env-check.sh
   ```

   The check must print only `check done`.

3. Check (pick a salon whose service has a photo):

   ```bash
   curl -s "https://api.gostyle.uk/api/v1/salon/<salon_id>/services" | jq '.service_groups[].services[] | {name, images}'
   ```

   Every row has `images`; a service with photos shows its URLs.

4. Off again: `--env-add SERVICE_IMAGES_V1=False` and the same line in
   stack.env. The rows go back to today's shape.
