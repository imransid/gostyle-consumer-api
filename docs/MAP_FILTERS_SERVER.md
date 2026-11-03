# Map filters: server steps

For: whoever deploys customer-api. The app side is docs/MAP_FILTERS_API.md.

## What ships

One PR, `feat/map-filters`, base `main`.

- **`GET /me/map/filters`**: the map's Filters drawer.
- **Five filter params on `GET /discover/map`**: `service_categories`,
  `rating`, `availability`, `distance` (with `origin_latitude`,
  `origin_longitude`) and `additional`.
- **One switch, `MAP_FILTER_V1`**, off by default. Off, the route is `404`
  and the map ignores the params: nothing changes on deploy.
- **No migration, no new package, no gRPC change** (only `_api` needs the
  new image).
- **`availability` reads booking-api's database** (read-only, the same
  `BOOKING_DB_NAME` connection the admin uses), by booking-api's own `/busy`
  rule. Without `BOOKING_DB_NAME`, a map call with `availability` is `503
  availability_unavailable`; the other filters still work.

## Before you turn it on

`BOOKING_DB_NAME` must be set on the api service (it is for the admin):

```sh
docker service inspect gostyle-consumer_api --format '{{json .Spec.TaskTemplate.ContainerSpec.Env}}' | tr ',' '\n' | grep -oE '(BOOKING_DB_NAME|MAP_FILTER_V1)=[^"]*'
```

## Deploy

After the PR is merged into `main`. Pull first: the server folder may be
behind.

```sh
docker tag gostyle-consumer-api:local gostyle-consumer-api:before-map-filters
cd ~/gostyle-customer && git pull
docker build -t gostyle-consumer-api:local .
docker service update --force --image gostyle-consumer-api:local gostyle-consumer_api
```

## Turn it on

On the service, and in stack.env the same day, so a stack deploy keeps it:

```sh
docker service update --env-add MAP_FILTER_V1=True gostyle-consumer_api
cd /root/gostyle-customer && grep -q '^MAP_FILTER_V1=' stack.env || echo "MAP_FILTER_V1='True'" >> stack.env
bash scripts/stack-env-check.sh
```

The check must print only `check done`.

## Live checks

```sh
curl -s https://api.gostyle.uk/api/v1/me/map/filters
curl -s 'https://api.gostyle.uk/api/v1/discover/map?latitude=25.2048&longitude=55.2708&latitudeDelta=0.5&longitudeDelta=0.5&additional=open_now'
curl -s 'https://api.gostyle.uk/api/v1/discover/map?latitude=25.2048&longitude=55.2708&latitudeDelta=0.5&longitudeDelta=0.5&availability=week'
```

The first lists the sections, with the categories the live salons offer.
The other two answer `200` with `mode`, `count` and `venues`; the third is
never `503` when `BOOKING_DB_NAME` is set.

## Turn it off, roll back

Off: `docker service update --env-add MAP_FILTER_V1=False gostyle-consumer_api`
(and `False` in stack.env). Rollback: `docker service update --force --image
gostyle-consumer-api:before-map-filters gostyle-consumer_api`.

## The Parking chip (MAP_FILTER_PARKING_V1, added 2026-11-03)

PR `feat/map-filter-parking`. One more switch, off by default: the drawer
offers "Parking" and the map takes `additional=parking`. Only with
`MAP_FILTER_V1` on. No migration, only `_api` needs the new image. Deploy
as above (tag first, for example `before-map-parking`).

Before turning it on, see how many live salons list parking (read only,
inside the api container). Zero means the chip would find nothing yet:

```sh
docker exec $(docker ps -q -f name=gostyle-consumer_api | head -1) python manage.py shell -c "
from apps.platform_data.models import Storefront
from apps.salons import map_filters
print(Storefront.objects.filter(visibility='PUBLIC', deleted_at__isnull=True).filter(map_filters._has_amenity('PARKING')).count())"
```

Turn it on, on the service and in stack.env the same day:

```sh
docker service update --env-add MAP_FILTER_PARKING_V1=True gostyle-consumer_api
cd /root/gostyle-customer && grep -q '^MAP_FILTER_PARKING_V1=' stack.env || echo "MAP_FILTER_PARKING_V1='True'" >> stack.env
bash scripts/stack-env-check.sh
```

Live check: the drawer lists `parking`, and the map answers `200`:

```sh
curl -s https://api.gostyle.uk/api/v1/me/map/filters | grep -o '"parking"'
curl -s 'https://api.gostyle.uk/api/v1/discover/map?latitude=25.2048&longitude=55.2708&latitudeDelta=0.5&longitudeDelta=0.5&additional=parking'
```

Off: `docker service update --env-add MAP_FILTER_PARKING_V1=False gostyle-consumer_api`
(and `False` in stack.env).

