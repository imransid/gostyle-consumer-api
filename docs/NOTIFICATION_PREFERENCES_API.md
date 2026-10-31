# Notification Preferences: channels and reminder time (FE Integration Guide)

> **Live on production. Build from `docs/NOTIFICATIONS_FE_GUIDE.md`**, the
> one guide for the whole feature. This page is the first version, kept for
> its history.

Date: 2026-10-31. For: the mobile app team. The Notification Preferences
screen: "Notification Channels" (Push, Email, SMS) and "Appointment reminder
timing" (30 minutes, 1 hour, 2 hours, 1 day before). The six event switches
and Push are unchanged.

Base URL: `https://api.gostyle.uk/api/v1`
Auth: `Authorization: Bearer <access_token>`.

---

## 0. Read this first

1. **Same endpoint as today**: `GET` and `PATCH /notifications/preferences`.
   Three new fields and one read-only object, each behind a server switch:

   | Field | Switch | Off |
   | --- | --- | --- |
   | `email`, `sms`, `channels_available` | `NOTIFICATION_CHANNELS_V1` | not in `GET`; in `PATCH`, `400 unknown_field`, exactly as today |
   | `reminder_lead_minutes` | `REMINDER_TIMING_V1` | the same |

   We tell you when each is on.
2. **Still `400`**, not `422`, on this endpoint, as before. New: every item
   in `errors` now has a `code` (section 3).
3. **SMS has no provider yet.** `sms` is stored and answered, but turning it
   on answers `400 sms_unavailable`, and `channels_available.sms` is
   `false`. Grey the SMS switch out while it is `false`.
4. **Email goes only to a verified address.** Turning `email` on needs one.
5. **One reminder per visit, at the chosen time.** `REMINDER_TIMING_V1`
   goes on together with booking-api's `MOBILE_REMINDER_CHOICE`, which sends
   it. A new time counts for every coming visit within a minute. A booking
   made (or moved) after its reminder time gets no reminder: its
   confirmation (or "moved" message) just told them. Switched to an
   earlier time after it passed: the reminder goes at once. Nothing goes in
   the last 15 minutes before the visit.

## 1. GET

```http
GET /notifications/preferences
```

Both switches on, a customer with a verified email and a phone:

```json
{
  "booking_confirmation": true,
  "appointment_reminder": true,
  "booking_changes": true,
  "special_offers": false,
  "new_salons": false,
  "loyalty": true,
  "push": true,
  "email": true,
  "sms": false,
  "reminder_lead_minutes": 1440,
  "channels_available": {"push": true, "email": true, "sms": false}
}
```

| Field | Meaning | Default |
| --- | --- | --- |
| `email` | the Email channel switch | `true` |
| `sms` | the SMS channel switch | `false` |
| `reminder_lead_minutes` | `30`, `60`, `120` or `1440` (1 day) | `1440` |
| `channels_available` | read-only: can this channel reach the customer at all? `email` is `false` without a verified email, `sms` is `false` (no provider) | |

Show the switch's own value; grey it out when its `channels_available`
entry is `false`. `email: true` with `channels_available.email: false` is
normal: the switch is on, there is just no verified address yet.

## 2. PATCH

Partial, as before: send only what changed. All of it is saved, or none.

```http
PATCH /notifications/preferences
Content-Type: application/json

{"email": false, "reminder_lead_minutes": 60}
```

`200` with the whole object, as in section 1.

- Switches are JSON `true`/`false` only.
- `reminder_lead_minutes` is a JSON number, one of `30`, `60`, `120`,
  `1440`. Not `"60"`, not `45`.
- `channels_available` sent back is ignored.
- Turning `email` or `sms` **off** always works. Sending a switch back with
  the value it already has always works too.

## 3. Errors (`400`)

```json
{
  "detail": "Invalid notification preference.",
  "code": "validation_error",
  "errors": [
    {"field": "email", "code": "email_not_verified", "message": "Verify your email address first."}
  ]
}
```

| `field` | `code` | When |
| --- | --- | --- |
| `email` | `no_email_on_account` | turning email on, the account has no email |
| `email` | `email_not_verified` | turning email on, the email is not verified |
| `sms` | `no_phone_on_account` | turning SMS on, no phone |
| `sms` | `phone_not_verified` | turning SMS on, the phone is not verified |
| `sms` | `sms_unavailable` | turning SMS on: no provider yet |
| `reminder_lead_minutes` | `invalid` | not one of the four numbers ("Choose 30, 60, 120 or 1440.") |
| any switch | `invalid` | not a JSON boolean ("Must be a boolean.") |
| a key we do not know | `unknown_field` | a typo, or a field whose switch is off. `detail` is then "Unknown notification preference." |

## 4. Checklist

- [ ] Channel switches read `email` / `sms`, greyed by `channels_available`.
- [ ] SMS switch greyed while `channels_available.sms` is `false`.
- [ ] `email_not_verified`: offer "Verify your email" (the profile's verify
      flow), then try again.
- [ ] Reminder timing sends `reminder_lead_minutes` as a number.
- [ ] A field missing from `GET` means its switch is off: hide that row.
