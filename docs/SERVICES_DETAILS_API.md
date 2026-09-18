# Services — Details by id

`GET /api/v1/services-details?service_ids=<uuid>,<uuid>` · **Bearer token required**

Resolves a set of service ids into their full details, in one call.

The one endpoint in this app keyed by SERVICE rather than by salon. Every
other services route answers "what does this salon sell"; this one answers
"what are these, exactly", for ids the app is already holding — a basket it
kept across a restart, a booking it is drawing, a link someone shared.

---

## 1. Request

| Param         | Type   | Required | Notes                                     |
| ------------- | ------ | -------- | ----------------------------------------- |
| `service_ids` | string | yes      | Service UUIDs, comma separated. Up to 50. |

Three spellings are accepted and normalise to the same list, because three
clients spell an array three ways — the same parser the Expert step uses
(`BOOKING_EXPERT_API.md` §1):

```
?service_ids=a,b                  the customer app
?service_ids=a&service_ids=b
?service_ids[]=a&service_ids[]=b  axios 1.x default for an array
```

**Required here**, unlike on the stylists endpoint where an absent
`service_ids` means "the whole roster". There is no whole roster to fall back
on: a lookup with nothing to look up is a caller bug, and answering `[]` would
hide it.

---

## 2. Response — `200 OK`

A bare array, one object per resolved service.

```json
[
  {
    "id": "66666666-6666-6666-6666-666666666660",
    "salon_id": "55555555-5555-5555-5555-555555555555",
    "name": "Signature Fade",
    "description": "Skin fade with a scissor-cut top and a hot towel finish.",
    "price": 120.00,
    "duration_min": 45,
    "duration_max": 45,
    "category": {
      "id": "77777777-7777-7777-7777-777777777771",
      "label": "Haircut & Styling"
    },
    "image_url": "https://cdn.gostyles.app/services/fade.jpg",
    "is_active": true
  }
]
```

| Field          | Type           | Notes                                                                    |
| -------------- | -------------- | ------------------------------------------------------------------------ |
| `id`           | uuid           | Echo of the requested id.                                                |
| `salon_id`     | uuid \| null   | The storefront that sells it. Null is a data hole — see §4.              |
| `name`         | string         | `service.name`.                                                          |
| `description`  | string \| null | `service.description`.                                                   |
| `price`        | number         | Current price, two decimals, from `service.price_minor`.                 |
| `duration_min` | number         | Minutes.                                                                 |
| `duration_max` | number         | Minutes. **The same number as `duration_min`** — see §4.                 |
| `category`     | object \| null | `{ id, label }`, the same chip the services tab draws.                   |
| `image_url`    | string \| null | The service's primary image, else its first by sort order.               |
| `is_active`    | boolean        | `false` for a service the salon has retired — see §3.4.                  |

`category` is the PARENT category when the service's own category has one,
which is exactly what `SALON_PROFILE_API.md` §2 puts in the chip row. So an
app can group a basket against ids it has already drawn, rather than learning
a second vocabulary. A service with no category, or one pointing at a deleted
row, gets `null` — not the menu's invented "Other" group, because one service
on its own has nothing to be grouped under.

---

## 3. Rules this endpoint follows

1. **The requested order is preserved.** The array comes back in the order the
   ids were sent, not the database's, so the app renders a basket the way the
   customer built it.
2. **Duplicates collapse.** A repeated id yields one entry.
3. **What cannot be resolved is skipped.** An unknown id is left out rather
   than erroring, so the array may be shorter than the request, or empty. The
   caller compares lengths to notice.
4. **Retired services still resolve**, carrying `is_active: false`. This is the
   reason the endpoint does not reuse the menu's selector: that one drops
   anything unpublished, deleted, or off online booking, which is exactly the
   set of rows a six-month-old booking needs described. A gap in the array is
   worse than a row marked inactive.

   `is_active` is true when all three of the menu's conditions hold —
   `status = PUBLISHED`, `deleted_at IS NULL`, `online_booking_enabled` — so
   the two endpoints cannot disagree about what "active" means.
5. **Prices are current, not quoted.** This is a lookup. booking-api recomputes
   every figure against its own quote and is the only authority on what is
   charged (`BOOKING_CREATE_API.md`), so a customer holding a stale basket is
   corrected there, not here.
6. **Ids may span salons**, and therefore tenants. Nothing assumes one salon;
   each entry names its own `salon_id`, and categories are read for every
   tenant in the batch in one query.
7. **No pagination**, and a cap of 50 ids. The caller asks for a known, small
   set; the cap keeps a hand-written query string from becoming an unbounded
   `IN` list.

---

## 4. Known gaps

- **`duration_min` and `duration_max` are the same number.** A real range needs
  `service_variant` rows with differing durations, and nothing reads those yet.
  Sending one value twice is honest and lets the app collapse "45 – 45 mins" to
  "45 mins" itself. Same as the services tab (`SALON_PROFILE_API.md` §2).
- **`salon_id` is resolved through the tenant, not the branch.** A service row
  belongs to a tenant; there is no salon column to read. What the app calls a
  salon is a storefront, one per branch, so this picks the tenant's storefront —
  preferring a `PUBLIC` one, then the oldest. **A tenant with several branches
  therefore always names the same salon**, even for a service sold at only one
  of them. The fix is `service_branch_availability`, which is not read anywhere
  yet (`selectors.BRANCH_AVAILABILITY_ENABLED` is `False`); when it is, this is
  one of the call sites that has to learn about it.
- **`salon_id` can be `null`**, when a service's tenant has no live storefront
  at all. That is a data hole rather than a state the product has, and the row
  is still returned: dropping it would put a gap in a basket over a link the app
  probably was not going to draw.
- **`price` ignores branch pricing** for the same reason. A service id arrives
  without a branch, so `service_branch_availability.price_minor` cannot be
  applied even where it exists. The menu endpoint has the same hole today.

---

## 5. Errors

Same envelope as `AUTH_GUIDE.md`. Only a malformed request fails.

| Case                                | Status | `code`             | `errors[0].code` |
| ----------------------------------- | ------ | ------------------ | ---------------- |
| `service_ids` missing or empty      | 422    | `validation_error` | `missing_filter` |
| `service_ids` is not a list of UUIDs| 422    | `validation_error` | `invalid`        |
| More than 50 ids                    | 422    | `validation_error` | `invalid`        |
| No bearer token                     | 401    | `not_authenticated`| `not_authenticated` |
| No id resolves                      | 200    | — (empty array)    | —                |
| Some ids do not resolve             | 200    | — (short array)    | —                |

```json
{
  "detail": "Please correct the highlighted fields.",
  "code": "validation_error",
  "errors": [
    {
      "field": "service_ids",
      "code": "missing_filter",
      "message": "Provide at least one service id."
    }
  ]
}
```
