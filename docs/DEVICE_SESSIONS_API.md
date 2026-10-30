# Where you're signed in: the Sessions API (FE Integration Guide)

Date: 2026-10-27. For: the mobile app team. Our answer to your Sessions API
brief of the same day, for Sign-in & Security
(`src/app/(profile)/settings/security.tsx`).

Base URL: `https://api.gostyle.uk/api/v1`
Auth: `Authorization: Bearer <access_token>` on all three calls.

Every JSON block on this page is a real answer: a test
(`SessionsDocTests`) builds the same sessions, sends the same requests and
must get these exact bodies, so this page cannot drift from the code. They
are the examples of your brief, byte for byte.

---

## 0. Read this first

1. **Built exactly as your brief asks**: the same three routes, headers,
   fields, times, order, status codes, error codes and words. Section 9 has
   the one thing that cannot be the same in this API.
2. **Behind a server switch, `DEVICE_SESSIONS_V1`, off for now.** While it is
   off the three routes answer `404` and nothing else changes. We tell you
   when it is on.
3. **When the switch goes on, every phone refreshes its token once.** An
   access token issued before has no `sid`, and it answers the same `401` as
   an expired token. Your app already refreshes on that, and the refresh
   gives it a session. Nobody has to sign in again (section 8).
4. **Handle `401 session_revoked` on every call**, not only on refresh:
   clear the tokens and go to sign in (section 7).
5. **Call `POST /auth/logout` with the refresh token when signing out.** It
   ends this device's session. Without it a phone that signed out stays in
   the list until its refresh token expires (30 days).

---

## 1. Endpoints

| Method   | Path                  | Purpose                                                |
| -------- | --------------------- | ------------------------------------------------------ |
| `GET`    | `/me/sessions`        | Every device signed in to this account, this one first |
| `DELETE` | `/me/sessions/{id}`   | Sign one other device out                              |
| `DELETE` | `/me/sessions`        | Sign out every device except this one                  |

"This device" always means the session the bearer token belongs to: its
`sid` claim (section 3).

---

## 2. The headers we read

Exactly the ones in your brief, on every request, the refresh call too.

| Header          | Stored as     |
| --------------- | ------------- |
| `X-Device-Id`   | `device_id`   |
| `X-Device-Name` | `device_name` |
| `X-Device-Type` | `device_type` |
| `X-Platform`    | `platform`    |
| `X-OS-Version`  | `os_version`  |
| `X-App-Version` | `app_version` |
| `X-App-Build`   | `app_build`   |

- **Every header is optional.** A missing or empty value, or one that is not
  allowed (`X-Device-Type` other than `phone`, `tablet`, `browser`,
  `desktop`; `X-Platform` other than `ios`, `android`, `web`), is the same
  as no header. No header ever fails a request.
- **Only printable ASCII is kept**, as you already send.
- **A header you leave out never clears a stored value.**
- **They name a session, they never authenticate one.** Access is decided by
  the token alone.
- **A web browser** sends no device headers. We name it from its
  User-Agent: `"Chrome on macOS"`, type `browser`, platform `web`.

---

## 3. What a session is

- **One session per sign-in on one device.** It starts whenever the API
  issues a token pair: `POST /auth/login`, `POST /auth/register`, and the
  first refresh of a token from before this release (section 9).
- **Both tokens carry its id as a `sid` claim.** You read only `sub`, `exp`
  and `iat` today, so nothing breaks.
- **It owns the refresh token.** `POST /auth/token/refresh` keeps the same
  session while it rotates the refresh token, and updates the session's
  `last_seen_at`, IP, location and device fields from that request's
  headers. A phone that updates the app shows its new version without
  signing in again.
- **One session per device per account.** Signing in again with the same
  `X-Device-Id` on the same account ends that device's previous session
  instead of adding a row. Without an `X-Device-Id`, each sign-in is its own
  session.
- **`last_seen_at` moves on refresh and on authenticated requests**, at most
  once every 5 minutes per session.
- **It ends** on `POST /auth/logout`, on Sign Out from another device, on a
  newer sign-in from the same device, and when its refresh token is revoked
  (a password reset revokes them all). A session whose refresh token has
  expired has ended too. Ended sessions are not listed.

### Location

`location` is the server's, from the IP address each request comes from,
looked up in a city database on the server (DB-IP City Lite, free). The app
sends nothing for it and never needs location permission for this screen.

**Please show a small credit on this screen**, under the list: "IP
geolocation by DB-IP", linking to https://db-ip.com. The free database's
licence (CC BY 4.0) asks for it.

- The client's real IP: behind our proxy, the right-most address it added to
  `X-Forwarded-For`, and only for a request that really came through it.
- **The city in English and a short country code**: `Dubai, UAE`,
  `Dhaka, BD`, `New York, USA`, `London, UK` (changed 2026-10-30, it was
  the city alone). The code is the two-letter ISO one, except `USA`, `UK`
  and `UAE`. In the UAE the city is the emirate (`Dubai`, `Abu Dhabi`,
  `Sharjah`), never a district such as Motor City. The city alone when the
  database knows no country for it, the country's name
  (`United Arab Emirates`) when it has no city, and `null` when it has
  neither: show the device name alone. Show it as it comes.
- Looked up when the session starts, and again on a refresh or a
  `last_seen_at` update when the IP has changed since the last lookup. A
  phone on the same network causes no lookups.
- A private or reserved address (local testing) leaves the location as it
  was. IPv6 works like IPv4.
- City only. We never store the coordinates.
- **Sometimes wrong**: carriers route through a few gateways, so a phone in
  Sharjah may read Dubai, UAE, and a VPN shows the VPN's city.

---

## 4. GET /me/sessions

```http
GET /api/v1/me/sessions
Authorization: Bearer <access_token>
```

`200 OK`

```json
{
  "count": 3,
  "next": null,
  "previous": null,
  "results": [
    {
      "id": "6f2a1c9e-8b3d-4e57-9a10-2c4d6e8f0a1b",
      "device_name": "iPhone 15",
      "device_type": "phone",
      "platform": "ios",
      "os_version": "18.1",
      "app_version": "1.0.0",
      "ip_address": "94.200.14.31",
      "location": "Dubai, UAE",
      "is_current": true,
      "created_at": "2026-10-20T09:12:44+04:00",
      "last_seen_at": "2026-10-27T11:58:03+04:00"
    },
    {
      "id": "1b7e4d2a-0c6f-4a93-8e15-7d9b3f2c6a40",
      "device_name": "iPad",
      "device_type": "tablet",
      "platform": "ios",
      "os_version": "17.6",
      "app_version": "1.0.0",
      "ip_address": "94.200.18.7",
      "location": "Dubai, UAE",
      "is_current": false,
      "created_at": "2026-10-02T18:40:10+04:00",
      "last_seen_at": "2026-10-24T21:15:37+04:00"
    },
    {
      "id": "c3e9a7f1-5d2b-4f08-b6a4-1e8c0d7f9b25",
      "device_name": "Chrome on macOS",
      "device_type": "browser",
      "platform": "web",
      "os_version": null,
      "app_version": null,
      "ip_address": "86.98.44.120",
      "location": "Abu Dhabi, UAE",
      "is_current": false,
      "created_at": "2026-10-08T12:03:51+04:00",
      "last_seen_at": "2026-10-13T16:22:09+04:00"
    }
  ]
}
```

The page shape is the one `/bookings` uses. Everything comes in one page:
`next` and `previous` are always `null`.

| Field          | Type            | Notes                                                       |
| -------------- | --------------- | ----------------------------------------------------------- |
| `id`           | string (uuid)   | What `DELETE /me/sessions/{id}` takes.                      |
| `device_name`  | string \| null  | From `X-Device-Name`, or built for a browser. `null`: show "Unknown device". |
| `device_type`  | string          | `phone`, `tablet`, `browser`, `desktop` or `unknown`. Picks the icon. |
| `platform`     | string \| null  | `ios`, `android` or `web`.                                  |
| `os_version`   | string \| null  | From `X-OS-Version`.                                        |
| `app_version`  | string \| null  | From `X-App-Version`. `null` for a browser.                 |
| `ip_address`   | string \| null  | The last request's IP. Not shown today, kept for support.   |
| `location`     | string \| null  | "City, CC", else city, else country, else `null` (section 3). |
| `is_current`   | boolean         | `true` for exactly one session: the bearer token's own.     |
| `created_at`   | ISO 8601        | When this device signed in. UAE time, `+04:00`.             |
| `last_seen_at` | ISO 8601        | The last refresh or request, to the nearest 5 minutes. UAE time, `+04:00`. |

**Order:** the current session first, then the others by `last_seen_at`,
most recent first.

How the app reads a row, as in your brief:

| Shown as        | From                                                                 |
| --------------- | -------------------------------------------------------------------- |
| Title           | `device_name · location` ("iPad · Dubai, UAE"); the name alone without a location |
| Line under it   | "This device · active now" when `is_current`, else "Last active 3 days ago" from `last_seen_at` |
| Icon            | phone for `phone`, tablet for `tablet`, browser for anything else    |
| Sign Out        | every row except the current one                                     |
| Sign out of all | while there is at least one row besides the current one              |

---

## 5. DELETE /me/sessions/{id}

Signs one other device out.

```http
DELETE /api/v1/me/sessions/1b7e4d2a-0c6f-4a93-8e15-7d9b3f2c6a40
Authorization: Bearer <access_token>
```

`204 No Content`, empty body.

What it does:

- Revokes that session's refresh token and marks the session ended.
- That device's **very next request** answers `401 session_revoked`
  (section 7), not only its next refresh.
- Deletes the push tokens that session registered with
  `POST /me/device-tokens` (they are now recorded with the bearer's
  session), so a signed-out phone stops getting booking notifications. If
  the push service is down at that moment, they are deleted as soon as it
  answers again.

| Case                                          | Answer                      |
| --------------------------------------------- | --------------------------- |
| A session of this account, still active       | `204`                       |
| A session of this account that already ended  | `204`: it is out either way |
| The caller's own session                      | `409 current_session`       |
| Not a session of this account, or no such id  | `404 not_found`             |

You never offer Sign Out on the current row. The `409` guards against a
stale list and tells the caller to use Sign Out (logout) instead.

---

## 6. DELETE /me/sessions

Signs out every device except this one.

```http
DELETE /api/v1/me/sessions
Authorization: Bearer <access_token>
```

`200 OK`

```json
{
  "revoked": 2
}
```

Every other active session of the account is signed out the same way as in
section 5, push tokens included. The caller's own session is never touched.
With no other session it still answers `200` with `"revoked": 0`. You can
ignore the count.

Signing out elsewhere does not cancel bookings or touch wallet credit, which
is what the screen promises.

---

## 7. What a signed-out device sees

Any authenticated request whose session has ended answers:

`401 Unauthorized`

```json
{
  "detail": "You’ve been signed out on this device.",
  "code": "session_revoked",
  "errors": []
}
```

`POST /auth/token/refresh` with that session's refresh token answers the
same. Clear both tokens and go to sign in.

---

## 8. Errors

Same envelope as every other error in this API: `detail`, `code` and an
`errors` array, empty here because none of these belongs to a field.

| Status | `code`              | When                                   | `detail`                                 |
| ------ | ------------------- | -------------------------------------- | ---------------------------------------- |
| `401`  | `session_revoked`   | The bearer's session has ended         | You’ve been signed out on this device.   |
| `401`  | `not_authenticated` | No token, or an invalid one            | Please sign in again.                    |
| `404`  | `not_found`         | The id is not one of this account's    | That device isn’t signed in anymore.     |
| `409`  | `current_session`   | The id is the caller's own session     | Use Sign Out to leave this device.       |

`409`:

```json
{
  "detail": "Use Sign Out to leave this device.",
  "code": "current_session",
  "errors": []
}
```

`404`:

```json
{
  "detail": "That device isn’t signed in anymore.",
  "code": "not_found",
  "errors": []
}
```

`401 not_authenticated`:

```json
{
  "detail": "Please sign in again.",
  "code": "not_authenticated",
  "errors": []
}
```

### The day the switch goes on

Tokens issued before have no `sid`. While the switch is on:

- such an access token answers `401` on any call, the same `401` as an
  expired token, so your app refreshes it as it does every week;
- that refresh opens the phone's session, with the headers of that refresh,
  and hands back a token pair with a `sid`. Nobody signs in again;
- from then on every token in use has a session: `is_current` is `true` on
  exactly one row, and Sign out of all other devices reaches every device.

If your app ever answers a `401` by signing out instead of refreshing,
tell us before the switch goes on.

---

## 9. The one thing that differs, and why

Your brief says a session also starts at "OTP sign-in" and "the new password
after a reset". The rule is the same here (a session starts whenever the
API issues a token pair), but this API has no OTP sign-in, and a password
reset issues no tokens (it answers "Password reset. Please sign in."). So in
this API a session starts at login and register, and the reset revokes every
refresh token, which ends every session.

---

## 10. On the app side

Unchanged from your brief:

- `Session` type and `fetchSessions`, `deleteSession(id)` and
  `deleteOtherSessions()` in `src/helpers/auth.ts`.
- Sign-in & Security reads the list instead of its placeholders, with
  skeleton rows while it loads; its two sign-out sheets call the API.
- A response handler for `session_revoked` on every call: clear the tokens
  and go to sign in.
- `POST /auth/logout` with the refresh token when signing out.
