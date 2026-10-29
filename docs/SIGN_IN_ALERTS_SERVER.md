# Password age and sign-in alerts: server steps

For: whoever deploys customer-api. The app side is docs/SIGN_IN_ALERTS_API.md.

## What ships

One PR, `feat/sign-in-alerts`, base `main`.

- **Migration `accounts/0027`**: two columns on `consumer_account`,
  `password_changed_at` (null) and `sign_in_alerts` (`DEFAULT true`), and a
  backfill: every account with a password gets its last used password reset
  token's time, else its `created_at`. Runs when the container starts. An
  older image runs fine on top of it.
- **`GET /me/security`** answers both, and **`PATCH /me/security`** changes
  `sign_in_alerts`.
- **The new sign-in alert** (it existed: an email since the SendGrid PR):
  - the switch turns it on and off;
  - a "new device" is now one with no live session for its `X-Device-Id`,
    or no `X-Device-Id` (every web sign-in). Before, it was a device the
    account had never used. Commit `f1cf4f3`, on its own: `git revert
    f1cf4f3` brings the old rule back;
  - it goes by push too, through the push service.
- **No new setting, no new package.**

## Which switches it rides

| What | Needs |
| --- | --- |
| `GET` and `PATCH /me/security` | `TWO_FACTOR_V1` and `DEVICE_SESSIONS_V1` on, as every `/me/security` route. Off: `404`. |
| The new sign-in alert (email and push) | `DEVICE_SESSIONS_V1` on. Off: no alert at all, as today. |
| The push | `PUSH_API_KEY` set on `gostyle-consumer_api`. Empty: email only. |

So with `DEVICE_SESSIONS_V1` on, the deploy itself changes who gets the
alert (more: every web sign-in, a phone signing in again after a sign out)
and adds the push. Every account starts with alerts on.

Check what is set (the key's value is cut to its first letter):

```sh
docker service inspect gostyle-consumer_api --format '{{json .Spec.TaskTemplate.ContainerSpec.Env}}' | tr ',' '\n' | grep -oE '(DEVICE_SESSIONS_V1|TWO_FACTOR_V1|PUSH_API_URL)=[^"]*|PUSH_API_KEY=.'
```

## Deploy

After the PR is merged into `main`. Pull first: the server folder may be
behind.

```sh
docker tag gostyle-consumer-api:local gostyle-consumer-api:before-sign-in-alerts
cd ~/gostyle-customer && git pull
docker build -t gostyle-consumer-api:local .
docker service update --force --image gostyle-consumer-api:local gostyle-consumer_api
docker service update --force --image gostyle-consumer-api:local gostyle-consumer_grpc
```

Check that login and refresh work as before, then the backfill: no account
with a password is left without a time.

```sql
SELECT count(*) FILTER (WHERE password_changed_at IS NULL) AS no_time,
       count(*) FILTER (WHERE password_changed_at <> created_at) AS from_a_reset,
       count(*) AS with_a_password
FROM consumer.consumer_account
WHERE password NOT LIKE '!%' AND password <> '';
```

`no_time` must be 0.

## The push

A sign-in that alerts calls the push service (`POST /notifications/user`)
before it answers, with `PUSH_API_TIMEOUT` (3 seconds by default). A push
service that is down slows such a sign-in by up to that, and logs `new
sign-in push not sent`. It never fails the sign-in. A refusal (a wrong key)
logs `push service refused the new sign-in push (401)`.

## Rollback

`docker service update --force --image
gostyle-consumer-api:before-sign-in-alerts gostyle-consumer_api` (and
`gostyle-consumer_grpc`). The migration can stay: the old code never reads
the two columns, and `sign_in_alerts` has a database default. Back on the
old image, the alert is the old one again (email only, old rule, no
switch).

## Live proof

With `TWO_FACTOR_V1` and `DEVICE_SESSIONS_V1` on, and a test account with a
verified email. One call at a time.

1. Sign in (`POST /api/v1/auth/login`) with a made-up `X-Device-Id`:
   the account's email gets "Go Style: new sign-in to your account".
2. `GET /api/v1/me/security`: `password_changed_at` is a `+04:00` time,
   `sign_in_alerts` is `true`.
3. `PATCH /api/v1/me/security` with `{"sign_in_alerts": false}`: `200`,
   `sign_in_alerts: false`, and the email "Go Style: sign-in alerts turned
   off".
4. Sign in again with another made-up `X-Device-Id`: no email.
5. `PATCH` with `{"sign_in_alerts": true, "two_factor_enabled": false}`:
   `422`, `read_only` on `two_factor_enabled`, and `GET` still says
   `false` (nothing saved).
6. `PATCH` with `{"sign_in_alerts": true}`: `200`, no email.
