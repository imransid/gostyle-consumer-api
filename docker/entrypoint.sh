#!/bin/sh
set -e

# The db service is gated by a healthcheck in compose, but keep a short retry
# loop so migrations don't race a not-quite-ready Postgres.
echo "Applying database migrations..."
python manage.py migrate --noinput

exec "$@"
