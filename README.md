# Multi-Modal Ticketing & Dynamic Pricing Aggregator

"Skyscanner for Nigeria". For a route and date it returns the **cheapest** and
**fastest** options across 8 road and air carriers, predicts whether the price
will **rise in the next 24 hours**, and books — or hands you a pre-filled
carrier checkout.

DAT 608 capstone project

> **All prices are simulated.** No public fare API exists for Nigerian carriers
> without a paid credential, so the project's own simulator drives all eight.
> The model's accuracy describes the simulator, not Nigeria.

---

## Requirements

Docker + Compose, **Python 3.12**, **~8 GB free RAM** (Kafka, ksqlDB and Connect
are JVMs). Node 22 for the web app. The dashboard needs R, which is optional —
nothing else depends on it:

```bash
sudo apt-get install -y r-base-core libpq-dev \
    r-cran-shiny r-cran-dbi r-cran-ggplot2 r-cran-dplyr
sudo Rscript -e 'install.packages("RPostgres", repos="https://cloud.r-project.org")'
```

Take shiny/ggplot2/dplyr from apt — from source they compile for ~20 minutes.

## Setup

```bash
git clone https://ProjectTeamCapstone/transport-aggregator
cd /transport-aggregator

cp .env.example .env                   # works with no API keys
pip install -r requirements.txt
pip install -r requirements-ml.txt     # optional, large: enables /predict

./scripts/bootstrap.sh                 # 6 containers, topics, ksqlDB, sinks
                                       # first run also BUILDS the Connect image (~5 min)
python -m simulator.run --once         # 2,280 prices — wait ~20s for the sinks
python -m ml.backfill                  # optional: history so /predict works
```

`bootstrap.sh` is idempotent — re-run it whenever the stack is cold. The trained
model ships in `ml/artifacts/`, so no retraining is needed.

## Run

```bash
python -m uvicorn api.main:app --port 8000 --workers 4
(cd ui && npm install && npm run dev)                  # :5173
Rscript -e "shiny::runApp('dashboard', port=3838)"     # :3838
```

Use `--workers 4` — ranking is CPU-bound Python. On a remote server add
`--host 0.0.0.0`, or tunnel:
`ssh -L 5173:localhost:5173 -L 8000:localhost:8000 -L 3838:localhost:3838 user@server`

## Try it

```bash
curl "localhost:8000/search?origin=LOS&dest=ABV&limit=3"
curl "localhost:8000/search/nl?q=cheapest+way+to+Abuja+next+week"
```

API docs at **/docs**. In the web app: search `cheapest way to Abuja next week`,
check **Fastest** (a flight's door-to-door time vs its time in the air), then
press **Book** twice — you get the same booking back, never two.

## Layout

| | |
|---|---|
| `common/` | Places, carriers, config, canonical schema, normalisation |
| `simulator/` | The data source — **there is no `connectors/` folder** |
| `stream/` | ksqlDB statements (one per carrier) + 2 Kafka Connect sinks |
| `api/` | `/search` `/search/nl` `/offer/{id}` `/predict` `/book` `/health` |
| `booking/` | Idempotency ledger, Duffel sandbox, deep links |
| `ml/` | Features, training, backfill, trained model |
| `ui/` `dashboard/` | React app; R Shiny dashboard (reads PostgreSQL directly) |
| `schemas/` `infra/` `scripts/` | Offer contract; Postgres init + Connect image; bootstrap |

Runtime code only — tests, session reports and design docs are kept separately.

## Two design points

**Booking can never happen twice.** The idempotency key is written to PostgreSQL
*before* any carrier is called, so a lost reply leaves a booking `unknown`
rather than duplicated. Four states: `pending` (handed to the carrier's
checkout — no seat held; the final answer for 6 of 8 carriers), `confirmed`,
`failed`, `unknown` (never retried automatically).

**Payment is structurally impossible.** The booking client can only request a
hold, refuses fares needing instant payment, rejects a live API token at
startup, and returns 422 for a request carrying card details.

## Troubleshooting

| Symptom | Cause |
|---|---|
| `pull access denied for naijafares-connect` | Old Docker Compose ignoring `pull_policy: build`. Run `docker compose build connect` first |
| `bootstrap.sh`: "only N/6 healthy" | Not enough RAM, or a port is taken — `docker compose ps` |
| `/search` returns `count: 0` | No prices yet — `python -m simulator.run --once`, wait ~20s |
| `/health`: `redis: down` | Containers not up — re-run `./scripts/bootstrap.sh` |
| `/predict` 503 | Missing ML packages, or no history (`python -m ml.backfill`) |
| `/predict` 404 | That offer has no recorded history yet |
| `Rscript: command not found` | R not installed — see Requirements. Dashboard only; nothing else is blocked |
| "Price may be out of date" | Working as designed — stale prices are shown and labelled, never hidden |
