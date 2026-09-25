# Group Booking

Base URL: `https://api.gostyle.uk/api/v1`
Auth: Bearer token on every request. The booker is taken from the token, never
from the payload.

One group booking is one party at one salon at one start time: several members,
each with their own services, their own stylist, and their own slot inside the
window. It is created as a single booking so the party is confirmed, paid and
cancelled together.

Only **three new endpoints** are needed. Everything else the flow uses already
exists and is unchanged — see §1.

---

## 1. Endpoints

| Method | Path                          | Purpose                             |
| ------ | ----------------------------- | ----------------------------------- |
| `GET`  | `/user/lookup`                | Resolve an invited member — §2      |
| `POST` | `/booking/group-availability` | Slots that fit the whole party — §3 |
| `POST` | `/booking/group`              | Create the group booking — §4       |

Already built, reused as-is:

| Method  | Path                               | Used by                                      |
| ------- | ---------------------------------- | -------------------------------------------- |
| `GET`   | `/salon/:id/services`              | Step 4, picking services per member          |
| `GET`   | `/salon/:id/packages`              | Step 4, Packages tab                         |
| `GET`   | `/salon/:id/products`              | Step 4, Shop tab                             |
| `GET`   | `/salon/:id/stylists?service_ids=` | Step 5, stylists who can do a member's work  |
| `GET`   | `/booking/:id`                     | The pass after paying                        |
| `PATCH` | `/booking/:id`                     | Record the payment (`booking-create.md` §11) |
| `GET`   | `/bookings`                        | My Bookings list                             |

A group booking is an ordinary booking with `booking_type: "GROUP"` and a
`members` array, so the read, list and payment endpoints need no new shapes —
only the extra array in their response (§6).

---

## 2. Find an invited member — `GET /user/lookup`

Step 3, the **Invite Registered** tab: the booker types an email or a phone
number, and the app needs the account behind it — the `id` that goes into the
member row (§4), the real name to show on the card, and the photo if there is
one.

### Request

```http
GET /user/lookup?contact=rana@example.com
GET /user/lookup?contact=%2B971501234567
```

| Param     | Type   | Required | Notes                                                      |
| --------- | ------ | -------- | ---------------------------------------------------------- |
| `contact` | string | yes      | One email, or one phone in E.164 (`+971...`). URL-encoded. |

### Response — `200 OK`

```json
{
  "found": true,
  "user": {
    "id": "usr_01j8c4rana",
    "name": "Rana Hassan",
    "image": "https://cdn.gostyle.uk/users/rana.jpg"
  }
}
```

When the contact belongs to nobody:

```json
{ "found": false, "user": null }
```

| Field     | Type   | Notes                                                                  |
| --------- | ------ | ---------------------------------------------------------------------- |
| `found`   | bool   | `true` when an account matched exactly.                                |
| `user`    | object | `null` when `found` is `false`.                                        |
| `↳ id`    | string | Send this as the member's `id` with `kind: "registered"` (§4).         |
| `↳ name`  | string | The account's display name. The app shows it instead of typed text.    |
| `↳ image` | string | Absolute URL, or `null` when the account has no photo — show initials. |

Rules:

1. **No match is not an error.** `found: false` comes back `200` so the app can
   offer "Add as Guest" in the same screen.
2. **Exact match only.** No partial or fuzzy search, no listing, and the
   response never echoes the contact back — it answers one typed value and
   nothing more. Rate-limit it like the auth endpoints.
3. **Nothing is invited here.** The call only resolves a contact; the member is
   attached when the booking is created (§4).
4. The booker's own contact resolves to their own account — the app should say
   so rather than adding them twice.

---

## 3. Availability — `POST /booking/group-availability`

The party books one start time, but each member occupies their own stylist for
their own length, so the server has to fit all of them at once. The request is a
query with a body because it carries a list per member.

### Request

```json
{
  "salon_id": "sal_01j8xk2e9",
  "date": "2026-09-21",
  "members": [
    {
      "ref": 0,
      "service_ids": ["svc_fade", "svc_facial"],
      "stylist_id": "sty_anna"
    },
    { "ref": 1, "service_ids": ["svc_fade"], "stylist_id": null }
  ]
}
```

| Field           | Type     | Required | Notes                                                            |
| --------------- | -------- | -------- | ---------------------------------------------------------------- |
| `salon_id`      | string   | yes      | Salon the party is booking.                                      |
| `date`          | string   | yes      | `YYYY-MM-DD`, salon-local.                                       |
| `members`       | array    | yes      | 2–8 entries — the same range the app enforces.                   |
| `↳ ref`         | number   | yes      | Client-side member index. Echoed back so the app can match rows. |
| `↳ service_ids` | string[] | yes      | Services that member picked. Empty array is a `422` — see §7.    |
| `↳ stylist_id`  | string   | no       | `null` or omitted means "any stylist" — the server picks.        |

### Response — `200 OK`

```json
{
  "date": "2026-09-21",
  "bands": [
    {
      "label": "Morning",
      "slots": [
        {
          "start": "2026-09-21T10:00:00+04:00",
          "end": "2026-09-21T11:30:00+04:00",
          "available": true,
          "plan": [
            {
              "ref": 0,
              "stylist": { "id": "sty_anna", "name": "Anna Petrova" },
              "start": "2026-09-21T10:00:00+04:00",
              "end": "2026-09-21T11:00:00+04:00"
            },
            {
              "ref": 1,
              "stylist": { "id": "sty_mia", "name": "Mia Chen" },
              "start": "2026-09-21T10:15:00+04:00",
              "end": "2026-09-21T11:00:00+04:00"
            }
          ]
        },
        {
          "start": "2026-09-21T10:45:00+04:00",
          "end": "2026-09-21T12:15:00+04:00",
          "available": false,
          "plan": []
        }
      ]
    }
  ]
}
```

| Field           | Type    | Notes                                                                              |
| --------------- | ------- | ---------------------------------------------------------------------------------- |
| `bands`         | array   | `Morning`, `Afternoon`, `Evening`, in that order. A band with no slots is omitted. |
| `↳ slots`       | array   | Every start the salon offers that day, in time order.                              |
| `↳ start`/`end` | string  | ISO 8601 with the salon's offset. `end` is when the **last** member finishes.      |
| `↳ available`   | boolean | `false` renders struck through; the app will not let it be picked.                 |
| `↳ plan`        | array   | Who is with whom and when, for the Review timeline. `[]` when unavailable.         |
| `↳↳ ref`        | number  | The `ref` sent in the request.                                                     |
| `↳↳ stylist`    | object  | The stylist actually reserved, even when the client sent `null`.                   |

Rules:

1. **A slot is available only if every member fits.** One member without a free
   stylist makes the whole slot `false`.
2. **Members run in parallel where the salon can.** The plan is the server's
   decision; the app only draws it.
3. **No booking is held.** Availability is a look, not a lock — the slot is
   taken at create time (§4), and a slot that went in between fails there.

---

## 4. Create — `POST /booking/group`

### Payload

```json
{
  "salon_id": "sal_01j8xk2e9",
  "date": "2026-09-21",
  "start_time": "2026-09-21T15:15:00+04:00",
  "members": [
    {
      "ref": 0,
      "kind": "self",
      "id": "usr_01j8b2sarah",
      "name": "Sarah Kassem",
      "age_group": "adult",
      "services": [{ "id": "svc_fade", "amount": 250 }],
      "products": [{ "id": "var_pomade", "amount": 25, "quantity": 1 }],
      "stylist_id": "sty_anna"
    },
    {
      "ref": 1,
      "kind": "registered",
      "id": "usr_01j8c4rana",
      "name": null,
      "age_group": "adult",
      "services": [{ "id": "svc_fade", "amount": 250 }],
      "products": [],
      "stylist_id": null
    },
    {
      "ref": 2,
      "kind": "guest",
      "name": "Liam (8 yrs)",
      "age_group": "child",
      "services": [{ "id": "svc_kids_cut", "amount": 120 }],
      "products": [{ "id": "var_kids_gel", "amount": 15, "quantity": 1 }],
      "stylist_id": "sty_ava"
    }
  ],
  "amount_without_tax": 685,
  "tax_amount": 34.25,
  "discount": 0,
  "promo_code": null,
  "total": 719.25,
  "deposit_percent": 20,
  "advance_paid_amount": 0,
  "due_amount": 719.25,
  "payment_status": "DRAFT",
  "status": "BOOKED",
  "booking_type": "GROUP"
}
```

### Fields

| Field                 | Type   | Required | Notes                                                                             |
| --------------------- | ------ | -------- | --------------------------------------------------------------------------------- |
| `salon_id`            | string | yes      | Every id below must belong to it.                                                 |
| `date`                | string | yes      | `YYYY-MM-DD`, salon-local. Must match the date part of `start_time`.              |
| `start_time`          | string | yes      | ISO 8601 with offset. Must be a `start` §3 returned as `available`.               |
| `members`             | array  | yes      | 2–8 entries. Order is the order shown in the app; keep it.                        |
| `↳ ref`               | number | yes      | Client-side index, unique in the payload. Echoed in the response.                 |
| `↳ kind`              | enum   | yes      | `self` (the booker, exactly one), `registered`, `guest`.                          |
| `↳ id`                | string | cond.    | The **user id** — `self` is the booker, `registered` comes from §2, `guest` none. |
| `↳ name`              | string | cond.    | Required for `guest`; optional for `registered` with an `id`, ignored for `self`. |
| `↳ age_group`         | enum   | yes      | `adult` or `child`. Drives the child discount — see §5.                           |
| `↳ services`          | array  | yes      | At least one. `id` + the `amount` the client displayed.                           |
| `↳ products`          | array  | no       | `id` is the **variant** id, with `amount` and `quantity` — as `POST /booking`.    |
| `↳ stylist_id`        | string | no       | `null` means the salon assigns (the app's "Auto-assign best available").          |
| `amount_without_tax`  | number | yes      | Sum over all members, before tax and discount.                                    |
| `tax_amount`          | number | yes      | Tax on the taxable base.                                                          |
| `discount`            | number | yes      | `0` when none.                                                                    |
| `promo_code`          | string | no       | The code that produced `discount`.                                                |
| `total`               | number | yes      | What the party owes in the end.                                                   |
| `deposit_percent`     | number | no       | Deposit the app offered, default `20`. The server decides the amount — §5.        |
| `advance_paid_amount` | number | yes      | Always `0` on create.                                                             |
| `due_amount`          | number | yes      | Equals `total` on create.                                                         |
| `payment_status`      | enum   | yes      | Only `DRAFT` is accepted, as in `booking-create.md` §4.                           |
| `status`              | enum   | yes      | Only `BOOKED` is accepted.                                                        |
| `booking_type`        | enum   | yes      | Always `GROUP` here.                                                              |

---

## 5. What the server decides, not the client

The same rule as `booking-create.md` §3: every figure in the payload is a
display value and is recomputed before anything is stored.

1. **Prices** come from the salon's own service and product records.
2. **The child discount is server-side.** A member with `age_group: "child"`
   pays 50% of each service price. The client shows it; the server applies it.
3. **The deposit** is `deposit_percent` of the recomputed `total`, rounded to 2
   decimals, and returned as `deposit_amount`. The client never sets the amount.
4. **Stylists** are verified against each member's services, exactly as
   `booking-expert-list.md` describes. A `null` is filled in by the server.
5. **The slot** is re-checked for the whole party. If it no longer fits, nothing
   is written and the call fails with `slot_taken` (§7).

If a recomputed total differs from the payload, the server keeps its own figure
and returns it — the client shows what comes back.

---

## 6. Response — `201 Created`

```json
{
  "id": "bkg_01j9m2k",
  "salon_id": "sal_01j8xk2e9",
  "booking_type": "GROUP",
  "status": "BOOKED",
  "payment_status": "DRAFT",
  "date": "2026-09-21",
  "start_time": "2026-09-21T15:15:00+04:00",
  "end_time": "2026-09-21T17:30:00+04:00",
  "members": [
    {
      "ref": 0,
      "id": "gmb_01j9m2k1",
      "user_id": "usr_01j8b2sarah",
      "name": "Sarah Kassem",
      "kind": "self",
      "age_group": "adult",
      "services": [
        { "id": "svc_fade", "name": "Signature Fade", "amount": 250 }
      ],
      "products": [
        {
          "id": "var_pomade",
          "name": "Matte Pomade",
          "amount": 25,
          "quantity": 1
        }
      ],
      "stylist": { "id": "sty_anna", "name": "Anna Petrova" },
      "start_time": "2026-09-21T15:15:00+04:00",
      "end_time": "2026-09-21T16:15:00+04:00",
      "total": 275
    }
  ],
  "amount_without_tax": 685,
  "tax_amount": 34.25,
  "discount": 0,
  "promo_code": null,
  "total": 719.25,
  "deposit_percent": 20,
  "deposit_amount": 143.85,
  "advance_paid_amount": 0,
  "due_amount": 719.25,
  "pass_qr_code": "GS-BKG-01J9M2K-8F3A",
  "expires_at": "2026-09-18T14:17:11+04:00",
  "created_at": "2026-09-18T14:02:11+04:00"
}
```

Notes:

1. **One pass for the party.** `pass_qr_code` is the group's, not per member.
2. **`members[].start_time`/`end_time`** are what the Review timeline draws, so
   they must be the times actually reserved.
3. **`members[].total`** is that member's share after the child discount — the
   four numbers add up to `amount_without_tax` before tax.
4. **`expires_at`** is the draft hold, same as a single booking.
5. `GET /booking/:id` returns this same object, and `/bookings` rows carry
   `booking_type: "GROUP"` plus the member count.

Paying is unchanged: `PATCH /booking/:id` with `payment_status`,
`payment_method`, `advance_paid_amount` and `due_amount`
(`booking-list.md` §7). A deposit is `PARTIALLY` with
`advance_paid_amount: 143.85`.

---

## 7. Errors

Same envelope as `auth-error-response.md`.

| Case                                            | Status | `code`                |
| ----------------------------------------------- | ------ | --------------------- |
| Fewer than 2 or more than 8 members             | 422    | `invalid_party_size`  |
| A member with no services                       | 422    | `member_no_services`  |
| More than one `kind: "self"`, or none           | 422    | `invalid_member_kind` |
| `id` missing on a `self` or `registered` member | 422    | `member_id_required`  |
| `id` matches no account                         | 422    | `unknown_user`        |
| `contact` is not a valid email or phone         | 422    | `invalid_contact`     |
| A stylist cannot do that member's services      | 422    | `stylist_mismatch`    |
| Service, product or stylist from another salon  | 422    | `foreign_id`          |
| The slot no longer fits the party               | 409    | `slot_taken`          |

The `self` member's `id` must be the token's own user; anything else is
`invalid_member_kind`. Someone without an account is sent as a `guest` with a
`name` and no `id` — never as a `registered` member with a made-up id.
