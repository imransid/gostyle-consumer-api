# Deployment

CI/CD lives in [`.github/workflows/ci-cd.yml`](../.github/workflows/ci-cd.yml) and
[`scripts/deploy.sh`](../scripts/deploy.sh).

| Trigger | What runs |
| --- | --- |
| Pull request → `main` | Tests + Docker build (image is **not** pushed) |
| Push/merge → `main` | Tests → build & push to GHCR → migrate → `docker stack deploy` |
| Manual (`workflow_dispatch` with a `tag`) | Redeploys an existing image — the rollback lever |

Every image is tagged `sha-<short-sha>`, so any commit that ever reached
production can be redeployed by tag. `latest` is also moved, for convenience only —
the stack always runs an immutable tag.

## One-time setup

### 1. GitHub repository secrets

`Settings → Secrets and variables → Actions → Secrets`

| Secret | What it is |
| --- | --- |
| `SWARM_HOST` | IP/hostname of the Swarm **manager** node |
| `SWARM_USER` | SSH user on that node (must be in the `docker` group) |
| `SWARM_SSH_KEY` | Private half of a deploy-only keypair (full PEM, including header/footer) |
| `SWARM_KNOWN_HOSTS` | Output of `ssh-keyscan -p 22 <host>` — pins the host key so a MITM can't hijack the deploy |
| `GHCR_TOKEN` *(maybe)* | See "GHCR permissions" below |
| `GHCR_USERNAME` *(maybe)* | Username matching `GHCR_TOKEN` |

Generate the deploy key:

```sh
ssh-keygen -t ed25519 -C "gh-actions-deploy" -f gostyle-deploy -N ""
ssh-copy-id -i gostyle-deploy.pub <user>@<swarm-host>   # public half → server
ssh-keyscan <swarm-host>                                # → SWARM_KNOWN_HOSTS
# private half (gostyle-deploy) → SWARM_SSH_KEY, then delete your local copy
```

### 2. GitHub repository variables (optional)

`Settings → Secrets and variables → Actions → Variables`. All have defaults:

| Variable | Default |
| --- | --- |
| `SWARM_PORT` | `22` |
| `DEPLOY_DIR` | `/opt/gostyle-consumer` |
| `STACK_NAME` | `gostyle-consumer` |

### 3. GHCR permissions

The image is `ghcr.io/entity-tech-solutions/gostyle-platform/gostyle-consumer-api`,
which sits under the `gostyle-platform` package namespace. The built-in
`GITHUB_TOKEN` of *this* repo can only push to it if the package grants this repo
write access:

> ghcr.io package → `Package settings` → `Manage Actions access` → add
> `Entity-tech-solutions/gostyle-customer` with the **Write** role.

If you'd rather not link them, create a classic PAT with `write:packages` and set
it as `GHCR_TOKEN` (+ `GHCR_USERNAME`). The workflow prefers that secret and falls
back to `GITHUB_TOKEN`.

### 4. Server preparation (manager node)

```sh
sudo install -d -o <deploy-user> -g <deploy-user> -m 750 /opt/gostyle-consumer
```

Create `/opt/gostyle-consumer/.env`, owned by the deploy user, mode `600`. This is
the only place production secrets live — they are never stored in GitHub:

```sh
DJANGO_SECRET_KEY=<long random string>
DJANGO_ALLOWED_HOSTS=api.gostyle.uk,127.0.0.1
DJANGO_CSRF_TRUSTED_ORIGINS=https://api.gostyle.uk
DB_PASSWORD=<consumer_app password>
WHATSAPP_PHONE_NUMBER_ID=...
WHATSAPP_ACCESS_TOKEN=...
WHATSAPP_TEMPLATE_NAME=...

# Long-lived read:packages token so Swarm can still pull when it reschedules a
# task days later. A CI-issued token would be expired by then.
GHCR_USER=<github-username>
GHCR_TOKEN=<PAT with read:packages>
```

The shared overlay network must be **attachable**, because migrations run in a
one-off container that has to reach `gostyle_postgres`:

```sh
docker network inspect -f '{{.Attachable}}' gostyle_gostyle-net   # must print: true
```

If it prints `false`, recreate it from the `gostyle` stack with
`--attachable` (compose: `attachable: true`). `deploy.sh` refuses to deploy
otherwise rather than half-applying a release.

### 5. HTTPS on `api.gostyle.uk`

`docker-compose.yml` publishes gunicorn on port **3850**, plain HTTP. Host nginx
on the manager node terminates TLS in front of it, with a Let's Encrypt
certificate; nothing about the container changes.

```
client ──https──▶ nginx :443 ──http──▶ 127.0.0.1:3850 ──▶ gunicorn (2 replicas)
```

The site config lives in git at [`nginx/api.gostyle.uk.conf`](../nginx/api.gostyle.uk.conf)
and is installed by [`scripts/setup-nginx.sh`](../scripts/setup-nginx.sh). Before
running it, point the DNS record at the manager node and make sure ports 80 and
443 are reachable — certbot proves ownership over HTTP-01:

```sh
dig +short api.gostyle.uk        # must be the manager node's public IP
```

Then, on the manager node, from a checkout of this repo:

```sh
sudo EMAIL=ops@gostyle.uk ./scripts/setup-nginx.sh
```

The script installs nginx + certbot, serves the ACME challenge from a temporary
HTTP-only site (the real config can't start before the certificate file exists),
issues the certificate, installs the real config, and registers a renewal deploy
hook that reloads nginx. It is idempotent — re-run it after editing the config in
git, and it will reuse the existing certificate. `STAGING=1` uses Let's Encrypt's
staging CA, which is the right way to rehearse without burning the
5-per-week duplicate-certificate limit.

Verify:

```sh
curl -sI https://api.gostyle.uk/api/docs/          # 200
curl -sI http://api.gostyle.uk/api/docs/           # 301 → https
sudo certbot renew --dry-run                       # renewal actually works
```

**Two things the script cannot do for you:**

1. **The stack's env file** must list the domain, or Django answers every request
   with `400 Bad Request` (`DisallowedHost`):
   ```sh
   DJANGO_ALLOWED_HOSTS=api.gostyle.uk,156.67.214.42,localhost,127.0.0.1
   DJANGO_CSRF_TRUSTED_ORIGINS=https://api.gostyle.uk,http://156.67.214.42:3850
   ```
   Redeploy afterwards, since Swarm only re-reads it on deploy. **Note that the
   manager node does not currently use the `deploy.sh` flow described above:** it
   runs `/root/gostyle-customer/docker-stack.consumer.yml` with `env_file:
   stack.env` and a locally built `gostyle-consumer-api:local` image, deployed by
   hand with
   ```sh
   cd /root/gostyle-customer && docker stack deploy -c docker-stack.consumer.yml gostyle-consumer
   ```
   So the live env file is `/root/gostyle-customer/stack.env`, not
   `/opt/gostyle-consumer/.env`, and the running image is not the one CI pushes to
   GHCR. Reconciling those two is worth doing; until then, edit the file the
   stack actually reads.

2. **Port 3850 stays open to the world.** Swarm publishes it on `0.0.0.0`, so
   `http://<manager-ip>:3850` still serves the API unencrypted, bypassing nginx
   entirely. Docker inserts its iptables rules ahead of ufw, so `ufw deny 3850`
   does nothing; block it in the `DOCKER-USER` chain instead, or at the cloud
   firewall:
   ```sh
   sudo iptables -I DOCKER-USER -p tcp --dport 3850 ! -s 127.0.0.1 -j DROP
   sudo apt install iptables-persistent   # so it survives a reboot
   ```

Django's side of the arrangement is in
[`config/settings/production.py`](../config/settings/production.py):
`SECURE_PROXY_SSL_HEADER` makes `request.is_secure()` trust nginx's
`X-Forwarded-Proto` (without it, `SECURE_SSL_REDIRECT` would loop), HSTS is sent
for a year, cookies are secure-only, and DRF's `NUM_PROXIES=1` keeps the login
throttle per-client rather than per-proxy. Each of those is overridable by env
var — e.g. `DJANGO_HSTS_SECONDS=0` while first bringing a domain up.

### 6. Protect `main`

`Settings → Branches → Add rule` for `main`:

- Require a pull request before merging
- Require status checks to pass → select **Test**
- Do not allow bypassing

That is what makes "merge to main" a safe deploy trigger rather than a risky one.

## How a deploy actually proceeds

1. **Tests** run against a real Postgres 16 service container. The `consumer`
   schema is created in `template1` so the test database exercises the same
   `search_path` as production. Redis isn't needed — `config.settings.local`
   uses an in-memory cache.
2. **Build** with Buildx + GitHub Actions layer cache, pushed as `sha-<sha>`.
3. **Deploy** copies `docker-compose.yml` and `deploy.sh` to the server (so the
   compose file in git is always the source of truth), then over SSH:
   - `docker login ghcr.io` with the server's long-lived token
   - `docker pull` the exact tag — fails fast if the image is unreachable
   - `manage.py migrate` in a **one-off container**, before any new task serves
     traffic. The image entrypoint deliberately does not migrate: with
     `replicas: 2` the replicas would race on the migration lock.
   - `docker stack deploy --with-registry-auth --prune`
   - polls until both replicas report `Running`, and **fails the build** if Swarm
     reports `rollback_started` / `rollback_completed` / `paused`

The compose file already sets `order: start-first` and
`failure_action: rollback`, so a broken image is replaced by the previous one
automatically; the workflow just makes sure that shows up as a red build instead
of a silent non-event.

## Rolling back

`Actions → CI/CD → Run workflow → tag: sha-1a2b3c4`

That skips the build and redeploys a known-good image. Note that it does **not**
reverse migrations — an already-applied schema change stays applied, so keep
migrations backward-compatible with the previous release (add columns, don't drop
them in the same deploy).

## Recommended follow-up: a health endpoint

Swarm currently treats a task as healthy as soon as the process starts, so
`failure_action: rollback` only catches a container that exits. Adding a
`/api/v1/health` view plus a compose `healthcheck` would make the rollback fire on
an app that boots but can't reach Postgres or Redis — which is the failure mode
that actually happens.
