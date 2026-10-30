#!/usr/bin/env bash
#
# Downloads DB-IP's free "IP to City Lite" database, the file a session's
# location ("Dubai, UAE") is read from (apps/accounts/geoip.py), to
# /var/lib/GeoIP/dbip-city-lite.mmdb. The api reads it as GEOIP_CITY_DB.
#
# Free: no account, no key, nothing that can ever charge. Its licence is
# CC BY 4.0, so the app credits DB-IP (docs/DEVICE_SESSIONS_API.md).
#
# Runs ON the Swarm node, as root, once by hand and then monthly from cron
# (docs/DEVICE_SESSIONS_SERVER.md step 3):
#
#   sudo ./scripts/update-geoip.sh
#
# DB-IP publishes a new file early each month; until this month's is out,
# last month's is fetched. The new file replaces the old one in one move,
# and the api notices it by itself, no restart. A failed download leaves the
# old file as it was.
#
set -euo pipefail

DIR="${GEOIP_DIR:-/var/lib/GeoIP}"
OUT="$DIR/dbip-city-lite.mmdb"

mkdir -p "$DIR"
tmp="$(mktemp "$DIR/.dbip-city-lite.XXXXXX")"
trap 'rm -f "$tmp"' EXIT

this_month="$(date -u +%Y-%m)"
last_month="$(date -u -d "$(date -u +%Y-%m-01) -1 day" +%Y-%m)"

for month in "$this_month" "$last_month"; do
  url="https://download.db-ip.com/free/dbip-city-lite-$month.mmdb.gz"
  if curl -fsSL --max-time 900 "$url" | gunzip > "$tmp" && [ "$(stat -c %s "$tmp")" -gt 10000000 ]; then
    chmod 644 "$tmp"
    mv -f "$tmp" "$OUT"
    echo "$(date -u +%FT%TZ) $OUT is now DB-IP City Lite $month ($(du -h "$OUT" | cut -f1))"
    exit 0
  fi
done

echo "$(date -u +%FT%TZ) could not download DB-IP City Lite; $OUT stays as it was" >&2
exit 1
