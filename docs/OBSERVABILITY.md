# Logging with Grafana Loki

Every log line on the node — Django, gunicorn, nginx, the platform API, Postgres,
Redis — ends up in one place, searchable by time, service and level, for 30 days.

```
Django   ──stdout──┐
gunicorn ──stdout──┤
platform API ──────┼── Docker ──┐
Postgres ──stdout──┤            ├── Alloy ── push ── gostyle_loki ── gostyle_grafana
Redis    ──stdout──┘            │   (global                          (:3001)
                                │    service)
nginx ── /var/log/nginx/*.log ──┘
```

## What runs where

Loki and Grafana were **already on this server**, in the `gostyle` platform stack.
They had been up for months holding **zero logs**, because nobody ever deployed a
collector. This repo adds the missing piece and nothing else:

| Component | Owned by | Where |
| --- | --- | --- |
| `gostyle_loki` | platform stack | storage + query, `:3100`, **firewalled** |
| `gostyle_grafana` | platform stack | the UI, `:3001`, Loki already wired |
| `gostyle-observability_alloy` | **this repo** | the collector |

Alloy reads the Docker socket, so it picks up every container on the node without
being told about any of them. **No application service has a logging driver, a
sidecar, or any awareness that Loki exists.** If Loki goes down, Alloy buffers and
retries; nothing else notices.

## Quick start

### On the server

```sh
cd /root/gostyle-customer
sudo ./scripts/setup-observability.sh
```

Idempotent. It refuses to run if `gostyle_loki` is missing, and proves it can
resolve Loki over the overlay network before deploying anything.

### On a laptop

```sh
docker compose -f docker-compose.observability.yml up -d
open http://localhost:3001      # admin / admin
```

That file *does* bring its own Loki and Grafana, since there is no platform stack
to borrow them from. Same Alloy config either way — `LOKI_URL` is the only
difference.

## What a log line looks like

Django emits one JSON object per line (`config/observability.py`):

```json
{"ts":"2026-08-14T08:10:01.186Z","level":"WARNING","logger":"django.request",
 "msg":"Not Found: /api/v1/nope","request_id":"prod-verify-001","status_code":404}
```

gunicorn's access lines use the same shape, tagged `"src":"gunicorn.access"`:

```json
{"level":"INFO","src":"gunicorn.access","method":"GET","path":"/api/v1/nope",
 "query":"","status":404,"duration_us":7152,"bytes":"179","request_id":"prod-verify-001"}
```

nginx's too, tagged `"src":"nginx.access"` (`nginx/log-format-json.conf`):

```json
{"src":"nginx.access","ts":"2026-08-14T08:13:06+00:00","vhost":"api.gostyle.uk",
 "remote_addr":"…","method":"GET","path":"/api/docs/","status":200,
 "duration_s":0.096,"request_id":"prod-verify-001"}
```

### The request ID ties them together

`RequestIDMiddleware` gives every request an ID — the client's `X-Request-ID` if
it sent one, otherwise a fresh UUID — and echoes it on the response. That echo is
what lets the other two find it: gunicorn reads it back with
`%({X-Request-ID}o)s`, nginx with `$sent_http_x_request_id`.

One request, three lines, one query:

```logql
{node=~".+"} | request_id="prod-verify-001"
```

An inbound ID is sanitized to `[A-Za-z0-9._-]`, max 64 characters, before being
echoed or logged — otherwise a newline in it would let a caller forge log entries.
The mobile app can safely set its own ID to trace a request end to end.

One subtlety worth knowing: `RequestIDMiddleware` deliberately does **not** clear
the ID on the way out. Django logs every 4xx and 5xx from
`BaseHandler.get_response`, *after* the middleware chain has returned, so clearing
it there would strip the ID from exactly the lines most worth correlating.

## Labels, and why there are so few

A Loki label creates a *stream*, and streams are the index. A label with many
distinct values makes many streams — the standard way to make Loki slow and
expensive. So the label set is deliberately small:

| Label | Example | Where it comes from |
| --- | --- | --- |
| `stack` | `gostyle-consumer` | Swarm's `com.docker.stack.namespace` |
| `service` | `gostyle-consumer_api` | Swarm's service name |
| `container` | `gostyle-consumer_api.1.q7x…` | the individual replica |
| `level` | `ERROR` | parsed out of the JSON |
| `job` | `docker` / `nginx` | which collector saw it |
| `vhost` | `api.gostyle.uk` | nginx access lines |
| `node` | `srv1727706` | added by Alloy |

Containers started outside Swarm (`hq-admin-nextjs-1`, the business front end)
have no Swarm labels, so Alloy falls back to labelling them by container name.

`request_id` is **not** a label — it would create one stream per request. It
travels as *structured metadata*: queryable with `| request_id="…"`, not indexed.
Everything else lives in the line body, reached with `| json | field="value"`.

Loki 3 also adds `service_name` and `detected_level` on its own. They duplicate
`service` and `level`; ignore them.

### Two log-level conventions

Django uses names (`INFO`, `WARNING`, `ERROR`). The platform API is a Node app
using **pino**, which uses numbers: `30`=info, `40`=warn, `50`=error, `60`=fatal.
Both end up in the same `level` label, so an "everything bad" query matches both:

```logql
{service=~".+", level=~"WARNING|ERROR|CRITICAL|4[0-9]|5[0-9]|6[0-9]"}
```

## Queries worth knowing

```logql
# Everything the consumer API logged
{service="gostyle-consumer_api"}

# Errors only
{service="gostyle-consumer_api", level=~"ERROR|CRITICAL"}

# Every 5xx the API returned
{service="gostyle-consumer_api"} |= "gunicorn.access" | json | status >= 500

# Requests slower than 2 seconds
{service="gostyle-consumer_api"} |= "gunicorn.access" | json | duration_us > 2000000

# One request, across nginx, gunicorn and Django
{node=~".+"} | request_id="9f8c2e1a4b7d"

# Failed logins by IP, from nginx (which sees the real client address)
sum by (remote_addr) (
  count_over_time({job="nginx"} | json | path="/api/v1/auth/login" | status=401 [1h])
)

# The platform API's errors
{service="gostyle_api", level=~"5[0-9]|6[0-9]"}
```

### The dashboard

`observability/grafana/dashboards/gostyle-api-logs.json` — log volume by level,
HTTP status classes, an errors panel, the full stream, and the nginx view.

Import it by hand: **Dashboards → New → Import → paste the JSON**. It is
deliberately *not* provisioned from a file, because that would mean editing the
platform stack's Grafana config.

## Security: Loki is firewalled

Loki is published on the Swarm ingress network at `:3100` with
`auth_enabled: false`. Before this work, **anyone on the internet could read every
log and inject fake ones**. That was harmless only because it was empty.

`gostyle-loki-firewall.service` (a systemd oneshot, installed by this work)
restricts `:3100` to localhost and Docker's own networks:

```sh
systemctl status gostyle-loki-firewall
iptables -t raw -S PREROUTING | grep 3100
```

It uses `raw/PREROUTING`, **not** `DOCKER-USER`. A Swarm ingress port is DNAT'd
before the FORWARD chain ever sees it, so a `DOCKER-USER` rule does nothing — the
advice in `docs/CI_CD_HANDOFF.md` for port 3850 has the same flaw. `raw/PREROUTING`
is the table Docker itself uses to enforce `127.0.0.1`-bound published ports.

To remove it: `systemctl disable --now gostyle-loki-firewall` and delete the rules
by hand.

**Still open:** Grafana on `:3001` is reachable from the internet over plain HTTP.
It has a login, so this is far less severe than Loki was, but the password crosses
the network in the clear. The fix is a TLS vhost — `nginx/logs.gostyle.uk.conf` is
ready, and needs a `logs.gostyle.uk` DNS record:

```sh
sudo DOMAIN=logs.gostyle.uk UPSTREAM_PORT=3001 HEALTH_PATH=/login \
     TEMPLATE=nginx/logs.gostyle.uk.conf EMAIL=ops@gostyle.uk ./scripts/setup-nginx.sh
```

## Retention and disk

30 days, enforced by the platform Loki's compactor
(`/opt/gostyle/loki/loki-config.yaml`, `retention_period: 720h`). Chunks live in
the `gostyle_loki-data` volume.

```sh
docker system df -v | grep loki-data
```

The node was at 81% disk when this was set up. Watch that volume for the first
few weeks; if it grows faster than expected, lower `retention_period` in the
platform's config (that file belongs to the platform stack, not this repo).

## When something is missing

**No logs from a service.** Alloy discovers containers through the Docker socket:

```sh
docker service ps gostyle-observability_alloy
docker service logs gostyle-observability_alloy --tail 50
```

**Logs arrive but have no `level`.** The line is not JSON. Either
`DJANGO_LOG_FORMAT` is not `json`, or the service name does not match Alloy's
filter — `config.alloy` only attempts JSON parsing for services matching
`.*(api|consumer).*`, so it does not log a parse error for every Redis line.

**`has timestamp too old` errors.** A container Alloy has never seen is read from
the *start* of its log, which for something up since July means replaying months.
Loki rejects anything older than 168h and drops the whole batch. `config.alloy`
has a `stage.drop { older_than = "160h" }` that discards those before they are
sent — if you see this error, that stage is missing or the window changed.

**No nginx logs.** They are files, not container output. Confirm `/var/log/nginx`
is mounted into Alloy and that the format is defined:

```sh
nginx -T | grep -c json_combined       # expect >= 1
```

Alloy starts tailing at the *end* of existing files (`tail_from_end`), so anything
written before it first ran is not backfilled.

**`too many outstanding requests` in Grafana.** A query spanning weeks with no
label selector. Always start from `{service="…"}`, then filter.

## Rolling back

| To undo | Command |
| --- | --- |
| The collector | `docker stack rm gostyle-observability` |
| The API's JSON logging | `docker tag gostyle-consumer-api:pre-loki gostyle-consumer-api:local && docker stack deploy -c docker-stack.consumer.yml gostyle-consumer` |
| nginx JSON logs | restore from `/root/gostyle-customer/.backup-before-loki-*/etc-nginx/` then `nginx -t && systemctl reload nginx` |
| The firewall rule | `systemctl disable --now gostyle-loki-firewall` |

Loki and Grafana are untouched by all of the above — removing this stack simply
returns them to holding nothing.

## Files

| Path | What |
| --- | --- |
| `docker-stack.observability.yml` | the Alloy-only production stack |
| `docker-compose.observability.yml` | self-contained Loki+Grafana+Alloy, for a laptop |
| `scripts/setup-observability.sh` | deploys it, with preflight checks |
| `observability/alloy/config.alloy` | what is collected and how it is labelled |
| `observability/loki/loki.yml` | only used by the laptop compose file |
| `observability/grafana/dashboards/` | the dashboard JSON, imported by hand |
| `config/observability.py` | JSON formatter, request-ID middleware |
| `config/settings/base.py` | the `LOGGING` dict |
| `config/tests/test_observability.py` | 23 tests over the formatter and middleware |
| `nginx/log-format-json.conf` | the `json_combined` access-log format |
| `nginx/logs.gostyle.uk.conf` | TLS vhost for Grafana (not yet installed) |
| `observability.env` | per-host settings — **not in git** |
