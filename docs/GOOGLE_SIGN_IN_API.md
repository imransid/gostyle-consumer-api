# Google Sign-In, and One Account for Both Ways In (FE Integration Guide)

Date: 2026-11-05. For: the mobile app team. Our answer to your brief of
2026-11-05 ("Google sign-in, and one account for both ways in"), for
"Continue with Google" on Login and Register, Settings (Sign-in & Security)
and Delete Account.

Base URL: `https://api.gostyle.uk/api/v1`

The error bodies on this page are the real ones: a test
(`GoogleSignInDocTests`) checks them against the code.

---

## 0. Read this first

1. **Built as your brief asks**: the same routes, fields, codes and rules.
   Section 9 lists every difference, all small.
2. **Behind one server switch, `GOOGLE_SIGN_IN_V1`, off for now.** While it
   is off, `POST /auth/google` and `POST /auth/password/set` answer `404`,
   `/auth/me` does not send the three new fields, and login and delete are
   exactly what they are today. So your Settings rows stay hidden until we
   turn it on. We tell you when it is on.
3. **Our client IDs are built in.** A token is accepted only when its `aud`
   is the web client (`1002425773215-llauqr34...`) or the iOS one
   (`1002425773215-ks6giaqd...`), exactly the two in your brief. Ask Google
   for the ID token with the **web client ID** as the server client ID, as
   you wrote, on Android and iOS.
4. **Ask Google for the `email` and `profile` scopes.** We read `email`,
   `email_verified` and `name` from the token. Without `profile` there is no
   `name`, and a new account is named after its email (the part before the
   `@`).

---

## 1. Endpoints

| Method | Path | What changes |
| --- | --- | --- |
| `POST` | `/auth/google` | New: sign in or sign up with a Google ID token |
| `GET` | `/auth/me` | Three new fields: `has_password`, `sign_in_methods`, `google_email` |
| `POST` | `/auth/login` | An account with no password answers `401 password_not_set` |
| `POST` | `/me/security/verify/send`, `/me/security/verify` | New purpose `password_set` |
| `POST` | `/auth/password/set` | New: add a password to an account made with Google |
| `POST` | `/auth/me/delete` | Takes `id_token` in place of `password` |

Send the device headers of the Sessions guide (`X-Device-Id`,
`X-Device-Name`, `X-App-Version` and the rest) on `POST /auth/google` too,
as on `/auth/login`: they name the session, and 2-step verification uses
`X-Device-Id` (section 2.2).

---

## 2. POST /auth/google

```http
POST /auth/google
Content-Type: application/json

{"id_token": "<the Google ID token>"}
```

No `Authorization` header: this is a sign-in. The same call from Login and
from Register. A stale bearer token, if the app sends one, is ignored.

We check the token against Google's public keys: the signature, `iss`
(`accounts.google.com` or `https://accounts.google.com`), `aud` (one of our
two client IDs), `exp` not passed. No client secret is involved. We never
store or log the ID token.

`200 OK`:

```json
{"access": "eyJhbGciOi...", "refresh": "eyJhbGciOi...", "is_new_user": true}
```

- `is_new_user: true` when this call made the account: show the welcome you
  show after a normal sign-up.
- Everything else is as after a password sign-in: a session in "Where
  you're signed in", the new sign-in alert for a new device, the device
  headers. A brand-new account gets no new sign-in alert for its very first
  session (as after register); it gets the usual welcome email instead.

### 2.1 Which account

In this order:

1. **The Google account is already linked** (its `sub` is stored on one of
   ours): that account, whatever its email is now.
2. **An account has the same email**, case ignored, and Google says
   `email_verified: true`: we link it (store the `sub`) and sign in.
   - Its email was already verified on our side: linked as it is. **Its
     password keeps working.**
   - Its email was never verified: we mark it verified now, **remove its
     password**, and **end its other sessions** (and its push
     registrations). Someone may have signed up with this address before
     its owner did.
   - Either way, the account's email is told (section 7).
3. **Neither**: a new account. `full_name` from Google's `name` (else the
   part of the email before the `@`), the email marked verified, no
   password, no gender. `is_new_user: true`.

Also:

- We link by `sub`, never by email, so a later email change on either side
  does not matter. Changing the email on our side (the contact-change flow)
  does not unlink Google. `google_email` stays the Google account's own
  address, refreshed at each Google sign-in.
- **Google says `email_verified: false`** (rare): never linked by email. If
  no account uses the email, a new account is made with the email
  unverified. If one does: `409 email_in_use`.
- **A deletion is scheduled**: the sign-in works as normal. `/auth/me` has
  `purge_at`, so show the Scheduled screen with "Cancel deletion", as after
  a password sign-in.
- **One Google account per account.** When the email matches an account
  already linked to a different Google account: `409 google_already_linked`.
- **A phone-only account** has no email to match: Google makes a new one.

### 2.2 2-step verification

Google does not skip it. When the account has 2-step verification on, and
this phone has not passed it, `POST /auth/google` answers exactly what
`POST /auth/login` answers then, and no tokens:

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

Open the 2-step screen you already have: `POST /auth/2fa/send` and
`POST /auth/2fa/verify` (docs/TWO_FACTOR_API.md section 6), from the same
`X-Device-Id`. The verify answers the token pair. The same rules as a
password sign-in: a phone that has passed 2-step and is still signed in
gets the tokens at once, then with `"is_new_user": false`.

A new account starts with 2-step off, so the challenge never comes with a
new account, and `is_new_user` has no place in it.

### 2.3 Errors

An error of the whole request has its code at the top and `errors: []`, as
your section 4 shows:

```json
{"detail": "We couldn’t sign you in with Google. Please try again.", "code": "invalid_token", "errors": []}
```

```json
{"detail": "An account already uses this email. Sign in with your password instead.", "code": "email_in_use", "errors": []}
```

```json
{"detail": "This account is already linked to a different Google account.", "code": "google_already_linked", "errors": []}
```

No `id_token`:

```json
{"detail": "Please correct the highlighted fields.", "code": "validation_error", "errors": [{"field": "id_token", "code": "required", "message": "This field is required."}]}
```

| Status | `code` | When | `detail` |
| --- | --- | --- | --- |
| 401 | `invalid_token` | The token is bad, expired, or not for our client IDs | We couldn’t sign you in with Google. Please try again. |
| 409 | `email_in_use` | Google did not verify the email and an account uses it (also section 9, item 1) | An account already uses this email. Sign in with your password instead. |
| 409 | `google_already_linked` | The matching account is linked to a different Google account | This account is already linked to a different Google account. |
| 422 | `required` (field `id_token`) | No `id_token` | This field is required. |
| 422 | `validation_error` (field null) | The account is disabled, as `/auth/login` answers it | Account disabled. |
| 503 | `service_unavailable` | Google's public keys cannot be read right now. Ours, rare: not the customer's fault, try again | Google sign-in isn’t available right now. Please try again. |
| 429 | `rate_limited` | More than 10 a minute from one address, as `/auth/login` | |

---

## 3. GET /auth/me: how the account signs in

Three new read-only fields, sent only while the switch is on:

```json
{"has_password": false, "sign_in_methods": ["google"], "google_email": "kevin.new@gmail.com"}
```

| Field | Type | Meaning |
| --- | --- | --- |
| `has_password` | boolean | `false` for an account made with Google that has not set one |
| `sign_in_methods` | string[] | `"password"`, `"google"`, or both, in that order. Empty only for an account with neither (none in the app) |
| `google_email` | string or null | The linked Google account's address; `null` when not linked. It can differ from `email` |

The same three fields come on `PATCH /auth/me` answers.

---

## 4. Email + password on an account with no password

`POST /auth/login` with the email (or phone) of an account linked to
Google that has no password, `401`:

```json
{"detail": "This account signs in with Google. Continue with Google, or set a password in Settings.", "code": "password_not_set", "errors": []}
```

Show that message with a "Continue with Google" button. As you noted, it
tells someone that the email has an account; we went with the clear
message, as you suggested.

**Forgot password works for these accounts as it does today**: the code goes
to the email, the new password is set, and from then on both ways work.
That is a second way to add a password.

---

## 5. Set a password: POST /auth/password/set

For an account with `has_password: false`. Show "Set a password" there in
place of "Change password".

First the code, with the flow you already use for 2-step and the
authenticator, and the new purpose `password_set`:

```http
POST /me/security/verify/send
Authorization: Bearer <access_token>
Content-Type: application/json

{"method": "email", "purpose": "password_set"}
```

```http
POST /me/security/verify
Authorization: Bearer <access_token>
Content-Type: application/json

{"method": "email", "purpose": "password_set", "code": "123456"}
```

answers `{"verification_token": "vt_...", "expires_in": 300}`: for this
phone's session, this purpose, 5 minutes, used once (the 2-step guide's
rules; `method` may also be `phone` or `authenticator`). Then:

```http
POST /auth/password/set
Authorization: Bearer <access_token>
Content-Type: application/json

{"verification_token": "vt_...", "new_password": "…", "confirm_password": "…"}
```

`204 No Content`. The account then has `has_password: true` and
`"password"` in `sign_in_methods`, `password_changed_at` on `/me/security`
is set, and the account's email is told (section 7). Other sessions stay
signed in.

```json
{"detail": "This account already has a password. Use Change password instead.", "code": "password_already_set", "errors": []}
```

```json
{"detail": "You do not have permission to do that.", "code": "permission_denied", "errors": [{"field": "verification_token", "code": "invalid", "message": "Please verify it’s you first."}]}
```

```json
{"detail": "Please correct the highlighted fields.", "code": "validation_error", "errors": [{"field": "confirm_password", "code": "invalid", "message": "Passwords do not match."}]}
```

| Status | `field` / `code` | When |
| --- | --- | --- |
| 409 | `password_already_set` | The account already has one: Change password is the way |
| 403 | `verification_token` / `invalid` | No token, or a wrong, used, expired or other-purpose one |
| 422 | `new_password` / the sign-up rules | Too short, too weak (the same rules and messages as at sign-up) |
| 422 | `confirm_password` / `invalid` | The two differ (as at sign-up) |

A refused password does not use the token up: fix it and send again within
the 5 minutes.

---

## 6. Delete Account without a password

`POST /auth/me/delete` takes exactly one of `password` and `id_token`. Your
"Confirm with Google": the customer picks their Google account once more,
and the app sends that fresh token:

```http
POST /auth/me/delete
Authorization: Bearer <access_token>
Content-Type: application/json

{"id_token": "<a Google ID token taken just now>", "reason": "privacy"}
```

- `password`: as today, for an account with a password.
- `id_token`: checked as in section 2, accepted only when its `sub` is the
  Google account linked to this account and it was issued in the last 10
  minutes (`iat`). It works for any linked account, with a password or
  without.

Everything else stays as it is: the answer (`purge_at`,
`confirmation_sent_to`), the scheduled email, the cancel.

```json
{"detail": "Authentication required.", "code": "invalid_token", "errors": [{"field": "id_token", "code": "invalid_token", "message": "Google couldn’t confirm it’s you. Please try again."}]}
```

```json
{"detail": "You do not have permission to do that.", "code": "permission_denied", "errors": [{"field": "id_token", "code": "wrong_google_account", "message": "That isn’t the Google account linked to your Go Style account."}]}
```

```json
{"detail": "Please correct the highlighted fields.", "code": "validation_error", "errors": [{"field": "password", "code": "required", "message": "Enter your password, or confirm with Google."}]}
```

| Status | `field` / `code` | When |
| --- | --- | --- |
| 401 | `id_token` / `invalid_token` | Bad, expired, or older than 10 minutes. The top-level `code` is `invalid_token` too, never `not_authenticated`: **this 401 is not the session ending, do not sign the customer out** |
| 403 | `id_token` / `wrong_google_account` | A Google account that is not the linked one (or the account has no Google link) |
| 403 | `password` / `wrong_password` | As today |
| 422 | `password` / `required` | Neither sent, or both |

Show `errors[0].message` for these: the top-level `detail` is the generic
one of its status.

---

## 7. Emails

To the account's verified email, once the change is saved:

| When | Subject | First line |
| --- | --- | --- |
| Google is linked to an existing account (2.1, step 2) | Go Style: Google sign-in was added to your account | Google sign-in was added to your Go Style account on iPhone 15 in Dubai, UAE. From now on, Continue with Google signs you in to it. |
| A password is added (section 5) | Go Style: a password was added to your account | A password was added to your Go Style account on iPhone 15 in Dubai, UAE. |

Then: "If this was not you, ..." and what to do. The device is the
`X-Device-Name` header, and the place is the one the Sessions list shows,
each only when known. A new account made with Google
gets the usual "Welcome to GoStyle" email.

---

## 8. On the app side

- [ ] Login and Register: "Continue with Google", then `POST /auth/google`.
      `two_factor_required` goes to the 2-step screen; otherwise keep the
      tokens, and `is_new_user: true` shows the welcome.
- [ ] Login with email and password: on `password_not_set`, the message
      with a "Continue with Google" button.
- [ ] Settings, Sign-in & Security: show the Google rows only when
      `/auth/me` has the fields. "Linked to …" from `google_email` when
      `sign_in_methods` has `"google"`; "Set a password" when
      `has_password` is `false` (purpose `password_set`, then
      `POST /auth/password/set`).
- [ ] Delete Account, Confirm: the password field when `has_password`,
      "Confirm with Google" when not.
- [ ] On the delete's `401 invalid_token`: ask for Google again, do not
      sign out.

---

## 9. What differs from your brief

1. **`409 email_in_use` also in one case the brief does not name**: the
   matching account's email was never verified, but its **phone** was. That
   account is in use by whoever proved the phone; taking it over would hand
   their bookings to the Google account's owner. They keep signing in with
   the phone. Very rare.
2. **"The two differ" is on `confirm_password`**, as at sign-up, not on
   `new_password`. The weak-password errors are on `new_password`.
3. **`503 service_unavailable`** when Google's public keys cannot be read.
   Not in the brief; not the token's fault.
4. **A disabled account** answers what `/auth/login` answers it:
   `422`, "Account disabled.".
5. **The 2-step challenge is the full one login answers**
   (`two_factor_required`, `challenge_token`, `expires_in`, `methods` with
   hints), not the short example in the brief.
6. **The 401 on delete has `invalid_token` as its top-level code**, so it
   never looks like the session ending.
7. **`password_not_set` only for an account linked to Google.** An account
   with no password and no Google link (none made by the app) answers
   "Invalid credentials." as today.
8. **The takeover in 2.1 also removes the old push registrations**, so the
   person who signed up first gets none of the account's notifications.
9. **A new account's first session sends no new sign-in alert** (as after
   register), and the account gets the usual welcome email.
10. **10 seconds of grace on the token's times**, for clocks that differ a
    little between Google, the phone and us.
11. **No `account_delete` code by email** (your alternative in section 6):
    we built "Confirm with Google", your suggestion.
