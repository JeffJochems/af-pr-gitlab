#!/usr/bin/env sh
# Poll pr-af's /health endpoint until it responds or a timeout is hit.
#
# docker-compose.yml's own healthcheck gates `docker ps`'s view of container
# health, but a CI job additionally needs a *script-visible* wait, since
# `docker compose up -d` returns as soon as containers start, not once
# they're healthy.
set -eu

URL="${1:-http://localhost:8004/health}"
TIMEOUT="${2:-120}"
INTERVAL=3
elapsed=0

echo "[CI] Waiting for $URL (timeout ${TIMEOUT}s)..."
until curl -sf "$URL" >/dev/null 2>&1; do
  elapsed=$((elapsed + INTERVAL))
  if [ "$elapsed" -ge "$TIMEOUT" ]; then
    echo "[CI] Timed out waiting for $URL after ${TIMEOUT}s" >&2
    exit 1
  fi
  sleep "$INTERVAL"
done
echo "[CI] $URL is healthy after ${elapsed}s"
