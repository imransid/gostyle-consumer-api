# 2-Step Verification and the Authenticator App (FE Integration Guide)

Date: 2026-10-28. For: the mobile app team. Our answer to your brief of
2026-10-27, for Sign-in & Security (`security.tsx`, `two-fa.tsx`,
`authenticator.tsx`, `verify-identity.tsx`) and the sign-in challenge.

Base URL: `https://api.gostyle.uk/api/v1`
Auth: `Authorization: Bearer <access_token>` on every `/me/security` call.
The two `/auth/2fa` calls are anonymous and carry a `challenge_token`.

Every JSON block on this page is a real answer: a test
(`TwoFactorDocTests`) builds the same account, sends the same requests and
must get these exact bodies, so this page cannot drift from the code. The
authenticator codes in the requests are real too, for the example secret at
the example times (UAE time, 27 Oct 2026): `377576` at 14:05:12, `057138` at
14:06:40, `417231` at 14:10:05. The codes in the brief were made up.

---

## Changed on 2026-10-28: no app version check

As you asked, the API never looks at the app's version. No call answers
`426 app_update_required` any more, with or without `X-App-Version`, and
sign-in for an account with 2-step on always answers the challenge
(section 6), whatever the client sends. We need no app version from you, now
or for later releases.

---

## 0. Read this first

1. **Built as your brief asks**: the same routes, fields, purposes, error
   codes and words. Section 10 lists every difference.
2. **Behind server switches, off for now.** While they are off every new
   route answers `404` and sign-in is exactly what it is today. We tell you
   when they are on.
3. **`X-App-Version` is optional.** It only names the session in Sign-in
   & Security (the Sessions guide). No call looks at it. An app without the
   challenge screen that signs in to an account with 2-step on gets the
   challenge (section 6) and no tokens, like any other client.
4. **A web browser gets the same challenge.** Once an account turns 2-step
   on, it cannot sign in from a web browser until the web app shows the
   challenge screen. The phone app is not affected.
5. **Phone codes go by WhatsApp, not SMS**, the way the existing OTP codes
   already do. This API has no SMS sender. Show "WhatsApp" where the screens
   say "text message".

---

## 1. Endpoints

| Method   | Path                                 | Purpose                                                       |
| -------- | ------------------------------------ | ------------------------------------------------------------- |
| `GET`    | `/me/security`                       | The switch and which methods the account has                  |
| `POST`   | `/me/security/verify/send`           | Send a "Verify it's you" code by email or WhatsApp            |
| `POST`   | `/me/security/verify`                | Check that code, or an authenticator code: a verification token |
| `PATCH`  | `/me/security/two-factor`            | Turn 2-step verification on or off                            |
| `POST`   | `/me/security/authenticator`         | Start adding an authenticator: its key and QR link            |
| `POST`   | `/me/security/authenticator/confirm` | The app's first code; turns the authenticator on              |
| `DELETE` | `/me/security/authenticator`         | Remove the authenticator                                      |
| `POST`   | `/auth/login`                        | Changed: may answer a 2-step challenge instead of tokens      |
| `POST`   | `/auth/2fa/send`                     | Send the sign-in code by email or WhatsApp                    |
| `POST`   | `/auth/2fa/verify`                   | Check the sign-in code: the token pair                        |

Every request also carries the device headers of the Sessions guide
(`X-Device-Id`, `X-Device-Name`, `X-App-Version` and the rest). Section 6
uses `X-Device-Id`.

---

## 2. GET /me/security

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
  }
}
```

| Field | Type | Notes |
| --- | --- | --- |
| `two_factor_enabled` | boolean | The switch. `false` for a new account. |
| `methods.email.available` | boolean | The account has a verified email. |
| `methods.email.hint` | string or null | Masked on the server: first letter, `•••`, then the domain. `null` when not available. |
| `methods.phone.available` | boolean | The account has a verified phone. |
| `methods.phone.hint` | string or null | Masked: the country code and the last 4 digits. `null` when not available. |
| `methods.authenticator.available` | boolean | An authenticator was added and confirmed. A pending setup does not count. |
| `methods.authenticator.added_at` | string or null | `2026-10-27T14:05:12+04:00`: UAE time, whole seconds, like the Sessions API. |

Not behind the app version check: any app may read it.

---

## 3. Verify it's you

| `purpose` | Asked before |
| --- | --- |
| `two_step_on` | Turning 2-step verification on |
| `two_step_off` | Turning 2-step verification off |
| `authenticator_add` | Adding an authenticator app |
| `authenticator_remove` | Removing the authenticator app |

### 3.1 POST /me/security/verify/send

```http
POST /me/security/verify/send
Authorization: Bearer <access_token>
Content-Type: application/json

{"method": "email", "purpose": "two_step_on"}
```

```json
{"sent_to": "k•••@gmail.com", "expires_in": 600, "resend_in": 30}
```

- **Only to the account's own verified email or phone.** The request never
  names a destination.
- **Six digits, 10 minutes, used once.** A new send for the same purpose
  voids the code before.
- **`resend_in`: 30 seconds.** Sending again sooner answers `429
  too_many_attempts`. Also at most 10 sends an hour and 20 a day per
  account.
- **`method`: `email` or `phone`.** Phone codes go by WhatsApp. For an
  authenticator there is nothing to send: `method: "authenticator"` answers
  `422` (`invalid_choice`). Go straight to 3.2.
- The email's subject is "Your Go Style security code".

### 3.2 POST /me/security/verify

```http
POST /me/security/verify
Authorization: Bearer <access_token>
Content-Type: application/json

{"method": "authenticator", "purpose": "two_step_off", "code": "057138"}
```

```json
{"verification_token": "vt_4f8b2c9e1a7d5e3b9c0f6a2d8e4b1c7f3a9d5e2b0c6", "expires_in": 300}
```

- `method`: `email`, `phone` or `authenticator`. For email and phone the
  code is the one 3.1 sent. For the authenticator it is the app's current
  code (5.4).
- **The token is for this account, this session and this purpose only**: a
  token for `authenticator_add` cannot turn the switch off, and another
  phone's token does not work here. 5 minutes, used once. We store only a
  hash.
- A code that is not six digits answers `422` `invalid` without being
  checked, and does not count as a try.

---

## 4. PATCH /me/security/two-factor

```http
PATCH /me/security/two-factor
Authorization: Bearer <access_token>
Content-Type: application/json

{"enabled": false, "verification_token": "vt_4f8b2c9e1a7d5e3b9c0f6a2d8e4b1c7f3a9d5e2b0c6"}
```

```json
{"two_factor_enabled": false}
```

- `enabled: true` needs a token for `two_step_on`, `false` one for
  `two_step_off`. A missing, empty, `null`, used, expired, wrong-purpose or
  other-phone token answers `403 verification_required`.
- **The value it already has is a `200`**, and the token is still used up.
- Turning it on needs nothing else. **This phone is not asked for a code at
  its next sign-in**: it has just passed Verify it's you.
- Turning it off leaves the authenticator linked.
- **The account's email hears of every change** (section 8).

---

## 5. The authenticator app

### 5.1 POST /me/security/authenticator

```http
POST /me/security/authenticator
Authorization: Bearer <access_token>
Content-Type: application/json

{"verification_token": "vt_...", "purpose": "authenticator_add"}
```

`201 Created`:

```json
{
  "secret": "JBSWY3DPEHPK3PXPJBSWY3DPEHPK3PXP",
  "otpauth_url": "otpauth://totp/Go%20Style:kevin.new%40gmail.com?secret=JBSWY3DPEHPK3PXPJBSWY3DPEHPK3PXP&issuer=Go%20Style&algorithm=SHA1&digits=6&period=30",
  "expires_in": 600
}
```

| Shown as | From |
| --- | --- |
| The QR code | `otpauth_url`, drawn on the phone |
| "Can't scan it? Enter this key" | `secret`, in groups of four, with Copy |
| "On this phone? Open in authenticator app" | `otpauth_url`, opened as a link |

- **Needs a token for `authenticator_add`.** `purpose` in the body is
  optional; the route decides which purpose the token must be for.
- Calling it again replaces a pending setup with a new secret. The old QR
  code stops working.
- **Pending for 10 minutes, and only for this phone's session.**
- With an authenticator already confirmed: `409 already_added`, and the
  token is **not** used up. Remove the old one and use the same token again
  within its 5 minutes.
- The secret is answered only here. After confirmation it never leaves the
  server again. We keep it encrypted.

### 5.2 POST /me/security/authenticator/confirm

```http
POST /me/security/authenticator/confirm
Authorization: Bearer <access_token>
Content-Type: application/json

{"code": "377576"}
```

```json
{"added_at": "2026-10-27T14:05:12+04:00"}
```

- No new token: 5.1 asked for one, and the pending setup belongs to the
  session that started it.
- No pending setup (none, past its 10 minutes, or started on another phone):
  `409 no_setup`.
- A wrong code: `422` `invalid`, and the setup stays pending, so the
  customer can try the next code.
- **The account's email is told an authenticator was added** (section 8).

### 5.3 DELETE /me/security/authenticator

```http
DELETE /me/security/authenticator
Authorization: Bearer <access_token>
Content-Type: application/json

{"verification_token": "vt_...", "purpose": "authenticator_remove"}
```

`204 No Content`, with the body on the DELETE as you already send for
`/me/device-tokens`.

- The token may come from any method, the authenticator itself included.
- Removing it does not turn 2-step verification off. Email and phone still
  work.
- With no authenticator to remove: still `204`, still needs the token.

### 5.4 The codes

Standard TOTP (RFC 6238), so every common authenticator app works.

| Setting | Value |
| --- | --- |
| Algorithm | SHA1 |
| Digits | 6 |
| Period | 30 seconds |
| Secret | 20 random bytes as base32 without padding: 32 characters |
| Issuer | `Go Style` |
| Label | `Go Style:` and the account's email, or its phone when it has no email |

- The current 30-second step and one either side pass, for clock drift.
- **A code passes once**, even inside its window, and never an older one
  after a newer one has passed.
- **5 wrong codes per 15 minutes per account**, across every call here
  (`/me/security/verify`, `/authenticator/confirm` and `/auth/2fa/verify`).
  After that: `429 too_many_attempts`, even for the right code.

---

## 6. Signing in with 2-step verification on

### When we ask

On a password sign-in, when the account has `two_factor_enabled`:

- **Any client is asked**, whatever `X-App-Version` it sends, or none: a web
  browser too.
- **A device that has passed 2-step and is still signed in is not asked**:
  the token pair at once. "Still signed in" is a session the Sessions list
  would show for this `X-Device-Id`. It is forgotten when the device signs
  out (`POST /auth/logout`) or is signed out from Sign-in & Security, and
  then asked again.
- **No `X-Device-Id`: always asked.**
- A wrong password is the same `422` as today: nothing tells it apart from
  an account without 2-step.

### POST /auth/login answers a challenge

Instead of the token pair, `200 OK` with:

```json
{
  "two_factor_required": true,
  "challenge_token": "ch_6a1f9e3c7b2d8f4a0e5c1b9d7f3a6e2c8b4d0f5a1e7",
  "expires_in": 600,
  "methods": [
    {"method": "email", "hint": "k•••@gmail.com"},
    {"method": "phone", "hint": "+971 •• ••• 4567"},
    {"method": "authenticator", "hint": null}
  ]
}
```

`methods` lists only what the account has, always in this order. The
challenge lasts 10 minutes and allows 5 wrong codes.

### POST /auth/2fa/send

```http
POST /auth/2fa/send
Content-Type: application/json

{"challenge_token": "ch_6a1f9e3c7b2d8f4a0e5c1b9d7f3a6e2c8b4d0f5a1e7", "method": "phone"}
```

```json
{"sent_to": "+971 •• ••• 4567", "expires_in": 600, "resend_in": 30}
```

The rules of 3.1. The email's subject is "Your Go Style sign-in code".

### POST /auth/2fa/verify

```http
POST /auth/2fa/verify
Content-Type: application/json

{"challenge_token": "ch_6a1f9e3c7b2d8f4a0e5c1b9d7f3a6e2c8b4d0f5a1e7", "method": "authenticator", "code": "417231"}
```

`200 OK`: the same `{"access": "...", "refresh": "..."}` that login answers
today. The session starts here, and this device is remembered as having
passed 2-step.

- **Send it from the same `X-Device-Id` as the login.** Another device, or
  none when the login had one, answers `401 challenge_expired`.
- A wrong code is `422` `invalid` and counts against the challenge. After 5,
  the challenge is used up: `401 challenge_expired`, and the customer signs
  in again.
- A challenge works once.
- A code sent for Verify it's you does not sign in (`422` `expired`).

---

## 7. Errors

The envelope of the Auth error guide: `detail`, `code`, and `errors`. An
error of the whole request has its own code at the top **and** as the one
entry in `errors`, with `field: null`:

```json
{
  "detail": "Please verify it’s you first.",
  "code": "verification_required",
  "errors": [
    {"field": null, "code": "verification_required", "message": "Please verify it’s you first."}
  ]
}
```

An error of one field is `422` `validation_error`, with the field:

```json
{
  "detail": "Please correct the highlighted fields.",
  "code": "validation_error",
  "errors": [
    {"field": "code", "code": "invalid", "message": "That code isn’t right. Check it and try again."}
  ]
}
```

| Status | `field` | `code` | When | Message |
| --- | --- | --- | --- | --- |
| 422 | `code` | `invalid` | Wrong code, or not six digits | That code isn’t right. Check it and try again. |
| 422 | `code` | `expired` | No live code: its 10 minutes are up, a newer one voided it, or none was sent | This code has expired. Send a new one. |
| 422 | `method` | `unavailable` | No such method on this account | That way to verify isn’t set up on your account. |
| 422 | `method` | `invalid_choice` | `authenticator` on a send, or not a method at all | "authenticator" is not a valid choice. |
| 403 | null | `verification_required` | No, used, expired, wrong-purpose or other-phone token | Please verify it’s you first. |
| 409 | null | `already_added` | Adding with an authenticator already on | Remove your current authenticator app first. |
| 409 | null | `no_setup` | Confirming with no pending setup on this phone | Start setting up your authenticator app again. |
| 429 | null | `too_many_attempts` | 5 wrong codes in 15 minutes, or a send too soon or too often | Too many tries. Wait a few minutes and try again. |
| 401 | null | `challenge_expired` | Sign-in challenge expired, used up, or from another device | Your sign-in timed out. Please sign in again. |
| 503 | null | `authenticator_unavailable` | A problem with the server's key | The authenticator app can’t be used right now. Please try again later. |

The last one is ours, not in your brief.

---

## 8. The emails the account gets

To its verified email, once the change is saved, naming the device and the
place of the phone that made it:

| After | Subject | First line |
| --- | --- | --- |
| Turning 2-step on | Go Style: 2-step verification turned on | 2-step verification was turned on for your Go Style account on iPhone 15 in Dubai. |
| Turning it off | Go Style: 2-step verification turned off | 2-step verification was turned off for your Go Style account on iPhone 15 in Dubai. |
| Adding an authenticator | Go Style: authenticator app added | An authenticator app was added to your Go Style account on iPhone 15 in Dubai. |
| Removing it | Go Style: authenticator app removed | The authenticator app was removed from your Go Style account on iPhone 15 in Dubai. |

Then: "If this was not you, reset your password and contact support
straight away."

- **The email on adding an authenticator is a deliberate addition to your
  brief.** Adding one is the change an intruder would make to keep access,
  so the owner hears of it.
- Setting the switch to the value it already has sends nothing: nothing
  changed.
- An account with no verified email gets no email. WhatsApp needs an
  approved template for these, so there is no WhatsApp notice yet.

---

## 9. On the app side

- [ ] Send the device headers of the Sessions guide on every call. Each
      one is optional, and none of them decides access.
- [ ] Read `GET /me/security` on the three screens.
- [ ] Verify it's you: send with 3.1, check with 3.2, then pass the
      `verification_token` to the change it was asked for.
- [ ] Authenticator: take `secret` and `otpauth_url` from 5.1, confirm with
      5.2, remove with 5.3.
- [ ] The sign-in challenge screen: on `two_factor_required`, show
      `methods`, send with `/auth/2fa/send`, finish with `/auth/2fa/verify`
      from the same device.
- [ ] On `401 challenge_expired`, go back to sign-in.
- [ ] Say "WhatsApp", not "text message", for phone codes.

---

## 10. What differs from your brief

1. **Phone codes go by WhatsApp**, not SMS (section 0).
2. **`503 authenticator_unavailable`**, for a problem with the server's key.
   Not in the brief.
3. **An email when an authenticator is added** (section 8). Deliberate.
4. **No email when nothing changed**, and none for an account with no
   verified email.
5. **`/auth/2fa/verify` must come from the login's `X-Device-Id`**, or it is
   `401 challenge_expired`.
6. **A code that is not six digits** is `422` `invalid` without a try being
   used, and `method: "authenticator"` on a send is `422` `invalid_choice`.
7. **`hint` is `null`** for a method that is not available.
8. **More limits than the brief's**: at most 10 sends an hour and 20 a day
   per account, and 20 an hour from one IP address.
9. **Sign-in by one-time code**: this API has no such sign-in, so the
   brief's rule for it (the second code from another method) has nothing to
   apply to.
10. **The example codes are real**, for the example secret at the example
    times. The brief's were made up.
