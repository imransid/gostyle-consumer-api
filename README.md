# GoStyle Customer API

The consumer-facing HTTP API behind the GoStyle mobile app: account signup and
login, salon discovery, and the salon profile screen.

Django 6 + Django REST Framework, reading a PostgreSQL database whose schema is
owned by a **different repository** — `gostyle-platform`, a NestJS/Prisma
service that runs the salon-side console. That single fact shapes almost every
convention in this codebase, so it is worth understanding before writing any
code here.

---

## The two-repo split

There is one Postgres database and two applications on top of it.

|                    | `gostyle-platform` (NestJS + Prisma)              | `gostyle-customer-api` (this repo)    |
| ------------------ | ------------------------------------------------- | ------------------------------------- |
| Audience           | Salon owners and staff                            | Customers, via the mobile app         |
| Schema             | **Owns it.** Prisma migrations define every table | **Borrows it.** Reads, never migrates |
| Postgres schema    | `public`                                          | `consumer`                            |
| Access to `public` | Full                                              | `SELECT` only, enforced by DB grant   |

The connection sets `search_path=consumer,public` (see
[base.py](config/settings/base.py)), so both schemas are visible on one
connection. Tables this service owns — accounts, OTP codes, JWT blacklist, the
demo `salons_salon` table — live in `consumer` and have ordinary Django
migrations. Everything describing a real salon lives in `public` and does not.

**When you need to know what a platform column means, read `gostyle-platform`.**
Not this repo, and not the column name. A large amount of the customer-visible
profile content is not in columns at all: it lives in
`storefront_version.snapshot`, a JSONB blob reached through
`storefront.live_version_id`. See [snapshot.py](apps/salons/snapshot.py).

---

## The `managed = False` constraint

Every one of the ~150 models in [apps/platform_data/models.py](apps/platform_data/models.py)
carries:

```python
class Meta:
    managed = False
    db_table = 'storefront'
```

The file was generated with `inspectdb` and then hand-cleaned. `managed = False`
tells Django: this table exists, you may query it, you will never create, alter
or drop it. [apps/platform_data/migrations/0001_initial.py](apps/platform_data/migrations/0001_initial.py)
records the models in migration state but emits no DDL.

### What this means in practice

**Never run `makemigrations apps.platform_data`.** If the platform adds a column
you need, re-run `inspectdb` against a database that has it, take the lines you
want, and paste them in with `managed = False` intact. A migration generated
here would try to own a table it does not own.

**Never write to a `public` table.** The `consumer_app` role is granted `SELECT`
only, so an accidental write fails at the database rather than corrupting salon
data. The admin enforces the same rule in the UI via `ReadOnlyAdmin`
([admin.py](apps/platform_data/admin.py)). The one deliberate exception is
`seed_salon`, which refuses to run against a non-local host.

**The platform can change the schema without telling you.** Treat every read as
defensive: a JSONB section may predate a field, a nullable column may be null in
older rows, a soft-deleted row is still a row. Filter on `deleted_at__isnull=True`,
scope by `tenant_id` and usually `branch_id`, and never assume a snapshot key
exists.

**Unmanaged tables do not exist in the test database.** Django does not create
them, so no test can insert a `storefront` row. This is not a nuisance to work
around — it is the reason the architecture below exists.

### Adding a selector: the checklist

1. Put the query in [selectors.py](apps/salons/selectors.py). Views and
   serializers do not touch `.objects` directly.
2. Filter for what a _customer_ may see, which is narrower than what the salon's
   console shows: `deleted_at__isnull=True`, the right `status`/`visibility`,
   `online_booking_enabled`, and branch availability.
3. Prefer `Subquery`/`annotate` over joins when a row could otherwise be
   multiplied by its children.
4. Money arrives as integer minor units (`19900` is `199.00`). Convert with
   [money.py](apps/salons/money.py), never inline, and never through `float`.
5. Any real logic — a decision, a format, a fallback — goes in a pure module, not
   the selector and not the serializer. That is the only part you can test.

---

## Architecture

Three layers, and the split is driven by testability under the constraint above.

```
views.py        thin. HTTP in, HTTP out. Owns the clock and the request.
  ↓
selectors.py    every database read. Returns querysets/instances, no formatting.
  ↓
snapshot.py     pure. No Django, no DB, no clock. All the logic worth testing.
hours.py
money.py
translate.py
```

### Selectors — queries only

One function per question: `discoverable_salons()`, `salon_services(storefront)`,
`salon_packages(storefront)`. They annotate what the response needs and filter
for customer visibility. They do not format, round, translate or decide.

### Pure modules — logic only

| Module                                   | Answers                                                                                                       |
| ---------------------------------------- | ------------------------------------------------------------------------------------------------------------- |
| [snapshot.py](apps/salons/snapshot.py)   | "What did this salon publish?" — safe reads of the JSONB snapshot, with every section guaranteed present      |
| [hours.py](apps/salons/hours.py)         | "Is it open?" — the weekly grid, dated exceptions, and manual states like `BUSY`, including overnight windows |
| [money.py](apps/salons/money.py)         | Minor units → `Decimal`, in one place                                                                         |
| [translate.py](apps/salons/translate.py) | Platform vocabulary → app vocabulary (amenities, price tiers, social handles → URLs)                          |

These import nothing from Django. No fixtures, no test database, no transactions,
and the whole suite runs in under a tenth of a second. They are also the reason
two endpoints cannot disagree: `/discover` and `/salon/:id` both call
`hours.resolve()`, so the card and the profile always give the same answer.

`hours.py` is a deliberate port of the platform's `opening-hours.ts` and
`daily-status-rules.ts`. A customer seeing "Open" while the salon's own console
says "Closed" is a support ticket, so the two implementations are kept in step
on purpose.

### Views — thin

Parse the request, call a selector, hand the result to a serializer, return it.
The one thing views own exclusively is **the clock**: `hours.resolve()` is pure
and takes the local time it should reason about, so `datetime.now(tz)` happens at
the edge and nowhere else.

Profile endpoints are `APIView` rather than `RetrieveAPIView` because
`PAGE_SIZE = 15` pagination would otherwise wrap object responses in an envelope
the app does not expect. They set `permission_classes = [AllowAny]` explicitly,
since the project default is `IsAuthenticated`; with `AllowAny` a JWT still
populates `request.user` when one is sent.

---

## Local setup

Requires Python 3.13 (what CI runs) and a local PostgreSQL 16.

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

cp .env.example .env
# Set DJANGO_SECRET_KEY. Defaults assume Postgres on 127.0.0.1:5432,
# database "gostyle", user "postgres". DB_SCHEMA=consumer.

createdb gostyle
psql gostyle -c 'CREATE SCHEMA IF NOT EXISTS consumer'

python manage.py migrate
python manage.py runserver
```

`manage.py` defaults to `config.settings.local`, which turns on `DEBUG`, swaps
Redis for an in-process cache, and prints OTP codes to the console instead of
sending them. No Redis required for local work.

Interactive API docs: <http://127.0.0.1:8000/api/docs/>

### Migrations and the platform tables

`migrate` creates only the `consumer`-schema tables. It will _not_ create
`storefront`, `branch`, `service` or any other platform table — those are
expected to already exist in `public`, put there by `gostyle-platform`. On a
fresh local database they will not, which is what `seed_salon` is for.

---

## Seeding a salon

The profile endpoints read across storefront, branch, tenant, categories,
services, per-branch availability, staff, packages, products and a published
version snapshot — all of which must exist _and agree with each other_ before a
single request returns anything meaningful. No fixture file expresses that
readably, so it is a command:

```bash
python manage.py seed_salon
python manage.py seed_salon --force   # delete and recreate the seeded rows
```

It prints the URLs it just made work:

```
GET /api/v1/salon/33333333-3333-3333-3333-333333333333
GET /api/v1/salon/33333333-3333-3333-3333-333333333333/services
GET /api/v1/salon/33333333-3333-3333-3333-333333333333/stylists
GET /api/v1/salon/33333333-3333-3333-3333-333333333333/packages
GET /api/v1/salon/33333333-3333-3333-3333-333333333333/products
```

The IDs are fixed, so the URL survives a re-seed.

**This command writes to platform-owned tables**, which is acceptable on a local
database and nowhere else. It reads `DB_HOST` and refuses to run unless it is
`127.0.0.1`, `localhost`, `db` or empty.

The seeded data is chosen to exercise the cases a happy-path fixture would miss:
a two-level category tree (which no real tenant populates yet), a service with no
category at all, a branch price override that must beat the catalogue price, a
staff row that is `ACTIVE`/`INVITED` and must _not_ appear publicly, a
`PROFESSIONAL` product that must not reach the shop tab, and a package whose
`price_before` and `save_amount` are derived rather than stored.

A second command, `seed_salons` (plural), fills the legacy `salons_salon` demo
table used by the old `/api/v1/salons` endpoint. Unrelated to the platform data.

---

## Tests

```bash
python manage.py test apps.salons   # 44 tests
python manage.py test               # 81 tests, everything
python manage.py check
```

`apps.salons` is 41 `SimpleTestCase` tests over the four pure modules plus 3
`TestCase` tests driving the map endpoint. `SimpleTestCase` refuses database
access outright, so if someone later adds a model import to a pure module, those
tests fail loudly rather than quietly opening a connection.

What is worth testing here is not the happy path — the seeded salon proves that
end to end — but the shapes the seed _cannot_ reach: a snapshot published before
a section existed, a weekly grid missing today's row, a manual state overruling
real opening hours, a social payload with every network switched off. Those
arrive from years of production data and never from a fixture written this
morning.

Note for anyone extending the DB-backed tests: platform tables do not exist in
the test database, so a query against `storefront` raises. `map_venues()` catches
that and falls back to the local `salons_salon` model, which is why the map tests
can run at all. A new endpoint reading platform tables cannot be integration-
tested this way — put its logic in a pure module and test that.

`apps.accounts` is 37 tests over the auth flow, driven through the real URL conf.
Two of them exist to pin down one security property: the OTP endpoints send the
code to the contact stored on the caller's account and ignore any destination in
the request body, so a logged-in user cannot make the server deliver mail to an
address they do not own. If that ever regresses, those two fail. See
[docs/AUTH_GUIDE.md](docs/AUTH_GUIDE.md) for the flow in plain language.

---

## deploy

`cd ~/gostyle-customer && git pull origin main && docker build -t gostyle-consumer-api:local . && docker service update --force gostyle-consumer_api`

## API surface

All routes are under `/api/v1/`. Auth is JWT (`rest_framework_simplejwt`):
7-day access token, 30-day rotating refresh token with blacklist-on-rotate.

| Endpoint                                                                    | Auth   | Notes                                                                                                                 |
| --------------------------------------------------------------------------- | ------ | --------------------------------------------------------------------------------------------------------------------- |
| `POST /auth/otp/request`, `/auth/otp/resend`, `/auth/otp/verify`            | JWT    | Verifies the caller's OWN contact on file; any `destination` in the body is validated and then ignored                |
| `POST /auth/register`, `/auth/login`, `/auth/logout`, `/auth/token/refresh` | —      | Register creates the account and returns tokens immediately; login then requires a verified contact                   |
| `GET/PATCH /auth/me`                                                        | JWT    |                                                                                                                       |
| `GET /discover`                                                             | JWT    | Salon cards. Filters: `lat`/`lng`, `sort`, `rating_min`, `city`, `category`, `hijab_mode`, `open_now`, `total_amount` |
| `GET /discover/map`                                                         | JWT    | Lightweight markers for a map viewport (center + delta)                                                               |
| `GET /discover/<uuid>`                                                      | JWT    | One card                                                                                                              |
| `GET /salon/<uuid>`                                                         | Public | Profile header, info card, check-in card                                                                              |
| `GET /salon/<uuid>/services`                                                | Public | Two-level category grouping                                                                                           |
| `GET /salon/<uuid>/stylists`                                                | Public |                                                                                                                       |
| `GET /salon/<uuid>/packages`                                                | Public |                                                                                                                       |
| `GET /salon/<uuid>/products`                                                | Public | Retail only                                                                                                           |
| `GET /salons/`                                                              | JWT    | Legacy demo list, superseded by `/discover`                                                                           |

`open_now` cannot be a SQL filter — whether a salon is open depends on its own
timezone and a JSONB grid — so it is computed in Python and fed back as an id
list, which costs one extra queryset evaluation and only runs when the flag is
set.

Further reading, all in [docs/](docs/):

- [DISCOVERY_API.md](docs/DISCOVERY_API.md) — mobile handoff for the discovery endpoints
- [SALON_PROFILE_API.md](docs/SALON_PROFILE_API.md) — mobile handoff for the five profile endpoints, including why `:id` is a storefront UUID
- [AUTH_GUIDE.md](docs/AUTH_GUIDE.md) — the OTP → verify → register flow in plain language
- [DEPLOYMENT.md](docs/DEPLOYMENT.md) — CI/CD, image tags, and the rollback lever

---

## Deployment

Docker Swarm, deployed by [.github/workflows/ci-cd.yml](.github/workflows/ci-cd.yml).
PRs run tests only; merges to `main` build, push to GHCR, migrate and deploy. The
service sits on port 3850, next to the platform API on 3849, sharing its Postgres
and the `gostyle-net` network. Every image is tagged `sha-<short-sha>` so any
commit that reached production can be redeployed by tag. See
[docs/DEPLOYMENT.md](docs/DEPLOYMENT.md).

Public traffic arrives at **https://api.gostyle.uk**, where host nginx on the
manager node terminates TLS and proxies to 3850. The site config is
[nginx/api.gostyle.uk.conf](nginx/api.gostyle.uk.conf), installed and
certificate-issued by [scripts/setup-nginx.sh](scripts/setup-nginx.sh).
