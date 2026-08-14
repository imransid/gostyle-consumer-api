#!/usr/bin/env bash
#
# Deploys Grafana Alloy, the log collector, onto this Swarm node.
#
# Loki and Grafana are NOT deployed here -- they already run in the `gostyle`
# platform stack (`gostyle_loki`, `gostyle_grafana`). Alloy is the piece that was
# missing: until it existed, that Loki held no logs at all. This script never
# touches either service.
#
# Runs ON the Swarm manager, as root, from the repo checkout. Idempotent:
# re-running redeploys with whatever the config files now say.
#
#   sudo ./scripts/setup-observability.sh
#
set -euo pipefail

REPO_DIR="$(cd "$(dirname "$0")/.." && pwd)"
STACK_NAME="${STACK_NAME:-gostyle-observability}"
STACK_FILE="${STACK_FILE:-$REPO_DIR/docker-stack.observability.yml}"
ENV_FILE="${ENV_FILE:-$REPO_DIR/observability.env}"

# Where Loki lives. Stack-qualified, because Loki is in the `gostyle` stack and
# Alloy is not, so the bare short name is not guaranteed to resolve.
LOKI_SERVICE="${LOKI_SERVICE:-gostyle_loki}"
LOKI_NETWORK="${LOKI_NETWORK:-gostyle_gostyle-net}"

log()  { printf '\n==> %s\n' "$*"; }
warn() { printf 'warning: %s\n' "$*" >&2; }
die()  { printf 'error: %s\n' "$*" >&2; exit 1; }

[ "$(id -u)" -eq 0 ] || die "run as root (sudo $0)"
[ -f "$STACK_FILE" ] || die "stack file not found: $STACK_FILE"

command -v docker >/dev/null || die "docker is not installed"
docker info 2>/dev/null | grep -q 'Swarm: active' \
  || die "this node is not in a Swarm — run 'docker swarm init' first"

# --- preflight ----------------------------------------------------------------

log "Preflight"

docker service inspect "$LOKI_SERVICE" >/dev/null 2>&1 \
  || die "$LOKI_SERVICE is not running — this stack ships logs to it, it does not deploy it"

docker network inspect "$LOKI_NETWORK" >/dev/null 2>&1 \
  || die "network $LOKI_NETWORK not found — Alloy attaches to it to reach Loki"

# The single most likely failure is Alloy not resolving Loki's name. Prove it
# from a throwaway container on the same network before deploying anything.
printf '    resolving %s on %s ... ' "$LOKI_SERVICE" "$LOKI_NETWORK"
if docker run --rm --network "$LOKI_NETWORK" busybox:1.36 \
     nslookup "$LOKI_SERVICE" >/dev/null 2>&1; then
  printf 'ok\n'
else
  die "cannot resolve $LOKI_SERVICE — set LOKI_URL in $ENV_FILE to a reachable address"
fi

# --- env ----------------------------------------------------------------------

if [ ! -f "$ENV_FILE" ]; then
  log "Creating $ENV_FILE"
  cat > "$ENV_FILE" <<EOF
# Read by scripts/setup-observability.sh. Holds no secrets today, but it is
# gitignored so per-host settings never end up in a commit.
LOKI_URL=http://$LOKI_SERVICE:3100/loki/api/v1/push
OBS_DIR=$REPO_DIR/observability
EOF
  chmod 600 "$ENV_FILE"
fi

# `docker stack deploy` interpolates from the environment, not from a file.
set -a
# shellcheck disable=SC1090
. "$ENV_FILE"
set +a
export OBS_DIR="${OBS_DIR:-$REPO_DIR/observability}"

# The config is bind-mounted, so a wrong OBS_DIR shows up as an empty mount and
# a crash-looping container with a confusing error. Check now.
[ -f "$OBS_DIR/alloy/config.alloy" ] || die "missing $OBS_DIR/alloy/config.alloy — is OBS_DIR right?"

# --- deploy -------------------------------------------------------------------

log "Deploying stack '$STACK_NAME' from $(basename "$STACK_FILE")"
# No --prune: this stack owns exactly one service, and pruning has a history of
# deleting things it was not meant to on this box.
docker stack deploy -c "$STACK_FILE" "$STACK_NAME"

# --- wait ---------------------------------------------------------------------

log "Waiting for Alloy to report a healthy pipeline"
for _ in $(seq 1 24); do
  state="$(docker service ps "${STACK_NAME}_alloy" \
            --filter desired-state=running --format '{{.CurrentState}}' 2>/dev/null | head -1)"
  case "$state" in
    Running*) printf '    %s\n' "$state"; break ;;
  esac
  sleep 5
done

log "Checking that logs are arriving in Loki"
found=""
for _ in $(seq 1 24); do
  if docker run --rm --network "$LOKI_NETWORK" curlimages/curl:8.10.1 \
       -fsS --max-time 5 "http://$LOKI_SERVICE:3100/loki/api/v1/label/service/values" \
       2>/dev/null | grep -q '"data"'; then
    found=yes
    break
  fi
  sleep 5
done
[ -n "$found" ] || warn "no 'service' label in Loki yet — check 'docker service logs ${STACK_NAME}_alloy'"

# --- done ---------------------------------------------------------------------

cat <<EOF

==> Stack '$STACK_NAME' deployed. Loki and Grafana were not touched.

  docker service logs -f ${STACK_NAME}_alloy
  docker service ps ${STACK_NAME}_alloy

Logs are now flowing into the existing $LOKI_SERVICE. Open the platform Grafana,
pick the Loki data source, and try:

  {service="gostyle-consumer_api"}
  {service="gostyle-consumer_api", level=~"WARNING|ERROR|CRITICAL"}

The dashboard JSON is at observability/grafana/dashboards/gostyle-api-logs.json —
import it through Grafana's UI (Dashboards -> New -> Import -> paste JSON). It is
not provisioned from a file, so that the platform's Grafana config stays as it is.
EOF
