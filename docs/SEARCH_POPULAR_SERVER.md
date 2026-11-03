# Search (popular services and salons): server steps

For: whoever deploys customer-api. The app side is docs/SEARCH_POPULAR_API.md.

## What ships

One PR, `feat/search-popular`, base `main`.

- **`GET /services` without `tenant_id`, `branch_id` or `category_id`**:
  services from every public salon (`is_popular`, `search`, position,
  `page`, `pageSize`). With any of those three ids it is still one salon's
  menu, unchanged.
- **`GET /discover`**: new `is_popular` (weighted rating order) and
  `pageSize`. `page_size` keeps its old reading.
- **One switch, `SEARCH_POPULAR_V1`**, off by default. Off, nothing changes
  on deploy: `GET /services` without `tenant_id` stays `422`, `/discover`
  ignores both new params.
- **No migration, no new package, no gRPC change**: only `_api` needs the new
  image.
- **Popular services count bookings in booking-api's database** (read-only,
  the same `BOOKING_DB_NAME` connection the admin and the map filters use).
  Without it, or if it is down, the list is still `200`, in the tie-break
  order (the salon's rating, then the newest), with a warning in the log:
  `popular services without booking counts`.

## Before you turn it on

`BOOKING_DB_NAME` must be set on the api service (it is, for the map filters):

```sh
docker service inspect gostyle-consumer_api --format '{{json .Spec.TaskTemplate.ContainerSpec.Env}}' | tr ',' '\n' | grep -oE '(BOOKING_DB_NAME|SEARCH_POPULAR_V1)=[^"]*'
```

## Deploy

After the PR is merged into `main`. Pull first: the server folder may be
behind.

```sh
docker tag gostyle-consumer-api:local gostyle-consumer-api:before-search-popular
cd ~/gostyle-customer && git pull
docker build -t gostyle-consumer-api:local .
docker service update --force --image gostyle-consumer-api:local gostyle-consumer_api
```

Rollback: `docker service update --force --image gostyle-consumer-api:before-search-popular gostyle-consumer_api`.

## Turn it on

On the service, and in stack.env the same day, so a stack deploy keeps it:

```sh
docker service update --env-add SEARCH_POPULAR_V1=True gostyle-consumer_api
cd /root/gostyle-customer && grep -q '^SEARCH_POPULAR_V1=' stack.env || echo "SEARCH_POPULAR_V1='True'" >> stack.env
bash scripts/stack-env-check.sh
```

The check must print only `check done`. To turn it off: `--env-add
SEARCH_POPULAR_V1=False` and the same line in stack.env.

## Live checks

```sh
curl -s 'https://api.gostyle.uk/api/v1/services?is_popular=true&pageSize=10'
curl -s 'https://api.gostyle.uk/api/v1/services?search=cut&latitude=25.1972&longitude=55.2744&radius=50'
curl -s 'https://api.gostyle.uk/api/v1/discover?is_popular=true&pageSize=10'
curl -s -o /dev/null -w '%{http_code}\n' 'https://api.gostyle.uk/api/v1/services?pageSize=51'
```

The last one must print `422`. The old menu must be unchanged:
`/services?tenant_id=<a salon's tenant>` answers `service_categories` and
`service_groups` as before.

## Checked locally (2026-11-03)

On the local `gostyle` database with `BOOKING_DB_NAME=gostyle_booking`: every
query ran (both routes, search, position, `other`, errors). With the window
widened to 60 days, the seeded salon's counts were right: The Gentleman's
Cut 8 (7 confirmed + 1 no-show; expired and cancelled left out), and it led
the popular list.

## Known limits

- The list is built in Python: every public salon in reach, times its
  menu. Fine for today's catalogue; at thousands of salons, move the order
  and the paging into SQL.
- No cache: popular counts are read on each `is_popular` call (one grouped
  query on booking-api's database).
