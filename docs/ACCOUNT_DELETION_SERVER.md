# Account Deletion: Server Steps

Date: 2026-11-03. For: whoever deploys customer-api.
The app side is `docs/MOBILE_ACCOUNT_DELETION_FE.md`.

## What this release changes

- New `GET /api/v1/auth/me/delete/summary` (the Warning screen).
- `POST /api/v1/auth/me/delete` also answers `confirmation_sent_to`.
- A second delete request keeps the first date.
- The purge (`purge_due_accounts`) now cancels the account's bookings still
  to come **before** it deletes the account, then also deletes its
  Notification Center items.
- The notification worker runs the purge every hour, **only** while
  `ACCOUNT_PURGE_V1` is on. It is **off** by default.

No migration. No booking-api change: the bookings are cancelled through
booking-api's existing customer routes (the same ones the app's Cancel
buttons use), with a token the server makes for the account for a few
seconds. Nothing the business web uses is touched.

## How the day 30 purge works

For each account whose `purge_at` has passed (and `deleted_at` is null):

1. Open a short server session for the account (device name "Account
   deletion") and make an access token. No sign-in alert is sent.
2. Read the account's Recurring and Upcoming lists from booking-api, then
   cancel: each active or paused routine (reason `OTHER`), each single
   booking (reason "Cancelled because the customer deleted their
   account."), and each party the account booked.
3. Left as it is, and logged: a place in someone else's party, a routine
   visit whose routine would not end, anything booking-api refuses (`4xx`).
4. End the server session.
5. Purge the account (`ConsumerAccount.purge()`), as before.

booking-api not reachable, a `5xx`, or a list it will not give: the
account is **not** purged on this run and is tried again on the next. After
3 days past its date it is purged anyway, and the log says its bookings were
not cancelled.

Log lines to look for (logger `apps.salons.account_deletion` and
`apps.accounts.deletion`):

- `account deletion: <kind> <id> of account <id> left as it is: <why>`
- `account deletion: booking-api could not be asked for <id>, the purge waits for the next run`
- `account deletion: purged=1 waiting=0 failed=0 bookings_cancelled=2 bookings_left=0` (worker, once an hour when something happened)

## Deploy

customer-api is built on the server. Run on the Swarm node:

```bash
cd /root/gostyle-customer && git pull

# Something to roll back to.
docker tag gostyle-consumer-api:local gostyle-consumer-api:before-account-deletion

docker build -t gostyle-consumer-api:local .

docker service update --force --image gostyle-consumer-api:local gostyle-consumer_api
# The gRPC servicer imports apps/accounts (models, sessions), so it is
# updated too, to keep one code version everywhere.
docker service update --force --image gostyle-consumer-api:local gostyle-consumer_grpc
docker service update --force --image gostyle-consumer-api:local gostyle-consumer_worker
```

### Check the API

Log in at `https://api.gostyle.uk/api/v1/auth/login` with a test customer,
then:

```bash
curl -s https://api.gostyle.uk/api/v1/auth/me/delete/summary \
  -H "Authorization: Bearer $TOKEN"
```

Expect `200` with `purge_after_days`, `purge_at`, `upcoming_bookings` (a
number) and `favourites`.

### Dry run of the purge

```bash
docker exec $(docker ps -q -f name=gostyle-consumer_api | head -1) \
  python manage.py purge_due_accounts --dry-run
```

It prints `Dry run: N account(s) due, none purged.` and changes nothing.
Nothing has ever been purged in production, so any account asked for
deletion more than 30 days ago is due now. Look at them before switching on
(in the admin: Consumer accounts, filter "By deletion: Pending deletion").

## Switch the hourly purge on

```bash
docker service update --env-add ACCOUNT_PURGE_V1=True gostyle-consumer_worker
```

Then add the same line to `/root/gostyle-customer/stack.env`, so a stack
deploy keeps it:

```bash
echo 'ACCOUNT_PURGE_V1=True' >> /root/gostyle-customer/stack.env
bash scripts/stack-env-check.sh   # must print only "check done"
```

The worker runs the purge when it starts and then once an hour. To watch it:

```bash
docker service logs --since 2h gostyle-consumer_worker 2>&1 | grep "account deletion"
```

### Switch off

```bash
docker service update --env-add ACCOUNT_PURGE_V1=False gostyle-consumer_worker
```

and set it to `False` in `stack.env`. The command still works by hand.

### Roll back the code

```bash
docker service update --force --image gostyle-consumer-api:before-account-deletion gostyle-consumer_api
docker service update --force --image gostyle-consumer-api:before-account-deletion gostyle-consumer_grpc
docker service update --force --image gostyle-consumer-api:before-account-deletion gostyle-consumer_worker
```

## Live proof (one test account)

1. Test customer: book one single booking for a day next week.
2. In the app (or with curl) delete the account:
   `POST /api/v1/auth/me/delete` with `{"password": "..."}`. Expect `200`
   with `purge_at` 30 days ahead.
3. Make it due now:

   ```bash
   docker exec -it $(docker ps -q -f name=gostyle-consumer_api | head -1) \
     python manage.py shell -c "
   from datetime import timedelta
   from django.utils import timezone
   from apps.accounts.models import ConsumerAccount
   print(ConsumerAccount.objects.filter(email='TEST_EMAIL').update(purge_at=timezone.now() - timedelta(minutes=1)))"
   ```

4. Run the purge by hand (or wait for the worker's hour):

   ```bash
   docker exec $(docker ps -q -f name=gostyle-consumer_api | head -1) \
     python manage.py purge_due_accounts
   ```

   Expect `Purged 1 account(s).` and `Bookings cancelled: 1, left as they
   were: 0.`
5. Business web: the booking shows cancelled, with the reason "Cancelled
   because the customer deleted their account."
6. The old token answers `401`, and logging in with the old password answers
   "Invalid credentials."
