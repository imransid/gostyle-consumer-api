#!/usr/bin/env bash
#
# Puts https://api.gostyle.uk in front of the consumer API.
#
# Runs ON the Swarm manager node, as root. Idempotent: re-running it reinstalls
# the config from git and reloads nginx, and it will not ask Let's Encrypt for a
# certificate it already has.
#
#   sudo EMAIL=ops@gostyle.uk ./scripts/setup-nginx.sh
#
# It also serves the Grafana vhost, by pointing it at the other template:
#
#   sudo DOMAIN=logs.gostyle.uk UPSTREAM_PORT=3851 HEALTH_PATH=/login \
#        TEMPLATE=nginx/logs.gostyle.uk.conf EMAIL=ops@gostyle.uk \
#        ./scripts/setup-nginx.sh
#
# Prerequisites, checked below:
#   - api.gostyle.uk's A/AAAA record already points at this host
#   - ports 80 and 443 reachable from the internet (ACME uses HTTP-01)
#   - the stack is up, i.e. something answers on 127.0.0.1:3850
#
set -euo pipefail

DOMAIN="${DOMAIN:-api.gostyle.uk}"
EMAIL="${EMAIL:-}"
UPSTREAM_PORT="${UPSTREAM_PORT:-3850}"
WEBROOT="${WEBROOT:-/var/www/certbot}"
STAGING="${STAGING:-0}"          # STAGING=1 uses Let's Encrypt's staging CA
REPO_DIR="$(cd "$(dirname "$0")/.." && pwd)"
TEMPLATE="${TEMPLATE:-$REPO_DIR/nginx/api.gostyle.uk.conf}"
HEALTH_PATH="${HEALTH_PATH:-/api/docs/}"   # what the pre/post-flight curls probe
LOG_FORMAT_SNIPPET="$REPO_DIR/nginx/log-format-json.conf"

AVAILABLE="/etc/nginx/sites-available/$DOMAIN"
ENABLED="/etc/nginx/sites-enabled/$DOMAIN"
LIVE_DIR="/etc/letsencrypt/live/$DOMAIN"

log()  { printf '\n==> %s\n' "$*"; }
warn() { printf 'warning: %s\n' "$*" >&2; }
die()  { printf 'error: %s\n' "$*" >&2; exit 1; }

[ "$(id -u)" -eq 0 ] || die "run as root (sudo $0)"
[ -f "$TEMPLATE" ] || die "nginx template not found: $TEMPLATE"

# --- preflight ----------------------------------------------------------------

log "Preflight"

if ! curl -fsS -o /dev/null --max-time 5 "http://127.0.0.1:$UPSTREAM_PORT$HEALTH_PATH"; then
  warn "nothing healthy on 127.0.0.1:$UPSTREAM_PORT — is the gostyle-consumer stack running?"
  warn "continuing; nginx will 502 until it is."
fi

# ACME resolves the name from the outside, so a record pointing elsewhere is the
# single most common reason issuance fails. Compare against this host's addresses.
domain_ips="$(getent ahosts "$DOMAIN" | awk '{print $1}' | sort -u || true)"
if [ -z "$domain_ips" ]; then
  die "$DOMAIN does not resolve — create the A record first, then re-run"
fi
host_ips="$(hostname -I 2>/dev/null | tr ' ' '\n'; curl -fsS --max-time 5 https://api.ipify.org 2>/dev/null || true)"
if ! comm -12 <(echo "$domain_ips") <(echo "$host_ips" | sort -u) | grep -q .; then
  warn "$DOMAIN resolves to: $(echo "$domain_ips" | tr '\n' ' ')"
  warn "this host answers to: $(echo "$host_ips" | tr '\n' ' ')"
  warn "if those disagree, the HTTP-01 challenge will fail."
fi

# --- packages -----------------------------------------------------------------

if ! command -v nginx >/dev/null || ! command -v certbot >/dev/null; then
  log "Installing nginx and certbot"
  export DEBIAN_FRONTEND=noninteractive
  apt-get update -qq
  apt-get install -y -qq nginx certbot
fi

install -d -m 755 "$WEBROOT"

# --- shared log format --------------------------------------------------------

# `log_format` is an http-context directive, so it cannot live in a vhost. Both
# templates reference json_combined and nginx -t rejects an undefined format, so
# this has to land before any config test below.
if [ -f "$LOG_FORMAT_SNIPPET" ]; then
  log "Installing the JSON access-log format to /etc/nginx/conf.d/"
  install -m 644 "$LOG_FORMAT_SNIPPET" /etc/nginx/conf.d/log-format-json.conf
else
  warn "$LOG_FORMAT_SNIPPET missing — vhosts using json_combined will fail nginx -t"
fi

# --- certificate --------------------------------------------------------------

if [ -d "$LIVE_DIR" ]; then
  log "Certificate for $DOMAIN already present — skipping issuance"
else
  # Chicken and egg: the real config references cert files that do not exist
  # yet, so nginx would refuse to start. Serve the challenge from a plain-HTTP
  # site first, get the cert, then install the real thing.
  log "Installing a temporary HTTP-only site to answer the ACME challenge"
  cat > "$AVAILABLE" <<EOF
server {
    listen 80;
    listen [::]:80;
    server_name $DOMAIN;

    location ^~ /.well-known/acme-challenge/ {
        root $WEBROOT;
        default_type "text/plain";
    }

    location / {
        proxy_pass http://127.0.0.1:$UPSTREAM_PORT;
        proxy_set_header Host \$host;
        proxy_set_header X-Forwarded-For \$proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto \$scheme;
    }
}
EOF
  ln -sfn "$AVAILABLE" "$ENABLED"
  nginx -t
  systemctl reload nginx || systemctl start nginx

  log "Requesting a certificate for $DOMAIN"
  certbot_args=(certonly --webroot -w "$WEBROOT" -d "$DOMAIN"
                --non-interactive --agree-tos --no-eff-email --keep-until-expiring)
  if [ -n "$EMAIL" ]; then
    certbot_args+=(-m "$EMAIL")
  else
    warn "no EMAIL set — registering without one means no expiry warnings"
    certbot_args+=(--register-unsafely-without-email)
  fi
  [ "$STAGING" = "1" ] && certbot_args+=(--staging)

  certbot "${certbot_args[@]}"
fi

# --- real config --------------------------------------------------------------

log "Installing $AVAILABLE from $(basename "$TEMPLATE")"
sed -e "s/api\.gostyle\.uk/$DOMAIN/g" \
    -e "s/127\.0\.0\.1:3850/127.0.0.1:$UPSTREAM_PORT/g" \
    -e "s#root /var/www/certbot;#root $WEBROOT;#g" \
    "$TEMPLATE" > "$AVAILABLE"

# `http2 on;` is nginx >= 1.25.1. On older builds it is an unknown directive and
# nginx refuses to start, so fall back to the deprecated listen parameter.
nginx_ver="$(nginx -v 2>&1 | sed 's/.*\///')"
if ! printf '1.25.1\n%s\n' "$nginx_ver" | sort -V -C; then
  log "nginx $nginx_ver predates the http2 directive — using 'listen ... http2'"
  sed -i -e 's/^\( *\)http2 on;/\1# http2 on;  # needs nginx >= 1.25.1/' \
         -e 's/listen 443 ssl;/listen 443 ssl http2;/' \
         -e 's/listen \[::\]:443 ssl;/listen [::]:443 ssl http2;/' "$AVAILABLE"
fi

ln -sfn "$AVAILABLE" "$ENABLED"
nginx -t
systemctl reload nginx
systemctl enable --now nginx >/dev/null 2>&1 || true

# --- renewal ------------------------------------------------------------------

# certbot renews from a systemd timer, but nginx keeps the old certificate in
# memory until it is told otherwise.
install -d -m 755 /etc/letsencrypt/renewal-hooks/deploy
cat > /etc/letsencrypt/renewal-hooks/deploy/reload-nginx.sh <<'EOF'
#!/bin/sh
# Installed by gostyle-customer/scripts/setup-nginx.sh
systemctl reload nginx
EOF
chmod +x /etc/letsencrypt/renewal-hooks/deploy/reload-nginx.sh

systemctl enable --now certbot.timer >/dev/null 2>&1 \
  || warn "could not enable certbot.timer — check that renewals are scheduled"

# --- firewall -----------------------------------------------------------------

if command -v ufw >/dev/null && ufw status 2>/dev/null | grep -q '^Status: active'; then
  log "Opening 80/443 in ufw"
  ufw allow 'Nginx Full' >/dev/null
fi

# --- done ---------------------------------------------------------------------

log "Verifying"
curl -fsS -o /dev/null -w '%{url_effective} -> %{http_code}\n' "https://$DOMAIN$HEALTH_PATH" \
  || warn "https://$DOMAIN$HEALTH_PATH did not answer 2xx — check the app's DJANGO_ALLOWED_HOSTS"

cat <<EOF

==> nginx is terminating TLS for https://$DOMAIN -> 127.0.0.1:$UPSTREAM_PORT

Remaining steps, if not already done:

  1. The env file your stack actually reads — /opt/gostyle-consumer/.env for the
     deploy.sh flow, or whatever \`env_file:\` your stack file names — must contain:
       DJANGO_ALLOWED_HOSTS=$DOMAIN,127.0.0.1
       DJANGO_CSRF_TRUSTED_ORIGINS=https://$DOMAIN
     Swarm only re-reads it on deploy, so redeploy the stack afterwards.

  2. Port $UPSTREAM_PORT is still published on 0.0.0.0 by the stack, so
     http://<this-host>:$UPSTREAM_PORT bypasses TLS entirely. Docker writes its own
     iptables rules ahead of ufw, so block it in the DOCKER-USER chain:
       iptables -I DOCKER-USER -p tcp --dport $UPSTREAM_PORT ! -s 127.0.0.1 -j DROP
     (persist with iptables-persistent) — or drop it at the cloud firewall.

  3. Renewal dry run:  certbot renew --dry-run
EOF
