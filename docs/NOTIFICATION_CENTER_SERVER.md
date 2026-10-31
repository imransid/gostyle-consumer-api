# Notification Center: server steps

For: whoever deploys customer-api. The app side is
docs/NOTIFICATION_CENTER_API.md; the whole plan is
docs/NOTIFICATIONS_SETTINGS_AUDIT.md (this is its phase 2).

## What ships

One PR, `feat/notification-center`, base `main`.

- **Migration `notifications/0001`**: two new tables, `notification` (the
  customer's Notification Center) and `customer_notification_delivery` (one
  row per channel to send, worked off by the worker). Nothing else changes in
  the database. Runs when the container starts. An older image ignores both
  tables.
- **Five routes** under `/api/v1/notifications` (list, one, read, read-all,
  unread-count), `404` until the switch is on.
- **A new process, `notification_worker`**: same image, one more service.
  It sends the queued pushes through the push service, retries with backoff
  (30 s, 2 min, 10 min, 30 min, then gives up), and deletes items older than
  180 days once an hour. It writes one log line a minute:
  `notification queue: pending=0 oldest_due_seconds=0 sent=... failed=...`.
- **Switch `NOTIFICATION_CENTER_V1`, off.** Read only by the `api` service.
  On, the sign-in alert's push goes through the Notification Center (kept,
  then sent by the worker) instead of straight to the push service.
- No new package. No change to booking-api or the push service.

## Steps (in this order)

1. Keep the running image to roll back to:

   ```sh
   docker tag gostyle-consumer-api:local gostyle-consumer-api:before-notification-center
   ```

2. Build and roll out, **both** services (the gRPC one runs the same code):

   ```sh
   cd ~/gostyle-customer && git pull
   docker build -t gostyle-consumer-api:local .
   docker service update --force --image gostyle-consumer-api:local gostyle-consumer_api
   docker service update --force --image gostyle-consumer-api:local gostyle-consumer_grpc
   ```

3. Create the worker service, once. It takes the gRPC service's settings
   (database, secret key, logging) plus the api's push settings, read
   straight from the running services, so no secret is typed or written to
   disk:

   ```sh
   docker service create --name gostyle-consumer_worker \
     --network gostyle_gostyle-net \
     --env-file <( {
         docker service inspect gostyle-consumer_grpc --format '{{range .Spec.TaskTemplate.ContainerSpec.Env}}{{println .}}{{end}}'
         docker service inspect gostyle-consumer_api --format '{{range .Spec.TaskTemplate.ContainerSpec.Env}}{{println .}}{{end}}' | grep -E '^PUSH_API_(URL|KEY)='
       } | grep -vE '^(INTERNAL_GRPC_KEY|DEVICE_SESSIONS_V1)=' | grep -v '^$' ) \
     --restart-condition on-failure \
     --update-order stop-first \
     --label com.docker.stack.namespace=gostyle-consumer \
     --container-label com.docker.stack.namespace=gostyle-consumer \
     gostyle-consumer-api:local python manage.py notification_worker
   ```

   Later deploys update it like the others:
   `docker service update --force --image gostyle-consumer-api:local gostyle-consumer_worker`.

4. Check it runs (a "started" line, then a queue line within a minute):

   ```sh
   docker service logs --since 3m gostyle-consumer_worker 2>&1 | grep -E "notification (worker|queue)" | tail -5
   ```

5. Turn the switch on:

   ```sh
   docker service update --env-add NOTIFICATION_CENTER_V1=true gostyle-consumer_api
   ```

   `--env-add` lives on the service only. Add `NOTIFICATION_CENTER_V1=true`
   to `/root/gostyle-customer/stack.env` as well, or a `docker stack deploy`
   turns it off again.

## How to check it on live

1. Sign in to a test account from a phone or device it has not used (that
   makes a new sign-in alert; it needs `DEVICE_SESSIONS_V1` on and the
   account's sign-in alerts on, as before).
2. With that account's token:

   ```sh
   curl -s https://api.gostyle.uk/api/v1/notifications/unread-count -H "Authorization: Bearer $TOKEN"
   curl -s https://api.gostyle.uk/api/v1/notifications -H "Authorization: Bearer $TOKEN"
   ```

   Expect `total: 1` and one `account.new_sign_in` item.
3. The account's other phones get the "New sign-in to your account" push.
   In the push service the row's `event_id` is now `ntf:<the item's id>`:

   ```sh
   docker exec push-notification-service-db-1 psql -U postgres -d push -c \
   "select event_id, status, created_at from notifications order by created_at desc limit 5;"
   ```

4. In the Django admin, Notifications shows the item and its push row
   (status `sent`).

## Roll back

- Switch only (the center's routes go back to `404`, the sign-in push goes
  straight to the push service again):
  `docker service update --env-rm NOTIFICATION_CENTER_V1 gostyle-consumer_api`.
- Whole release: the image from step 1 on `gostyle-consumer_api` and
  `gostyle-consumer_grpc`, then `docker service rm gostyle-consumer_worker`.
  The two tables stay; the older image does not read them.

---

# Phase 3: channels and reminder time

One PR, `feat/notification-channels`, base `main`. App side:
docs/NOTIFICATION_PREFERENCES_API.md.

## What ships

- **Migration `accounts/0028`**: three columns on `notification_preference`,
  `email` (`DEFAULT true`), `sms` (`DEFAULT false`), `reminder_lead_minutes`
  (`DEFAULT 1440`, a check for 30, 60, 120 or 1440). No backfill. An older
  image runs fine on top of it.
- **`/notifications/preferences`** takes and answers them, behind two new
  switches, both off:
  - `NOTIFICATION_CHANNELS_V1` (on `api` and `grpc`): `email`, `sms`,
    `channels_available`, and email as a Notification Center channel (the
    worker sends it through SendGrid, to verified addresses only). SMS is
    stored, never sent.
  - `REMINDER_TIMING_V1` (on `api`): `reminder_lead_minutes`.
- **gRPC**: a new service on the same `:50051`, `CustomerNotifications`
  with `GetReminderLeads` (booking-api's ladder, phase 5), and
  `email_enabled` on `GetConsumerContact`. Same `INTERNAL_GRPC_KEY`.
- Every error item on the preferences endpoint now has a `code`.

## Steps

```sh
docker tag gostyle-consumer-api:local gostyle-consumer-api:before-notification-channels
cd ~/gostyle-customer && git pull && git log --oneline -1
docker build -t gostyle-consumer-api:local .
docker service update --force --image gostyle-consumer-api:local gostyle-consumer_api
docker service update --force --image gostyle-consumer-api:local gostyle-consumer_grpc
docker service update --force --image gostyle-consumer-api:local gostyle-consumer_worker
```

All three services: `_grpc` serves the new call, `_worker` sends email.

## When to turn the switches on

- `NOTIFICATION_CHANNELS_V1`: after booking-api honours `email_enabled` for
  its reminder emails (booking-api `feat/reminder-email-switch`), so an
  Email switch turned off in the app is never ignored. Then on `api` and
  `grpc`, and in `stack.env`.
- `REMINDER_TIMING_V1`: only with phase 5 (booking-api reading the time).
  On before that, a customer could pick a time nothing uses.

## Roll back

The image from the first line on all three services. The three columns
stay; the older image does not read them.
