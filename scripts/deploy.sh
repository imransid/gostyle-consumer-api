#!/usr/bin/env bash
#
# Deploys the consumer API stack to Docker Swarm.
#
# Runs ON the manager node -- CI copies this file and docker-compose.yml into
# $DEPLOY_DIR, then executes it over SSH with TAG set. Runtime secrets are read
# from $DEPLOY_DIR/.env on the server; they never travel through GitHub.
#
#   TAG=sha-1a2b3c4 ./deploy.sh
#
set -euo pipefail

STACK="${STACK:-gostyle-consumer}"
DEPLOY_DIR="${DEPLOY_DIR:-$(cd "$(dirname "$0")" && pwd)}"
NETWORK="${NETWORK:-gostyle_gostyle-net}"
IMAGE_REPO="${IMAGE_REPO:-ghcr.io/entity-tech-solutions/gostyle-platform/gostyle-consumer-api}"
TIMEOUT="${TIMEOUT:-300}"

SERVICE="${STACK}_consumer-api"

: "${TAG:?TAG is required (the image tag to deploy)}"

log() { printf '\n==> %s\n' "$*"; }

cd "$DEPLOY_DIR"

if [ ! -f .env ]; then
  echo "error: $DEPLOY_DIR/.env is missing (holds DJANGO_SECRET_KEY, DB_PASSWORD, ...)" >&2
  exit 1
fi

# Export everything in .env so both `docker stack deploy` interpolation and the
# one-off migration container see it.
set -a
# shellcheck disable=SC1091
. ./.env
set +a
export TAG

IMAGE="${IMAGE_REPO}:${TAG}"

# --- registry -----------------------------------------------------------------
# A long-lived read:packages token on the server, so Swarm can still pull when it
# reschedules a task days after the deploy (a CI GITHUB_TOKEN would be expired).
if [ -n "${GHCR_TOKEN:-}" ]; then
  log "Logging in to ghcr.io"
  printf '%s' "$GHCR_TOKEN" \
    | docker login ghcr.io -u "${GHCR_USER:?GHCR_USER must be set alongside GHCR_TOKEN}" --password-stdin
fi

log "Pulling $IMAGE"
docker pull "$IMAGE"

# --- migrations ---------------------------------------------------------------
# One-off container, before any new task can serve traffic. The image entrypoint
# deliberately does not migrate: with replicas > 1 the replicas would race.
attachable="$(docker network inspect -f '{{.Attachable}}' "$NETWORK" 2>/dev/null || echo missing)"
if [ "$attachable" != "true" ]; then
  echo "error: network '$NETWORK' is $([ "$attachable" = missing ] && echo 'missing' || echo 'not attachable')," >&2
  echo "       so a one-off migration container cannot reach Postgres." >&2
  echo "       Recreate it as: docker network create --driver overlay --attachable $NETWORK" >&2
  exit 1
fi

log "Running migrations"
docker run --rm --network "$NETWORK" \
  -e DJANGO_SETTINGS_MODULE=config.settings.production \
  -e DJANGO_SECRET_KEY \
  -e DJANGO_ALLOWED_HOSTS \
  -e DB_HOST="${DB_HOST:-gostyle_postgres}" \
  -e DB_PORT="${DB_PORT:-5432}" \
  -e DB_NAME="${DB_NAME:-gostyle}" \
  -e DB_USER="${DB_USER:-consumer_app}" \
  -e DB_PASSWORD \
  -e DB_SCHEMA="${DB_SCHEMA:-consumer}" \
  "$IMAGE" python manage.py migrate --noinput

# --- rollout ------------------------------------------------------------------
# --with-registry-auth forwards this login to every node so workers can pull.
log "Deploying stack $STACK"
docker stack deploy \
  --compose-file docker-compose.yml \
  --with-registry-auth \
  --prune \
  --detach=true \
  "$STACK"

log "Waiting for $SERVICE to converge (timeout ${TIMEOUT}s)"
deadline=$(( SECONDS + TIMEOUT ))
while :; do
  state="$(docker service inspect -f '{{if .UpdateStatus}}{{.UpdateStatus.State}}{{end}}' "$SERVICE" 2>/dev/null || echo '')"

  case "$state" in
    rollback_started|rollback_completed|paused)
      echo "deploy failed: update state is '$state' -- Swarm rolled back to the previous image" >&2
      docker service ps --no-trunc "$SERVICE" | head -20 >&2
      exit 1
      ;;
  esac

  desired="$(docker service inspect -f '{{.Spec.Mode.Replicated.Replicas}}' "$SERVICE" 2>/dev/null || echo 0)"
  running="$(docker service ps "$SERVICE" --filter 'desired-state=running' --format '{{.CurrentState}}' 2>/dev/null \
             | grep -c '^Running' || true)"

  if [ "$desired" -gt 0 ] && [ "$running" -eq "$desired" ] \
     && { [ -z "$state" ] || [ "$state" = "completed" ]; }; then
    log "Converged: $running/$desired tasks running $IMAGE"
    exit 0
  fi

  if [ "$SECONDS" -ge "$deadline" ]; then
    echo "deploy timed out after ${TIMEOUT}s (running=$running/$desired, state=${state:-none})" >&2
    docker service ps --no-trunc "$SERVICE" | head -20 >&2
    exit 1
  fi

  sleep 5
done
