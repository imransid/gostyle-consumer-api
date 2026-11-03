# The map shows only the customer's country: server steps

For: whoever deploys customer-api. The app side is docs/MAP_COUNTRY_API.md.

## What ships

One PR, `feat/map-country`, base `main`.

- **`GET /discover/map`** reads `user_latitude` and `user_longitude`, limits
  the salons to the customer's country (the phone's, else the map centre's)
  and answers `country`.
- **One switch, `MAP_COUNTRY_V1`**, off by default. Off, nothing changes on
  deploy: both params are ignored, no `country` in the answer.
- **One migration**, `salons 0002_salon_country`: a new table
  `consumer.salon_country` (branch id, the pin it was worked out from, the
  country). It runs when the container starts, like every migration here.
  Platform's tables are not touched.
- **Country borders in the image**: `apps/salons/data/country_borders.json.gz`
  (2.2 MB, Natural Earth 1:10m, public domain, no account, no key). Looked up
  in our own process, offline. No PostGIS, no new package, no outside service.
- **No gRPC change**: only `_api` needs the new image.
- **Access logs**: `user_latitude` and `user_longitude` are cut out of
  gunicorn's access log (ships with the image) and nginx's (the snippet below,
  by hand).

## Deploy

After the PR is merged into `main`. Pull first: the server folder may be
behind.

```sh
docker tag gostyle-consumer-api:local gostyle-consumer-api:before-map-country
cd ~/gostyle-customer && git pull
docker build -t gostyle-consumer-api:local .
docker service update --force --image gostyle-consumer-api:local gostyle-consumer_api
```

Rollback: `docker service update --force --image gostyle-consumer-api:before-map-country gostyle-consumer_api`.
The new table can stay: nothing reads it with the switch off.

## nginx: keep the position out of its log

The same snippet `scripts/setup-nginx.sh` installs, now with two more maps:

```sh
cd ~/gostyle-customer
install -m 644 nginx/log-format-json.conf /etc/nginx/conf.d/log-format-json.conf
nginx -t && systemctl reload nginx
```

`nginx -t` must say `syntax is ok` and `test is successful` before the
reload. Do this before turning the switch on.

## Every salon's country (the backfill)

A dry run first: it lists each live salon, the country its pin is in, and
what its typed address says. It writes nothing.

```sh
docker exec $(docker ps -q -f name=gostyle-consumer_api | head -1) python manage.py salon_countries
```

Read the list: `(address says another country)` marks a pin and an address
that disagree; the map goes by the pin. Then write it:

```sh
docker exec $(docker ps -q -f name=gostyle-consumer_api | head -1) python manage.py salon_countries --apply
```

It ends with `Written: <n>` and the live salons per country. Run it again: it
must say `Salons with no country yet, or whose pin moved: 0`.

After this the map keeps it up to date itself: once a minute it redoes any
salon that is new or moved its pin (up to 500 at a time).

## Turn it on

On the service, and in stack.env the same day, so a stack deploy keeps it:

```sh
docker service update --env-add MAP_COUNTRY_V1=True gostyle-consumer_api
cd /root/gostyle-customer && grep -q '^MAP_COUNTRY_V1=' stack.env || echo "MAP_COUNTRY_V1='True'" >> stack.env
bash scripts/stack-env-check.sh
```

The check must print only `check done`. To turn it off: `--env-add
MAP_COUNTRY_V1=False` and the same line in stack.env.

## Live checks

```sh
B='https://api.gostyle.uk/api/v1/discover/map?latitudeDelta=0.5&longitudeDelta=0.5&limit=100'
# 1. In Dubai: UAE salons, country AE.
curl -s "$B&latitude=25.2048&longitude=55.2708&user_latitude=25.20&user_longitude=55.27"
# 2. In Dubai, map on Muscat: count 0, country AE, has_salons true.
curl -s "$B&latitude=23.5880&longitude=58.3829&user_latitude=25.20&user_longitude=55.27"
# 3. In Dhaka: count 0, country BD, has_salons false.
curl -s "$B&latitude=23.8103&longitude=90.4125&user_latitude=23.81&user_longitude=90.41"
# 4. Location off, map on Dubai: country AE.
curl -s "$B&latitude=25.2048&longitude=55.2708"
# 5. Map centre at sea, no position: country null.
curl -s "$B&latitude=26.2&longitude=53.8"
# 6. Half a position: 422.
curl -s -o /dev/null -w '%{http_code}\n' "$B&user_latitude=25.20"
```

The access logs must not show the position:

```sh
grep 'discover/map' /var/log/nginx/api.gostyle.uk.access.log | tail -1
docker service logs --since 5m gostyle-consumer_api 2>&1 | grep 'discover/map' | tail -1
```

Both lines keep `latitude=...` and have no `user_latitude` or
`user_longitude`.

## Checked locally (2026-11-03)

On the local `gostyle` database, inside a transaction rolled back at the
end: the migration's table, the command (dry run, `--apply`, then 0 left),
and every case above through the real view, with and without the map
filters on: Dubai `AE` (1 salon), Dubai panned to Muscat `AE` with 0 and
`has_salons: true`, Dhaka `BD` with `has_salons: false`, a centre in the
Gulf `null`, a phone in Muscat over a Dubai map `OM` with 0, and both 422s.
nginx's new maps were checked in `nginx:1.27-alpine` (`nginx -t`, and the
logged query of real requests).

## Known limits

- The borders are 1:10m: right to about a kilometre at a land border. A salon
  standing on one could, rarely, count for the neighbour. The dry run's
  "address says" column is where that shows.
- The borders load once per worker on first use: about 0.3 s and 4 MB of
  memory each. One lookup is about a millisecond, and the customer's is
  cached in the worker by the 2-decimal position.
- To refresh the borders (a new Natural Earth release), see
  `scripts/build_country_borders.py`.
