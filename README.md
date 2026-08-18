# NaijaFares — Multi-Modal Ticketing & Dynamic Pricing Aggregator

You need to be in Abuja on Friday. The flight takes 75 minutes and the coach
takes twelve hours, so the choice looks obvious — until you add the crawl to
Murtala Muhammed, the check-in queue, and the ride from Abuja's airport into
town. Suddenly the flight eats most of a day, and it still costs several times
the coach fare. Meanwhile that coach was cheaper last week, and will probably
be dearer tomorrow.

So you open four browser tabs. Each airline shows you its own flights. Each
coach operator shows you its own coaches. Nobody shows you both, nobody counts
the traffic, and nobody tells you whether to book now or wait.

**NaijaFares is that missing page.** Give it a route and a date, and it returns
the **cheapest** and **fastest** ways to get there across 8 road and air
carriers — ranking "fastest" on real door-to-door time, not time in the air —
tells you whether the price is likely to **rise in the next 24 hours**, and
takes you through to booking.

DAT 608 capstone project.

### Try it now

| | |
|---|---|
| **Web app** | https://naijafares.spaincentral.cloudapp.azure.com/ |
| **Analytics dashboard** | https://naijafares.spaincentral.cloudapp.azure.com/dashboard/ |

Search `cheapest way to Abuja next week` in plain English, or pick cities from
the dropdowns. Switch to the **Fastest** tab to compare a flight's real
door-to-door time (traffic, check-in, transfers) against an overnight coach.

> **All prices are simulated.** No Nigerian carrier publishes a free fare API,
> so the project generates its own price data for all eight carriers. The
> prediction model's accuracy describes that simulator, not the real Nigerian
> market.

### What it covers

- **Routes:** Lagos ↔ Abuja, Onitsha, Port Harcourt, and London Heathrow
- **Carriers:** GIGM, ABC Transport, Cross Country, CHISCO (road); Air Peace,
  Ibom Air, Arik Air, British Airways (air)

---

## Requirements

- **Docker** + Docker Compose
- **Python 3.12**
- **Node 22** (for the web app)
- **~8 GB free RAM** — Kafka, ksqlDB and Kafka Connect are JVMs

**R is optional.** It only powers the analytics dashboard; nothing else depends
on it. To install it:

```bash
sudo apt-get install -y r-base-core libpq-dev \
    r-cran-shiny r-cran-dbi r-cran-ggplot2 r-cran-dplyr
sudo Rscript -e 'install.packages("RPostgres", repos="https://cloud.r-project.org")'
```

Take shiny/ggplot2/dplyr from apt — building them from source takes ~20 minutes.

---

## Local setup

Run these in order from a terminal.

### 1. Get the code

```bash
git clone https://github.com/ProjectTeamCapstone/transport-aggregator
cd transport-aggregator
```

### 2. Create your config file

```bash
cp .env.example .env
```

Every setting has a working default, so **the whole project runs with no API
keys at all.** Add keys later only if you want live Claude search or Duffel
sandbox booking.

### 3. Install the Python packages

```bash
pip install -r requirements.txt
pip install -r requirements-ml.txt     # optional, large — only needed for /predict
```

Skip the second line if you don't need price prediction. Without it `/predict`
returns a message explaining what to install, and everything else works.

### 4. Start the infrastructure

```bash
./scripts/bootstrap.sh
```

This starts 6 containers (Kafka, ksqlDB, Schema Registry, Redis, PostgreSQL,
Kafka Connect), creates the Kafka topics, deploys the stream-processing
statements, and wires up the two data sinks. **The first run also builds the
Kafka Connect image, which takes ~5 minutes.**

Safe to re-run any time the stack is cold.

### 5. Generate some prices

```bash
python -m simulator.run --once
```

Publishes one sweep of 2,280 prices into the pipeline. **Wait ~20 seconds** for
them to land in Redis and PostgreSQL before searching.

### 6. Generate price history (optional)

```bash
python -m ml.backfill
```

Replays ~40 days of past prices into PostgreSQL. Needed for `/predict` to work
and for the dashboard's charts to have anything to show. The trained model
already ships in `ml/artifacts/`, so you never need to retrain.

### 7. Run the application

Each of these runs in its own terminal:

```bash
python -m uvicorn api.main:app --port 8000 --workers 4   # API      → :8000
(cd ui && npm install && npm run dev)                    # web app  → :5173
Rscript -e "shiny::runApp('dashboard', port=3838)"       # dashboard → :3838 (optional)
```

Use `--workers 4` — ranking is CPU-bound. On a remote server add `--host 0.0.0.0`,
or tunnel the ports over SSH:

```bash
ssh -L 5173:localhost:5173 -L 8000:localhost:8000 -L 3838:localhost:3838 user@server
```

### 8. Check it works

```bash
curl "localhost:8000/health"
curl "localhost:8000/search?origin=LOS&dest=ABV&limit=3"
curl "localhost:8000/search/nl?q=cheapest+way+to+Abuja+next+week"
```

Interactive API documentation is at **http://localhost:8000/docs**.

---

## Layout

Data flows left to right: the simulator publishes to Kafka, ksqlDB normalises
it, and Kafka Connect writes it to Redis (for search) and PostgreSQL (for
history).

| Folder | What lives there |
|---|---|
| `simulator/` | **The data source.** Generates prices for all 8 carriers, each in its own messy format |
| `stream/` | **Normalisation.** ksqlDB statements — one per carrier — turning 8 formats into one shape, plus the 2 Kafka Connect sinks |
| `common/` | **Shared rules.** Places, carriers, config, the canonical offer schema, and the ranking logic |
| `api/` | **The public API.** `/search` `/search/nl` `/offer/{id}` `/predict` `/book` `/health` |
| `booking/` | **Booking.** Idempotency ledger, Duffel sandbox client, carrier deep links |
| `ml/` | **Prediction.** Features, training, history backfill, and the trained model |
| `ui/` | **Web app.** React + Vite |
| `dashboard/` | **Analytics.** R Shiny, reading PostgreSQL directly |
| `schemas/` `infra/` `scripts/` | The offer contract; Docker/Postgres/Caddy config; setup scripts |
| `tests/` `docs/` | Test suite; deployment guide |

---

## Troubleshooting

| Symptom | What to do |
|---|---|
| `pull access denied for naijafares-connect` | Old Docker Compose. Run `docker compose build connect` first |
| `bootstrap.sh`: "only N/6 healthy" | Not enough RAM, or a port is already taken — check `docker compose ps` |
| `/search` returns `count: 0` | No prices yet. Run `python -m simulator.run --once` and wait ~20s |
| `/health` says `redis: down` | Containers aren't running — re-run `./scripts/bootstrap.sh` |
| `/predict` returns 503 | Missing ML packages, or no history yet — run `python -m ml.backfill` |
| `/predict` returns 404 | That specific offer has no recorded price history yet |
| `Rscript: command not found` | R isn't installed. Dashboard only — nothing else is affected |
| "Price may be out of date" | Working as designed. Stale prices are shown and labelled, never hidden |

---

## Project contributors

- [Warieta Gift Ejovwoke](https://github.com/giftwarieta)
- [Ndionu Nnamdi](https://github.com/nunclud)
- [Ipadeola Ladipo](https://github.com/rileydrizzy)
- [Macaulay Emmanuel](https://github.com/Oba-max22)
- [Maduechesi Chidiebere](https://github.com/jennifermaduechesi)
- [Lasisi Oluwadolapo](https://github.com/Oluwadolaposi)
- [Maduagwuna Onyedikachukwu](https://github.com/lotannamoldon)
