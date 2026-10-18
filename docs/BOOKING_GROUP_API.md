# Booking — Groups

`GET /api/v1/user/lookup` · `POST /api/v1/booking/group-availability` ·
`POST /api/v1/booking/group` · **Bearer token required**

A party of 2 to 8 people at one salon, arriving together. Each member has
their own services and their own stylist.

This service does not own bookings. The party is planned, held and confirmed
by **gostyle-booking-api**. This service translates the app's request into
that service's shape, and translates the answer back.

Sections follow the app's group-booking spec, so each can be compared line
for line. **Read §8 before building the payment and pass screens.** A group
booking carries less than the spec asks for, and the app must not promise
the customer anything on that list.


> **GROUP_BOOKING_V2 (off by default).** With it on, §4 to §8 below are out of
> date: the party is booked in ONE call to booking-api's mobile route, saved to
> pay at the salon (`CONFIRMED_BY_SALON`, `PAY_AFTER_CHECK_IN`, no
> `expires_at`), with **products**, the **50% child price**, **VAT** and a
> **deposit figure**; `members[].id` and one `pass_qr_code` are filled in;
> `GET /api/v1/booking/<group id>` reads the party back; `/bookings` shows it
> as one `booking_type: "GROUP"` row with `member_count`; and
> `POST /api/v1/booking/<group id>/cancel` cancels every member together
> (the booker only). Paying in the app is another team's work and is not
> built. See `docs/GROUP_BOOKING_PLAN.md` (Status) and booking-api
> `docs/api/MOBILE.md`, "Mobile group booking".

---

## 1. Endpoints

| Method | Path                          | Purpose                             |
| ------ | ----------------------------- | ----------------------------------- |
| `GET`  | `/user/lookup`                | Resolve an invited member (§2)      |
| `POST` | `/booking/group-availability` | Every start, and who fits (§3)      |
| `POST` | `/booking/group`              | Create the group booking (§4)       |

Reused unchanged: `/salon/:id/services` and `/salon/:id/stylists?service_ids=`.
Step 5 should only offer a member the stylists that call returns, because
§7's `stylist_mismatch` uses the same rule.

**Not usable for a group today:** `/salon/:id/products`, `/salon/:id/packages`,
`GET /booking/:id`, `PATCH /booking/:id`. Neither is the group part of
`/bookings`. See §8.

> **Changed 2026-10-18: `/salon/:id/packages` is**, while `PACKAGE_BOOKING_V1`
> is on: a bundle whose `can_book` is `true` is booked for a member as one
> line of that member's `services`. See §12.

---

## 2. Find an invited member — `GET /user/lookup`

```http
GET /user/lookup?contact=rana%40example.com
GET /user/lookup?contact=%2B971501234567
```

| Param     | Required | Notes                                                                   |
| --------- | -------- | ----------------------------------------------------------------------- |
| `contact` | yes      | One email, or one phone in E.164. An `@` makes it an email. URL-encoded. |

**Encode the `+` as `%2B`.** A bare `+` decodes to a space, and the number is
then read as a local one. A UAE number fails as `invalid_contact`; it is
never matched to someone else.

### `200 OK`, found or not

```json
{ "found": true, "user": { "id": "3f0c…", "name": "Rana Hassan", "image": "https://cdn.gostyle.uk/users/rana.jpg" } }
```

```json
{ "found": false, "user": null }
```

Send `user.id` as the member's `id` with `kind: "registered"` (§4). Show
`user.name` in place of what was typed. `user.image` is `null` when the
account has no photo; show initials. On `found: false`, offer "Add as Guest".

`found: false` is **identical, byte for byte**, for each of these:
- no account
- an account that never proved this contact
- an unverified account
- a disabled account

If they differed, this endpoint would tell anyone who has signed up.

### Rules

1. **Exact match only.** No partial or fuzzy search, and no listing. The
   contact is never echoed back, whatever the answer.
2. **Only verified callers.** An unverified caller gets `403 permission_denied`.
   The daily cap is per account, and a free unverified account would make it
   worthless.
3. **20 lookups per account per day.** Hits and misses cost the same.
   Call 21 is `429 rate_limited`. A malformed contact costs nothing.
4. **`422 invalid_contact`** on `contact` when it is neither an email nor a
   phone. A missing `contact` is `422 required`.
5. **Nothing is invited here.** The member is attached when the booking is
   created.
6. **The booker's own contact resolves to their own account.** The app should
   say so rather than add them twice. §4 refuses the same account twice.

**Logs.** Because this is a `GET`, the contact is in the URL. Both access
logs write this path's `query` as empty: nginx's
(`nginx/log-format-json.conf`) and gunicorn's (`config/gunicorn_logging.py`).
Django logs only the path. The two *error* logs are not covered: nginx on an
upstream failure, and gunicorn on a request that fails outside Django, print
the whole URL.

---

## 3. Availability — `POST /booking/group-availability`

### Request

```json
{
  "salon_id": "c6c248ab-f2cd-4f12-a31e-243c6e64b3b5",
  "date": "2026-10-11",
  "members": [
    { "ref": 0, "service_ids": ["0b7c…", "1a2b…"], "stylist_id": "7d1e…" },
    { "ref": 1, "service_ids": ["0b7c…"], "stylist_id": null }
  ]
}
```

| Field           | Required | Notes                                                                 |
| --------------- | -------- | --------------------------------------------------------------------- |
| `salon_id`      | yes      | The storefront uuid from `/discover` or `/salon/<id>`.                 |
| `date`          | yes      | `YYYY-MM-DD`, salon-local.                                            |
| `members`       | yes      | 2 to 8.                                                               |
| `↳ ref`         | yes      | A whole number ≥ 0, unique. Echoed in `plan`.                          |
| `↳ service_ids` | yes      | Uuids from `/salon/<id>/services`, or a package's id (§12). Empty is `member_no_services`. |
| `↳ stylist_id`  | no       | `null` or left out: the salon picks.                                   |

### `200 OK`

```json
{
  "date": "2026-10-11",
  "bands": [
    {
      "label": "Afternoon",
      "slots": [
        {
          "start": "2026-10-11T15:00:00+04:00",
          "end": "2026-10-11T16:00:00+04:00",
          "available": true,
          "plan": [
            { "ref": 0, "stylist": { "id": "7d1e…", "name": "Maya E." },
              "start": "2026-10-11T15:00:00+04:00", "end": "2026-10-11T16:00:00+04:00" },
            { "ref": 1, "stylist": { "id": "91aa…", "name": "Anya" },
              "start": "2026-10-11T15:00:00+04:00", "end": "2026-10-11T15:45:00+04:00" }
          ]
        },
        {
          "start": "2026-10-11T15:30:00+04:00",
          "end": "2026-10-11T16:30:00+04:00",
          "available": false,
          "plan": []
        }
      ]
    }
  ]
}
```

- **Every start the salon offers that day**, in time order, every half hour.
  A start is offered when the salon is open, booking-api books at that hour,
  and the longest visit in the party finishes before closing.
- **`available: true` only if every member fits.** The `plan` then says who
  is with whom, and when. `stylist` is the one actually reserved, even when
  the request sent `null`.
  - *Changed 2026-10-18, with `PACKAGE_BOOKING_V1` on:* every `stylist` in a
    `plan` can do all of that member's services, a package's every service
    included, by the rule `/salon/<id>/stylists?service_ids=` applies.
    booking-api does not check that (§5, item 4), so this service checks
    each stylist it plans. A start where booking-api gave a member someone
    who cannot is asked again with the stylists it planned there shared out
    so that each member's can; if there is no such sharing, or it does not
    fit, the start is unavailable. So the `plan` may name other stylists
    than booking-api first chose, and one more call may be made for those
    starts.
- **Starts already gone are listed as unavailable.** That covers past times
  and times inside the chosen services' notice period. booking-api is not
  asked about them.
- **`end`** is when the last member finishes. On an unavailable start it is
  the start plus the longest visit, so a struck-through row still shows how
  long the party takes.
- **Bands** go by salon-local start time: `Morning` before 12:00,
  `Afternoon` from 12:00 to before 17:00, `Evening` from 17:00. A band with
  no slots is left out. A closed salon is `bands: []`.
- **Everyone starts together.** The spec's example shows members starting at
  staggered times. The server decides the plan, and today it seats everyone
  at the slot's start. Draw each member's own `start` and `end` all the same;
  the day that changes, the app needs no update.
- **Nothing is held.** A time can go before the party is booked, and
  `/booking/group` then says `slot_taken`.

Asks booking-api in one call per 24 starts. A day longer than 12 hours takes
two calls.

---

## 4. Create — `POST /booking/group`

### Payload

```json
{
  "salon_id": "c6c248ab-f2cd-4f12-a31e-243c6e64b3b5",
  "date": "2026-10-11",
  "start_time": "2026-10-11T15:00:00+04:00",
  "members": [
    { "ref": 0, "kind": "self", "id": "11111111-…", "name": null, "age_group": "adult",
      "services": [{ "id": "0b7c…", "amount": 250 }], "products": [], "stylist_id": "7d1e…" },
    { "ref": 1, "kind": "registered", "id": "3f0c…", "name": null, "age_group": "adult",
      "services": [{ "id": "0b7c…", "amount": 250 }], "products": [], "stylist_id": null },
    { "ref": 2, "kind": "guest", "name": "Liam (8 yrs)", "age_group": "child",
      "services": [{ "id": "5e6f…", "amount": 120 }], "products": [], "stylist_id": null }
  ],
  "amount_without_tax": 620, "tax_amount": 31, "discount": 0, "promo_code": null,
  "total": 651, "deposit_percent": 20, "advance_paid_amount": 0, "due_amount": 651,
  "payment_status": "DRAFT", "status": "BOOKED", "booking_type": "GROUP"
}
```

| Field          | Required | Notes                                                                                   |
| -------------- | -------- | --------------------------------------------------------------------------------------- |
| `salon_id`     | yes      | As §3.                                                                                  |
| `date`         | yes      | Must be `start_time`'s date in the salon's own time, or `date_mismatch`.                  |
| `start_time`   | yes      | ISO 8601 **with** an offset. One of §3's `available` starts.                              |
| `members`      | yes      | 2 to 8. Answered in the order sent.                                                     |
| `↳ ref`        | yes      | Unique. Echoed.                                                                         |
| `↳ kind`       | yes      | `self` (exactly one: the token's own account), `registered`, `guest`.                    |
| `↳ id`         | cond.    | Required for `self` (the token's user id) and `registered` (from §2). Dropped for a guest. |
| `↳ name`       | cond.    | Required for `guest`. For `self` and `registered`, the account's name is used, and this only fills in for an account without one. |
| `↳ age_group`  | yes      | `adult` or `child`. Echoed back; **not priced** (§8).                                     |
| `↳ services`   | yes      | At least one. `amount` is accepted and ignored. A package is one line (§12).              |
| `↳ products`   | no       | **Must be empty**, or `products_not_supported` (§8).                                      |
| `↳ stylist_id` | no       | `null`: the salon assigns one.                                                          |

**Accepted and ignored:** every money figure, `promo_code`, `deposit_percent`,
`payment_status`, `status` and `booking_type`. The server decides all of them
(§5), and none is forwarded.

What each kind becomes:

- **`self`**: the booker's own booking, in their own `/bookings`.
- **`registered`**: a booking **on that person's account**. It appears in
  *their* `/bookings`, as an ordinary single booking. Nobody asks them first.
- **`guest`**: a booking filed under the group. Nobody can read it from the
  app, the booker included.

---

## 5. What the server decides

1. **Prices** come from the salon's records, at this branch: a service's
   branch price when the salon set one, as on the Services tab and as
   booking-api prices it. booking-api prices the party itself.
2. **No child discount.** booking-api has no field for age, so `child` costs
   the same as `adult`. The spec's 50% is not applied (§8).
3. **No deposit.** `deposit_amount` is `0`. Every group booking is confirmed
   with nothing due in the app.
4. **Stylists are checked before anything is held.** A chosen stylist must
   be on the salon's roster, chosen for only one member, and able to do
   all of that member's services, every service in a package included
   (§12). That last test is the one `/salon/<id>/stylists?service_ids=`
   applies. A `null` is filled in by booking-api.
   - *Changed 2026-10-18: booking-api does not check skills.* It gives each
     member one professional for all of their services, but the platform's
     services reach it with no skill, and its `SKILLS_UNVERIFIED` switch
     (which they cannot be booked without) counts every professional as
     qualified. So for a `null` it picks whoever is free, the groom's nails
     to someone who only cuts, and this check is the only one a chosen
     stylist gets. With `PACKAGE_BOOKING_V1` on (it brings packages, often
     more than one person's work), its picks for a `null` are checked here
     too, by the same test; off, as before, they are not. On availability
     (§3). On the old path, the professionals held, before the party is
     confirmed: a hold with someone who cannot is given back and the party
     held again once, with the professionals it held shared out so each
     member's can, or it is `slot_taken` and nothing is booked; the confirm
     then names the professional held for every member, so its re-plan
     cannot move anyone. With `GROUP_BOOKING_V2` the party is held and
     booked in one call, so a `null`'s pick can only be checked once it is
     booked: a wrong one is logged as an error, for the salon to put right,
     and the party stands. To book exactly the stylists a start's `plan`
     shows, checked, send each member's `plan[].stylist.id` as
     `stylist_id`. The lasting fix is booking-api's: skills from the
     platform (`GROUP_BOOKING_FOLLOW_UPS.md`, M9).
5. **The slot is re-checked for the whole party.** If it no longer fits,
   nothing is written and the call fails with `slot_taken`.
6. **`start_time` is held to §3's rule.** booking-api plans against its diary
   and nothing else, and would book after closing time or yesterday.

The server keeps its own figures and returns them. Show what comes back.

---

## 6. Response — `201 Created`

```json
{
  "id": "b2c6b0a9-17aa-4be8-91b2-11668d3b2e5a",
  "salon_id": "c6c248ab-f2cd-4f12-a31e-243c6e64b3b5",
  "booking_type": "GROUP",
  "status": "CONFIRMED_BY_SALON",
  "payment_status": "PAY_AFTER_CHECK_IN",
  "date": "2026-10-11",
  "start_time": "2026-10-11T15:00:00+04:00",
  "end_time": "2026-10-11T16:00:00+04:00",
  "members": [
    {
      "ref": 0,
      "id": null,
      "booking_code": "GS-1280",
      "user_id": "11111111-…",
      "name": "Sarah Kassem",
      "kind": "self",
      "age_group": "adult",
      "services": [{ "id": "0b7c…", "name": "Signature Fade", "amount": 250.0 }],
      "products": [],
      "stylist": { "id": "7d1e…", "name": "Anna Petrova" },
      "start_time": "2026-10-11T15:00:00+04:00",
      "end_time": "2026-10-11T16:00:00+04:00",
      "total": 250.0
    }
  ],
  "currency": "AED",
  "amount_without_tax": 620.0,
  "tax_amount": null,
  "discount": 0.0,
  "promo_code": null,
  "total": 620.0,
  "deposit_percent": 0,
  "deposit_amount": 0.0,
  "advance_paid_amount": 0.0,
  "due_amount": 620.0,
  "pass_qr_code": null,
  "expires_at": null,
  "created_at": "2026-10-09T14:02:11+04:00"
}
```

- **`id`** is the group's id. `GET /booking/:id` and `PATCH /booking/:id` do
  not accept it (§8). Keep this response if the app needs to show the party
  later.
- **`members[].booking_code`** is each member's own booking at the salon's
  desk, and serves as that member's pass. **`pass_qr_code` is `null`**: there
  is no one pass for the party.
- **`members[].id` is `null`.** booking-api gives a member no id of its own.
- **`members[].start_time` / `end_time`** are the times actually reserved.
- **`members[].total`** is that member's services at the salon's prices. The
  totals add up to `amount_without_tax`. booking-api books the party as one
  figure and names no price per person, so this breakdown comes from the
  salon's price list and is checked against booking-api's total. If the two
  disagree, every member's `total` and every service `amount` is `null`, and
  `total` is booking-api's figure.
- **`tax_amount` is `null`, not `0`.** booking-api computes no VAT for a
  group, and `0` would claim the salon charges none. `total` is service
  prices only.
- **`status`** is `CONFIRMED_BY_SALON`, the spelling `/bookings` uses for the
  booker's own row. The party is confirmed at once. There is no `DRAFT` and
  no hold, so `expires_at` is `null`.
- **`total`, `amount_without_tax` and `due_amount` are `null`**, never `0`,
  if booking-api answers with an amount this service cannot read. The party
  is still booked, and the problem is logged here.

---

## 7. Errors

This project's envelope (`{detail, code, errors: [{field, code, message}]}`).
**The spec's codes are `errors[0].code`.** The top-level `code` is the broad
one: `validation_error` for a 422, `error` for a 409.

| Case                                                         | Status | `errors[0].code`         | `field`       |
| ------------------------------------------------------------ | ------ | ------------------------ | ------------- |
| Fewer than 2 or more than 8 members                          | 422    | `invalid_party_size`     | `members`     |
| Two members with one `ref`                                   | 422    | `duplicate_ref`          | `members`     |
| A member with no services                                    | 422    | `member_no_services`     | `service_ids` / `services` |
| Not exactly one `self`; `self` not the token's user; one account twice | 422 | `invalid_member_kind` | `members` |
| `id` missing on a `self` or `registered` member              | 422    | `member_id_required`     | `id`          |
| A guest with no name                                         | 422    | `required`               | `name`        |
| `id` matches no active, verified account                     | 422    | `unknown_user`           | `id`          |
| `contact` is not a valid email or phone (§2)                 | 422    | `invalid_contact`        | `contact`     |
| A stylist cannot do all of that member's services            | 422    | `stylist_mismatch`       | `stylist_id`  |
| One stylist chosen for two members                           | 422    | `stylist_repeated`       | `stylist_id`  |
| A service, package or stylist not at this salon              | 422    | `foreign_id`             | `service_ids` / `services` / `stylist_id` |
| A member with products, or with `members[].packages`         | 422    | `products_not_supported` / `packages_not_supported` | `products` / `packages` |
| A package of this salon that cannot be booked here now (§12) | 422    | `package_not_bookable`   | `service_ids` / `services` |
| `start_time` without an offset                               | 422    | `offset_required`        | `start_time`  |
| `start_time` not on `date`                                   | 422    | `date_mismatch`          | `start_time`  |
| Salon closed that day / too soon or past / outside hours     | 422    | `salon_closed` / `too_soon` / `outside_hours` | `start_time` |
| The slot no longer fits the party                            | 409    | `slot_taken`             | `null`        |
| The same request is already running                         | 409    | `request_in_progress`    | `null`        |
| Same `Idempotency-Key`, different party                      | 422    | `idempotency_key_reused` | `null`        |
| booking-api unreachable, **nothing booked**                  | 503    | `booking_api_unavailable`| `null`        |
| booking-api stopped answering **after the party was held**   | 503    | `group_status_unknown`   | `null`        |

`slot_taken`'s `message` is booking-api's own reason where it gives one
("Only 1 professional can cover this party, and each of the 3 participants
needs their own."), and is worth showing. When booking-api held a member with
someone who cannot do their services and nobody it held can (§5, item 4), it
is this service's: "There is no stylist free at that time who can do all of
each member's services. Pick another time." (added 2026-10-18).

`duplicate_ref`, `stylist_repeated`, `products_not_supported`,
`packages_not_supported` and `package_not_bookable` are not in the spec.
`stylist_repeated` exists because everyone starts together and each member
needs their own professional. Without it, a party like that would get a day
of struck-through slots and no reason.

Anything else booking-api refuses is forwarded **in its own shape**, with
its status: `statusCode`, `message`, `error`, `code`. Only an unexpected
refusal is forwarded this way; everything the app can cause is in the
table above, with one exception (added 2026-10-18): a member's package
beside a hand-picked service priced in another currency is booking-api's
`422 BOOKING_CURRENCY_MIXED`, in that shape, on availability and on this
create's hold and confirm (§12, "One currency per member"). Read the
top-level `code` for it. *Changed 2026-10-18 (booking-api `3ae8e53`):* with
`GROUP_BOOKING_V2` on, create is booking-api's mobile route, and that route
answers it in the app's envelope instead, `422 currency_mismatch` on
`members[i].services`; availability still answers it in booking-api's shape.

**The two 503s mean different things:**

- **`booking_api_unavailable`**: nothing was booked. A retry is safe.
- **`group_status_unknown`**: the confirm was sent and no answer came back.
  This service then tried to release the hold and could not, which means the
  confirm probably went through. **The party may be booked.** Don't retry.
  Send the customer to their bookings. The body carries `group_id` for
  support:

  ```json
  {
    "detail": "We could not confirm whether this group booking went through. Check your bookings before trying again.",
    "code": "service_unavailable",
    "errors": [ { "field": null, "code": "group_status_unknown", "message": "…" } ],
    "group_id": "b2c6b0a9-17aa-4be8-91b2-11668d3b2e5a"
  }
  ```

---

## 8. What the app will NOT get

booking-api's group bookings are simpler than single bookings. Each item
below is in the spec and does not exist for a group today. Don't show it,
and don't promise it. Each one needs work in booking-api first.

1. **No products.** A member with products is refused with
   `products_not_supported`, not booked without them.
2. ~~**No packages.** Book the package's services instead.~~ **Changed
   2026-10-18:** with `PACKAGE_BOOKING_V1` on, a member may book a package,
   as one line of that member's `services` (§12). `members[].packages` is
   still refused.
3. **No child discount.** `age_group` is echoed and never stored, and it
   changes no price.
4. **No VAT, discount or promo code.** `tax_amount` is `null`, `discount` is
   `0`, and a `promo_code` sent is ignored.
5. **No deposit, no `DRAFT`, no in-app payment.** The party is confirmed with
   nothing due and pays at the salon. `PATCH /booking/:id` accepts only a
   booking still in `DRAFT`, so it would refuse a group booking even with
   the right id.
6. **No one booking, no one pass.** The party is one booking per member, each
   with its own `booking_code`. `id` is the group's, and nothing reads a
   group back by it.
7. **No read-back.** `GET /booking/:id` doesn't know a group. `/bookings`
   shows the booker's own booking and a registered member's own booking, as
   ordinary single bookings with no `members` and no `booking_type:
   "GROUP"`. **Guests' bookings cannot be read from the app at all**: they
   are filed under the group, and nobody signs in as a group.
8. **One line per person at the desk.** booking-api records each member's
   booking as a single line, named after the member, holding only the
   **first** service's id. The other services still count toward the time
   and the price, but the salon's screens do not list them by name.
9. **No per-person price from booking-api.** The breakdown in §6 is rebuilt
   here from the salon's price list, and is `null` when it cannot be checked.

---

## 9. Retries

**Sending the same request again is safe, with or without a header.** The
hold and the confirm share one key, kept in Redis. That key is the
`Idempotency-Key` you send or, if you send none, one built from the customer
and the body, exactly as for `POST /booking`.

| When the same request arrives…                                  | You get                                           |
| --------------------------------------------------------------- | ------------------------------------------------- |
| while the first is still running                                | `409 request_in_progress`                         |
| after the first **booked the party** (within 24 hours)          | the same `201`, replayed. No second party.        |
| after the first was **refused**                                 | a fresh attempt: refusals are not remembered      |
| after the first ended **`group_status_unknown`**                | the same `503 group_status_unknown`, replayed     |

A key you send is tied to your customer and to the body. The same key with a
different party is `422 idempotency_key_reused`.

If Redis itself is down, the booking goes ahead without this protection, and
the failure is logged. That's the rule booking-api's own idempotency store
follows.

---

## 10. Configuration

No new settings. Both booking endpoints use `BOOKING_API_URL` and
`BOOKING_API_TIMEOUT` (see `BOOKING_CREATE_API.md`), and the Redis cache
already configured as `CACHES["default"]`.

booking-api's clock is **read, not configured**. Its UTC offset and the hours
it books come from its `GET /v1/bookings/settings` on every request. Today
that's +06:00 and 10:00–22:00 there, which is 08:00–20:00 in Dubai.

For the lookup's query to leave the access logs, `nginx/log-format-json.conf`
must be re-installed (`scripts/setup-nginx.sh`), and gunicorn restarted with
the `--logger-class` that `docker-compose.yml` now passes.

---

## 11. Known gaps

- **Availability needs booking-api's `targetMins`**, which asks about many
  starts in one call. It is uncommitted work on booking-api's
  `feat/booking-products` branch. Against a booking-api without it,
  every availability call is refused with booking-api's `400`.
- **booking-api ignores stylists' shifts when planning a party.** It checks
  who is already busy, not who is working. This service limits every start
  to the salon's opening hours, but inside those hours booking-api can seat a
  member with a stylist whose shift has ended.
- **A big party can make booking-api slow.** When a party needs more chairs
  of one type than the branch has, its planner tries every way of seating
  them before giving up. That has been measured at up to a few hundred
  milliseconds per time, for a party of 8 at a branch with many stylists.
- **A timeout while holding leaves a hold behind.** If booking-api stops
  answering while placing the hold, this service answers
  `booking_api_unavailable`. A hold may have been placed. Nothing is booked
  on it, and it lapses on its own after 15 minutes, but until then a retry
  of the same party may be refused with `slot_taken`.
- **Recorded as a desk booking.** booking-api marks every group booking with
  the `desk` channel, not `online`, so app group bookings are counted as
  desk bookings in its reports.

---

## 12. A package for a member

> **Changed 2026-10-18.** Until now a member could not book a package (§8,
> item 2). With this service's `PACKAGE_BOOKING_V1` and booking-api's
> `PACKAGES_FROM_PLATFORM` both on, a member may: a bundle from the
> Packages tab, booked as one visit at the package's price. Off, nothing
> here applies, and a package id is `foreign_id`, as it was.

### The line

- **Availability (§3):** the package's id among the member's `service_ids`.
- **Create (§4):** one line of the member's `services`, at the package's
  price: `{ "id": "<package id>", "amount": 520 }`. Alone or beside
  hand-picked services; any number of members, the same package or
  different ones. Do not list its services as well.
- **One shape.** `members[].packages` is still refused,
  `packages_not_supported`. While the switch is on, its message says to
  send the package as a line of `services` instead.
- Offer a bundle only when its `can_book` is `true` (the Packages tab,
  Package Details). No `can_book` key means the switch is off.

### What this service checks, and what it sends

A package is checked as the services it holds: its published snapshot's
items, each as many times as the package holds it, read exactly as the
slot search reads them (`BOOKING_NEAREST_AVAILABLE_API.md`).

- **`package_not_bookable`** (`422`, on `service_ids` or `services`): a
  package of this salon's business that cannot be booked here now, by the
  `can_book` rule (published, online booking on, sold at this branch, every
  service in it on the menu, offered at this branch, priced above 0 here
  and priced in the package's own currency, none pinned to a variant, a
  price above 0). Another business's package, or one never on sale, is
  `foreign_id`, as is any id that is not this salon's.
  - *Changed 2026-10-18: a service in it priced at 0 here.* booking-api
    refuses the whole basket for one service with no price above 0 (`409
    BOOKING_STATE_INVALID`, "That service has no usable price in the
    catalogue."), which reached the app raw from availability and as
    `slot_taken` from the old path's hold, so Pick a time could only loop.
    `can_book` is false for such a package now, and this is
    `package_not_bookable` before booking-api is asked.
  - *Changed 2026-10-18: a service in it priced in another currency.* The
    platform has services in different currencies at one branch, and
    booking-api refuses a package priced in one currency with a service
    priced in another here (`422`, below: `currency_mismatch` from its
    mobile route, `BOOKING_CURRENCY_MIXED` everywhere else). The
    service's currency here is its branch row's when the row names one,
    else its own, as the platform's ListServices sends it.
- **The stylist** (§5, item 4): a chosen stylist must be able to do every
  service in the package, or `stylist_mismatch`.
  `/salon/<id>/stylists?service_ids=` takes services only: ask it with the
  package's services (`includes[].service_id` on Package Details). For a
  member with no chosen stylist, booking-api's own pick is checked by the
  same rule (changed 2026-10-18: booking-api checks no skills, and a
  package's services are often more than one person's work): on
  availability, a start whose plan gives the package to someone who cannot
  do all of it is asked again with the stylists shared out, or unavailable;
  on the old path the hold is checked the same way before it is confirmed;
  with `GROUP_BOOKING_V2`, only once booked, and logged.
- **The party's length:** each member's services end to end, a package's
  included, each as many times as the package holds it ("2 × Cut" is two
  cuts). The longest member decides which starts are offered (§3) and
  whether a start finishes in time (`outside_hours`).
- **The notice:** the longest any service in the party asks for, a
  package's services included (`too_soon`).
- **Sent on as it came.** booking-api expands a package itself, on
  availability, hold, confirm and its mobile route, so it is sent each
  member's lines exactly as the app sent them: the package id, not its
  services, and its `amount`. The one thing done to any id is its uuid
  spelling, lower case.

booking-api checks again, and its refusal is forwarded as it came. If it does
not sell the package at this branch: on availability and the old path's
hold and confirm, in its own shape (§7); on its mobile route
(`GROUP_BOOKING_V2`), in the app's envelope, `422 unknown_service` on
`members[i].services` ("Not sold at this salon: …"). The mobile route
answers a member's service with no usable price here (priced at 0 at this
branch) the same way, with the message "That service has no usable price in
the catalogue." (changed 2026-10-18, booking-api `3ae8e53`; a package with
such a service is `package_not_bookable` here first).

**One currency per member** (added 2026-10-18). booking-api adds a member's
lines up as one figure and has no rate to convert with. `can_book` covers a
package and its own services; what it cannot see is the member's basket. A
package beside a hand-picked service priced in another currency (an AED
package and a BDT Keratin), or two hand-picked services in two, is refused
by booking-api everywhere it is asked, in two envelopes:

- **Create with `GROUP_BOOKING_V2` (its mobile route): the app's envelope**
  (changed 2026-10-18, booking-api `3ae8e53`; it was booking-api's own, as
  below): `422 currency_mismatch` on `members[i].services`, the member
  named by its index in the request. Branch on `errors[0].code` as for any
  of that route's refusals.

  ```json
  {
    "detail": "Please correct the highlighted fields.",
    "code": "validation_error",
    "errors": [
      {
        "field": "members[0].services",
        "code": "currency_mismatch",
        "message": "This basket mixes AED and BDT. Prices in different currencies cannot be added, and there is no rate to convert them with."
      }
    ]
  }
  ```

  The same code comes on two other fields, and the field says which: on
  `members[i].products[j].id`, a product priced in another currency than
  the party's services; on `members`, members each in one currency but not
  the same one ("one party pays in one currency").
- **Availability, and the old path's hold and confirm: booking-api's own
  envelope**, `422 BOOKING_CURRENCY_MIXED`, forwarded as it came: no
  `errors[]`; read the top-level `code`.

  ```json
  {
    "statusCode": 422,
    "code": "BOOKING_CURRENCY_MIXED",
    "message": "This basket mixes AED and BDT. Prices in different currencies cannot be added, and there is no rate to convert them with.",
    "details": { "currencies": ["AED", "BDT"] },
    "error": "Unprocessable Entity"
  }
  ```

### The money (`GROUP_BOOKING_V2`, booking-api's mobile route)

booking-api checks the app's figures to within 1 fil, as for any party:

1. A package is ONE line at **the package's price**, not its services'
   prices added up.
2. A **child** pays half of each line: **half of the package's price,
   rounded once** (half up), not half of each service in it.
3. **No discount** for a party: `discount` is `0`. There is no saving line
   either; the package's price is the price.
4. **VAT**, 5%, once on the party's whole net, then split across the members.
5. **The deposit** is `deposit_percent` of the party's `total`.

For example, an adult and a child each with Groom and Go (520.00), and an
adult with a 199.00 cut: 520.00 + 260.00 + 199.00 = `amount_without_tax`
979.00, `tax_amount` 48.95, `total` 1027.95, `deposit_amount` 205.59 at
20% (`MOBILE_GROUP_BOOKING_FE.md`, Packages, works it through).

On the old path (`GROUP_BOOKING_V2` off) a package is held and confirmed
the same way, at its price, with no child price and no VAT, as every line
on that path (§8). Its line comes back in §6's answer as it was sent, with
the package's name and price.

### What comes back (`GROUP_BOOKING_V2`)

booking-api's fields, forwarded untouched, on the `201` and on `GET
/booking/<group id>`:

- **`members[].packages`**: `[{ id, name, amount, service_ids }]` on a
  member whose lane holds a package, **and only there**. A member of
  hand-picked services has no `packages` key at all, exactly as before.
  `amount` is the package as charged to that member, before VAT (a child's
  is the half); `service_ids` its services, a quantity repeated.
- That member's **`services`** lists the package's services, each at its
  share of the package's charge, adding up to its `amount`.
- **My Bookings:** the party's GROUP row has `packages: [{ id, name }]`
  only when any member holds one.

### Not built

- `/salon/<id>/stylists?service_ids=` does not take a package id.
- A package's own lead time and `lead_days` are not applied, here or by
  booking-api, as for a single booking.
- A `PARALLEL` package is booked one service after another.
- Prepaid packages (sessions, My Packages): nothing is bought ahead.
