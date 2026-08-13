#!/usr/bin/env bash
# Bring the whole stack up from cold, in the right order.
#
# Written because a Studio restart removes the containers and clears the Kafka
# topics, and doing this by hand invites doing it half-right. Safe to re-run.
set -euo pipefail

cd "$(dirname "$0")/.."

echo "==> Building the Kafka Connect image (first run takes a few minutes)"
# Explicit, because the connect service is built locally rather than pulled.
# `pull_policy: build` in docker-compose.yml covers this too, but older Compose
# versions ignore it and then fail with "pull access denied" on a fresh machine.
docker compose build connect

echo "==> Starting containers"
docker compose up -d

echo "==> Waiting for all services to report healthy"
for _ in $(seq 1 40); do
    healthy=$(docker ps --filter health=healthy --filter name=naijafares -q | wc -l)
    [ "$healthy" -ge 6 ] && break
    sleep 5
done

healthy=$(docker ps --filter health=healthy --filter name=naijafares -q | wc -l)
if [ "$healthy" -lt 6 ]; then
    echo "ERROR: only $healthy/6 services healthy. Check: docker compose ps" >&2
    docker compose ps >&2
    exit 1
fi
echo "    all 6 healthy"

echo "==> Creating Kafka topics (idempotent)"
python3 scripts/create_topics.py

echo "==> Applying ksqlDB statements"
python3 stream/deploy.py

echo "==> Deploying Kafka Connect sinks (offers -> Redis, offers -> PostgreSQL)"
python3 stream/deploy_connectors.py

echo
echo "Stack is up. Next:"
echo "  python -m simulator.run --once     # publish one sweep of prices"
echo "  uvicorn api.main:app --port 8000   # start the search API"
echo "  (cd ui && npm run dev)             # start the web app"
echo "  Rscript -e \"shiny::runApp('dashboard', port=3838)\"   # dashboard"
echo "  pytest                             # full suite"
