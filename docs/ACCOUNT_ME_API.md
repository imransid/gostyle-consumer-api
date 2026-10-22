# The Account: GET and PATCH /auth/me

Base URL: `https://api.gostyle.uk/api/v1`
Auth: Bearer token on both. No token, or an expired one, is `401`
`not_authenticated`.

Our answer to your brief of 2026-10-21, written 2026-10-22. Every JSON block
on this page is a real request or a real answer: a test (`AccountMeDocTests`)
sends the same requests to the code and must get these exact bodies, so this
page cannot drift from the code. The id, name and contacts are test data.

There is no switch to wait for: the three fields are on both calls as soon as
this release is deployed.

---

## What differs from your brief

1. **PATCH answers with the account itself, not `{success, user}`** (your §4).
   It is the same body GET answers, so you can put it straight into the
   provider. Re-reading GET after a save still works if you prefer it.
2. **The WhatsApp switch needs a VERIFIED phone** (your §8, point 2). With no
   phone it is `422 no_phone_on_account`. With a phone that is not verified it
   is `422 phone_not_verified`, a code that is not in your table (§3.3).
   Turning it off never fails.
3. **The switch is saved but does nothing yet.** Nothing sends booking
   messages on WhatsApp today: booking reminders go by push and email only.
4. **`gender` reads `null` for every account that exists today.** Register
   checked the gender it was sent and then never saved it. This release fixes
   that, going forward only: an account made before it reads `null` until the
   user picks one on Personal Details. It is not a bug in the read.
5. **Register now accepts `"other"`** as well as `"male"` and `"female"`. It
   still requires `gender`, and it has no "prefer not to say": `null` there
   is `422`.
6. **Do not send `whatsapp_booking_updates` to `/notifications/preferences`.**
   That endpoint refuses it as an unknown field with `400`, not `422` (§4).
   The switch is read and written on `/auth/me` only, because that is where
   the phone check is.
7. **Server-owned fields are ignored, not refused** (your §4, "What it must
   keep refusing"). `email`, `phone`, `email_verified_at`, `phone_verified_at`,
   `points`, `tier_level`, `push_token` and `id` in a PATCH get `200` and change
   nothing. It has always worked this way, and we keep it, so an app that
   sends the whole user back never fails on them.
8. **`full_name` cannot be emptied.** `""`, or only spaces, is now
   `422 blank`. Before this release it was saved.
9. **There is no code for "too far in the past".** A date of birth more than
   120 years ago is `invalid_date`, with its own message (§4).
10. **The switch has your name:** `whatsapp_booking_updates`.

---

## 1. GET /auth/me

```json
{
  "id": "1b9d6bcd-bbfd-4b2d-9b5d-ab8dfbbd4bed",
  "full_name": "Jack Roger",
  "email": "jack@example.com",
  "phone": "+8801712345678",
  "phone_verified_at": "2026-09-15T11:27:02Z",
  "email_verified_at": "2026-09-15T11:26:41Z",
  "created_at": "2026-09-15T11:26:19Z",
  "image": "https://cdn.example.com/customers/uploads/2026/10/jack.jpg",
  "location": "Dhaka",
  "language": "en",
  "currency": "AED",
  "is_hijab_mode": false,
  "date_of_birth": "1991-03-14",
  "gender": "male",
  "whatsapp_booking_updates": true,
  "points": 0,
  "tier_level": "BRONZE",
  "push_token": null
}
```

- **`date_of_birth`, `gender` and `whatsapp_booking_updates` are always
  there.** Unset reads as `null`, `null` and `false`.
- **`points` and `tier_level` are placeholders.** `points` is always `0` and
  `tier_level` is always `"BRONZE"`. Loyalty tiers belong to each salon, and
  each salon types its own tier names, so there is no one tier for the whole
  account. **Do not draw the points chip or the Gold Member chip yet.** Do not
  type `tier_level` as an enum.

---

## 2. PATCH /auth/me

Partial, as today: send only what changed, and anything left out stays as it
was. This request:

```json
{
  "full_name": "Jack Roger",
  "date_of_birth": "1991-03-14",
  "gender": "male",
  "whatsapp_booking_updates": true
}
```

answers `200` with the account, the same body as GET. The block in §1 is
exactly what this request answers, sent to an account that had none of the
three set.

| Field | Status | Notes |
|---|---|---|
| `full_name` | today | Trimmed. `""` or only spaces is `422 blank` (new) |
| `image` | today | The `file_url` an upload answered |
| `location` | today | Free text |
| `language` | today | From the Language screen |
| `currency` | today | From the Currency screen |
| `is_hijab_mode` | today | From Settings |
| `date_of_birth` | new | `YYYY-MM-DD`, or `null` to clear |
| `gender` | new | `"male"`, `"female"`, `"other"`, or `null` for prefer not to say |
| `whatsapp_booking_updates` | new | `true` or `false` only. `true` needs a verified phone |

All three new fields go in the same PATCH as the name and the photo, so one
Save Changes saves everything. If a save fails, nothing in it is saved: a
refused switch does not leave the date of birth half-saved.

---

## 3. The three fields

### 3.1 date_of_birth

- A date, not a timestamp: `"1991-03-14"`. No time, no zone. Your screen
  formats it as "14 March 1991" itself.
- Only `YYYY-MM-DD` is taken. `"19910314"`, `"1991-3-14"`, `"1991-W11-4"`, a
  timestamp, a date that does not exist (`"1991-02-30"`) and `""` are all
  `422 invalid_date`.
- Not in the future (`422 date_in_future`). "Today" is the server's date in
  UTC.
- Not more than 120 years ago (`422 invalid_date`, "Enter a real date of
  birth.").
- `null` clears it. It can be changed as often as the user likes: it does not
  lock after the first save (§5, point 3).

### 3.2 gender

- `"male"`, `"female"` or `"other"`.
- **Send `null` for "Prefer not to say".** `null` is a real choice and is
  saved. An empty string is `422 invalid_choice`.
- Reads `null` when the user chose "Prefer not to say", and on every account
  made before this release (point 4 above).

### 3.3 whatsapp_booking_updates

- `true` or `false` only. `"true"`, `1` and `null` are `422`.
- **Turning it on needs a verified phone.** No phone on the account:
  `422 no_phone_on_account`. A phone that is not verified:
  `422 phone_not_verified`.
- **What the app does:** grey the toggle out when the account's `phone` is
  `null` or its `phone_verified_at` is `null`, and point the user at the
  verify flow: `POST /auth/otp/request`, then `POST /auth/otp/verify`, with
  `destination_type: "phone"` and `purpose: "register"` (`AUTH_GUIDE.md`).
  Phone codes go out on WhatsApp, so a verified phone has already had a
  WhatsApp message from us.
- **Turning it off never fails**, with or without a phone.
- **It is saved but does nothing yet.** Nothing sends booking messages on
  WhatsApp today. The value is kept, so it works the day a sender exists.

---

## 4. Errors

The usual envelope, as in `auth-error-response.md`. `errors[].field` names the
one field that failed, so you can highlight it.

| Status | `field` | `code` | When | `message` |
|---|---|---|---|---|
| 401 | `null` | `not_authenticated` (top level) | No token, or it expired | |
| 422 | `date_of_birth` | `invalid_date` | Not `YYYY-MM-DD`, or not a real date | Enter the date as YYYY-MM-DD. |
| 422 | `date_of_birth` | `invalid_date` | More than 120 years ago | Enter a real date of birth. |
| 422 | `date_of_birth` | `date_in_future` | A birthday that has not happened | Your date of birth cannot be in the future. |
| 422 | `gender` | `invalid_choice` | Not one of the three, or `""` | "x" is not a valid choice. |
| 422 | `whatsapp_booking_updates` | `no_phone_on_account` | Switched on, no phone | Add a phone number first. |
| 422 | `whatsapp_booking_updates` | `phone_not_verified` | Switched on, phone not verified | Verify your phone number first. |
| 422 | `whatsapp_booking_updates` | `invalid` | Not `true` or `false` | Must be a boolean. |
| 422 | `whatsapp_booking_updates` | `null` | `null` | This field may not be null. |
| 422 | `full_name` | `blank` | `""` or only spaces | This field may not be blank. |

`too_young` is reserved and unused (§5, point 1).

Switched on with a phone that is not verified:

```json
{
  "detail": "Please correct the highlighted fields.",
  "code": "validation_error",
  "errors": [
    {
      "field": "whatsapp_booking_updates",
      "code": "phone_not_verified",
      "message": "Verify your phone number first."
    }
  ]
}
```

Switched on with no phone:

```json
{
  "detail": "Please correct the highlighted fields.",
  "code": "validation_error",
  "errors": [
    {
      "field": "whatsapp_booking_updates",
      "code": "no_phone_on_account",
      "message": "Add a phone number first."
    }
  ]
}
```

The same switch sent to `PATCH /notifications/preferences` instead. Note the
`400`, and no `code` on the item:

```json
{
  "detail": "Unknown notification preference.",
  "code": "validation_error",
  "errors": [
    {
      "field": "whatsapp_booking_updates",
      "message": "This notification preference is not supported."
    }
  ]
}
```

---

## 5. Your open points (§8)

1. **Minimum age: there is none.** Do not block the picker. Nothing in Go
   Style checks an age today: not sign-up, not booking. A salon can set a
   minimum age on a service, but nothing enforces it yet. `too_young` is
   reserved and unused. If an age gate comes, we will send you the number and
   the message first.
2. **The WhatsApp switch needs a verified phone.** Yes. The rule and what the
   app does are in §3.3.
3. **`date_of_birth` does not lock.** It stays a picker. Nothing reads it yet,
   so a lock would protect nothing. If an age gate or a birthday reward ever
   reads it, we will tell you before it locks.
4. **`tier_level` has no list of values.** It is a placeholder that always
   says `"BRONZE"` (§1). Each salon makes its own tiers with names it types
   itself, and points are kept per salon. Keep the points chip and the Gold
   Member chip off the profile header until there is a real source.
5. **The birthday reward needs nothing from the app.** Today it is only a
   label a salon can put on a loyalty tier; nothing gives it out. If it ever
   needs a screen, it will come with its own endpoint and its own brief.

---

## 6. For whoever builds the WhatsApp sender

The switch is stored on the customer's notification preferences, in this
service. booking-api does not read that table: it asks this service over gRPC
(`ConsumerDirectory.GetConsumerContact`), and today the answer has
`appointment_reminder`, `push_enabled` and `email_verified`, not this switch.
So before any sender can honour it, add it to `proto/consumer_directory.proto`
(this repo and booking-api's copy) and to `apps/grpc_gen/servicer.py`.
