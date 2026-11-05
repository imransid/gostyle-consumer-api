# Google sign-in: server steps

For: whoever deploys customer-api. The app side is docs/GOOGLE_SIGN_IN_API.md.

## What ships

One PR, `feat/google-sign-in`, base `main`.

- **`POST /auth/google`**: sign in or sign up with a Google ID token. One
  account for both ways in (linked by Google's `sub`, matched by a verified
  email).
- **`POST /auth/password/set`**: an account made with Google adds a
  password, after Verify it's you with the new purpose `password_set`.
- **`/auth/me`**: `has_password`, `sign_in_methods`, `google_email`.
- **`POST /auth/login`**: `401 password_not_set` for an account made with
  Google that has no password.
- **`POST /auth/me/delete`**: `id_token` in place of `password`.
- **One switch, `GOOGLE_SIGN_IN_V1`**, off by default. Off, nothing changes
  on deploy: the two new routes are `404`, `/auth/me` has no new field,
  login and delete are what they are today.
- **One migration, `accounts/0032_google_sign_in`**, additive: a new table
  `social_identity` (the Google links). The other two operations only add
  the purpose `password_set` to Django's list of choices: no SQL. It runs
  when the container starts.
- **No new package, no secret, no setting to fill.** The two Google client
  IDs from the mobile team (web `...g4grmqc4...`, iOS `...ks6giaqd...`; the web
  one corrected on 2026-11-05) are the default of `GOOGLE_CLIENT_IDS`, and
  the same value is set on the api service and in stack.env. An ID
  token is checked with Google's public keys only (PyJWT, already in), so
  no client secret exists for us to keep.
- **The server must reach `https://www.googleapis.com`** to read Google's
  public keys (once an hour per worker process).

## Before you turn it on

1. **Setting a password needs 2-step's switches.** Its code comes from
   Verify it's you, which works only while `TWO_FACTOR_V1` and
   `DEVICE_SESSIONS_V1` are both on (api service). Without them, Google
   sign-in works, but "Set a password" cannot get its code (Forgot password
   still adds one).

   ```sh
   docker service inspect gostyle-consumer_api --format '{{json .Spec.TaskTemplate.ContainerSpec.Env}}' | tr ',' '\n' | grep -oE '(TWO_FACTOR_V1|DEVICE_SESSIONS_V1|GOOGLE_SIGN_IN_V1)=[^"]*'
   ```

2. **Google's keys are reachable from the api container** (after the
   deploy below). Must print `200`:

   ```sh
   docker exec $(docker ps -q -f name=gostyle-consumer_api | head -1) python -c "import urllib.request; print(urllib.request.urlopen('https://www.googleapis.com/oauth2/v3/certs', timeout=10).status)"
   ```

## Deploy

After the PR is merged into `main`. Pull first: the server folder may be
behind.

```sh
docker tag gostyle-consumer-api:local gostyle-consumer-api:before-google-sign-in
cd ~/gostyle-customer && git pull
docker build -t gostyle-consumer-api:local .
docker service update --force --image gostyle-consumer-api:local gostyle-consumer_api
docker service update --force --image gostyle-consumer-api:local gostyle-consumer_grpc
docker service update --force --image gostyle-consumer-api:local gostyle-consumer_worker
```

All three, so they run the same code: the worker's day-30 purge now also
deletes the account's Google link.

Rollback: the same three lines with `gostyle-consumer-api:before-google-sign-in`.
The new table stays; the old code never reads it.

## Turn it on

On the service, and in stack.env the same day, so a stack deploy keeps it:

```sh
docker service update --env-add GOOGLE_SIGN_IN_V1=True gostyle-consumer_api
cd /root/gostyle-customer && grep -q '^GOOGLE_SIGN_IN_V1=' stack.env || echo "GOOGLE_SIGN_IN_V1='True'" >> stack.env
bash scripts/stack-env-check.sh
```

The check must print only `check done`.

To turn it off: `--env-add GOOGLE_SIGN_IN_V1=False` and the same line in
stack.env. **Know this first:** while it is off, an account made with
Google that has not set a password cannot sign in at all (no Google route,
no password). Its link stays and works again when the switch is back on.

## Live checks

With the switch on. A made-up token must answer `401 invalid_token`:

```sh
curl -s -X POST https://api.gostyle.uk/api/v1/auth/google -H 'Content-Type: application/json' -d '{"id_token": "not-a-token"}'
```

```json
{"detail": "We couldn’t sign you in with Google. Please try again.", "code": "invalid_token", "errors": []}
```

A real sign-in needs a real ID token for our client IDs, so it comes from
the app: ask the mobile team to sign in once with a test Gmail. Then:

```sh
docker exec -it $(docker ps -q -f name=gostyle-consumer_api | head -1) python manage.py shell -c "from apps.accounts.models import SocialIdentity as S; print([(s.email, s.account_id, s.created_at) for s in S.objects.order_by('-created_at')[:5]])"
```

The test Gmail must be there, and `GET /auth/me` with that sign-in's token
answers `"sign_in_methods": ["google"]`.

The logs name each refused token by its error class only (never the
token): `google sign-in: token refused: InvalidAudienceError`. Many of
those after the app's release means the app asks Google for the wrong
client ID. `google sign-in: Google's keys could not be read` means the
server cannot reach googleapis.com (step 2 above).

## Checked locally (2026-11-05)

`apps.accounts.tests.test_google_sign_in`: 59 tests, with tokens signed by a
test RSA key in place of Google's. Full suite: 2794 tests, all pass.
