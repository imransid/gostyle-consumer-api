# The Account: GET and PATCH /auth/me (FE Integration Guide)

Date: 2026-10-22, rewritten 2026-10-27 as the full integration guide. For:
the mobile app team. Our answer to your brief of 2026-10-21.

Base URL: `https://api.gostyle.uk/api/v1`
Auth: `Authorization: Bearer <access>` on both calls.

Every JSON block on this page is a real request or a real answer: a test
(`AccountMeDocTests`) sends the same requests to the code and must get these
exact bodies, so this page cannot drift from the code. The id, name and
contacts are test data.

---

## 0. Read this first

1. **No new route.** `GET /auth/me` reads the account, `PATCH /auth/me`
   writes it, as today. Three new fields on both: `date_of_birth`, `gender`
   and `whatsapp_booking_updates`.
2. **GET always sends all three.** Unset reads as `null`, `null` and `false`.
3. **PATCH answers the account itself**, the same body as GET. Put it
   straight into ProviderContext. It is not `{success, user}` (§9).
4. **All three go in the same PATCH** as the name and the photo, from the one
   Save Changes button. If a save fails, nothing in it is saved.
5. **The WhatsApp switch needs a verified phone.** Grey it out when `phone`
   or `phone_verified_at` is `null` (§4.3).
6. **Never build `date_of_birth` with `toISOString()`.** Dhaka is UTC+6, so
   it saves the day before (§4.1).
7. **`null` is a real value.** It clears the date of birth and means "Prefer
   not to say" for gender. `JSON.stringify` keeps `null` and drops
   `undefined`, so never use `undefined` to clear.
8. **Points and tier are placeholders.** Do not draw the points chip or the
   Gold Member chip yet (§3).
9. **No server switch.** The fields are on as soon as this release is
   deployed. Until then GET has no `date_of_birth` key (§8).

---

## 1. Endpoints at a glance

| Method | Path | What it does |
| ------ | ---- | ------------ |
| `GET` | `/auth/me` | The account. Every screen reads it through ProviderContext |
| `PATCH` | `/auth/me` | Change part of the account. Answers the whole account |

---

## 2. TypeScript types (copy these)

```ts
export type Gender = 'male' | 'female' | 'other';

export interface User {
  id: string;                        // uuid
  full_name: string;
  email: string | null;
  phone: string | null;              // e.g. "+8801712345678"
  phone_verified_at: string | null;  // ISO 8601, UTC
  email_verified_at: string | null;  // ISO 8601, UTC
  created_at: string;                // ISO 8601, UTC
  image: string;                     // "" when there is no photo
  location: string;                  // "" when unset
  language: string;                  // e.g. "en"
  currency: string;                  // e.g. "AED"
  is_hijab_mode: boolean;

  // New in this release
  date_of_birth: string | null;      // "YYYY-MM-DD": a date, no time, no zone
  gender: Gender | null;             // null: "Prefer not to say", or never set
  whatsapp_booking_updates: boolean; // false when never set

  // Placeholders, see §3
  points: number;                    // always 0 today
  tier_level: string;                // always "BRONZE" today. Not an enum
  push_token: string | null;

  // Account deletion
  purge_at: string | null;           // set while a deletion is scheduled
  deleted_at: string | null;         // you will always read null
}

// The PATCH body: send only what changed.
export type UserPatch = Partial<{
  full_name: string;                 // trimmed; "" or only spaces is 422
  image: string;                     // the file_url an upload answered; "" removes it
  location: string;                  // "" clears it
  language: string;
  currency: string;
  is_hijab_mode: boolean;
  date_of_birth: string | null;      // null clears it
  gender: Gender | null;             // null is "Prefer not to say"
  whatsapp_booking_updates: boolean; // true or false, never null
}>;

export interface FieldError {
  field: string | null;              // the field to highlight, null for a toast
  code: string;                      // branch on this, never on message
  message: string;                   // ready to show
}

export interface ApiError {
  detail: string;
  code: string;                      // "validation_error", "not_authenticated", ...
  errors: FieldError[];
}
```

---

## 3. GET /auth/me

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
  "push_token": null,
  "purge_at": null,
  "deleted_at": null
}
```

- **`date_of_birth`, `gender` and `whatsapp_booking_updates` are always
  there.** Unset reads as `null`, `null` and `false`.
- **`gender` reads `null` on every account made before this release.**
  Register checked the gender it was sent and then never saved it. This
  release fixes that for new accounts only. An older account reads `null`
  until the user picks one on Personal Details. It is not a bug in the read.
- **`points` and `tier_level` are placeholders.** `points` is always `0` and
  `tier_level` is always `"BRONZE"`. Loyalty tiers belong to each salon, and
  each salon types its own tier names, so there is no one tier for the whole
  account. **Do not draw the points chip or the Gold Member chip yet.** Do not
  type `tier_level` as an enum.
- **`purge_at` is set while a deletion is scheduled** (`POST /auth/me/delete`)
  and back to `null` after `POST /auth/me/delete/cancel`. When it is set, you
  can show "Deletion scheduled" with that date. `deleted_at` is set only when
  the account is purged, and a purged account cannot sign in, so you will
  always read it as `null`. Both are read-only.

---

## 4. Personal Details, field by field

All of these are read from the provider's user, so the screen fills in with
no extra call.

### 4.1 Date of birth

- **Show:** "Not set" when `null`. Otherwise format it yourself, for example
  "14 March 1991".
- **Read it without `new Date("1991-03-14")`.** That string is read as
  midnight UTC, so on a phone set to a zone west of UTC it shows 13 March.
  Split the string instead (below).
- **Send it without `toISOString()`.** In Dhaka (UTC+6), midnight on 14 March
  is 13 March 18:00 UTC, so `toISOString()` saves `"1991-03-13"`. Build the
  string from the picked year, month and day (below).
- **Picker limits:** latest today, earliest 120 years ago. There is **no
  minimum age**: do not stop young users.
- **Clear it:** send `"date_of_birth": null`.
- **It does not lock.** It stays a picker and can be changed as often as the
  user likes.
- Only `YYYY-MM-DD` is taken. Anything else is `422 invalid_date` (§6).
- "Today" on the server is the UTC date. Only someone born today, saving
  between 00:00 and 06:00 Dhaka time, can get `date_in_future`. Show the
  server's message.

```ts
const pad = (n: number) => String(n).padStart(2, '0');

// Picker value -> what PATCH takes.
export const toDateOfBirth = (d: Date): string =>
  `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}`;

// What GET answers -> a Date for the picker, at local midnight.
export const fromDateOfBirth = (s: string): Date => {
  const [y, m, d] = s.split('-').map(Number);
  return new Date(y, m - 1, d);
};

// What GET answers -> "14 March 1991".
export const formatDateOfBirth = (s: string): string =>
  fromDateOfBirth(s).toLocaleDateString('en-GB', {
    day: 'numeric',
    month: 'long',
    year: 'numeric',
  });
```

### 4.2 Gender

- Four choices on the screen: Male, Female, Other, Prefer not to say.
- They send `"male"`, `"female"`, `"other"` and `null`.
- **"Prefer not to say" is `null`, never `""`.** An empty string is
  `422 invalid_choice`.
- When GET reads `null`, select "Prefer not to say". That covers a user who
  chose it and an older account that never had a gender saved (§3).
- Register is unchanged for you: it still requires `gender`, and now also
  takes `"other"`. It has no "prefer not to say": `null` there is `422`.

### 4.3 WhatsApp booking updates

- **Enable the switch only when `phone !== null && phone_verified_at !==
  null`.** Otherwise grey it out and point the user at the phone verify
  flow: `POST /auth/otp/request`, then `POST /auth/otp/verify`, with
  `destination_type: "phone"` and `purpose: "register"` (`AUTH_GUIDE.md`).
  After the verify, read `GET /auth/me` again so `phone_verified_at` fills in
  and the switch comes alive.
- **Turning it off never fails**, with or without a phone. If it reads
  `true`, always let the user turn it off.
- Send `true` or `false` only. `"true"`, `1` and `null` are `422`.
- **It goes in the PATCH with Save Changes**, like the other fields. It is no
  longer local state.
- **It is saved but does nothing yet.** Nothing sends booking messages on
  WhatsApp today: booking reminders go by push and email only. The value is
  kept, so it works the day a sender exists.
- **Do not send it to `/notifications/preferences`.** That endpoint does not
  know it and answers `400` (§6). It lives on `/auth/me` only, because that
  is where the phone check is.

```ts
export const canTurnOnWhatsApp = (u: User): boolean =>
  u.phone !== null && u.phone_verified_at !== null;

// Disabled only when it is off and cannot be turned on.
export const whatsAppDisabled = (u: User, on: boolean): boolean =>
  !on && !canTurnOnWhatsApp(u);
```

### 4.4 Name, photo and the rest

| Field | What to send | Notes |
| ----- | ------------ | ----- |
| `full_name` | The name | Trimmed by the server. `""` or only spaces is `422 blank`: disable Save while the box is empty |
| `image` | The `file_url` an upload answered (`UPLOAD_FILES_GUIDE.md`) | `""` removes the photo. `null` is `422` |
| `location` | Free text | `""` clears it. `null` is `422` |
| `language` | From the Language screen | Unchanged |
| `currency` | From the Currency screen | Unchanged |
| `is_hijab_mode` | From Settings | Unchanged |
| `email`, `phone` | Never | Read-only here. They change through the contact-change flow (`contact-change.md`) |

---

## 5. Save Changes: PATCH /auth/me

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

answers `200` with the account, the same body as GET. The block in §3 is
exactly what this request answers, sent to an account that had none of the
three set.

- **Put the reply into ProviderContext.** That refreshes the screen. You do
  not need to read GET again, but it still works if you prefer it.
- **One save, all or nothing.** If any field is refused, nothing in the PATCH
  is saved: a refused switch does not leave the date of birth half-saved.
- **Server-owned fields are ignored, not refused.** `id`, `email`, `phone`,
  `email_verified_at`, `phone_verified_at`, `points`, `tier_level`,
  `push_token`, `purge_at` and `deleted_at` in a PATCH get `200` and change
  nothing. Do not send them anyway: send only what changed.

```ts
// Build the PATCH from the form and the user it started from.
export function buildPatch(user: User, form: {
  full_name: string;
  image: string;
  date_of_birth: string | null;
  gender: Gender | null;
  whatsapp_booking_updates: boolean;
}): UserPatch {
  const patch: UserPatch = {};
  const name = form.full_name.trim();
  if (name !== user.full_name) patch.full_name = name;
  if (form.image !== user.image) patch.image = form.image;
  if (form.date_of_birth !== user.date_of_birth) patch.date_of_birth = form.date_of_birth;
  if (form.gender !== user.gender) patch.gender = form.gender;
  if (form.whatsapp_booking_updates !== user.whatsapp_booking_updates) {
    patch.whatsapp_booking_updates = form.whatsapp_booking_updates;
  }
  return patch; // empty: nothing changed, do not call
}
```

---

## 6. Errors

The usual envelope, as in `auth-error-response.md`.

- **`422`:** highlight the field named in `errors[].field`, show
  `errors[].message` under it, and branch on `errors[].code`.
- **`401`:** read only the top-level `code` (`not_authenticated`) and run the
  usual refresh or sign-in flow. With a bad token, `errors` holds several
  token items you can ignore.

| Status | `field` | `code` | When | `message` |
|---|---|---|---|---|
| 401 | | `not_authenticated` (top level) | No token, or it expired | |
| 422 | `date_of_birth` | `invalid_date` | Not `YYYY-MM-DD`, or not a real date | Enter the date as YYYY-MM-DD. |
| 422 | `date_of_birth` | `invalid_date` | More than 120 years ago | Enter a real date of birth. |
| 422 | `date_of_birth` | `date_in_future` | A birthday that has not happened | Your date of birth cannot be in the future. |
| 422 | `gender` | `invalid_choice` | Not one of the three, or `""` | "x" is not a valid choice. |
| 422 | `whatsapp_booking_updates` | `no_phone_on_account` | Switched on, no phone | Add a phone number first. |
| 422 | `whatsapp_booking_updates` | `phone_not_verified` | Switched on, phone not verified | Verify your phone number first. |
| 422 | `whatsapp_booking_updates` | `invalid` | Not `true` or `false` | Must be a boolean. |
| 422 | `whatsapp_booking_updates` | `null` | `null` | This field may not be null. |
| 422 | `full_name` | `blank` | `""` or only spaces | This field may not be blank. |
| 422 | `image`, `location` | `null` | `null` | This field may not be null. |

`too_young` is reserved and never sent: there is no minimum age (§7).

```ts
// errors[] -> one message per field, for the inputs to show.
export function fieldErrors(err: ApiError): Record<string, string> {
  const out: Record<string, string> = {};
  for (const e of err.errors) {
    if (e.field && !(e.field in out)) out[e.field] = e.message;
  }
  return out;
}
```

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

The same switch sent to `PATCH /notifications/preferences` by mistake. Note
the `400`, and no `code` on the item:

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

## 7. Your open points (§8 of your brief)

1. **Minimum age: there is none.** Do not block the picker. Nothing in Go
   Style checks an age today: not sign-up, not booking. A salon can set a
   minimum age on a service, but nothing enforces it yet. `too_young` is
   reserved and unused. If an age gate comes, we will send you the number and
   the message first.
2. **The WhatsApp switch needs a verified phone.** Yes. The rule and what the
   app does are in §4.3.
3. **`date_of_birth` does not lock.** It stays a picker. Nothing reads it yet,
   so a lock would protect nothing. If an age gate or a birthday reward ever
   reads it, we will tell you before it locks.
4. **`tier_level` has no list of values.** It is a placeholder that always
   says `"BRONZE"` (§3). Each salon makes its own tiers with names it types
   itself, and points are kept per salon. Keep the points chip and the Gold
   Member chip off the profile header until there is a real source.
5. **The birthday reward needs nothing from the app.** Today it is only a
   label a salon can put on a loyalty tier; nothing gives it out. If it ever
   needs a screen, it will come with its own endpoint and its own brief.

---

## 8. Rollout

- **No switch.** The three fields are on as soon as this release is deployed.
- **How to tell it is live:** `GET /auth/me` has the `date_of_birth` key.
- **Until then, keep the screen as it is today** (your brief §7): the date of
  birth as "Not set", the gender read-only, the switch local. An older server
  ignores the new fields in a PATCH and still answers `200`, so a `200` alone
  does not prove they were saved. Turn the pickers on from the key, not from
  a save.

```ts
export const hasProfileFields = (u: Partial<User>): boolean =>
  'date_of_birth' in u;
```

---

## 9. Where this differs from your brief

1. **PATCH answers the account itself, not `{success, user}`** (your §4). It
   is the same body GET answers.
2. **`phone_not_verified` is a new code** (§6). The switch needs a verified
   phone, and "no phone" and "not verified" are told apart.
3. **Server-owned fields are ignored, not refused** (your §4, "What it must
   keep refusing"). It has always worked this way, so an app that sends the
   whole user back never fails on them.
4. **There is no code for "too far in the past".** More than 120 years ago is
   `invalid_date`, with its own message (§6).
5. **`too_young` is never sent:** there is no minimum age (§7).
6. **GET also has `purge_at` and `deleted_at`**, from account deletion (§3).
7. **`full_name` cannot be emptied.** `""`, or only spaces, is now
   `422 blank`. Before this release it was saved.
8. **The switch has your name:** `whatsapp_booking_updates`.

---

## 10. QA checklist

- [ ] An account made before this release: gender reads `null`, the screen
      shows "Prefer not to say".
- [ ] Pick 14 March 1991 on a phone in Dhaka time and save: GET reads
      `"1991-03-14"` and the screen shows "14 March 1991".
- [ ] The same on a phone set to a US time zone: still 14 March.
- [ ] Clear the date of birth: `null` is sent, GET reads `null`, the screen
      shows "Not set".
- [ ] The picker does not offer tomorrow. Sent anyway, it is
      `422 date_in_future` on the date of birth row.
- [ ] Each gender choice saves and reads back. "Prefer not to say" sends
      `null`.
- [ ] The switch is greyed out on an email-only account and on an account
      whose phone is not verified. After verifying the phone and reading GET
      again, it comes alive.
- [ ] Switch on, save, leave the screen, come back: still on.
- [ ] Switch off on an account with no phone: saves.
- [ ] A name of only spaces: Save is disabled. Sent anyway, it is
      `422 blank` on the name.
- [ ] Name, photo, date of birth, gender and switch in one save: one PATCH,
      and the provider is updated from its reply.
- [ ] A refused save (switch on with an unverified phone) changes nothing:
      the date of birth sent with it is not saved either.
- [ ] The profile header shows no points chip and no Gold Member chip.
- [ ] An expired token on save: the usual `401` handling.

---

## 11. For whoever builds the WhatsApp sender (backend)

The switch is stored on the customer's notification preferences, in this
service. booking-api does not read that table: it asks this service over gRPC
(`ConsumerDirectory.GetConsumerContact`), and today the answer has
`appointment_reminder`, `push_enabled` and `email_verified`, not this switch.
So before any sender can honour it, add it to `proto/consumer_directory.proto`
(this repo and booking-api's copy) and to `apps/grpc_gen/servicer.py`.
