# Where you're signed in: server steps

For: whoever deploys customer-api. The app side is docs/DEVICE_SESSIONS_API.md.

## What ships

- Migrations `accounts/0023_user_sessions` and `0024`: one NEW table,
  `user_sessions` (the brief's table). Nothing else changes in the database,
  so an older image runs fine on top of it. They run when the container
  starts.
- push-notification-service, branch `feat/device-session-id`: its `devices`
  table (the brief's `device_tokens`) gets a nullable `session_id`, set by
  `PUT /devices`, and `POST /devices/unregister-session` deletes a session's
  devices. The column is added when the service starts. Until it is
  deployed, customer-api's `sessionId` is dropped unread there and Sign Out
  cannot delete push tokens, so deploy it before turning the switch on.
- A new Python package, `maxminddb` (requirements.txt), so the image must be
  rebuilt.
- Switch `DEVICE_SESSIONS_V1`, off by default. Off, the three new routes are
  404 and login, register, refresh, logout and the password reset are exactly
  what they were. It must be set on BOTH services: `gostyle-consumer_api`
  (the routes and the check) and `gostyle-consumer_grpc` (VerifyConsumer
  refuses a signed-out device's token).
- Setting `GEOIP_CITY_DB`: the path of the GeoLite2 City file inside the api
  container. Empty, every session's `location` is `null` and nothing fails.
- Setting `PROXY_SECRET`: the value host nginx sends as `X-Proxy-Secret`. The
  app reads the client's IP from `X-Forwarded-For` only when the request
  carries it, so a request sent straight to port 3850 cannot choose its own
  IP. Empty, every session's IP is the container's socket address (no
  place). Step 2 sets it up.

## 1. Deploy with the switch off

The usual customer-api deploy: tag the running image first, so there is
something to roll back to.

```sh
docker tag gostyle-consumer-api:local gostyle-consumer-api:before-sessions
cd ~/gostyle-customer && git pull
docker build -t gostyle-consumer-api:local .
docker service update --force --image gostyle-consumer-api:local gostyle-consumer_api
docker service update --force --image gostyle-consumer-api:local gostyle-consumer_grpc
```

Then check the app still logs in, refreshes and books as before.

Then the push service, where it runs on the server (docker compose): pull
`feat/device-session-id` (or main once its PR is merged) and rebuild it as
usual, e.g. `git pull && docker compose up -d --build`. Check:

```sh
docker compose exec db psql -U postgres -d push -c '\d devices' | grep session_id
```

## 2. The proxy secret (the real client IP)

On the host, as root:

1. Write the nginx snippet once, with a random secret:

   ```sh
   install -d -m 755 /etc/nginx/snippets
   ( umask 077; printf 'proxy_set_header X-Proxy-Secret "%s";\n' "$(openssl rand -hex 32)" \
       > /etc/nginx/snippets/gostyle-proxy-secret.conf )
   ```

   (`scripts/setup-nginx.sh` does the same when the file is missing.)
2. Install the new site config, which includes it, and reload:

   ```sh
   cp ~/gostyle-customer/nginx/api.gostyle.uk.conf /etc/nginx/sites-available/api.gostyle.uk
   nginx -t && systemctl reload nginx
   ```

3. Give the app the same value:

   ```sh
   SECRET=$(sed -n 's/.*X-Proxy-Secret "\(.*\)";/\1/p' /etc/nginx/snippets/gostyle-proxy-secret.conf)
   docker service update --env-add PROXY_SECRET=$SECRET gostyle-consumer_api
   ```

   and add `PROXY_SECRET=<the value>` to `/root/gostyle-customer/stack.env`,
   or a stack deploy drops it.

## 3. The GeoIP file (for "iPhone 15 · Dubai, UAE")

Free, from MaxMind. Once:

1. Sign up for GeoLite2 at maxmind.com, then create a license key
   (Account, Manage License Keys).
2. Install `geoipupdate` on every node that can run the api service
   (Ubuntu: `sudo add-apt-repository ppa:maxmind/ppa && sudo apt install geoipupdate`).
3. Write `/etc/GeoIP.conf`:

   ```
   AccountID <your account id>
   LicenseKey <your license key>
   EditionIDs GeoLite2-City
   DatabaseDirectory /var/lib/GeoIP
   ```

4. Download it: `sudo geoipupdate`, then check
   `ls -l /var/lib/GeoIP/GeoLite2-City.mmdb`.
5. Keep it fresh, monthly, as the brief says (MaxMind publishes weekly):

   ```sh
   echo '17 4 1 * * root /usr/bin/geoipupdate' | sudo tee /etc/cron.d/geoipupdate
   ```

   The api notices the new file by itself; no restart.

Then give it to the api, read-only:

```sh
docker service update \
  --mount-add type=bind,source=/var/lib/GeoIP,target=/geoip,readonly \
  --env-add GEOIP_CITY_DB=/geoip/GeoLite2-City.mmdb \
  gostyle-consumer_api
```

Like the switches, `--mount-add` lives on the service only: a
`docker stack deploy` drops it. To keep it, add to the `api` service in
docker-compose.yml (only once the file exists on the node, or the stack
deploy fails):

```yaml
    volumes:
      - /var/lib/GeoIP:/geoip:ro
```

and `GEOIP_CITY_DB=/geoip/GeoLite2-City.mmdb` to stack.env.

## 4. Turn it on

```sh
docker service update --env-add DEVICE_SESSIONS_V1=True gostyle-consumer_api
docker service update --env-add DEVICE_SESSIONS_V1=True gostyle-consumer_grpc
```

Also add `DEVICE_SESSIONS_V1=True` to `/root/gostyle-customer/stack.env`, or
a stack deploy turns it off again.

**What happens then:** every access token issued before has no session, and
answers `401` like an expired token. The app refreshes it (as it does every
week) and the refresh opens the phone's session. So in the first minutes
there are many refreshes and one 401 per phone in the logs. Check with the
mobile team first that their app refreshes on a 401 rather than signing out.

## 5. Live proof

With a test account. Each login below opens one session.

1. Log in as "phone 1", keep its `access` as A:

   ```sh
   curl -s https://api.gostyle.uk/api/v1/auth/login -H 'Content-Type: application/json' \
     -H 'X-Device-Id: live-test-1' -H 'X-Device-Name: Test Phone' \
     -H 'X-Device-Type: phone' -H 'X-Platform: ios' \
     -d '{"destination_type":"email","destination":"<email>","password":"<password>"}'
   ```

2. The same with `X-Device-Id: live-test-2` and `X-Device-Name: Test Tablet`,
   `X-Device-Type: tablet`; keep its `access` as B.
3. `curl -s https://api.gostyle.uk/api/v1/me/sessions -H "Authorization: Bearer A"`:
   two rows, Test Phone first with `is_current: true`, `ip_address` your own
   public IP (step 2) and `location` a city (step 3).
4. Sign the tablet out from the phone, with the tablet row's `id`:
   `curl -s -o /dev/null -w '%{http_code}\n' -X DELETE https://api.gostyle.uk/api/v1/me/sessions/<id> -H "Authorization: Bearer A"`
   answers `204`.
5. `curl -s https://api.gostyle.uk/api/v1/auth/me -H "Authorization: Bearer B"`
   answers `401` with `"code": "session_revoked"`.

In the database:

```sql
SELECT device_name, location, last_seen_at, ended_at
FROM consumer.user_sessions ORDER BY created_at DESC LIMIT 10;
```

## When location is null

One command, inside the running api container, says why:

```sh
docker exec $(docker ps -q -f name=gostyle-consumer_api | head -1) python manage.py session_locations
```

It prints `NUM_PROXIES`, whether `PROXY_SECRET` is set, which GeoIP file
it reads and how old it is, and how many live sessions have a public IP, a
private one or none. It changes nothing. What it points at:

- **`GEOIP_CITY_DB: not set`, or `cannot be read`**: step 3. The file must
  be on the node and mounted (`ls -l /var/lib/GeoIP/`).
- **`PROXY_SECRET: not set`, or sessions with a private IP** (10.x): the app
  only sees Docker's own address, which has no place. Step 2: the nginx
  snippet, the site's `include` of it, and the same value in
  `PROXY_SECRET`.
- **`The GeoIP file is old`**: the monthly `geoipupdate` cron (step 3).

Once both are right, every live session gets its place by itself at its
next refresh or `last_seen_at` update (5 minutes of use). To do it now, for
every live session:

```sh
docker exec $(docker ps -q -f name=gostyle-consumer_api | head -1) python manage.py session_locations --apply
```

Since 2026-10-30 a place reads "Dubai, UAE" (city and short country code,
docs/DEVICE_SESSIONS_API.md section 3). A session saved as "Dubai" before
that is redone in the new words the same way.

## Rollback

Switch off on both services (`--env-add DEVICE_SESSIONS_V1=False`), or go
back to the tagged image:
`docker service update --force --image gostyle-consumer-api:before-sessions gostyle-consumer_api`
(and the same for `gostyle-consumer_grpc`). The table can stay, and so can
the push service's `session_id` column.

Off again after being on: a signed-out device's access token works again
until it expires (7 days at most); its refresh token stays blocked.

## Good to know

- **The client IP** is the right-most X-Forwarded-For entry, which host nginx
  appends (nginx/api.gostyle.uk.conf), the same one the login throttle uses
  (`DJANGO_NUM_PROXIES=1`), and only with the right `X-Proxy-Secret`. If a
  CDN such as Cloudflare is ever put in front of nginx, `DJANGO_NUM_PROXIES`
  must become 2.
- **The table** is the brief's: `user_sessions`, the same columns, defaults,
  CHECKs, indexes and `ON DELETE CASCADE` (migration 0024), plus
  `refresh_token_hash` (the brief: store the refresh token, or its hash).
