# 2-step verification: server steps

For: whoever deploys customer-api. The app side is docs/TWO_FACTOR_API.md.

## What ships

Two PRs, both on base `main`. PR A merges first.

- **PR A, `feat/two-factor`: the Sign-in & Security routes.**
  - Migrations `accounts/0025_two_factor` and `0026`: two columns on
    `consumer_account` (`two_factor_enabled` with `DEFAULT false`,
    `two_factor_changed_at`), one on `user_sessions`
    (`two_factor_passed_at`), and four NEW tables: `user_authenticators`,
    `verification_codes`, `verification_tokens`, `login_challenges`. An older
    image runs fine on top of them. They run when the container starts.
  - Two new Python packages, `pyotp` and `cryptography` (requirements.txt):
    the image must be rebuilt.
  - The 7 `/me/security` routes.
  - Commit `2cbfe63`, on its own: the code email said "expires in 5
    minutes" while codes last 10. **Every code email (register, login,
    password reset, contact change) says 10 minutes from this deploy on**,
    with no switch. `git revert 2cbfe63` undoes only that.
- **PR B, `feat/two-factor-sign-in`: the sign-in challenge.** `POST
  /auth/login` answers a challenge for an account with 2-step on,
  `POST /auth/2fa/send` and `/auth/2fa/verify`, account purge clears 2-step,
  and these two docs. No migration.
- **PR C, `fix/no-app-version-gate`: no app version check** (the mobile
  team's ask, 2026-10-28). No call answers `426 app_update_required` any
  more, and sign-in for an account with 2-step on always answers the
  challenge, from any client. The setting `TWO_FACTOR_MIN_APP_VERSION` is
  gone. No migration.
- **Settings**, on `gostyle-consumer_api` only (the gRPC service does not
  read them):
  - `TWO_FACTOR_V1`, off by default. Off, every new route is `404` and
    sign-in is what it was. It works only with `DEVICE_SESSIONS_V1` on too.
  - `TWO_FACTOR_ENCRYPTION_KEY`: the key the authenticator secrets are
    encrypted with. Missing, the authenticator calls answer `503` and log
    it; email and phone codes still work.

## Never run PR A with TWO_FACTOR_V1 on

PR A lets a customer turn 2-step on, but only PR B makes sign-in ask for the
second code. With PR A alone and the switch on, a customer could believe
their account is protected while every sign-in still answers tokens straight
away. So:

- Keep `TWO_FACTOR_V1` off until PR B is deployed.
- **Rolling back from PR B to PR A: turn `TWO_FACTOR_V1` off first.**

## Deploy order

1. **PR A merged: deploy it with the switches off.** Tag the running image
   first, so there is something to roll back to:

   ```sh
   docker tag gostyle-consumer-api:local gostyle-consumer-api:before-two-factor
   cd ~/gostyle-customer && git pull
   docker build -t gostyle-consumer-api:local .
   docker service update --force --image gostyle-consumer-api:local gostyle-consumer_api
   docker service update --force --image gostyle-consumer-api:local gostyle-consumer_grpc
   ```

   Check that login, refresh and booking work as before, and that a code
   email now says 10 minutes. `/api/v1/me/security` answers `404`.
2. **PR B rebased onto main and merged: deploy it the same way**, tagging
   `before-two-factor-sign-in`. The switches stay off. Check login again.
3. **`DEVICE_SESSIONS_V1` must be on**, on both services
   (docs/DEVICE_SESSIONS_SERVER.md). Check:

   ```sh
   docker service inspect gostyle-consumer_api --format '{{json .Spec.TaskTemplate.ContainerSpec.Env}}' | tr ',' '\n' | grep -E 'DEVICE_SESSIONS|TWO_FACTOR'
   ```

4. **Make the key and set it** (next section).
5. **Turn `TWO_FACTOR_V1` on.** From then on, a customer can turn 2-step on
   from an app that has the screens.

   ```sh
   docker service update --env-add TWO_FACTOR_V1=True gostyle-consumer_api
   ```

6. Live proof (below).

Like every switch, `--env-add` lives on the service only: also add
`TWO_FACTOR_V1` and `TWO_FACTOR_ENCRYPTION_KEY` to
`/root/gostyle-customer/stack.env`, or a `docker stack deploy` drops them
(docker-compose.yml passes both through).

### PR C on a server that ran PR B

Deploy it the same way, tagging `before-no-app-version-gate`. The switches
stay as they are. Then take the old setting off the service and out of
`/root/gostyle-customer/stack.env`. Nothing reads it any more, so this is
only tidying:

```sh
docker service update --env-rm TWO_FACTOR_MIN_APP_VERSION gostyle-consumer_api
```

Rolling back to PR B's image brings the `426` back, and with no minimum set
a customer with 2-step on cannot sign in at all. So turn `TWO_FACTOR_V1` off
first (Rollback, below).

## The key

32 random bytes in base64. Make it once:

```sh
openssl rand -base64 32
```

Set it, and add `TWO_FACTOR_ENCRYPTION_KEY=<the value>` to stack.env:

```sh
docker service update --env-add TWO_FACTOR_ENCRYPTION_KEY=<the value> gostyle-consumer_api
```

**Keep a copy somewhere safe (a password manager) and never change it**
once a customer has added an authenticator. Changed or lost, no secret opens
any more: every authenticator call, and signing in by authenticator, answer
`503 authenticator_unavailable` and log `does not open with
TWO_FACTOR_ENCRYPTION_KEY`. Email and phone codes keep working. The fix is
the old key; without it, each customer removes the authenticator (with an
email or phone code) and adds it again.

## Rollback

- **Turn `TWO_FACTOR_V1` off.** The new routes answer `404` and sign-in is
  what it was for everyone, including accounts with 2-step on: while it is
  off they sign in **without** the second code. Nothing is lost; turned on
  again, those accounts are asked again.
- **The image:** `docker service update --force --image
  gostyle-consumer-api:before-two-factor gostyle-consumer_api` (and
  `gostyle-consumer_grpc`). The migrations can stay: the new column has a
  database default, and an older image never reads the new tables. Going
  back to PR A's image: switch off first (above).

## Live proof

After step 5, with a test account that has a verified email. One call at a
time. The logins send an `X-Device-Id`.

1. Sign in as "phone 1" (`X-Device-Id: live-2fa-1`), keep its `access` as A.
2. `GET /api/v1/me/security` with A: `two_factor_enabled: false`, the email
   available with its hint.
3. `POST /api/v1/me/security/verify/send` with A,
   `{"method": "email", "purpose": "two_step_on"}`: `200`, and the email
   "Your Go Style security code" arrives.
4. `POST /api/v1/me/security/verify`, `{"method": "email", "purpose":
   "two_step_on", "code": "<the code>"}`: keep `verification_token` as T.
5. `PATCH /api/v1/me/security/two-factor`, `{"enabled": true,
   "verification_token": "T"}`: `{"two_factor_enabled": true}`, and the email
   "Go Style: 2-step verification turned on" arrives.
6. Sign in as "phone 2" (`X-Device-Id: live-2fa-2`): `200` with
   `"two_factor_required": true`. Keep `challenge_token` as C.
7. `POST /api/v1/auth/2fa/send`, `{"challenge_token": "C", "method":
   "email"}`: the email "Your Go Style sign-in code".
8. `POST /api/v1/auth/2fa/verify`, with `X-Device-Id: live-2fa-2`,
   `{"challenge_token": "C", "method": "email", "code": "<the code>"}`:
   `access` and `refresh`.
9. Sign in as "phone 2" again: the tokens at once (it is remembered).
10. Sign in as "phone 3" with no `X-App-Version` and no `X-Device-Id`, as a
    browser does: `200` with `"two_factor_required": true`, not `426`.
11. Turn it off again: 3 and 4 with `"purpose": "two_step_off"`, then 5 with
    `"enabled": false`.

In the database:

```sql
SELECT two_factor_enabled, two_factor_changed_at
FROM consumer.consumer_account WHERE email = '<email>';

SELECT device_id, device_name, two_factor_passed_at, ended_at
FROM consumer.user_sessions
WHERE user_id = (SELECT id FROM consumer.consumer_account WHERE email = '<email>')
ORDER BY created_at DESC LIMIT 5;
```

## Known risks, on record

- **A remembered device is recognised by its `X-Device-Id` header alone**,
  as the brief designs it, and any client can send any header. Someone who
  has the customer's password AND knows the exact `X-Device-Id` of one of
  their phones that has passed 2-step and is still signed in would sign in
  without the second code. The id is not shown anywhere in the API, and
  signing that phone out ends it. Rafa decided on 2026-10-28 to keep the
  brief's design. Closing it means remembering a secret the app keeps
  instead of the device id.
- **Someone with the password can spend the 5 wrong codes** and block the
  owner's codes for 15 minutes. That is the brief's per-account limit.
- **A web browser cannot sign in to an account with 2-step on** until the
  web app shows the challenge screen: it gets the challenge like any client.
- **An account with no verified email gets no notice emails.** WhatsApp
  needs an approved template for them.
- **Phone codes use the existing WhatsApp OTP template**
  (`WHATSAPP_TEMPLATE_NAME`), so their text is whatever Meta approved for it.

## Good to know

- **Limits**: one send per 30 seconds per purpose, 10 an hour and 20 a day
  per account, 20 an hour per IP (the sessions client IP, from nginx only);
  5 wrong codes per 15 minutes per account. They live in Redis.
- **A customer locked out of everything** (no email, no phone, no
  authenticator): nothing in the admin does this yet. Once support has
  proved who they are, in the database:

  ```sql
  UPDATE consumer.consumer_account
  SET two_factor_enabled = false, two_factor_changed_at = now()
  WHERE email = '<email>';
  ```

- **The tables** are the brief's four, column for column, each `ON DELETE
  CASCADE` (migration 0026), in the `consumer` schema. Codes and tokens are
  stored as hashes, the authenticator secret encrypted (AES-GCM, the account
  id bound in).
