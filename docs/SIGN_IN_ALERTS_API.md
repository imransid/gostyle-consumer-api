# Password Age and New Sign-in Alerts (FE Integration Guide)

Date: 2026-10-29. For: the mobile app team. Our answer to your brief of
2026-10-28, an addition to the 2-step verification guide
(`docs/TWO_FACTOR_API.md`), for Sign-in & Security
(`src/app/(profile)/settings/security.tsx`).

Base URL: `https://api.gostyle.uk/api/v1`
Auth: `Authorization: Bearer <access_token>` on both calls.

Every JSON block on this page is a real answer: a test
(`SignInAlertsDocTests`) builds the same account, sends the same requests
and must get these exact bodies, so this page cannot drift from the code.

---

## 0. Read this first

1. **Built as your brief asks**: `password_changed_at` and `sign_in_alerts`
   on `GET /me/security`, and `PATCH /me/security` for `sign_in_alerts`
   and nothing else. Every route of the 2-step guide stays as it is. 2-step
   is still turned on and off only by `PATCH /me/security/two-factor`, with
   a verification token.
2. **Behind the same server switches as the rest of `/me/security`.** While
   they are off, both calls answer `404`, as `GET /me/security` does today.
   We tell you when they are on.
3. **A new sign-in alert already existed** (an email, added 2026-10-28).
   The switch now turns it on and off, it now goes by push too, and a "new
   device" is now what your section 5 says. Section 5 below has exactly
   what it sends.

---

## 1. GET /me/security

```http
GET /me/security
Authorization: Bearer <access_token>
```

```json
{
  "two_factor_enabled": true,
  "methods": {
    "email": {"available": true, "hint": "k•••@gmail.com"},
    "phone": {"available": true, "hint": "+971 •• ••• 4567"},
    "authenticator": {"available": false, "added_at": null}
  },
  "password_changed_at": "2026-06-14T10:22:05+04:00",
  "sign_in_alerts": true
}
```

| Field | Type | Notes |
| --- | --- | --- |
| `password_changed_at` | string or null | When the account's password was last set. UAE time, `+04:00`, whole seconds, like `added_at` and the Sessions API. |
| `sign_in_alerts` | boolean | The "Alert me about new sign-ins" switch. `true` for every account until the customer turns it off: new accounts and existing ones. |

Both are always there. The rest of the answer is the 2-step guide's,
unchanged.

### password_changed_at

- **Set whenever a password is set**: `POST /auth/register`,
  `POST /auth/password/change` and `POST /auth/password/reset`.
- **Existing accounts**: the last password reset we have a record of, else
  the account's `created_at`. A change made with
  `/auth/password/change` before today left no record, so such an account
  shows its last reset, or its sign-up time.
- **`null` only for an account with no password at all.** Every app
  account has one (register needs it), so expect a string. Hide the line on
  `null` anyway.
- The server sends only the time; "4 months ago" is the app's.

---

## 2. PATCH /me/security

```http
PATCH /me/security
Authorization: Bearer <access_token>
Content-Type: application/json

{"sign_in_alerts": false}
```

`200 OK`, with the whole of `GET /me/security`, so one call refreshes the
screen:

```json
{
  "two_factor_enabled": true,
  "methods": {
    "email": {"available": true, "hint": "k•••@gmail.com"},
    "phone": {"available": true, "hint": "+971 •• ••• 4567"},
    "authenticator": {"available": false, "added_at": null}
  },
  "password_changed_at": "2026-06-14T10:22:05+04:00",
  "sign_in_alerts": false
}
```

| Field | Rule |
| --- | --- |
| `sign_in_alerts` | `true` or `false` only. Required. |

- **Every other field is refused**: `two_factor_enabled`, `methods`,
  `password_changed_at` or anything else answers `422` `read_only` on that
  field, and **nothing in the request is saved**, `sign_in_alerts`
  included.
- **No Verify it's you.** It is an on/off notice, like the notification
  preferences.
- **The value it already has is a `200`**, and nothing else happens.
- **Turning it off emails the account** (section 4). Turning it back on
  sends nothing.
- No app version check, as on every route.

---

## 3. Errors

The usual envelope, as in `auth-error-response.md`. Each wrong field is its
own entry in `errors`: `{"two_factor_enabled": false}` alone answers both
`sign_in_alerts` `required` and `two_factor_enabled` `read_only`.

| Status | `field` | `code` | When | `message` |
| --- | --- | --- | --- | --- |
| 401 | null | `not_authenticated` | No token, or it expired | Please sign in again. |
| 422 | `sign_in_alerts` | `required` | Missing from the body | This field is required. |
| 422 | `sign_in_alerts` | `invalid` | Not `true` or `false` (`"true"`, `1`, `null`) | Must be a boolean. |
| 422 | the field sent | `read_only` | Any other field in the body | This can’t be changed here. |

```http
PATCH /me/security

{"sign_in_alerts": false, "two_factor_enabled": false}
```

```json
{
  "detail": "Please correct the highlighted fields.",
  "code": "validation_error",
  "errors": [
    {"field": "two_factor_enabled", "code": "read_only", "message": "This can’t be changed here."}
  ]
}
```

No token, or an expired one, on either call:

```json
{
  "detail": "Please sign in again.",
  "code": "not_authenticated",
  "errors": [
    {"field": null, "code": "not_authenticated", "message": "Please sign in again."}
  ]
}
```

A device that was signed out still gets `401 session_revoked`, as on every
route (the Sessions guide).

---

## 4. The email on turning alerts off

To the account's verified email, once the change is saved, in the style of
the 2-step emails:

| Subject | Text |
| --- | --- |
| Go Style: sign-in alerts turned off | Sign-in alerts were turned off for your Go Style account on iPhone 15 in Dubai. If this was not you, reset your password and contact support straight away. |

The device and the place are the ones of the phone that turned them off, as
the Sessions list names them. Turning alerts back on, or "off" when they
are already off, sends nothing. An account with no verified email gets no
email (WhatsApp needs an approved template for it).

---

## 5. The new sign-in alert

**When.** A sign-in is a token pair from `POST /auth/login` or
`POST /auth/2fa/verify`. With 2-step on, that is the moment the code
passes, not the password. `POST /auth/register` is not a sign-in, and
neither is a token refresh.

**A new device** is a sign-in whose `X-Device-Id` has no live session on
the account (one the Sessions list would show), or one with no
`X-Device-Id` at all. So:

- signing in again on a phone that is still signed in: no alert;
- a phone that signed out, or was signed out, is new again;
- a web browser sends no `X-Device-Id`: every web sign-in alerts.

**How the account is told**, both naming the device and the place as the
Sessions list does:

| Way | To | What |
| --- | --- | --- |
| Email | The account's verified email | Subject "Go Style: new sign-in to your account". Text: "Your Go Style account was just signed in to on iPhone 15 in Dubai. If this was you, there is nothing to do. If not, change your password, sign that device out from Where you're signed in in the app's settings, and contact support straight away." |
| Push | Every phone of the account registered for push | Title "New sign-in to your account". Body "Your Go Style account was just signed in to on iPhone 15 in Dubai. If this was not you, change your password." Data `type`: `new_sign_in`, `sessionId`: the new session's `id`, as in `GET /me/sessions`. |

- **With `sign_in_alerts` off, nothing is sent.**
- No push when push is off in the notification preferences
  (`push: false`). The email still goes.
- The phone that just signed in has not registered for push yet as a rule,
  so it is the others that get the push. A phone still registered from an
  earlier sign-in may get it too.
- No device name: "an unknown device". No place: the words "in Dubai" are
  left out.
- On tapping the push, Where you're signed in is the natural screen:
  `sessionId` is the row to show.

---

## 6. On the app side

- [ ] Password row: "Last changed 4 months ago" from `password_changed_at`,
      in the largest whole unit. Hide the line on `null`. Read
      `GET /me/security` again after a password change.
- [ ] Alerts switch: read `sign_in_alerts`, save each flip with
      `PATCH /me/security`, go back on a failed save with the server's
      `message`. "Undo" after turning it off sends `true`.
- [ ] Send nothing but `sign_in_alerts` in the PATCH body.
- [ ] Push: handle `data.type` `new_sign_in` (open Where you're signed in).
- [ ] Match the switch's wording to section 5: a new sign-in is a sign-in
      on a device that is not signed in right now, and every web sign-in.

---

## 7. Your open points

1. **Backfill**: not just `created_at`. We use the account's last password
   reset where we have one, else `created_at`. A change made with the old
   password left no record before today, so for those accounts it is the
   best we have.
2. **Did a new sign-in alert exist?** Yes, added 2026-10-28: an email to
   the verified email, for a device the account had never used before (any
   session ever, matched by `X-Device-Id`, or by name on the web). It is now
   section 5: your definition of a new device, the switch, and a push too.
3. **The email on turning alerts off**: added, as you wrote it (section 4).

---

## 8. What differs from your brief

1. **`password_changed_at` for existing accounts** is the last reset where
   there is one, not only `created_at` (section 7).
2. **A password with no stored time answers `created_at`**, never `null`.
   Only an account written by the old code during the deploy can be like
   that.
3. **Phone-only accounts get no emails**, neither the alert nor the notice
   on turning it off: WhatsApp needs an approved template. They still get
   the push.
4. **The 401 words** ("Please sign in again.") are on these two calls. The
   other `/me/security` routes keep the 401 they shipped with.
5. **The push's data key is `sessionId`**, camelCase, as booking-api's
   pushes name theirs (`bookingId`).
