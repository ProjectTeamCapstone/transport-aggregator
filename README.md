# NaijaFare — Multi-Modal Ticketing & Dynamic Pricing (Project 03)

A **runnable, end-to-end reference implementation** of the "Skyscanner for Nigeria"
aggregator: it ingests fares from multiple road and air carriers, unifies them into
a single `offers` feed, predicts whether each fare will **rise in the next 24 hours**
with a LightGBM model, and serves cheapest/fastest search + natural-language search +
booking through a FastAPI web app.

Because live GIGM / Air Peace / Wakanow feeds aren't publicly available (and scraping
real carrier sites is fragile and often blocked), the ingestion stage produces
**realistic synthetic fare trajectories**. Every downstream stage — normalization,
modeling, serving, UI - is real and production-shaped, so you can swap the synthetic
generator for real Kafka source connectors without touching the rest.

## Quick start

# frontend deployed on vercel https://transport-aggregator-gp3.vercel.app/


```bash
pip install -r requirements.txt
python run_all.py            # generate -> normalize -> train -> serve on :8000
# then open http://127.0.0.1:8000 (this is link to currenlty run on our local machine)
```

Build the data + model without starting the server:

```bash
python run_all.py --no-serve
python src/api.py            # start the API separately
```

## Pipeline stages

| Stage | File | Production equivalent |
|---|---|---|
| 1. Ingest fares | `src/generate_fares.py` | Per-carrier **Kafka source connectors** (REST poll + Scrapy/Playwright scrapers), one topic per carrier |
| 2. Normalize | `src/normalize.py` | **ksqlDB** stream queries unifying carrier topics into one `offers` topic, sunk to **PostgreSQL** |
| 3. Model | `src/train_model.py` + `src/features.py` | **LightGBM** "price will rise in 24h" classifier, distributed with **Ray**, tracked in MLflow |
| 4. Serve | `src/api.py` + `src/ui/` | **FastAPI** search + booking-orchestrator, **Redis**-cached reads, **React/Streamlit + R Shiny** UI, **Anthropic Claude** for NL search |

The prototype uses **SQLite** in place of PostgreSQL+Redis so it runs with zero setup;
the schema and SQL are Postgres-compatible.

## API endpoints

| Method | Path | Description |
|---|---|---|
| GET | `/` | Search web UI |
| GET | `/health` | Readiness check |
| GET | `/routes` | Available routes |
| GET | `/metrics` | Model AUC / accuracy / feature importance |
| GET | `/search?origin=LOS&destination=ABV&sort=cheapest&mode=air` | Structured search |
| POST | `/search/nl` `{"q":"cheapest Lagos to Abuja tomorrow morning"}` | Natural-language search |
| POST | `/book` `{"trip_id":"..."}` | Simulated carrier booking |

## The model

Target: for each fare snapshot, **will the price be higher at the next daily snapshot (~24h)?**
Features: current price, days-to-departure, duration, departure hour, day-of-week,
mode (air/road), price relative to the route median, recent 3-snapshot price trend,
and carrier cheapness rank. A time-aware 80/20 split trains LightGBM; the latest
snapshot per trip is scored and written to a `predictions` table the API serves.

Typical run: **AUC ≈ 0.63, accuracy ≈ 69%** on held-out data, with `days_to_departure`
and `price_trend_3d` the dominant features — i.e. fares are most predictable close to
departure and when they already have upward momentum. (These are synthetic-data numbers;
real fare feeds would re-fit the same pipeline.)

## Plugging in real data

1. Replace `generate_fares.py` with Kafka source connectors / scrapers that write the
   same raw columns per carrier.
2. Point `normalize.py` (and `AGG_DB`) at PostgreSQL instead of SQLite.
3. Add a scheduler (cron/Airflow) to re-run stages 1–3 on an interval; keep stage 4 running.
4. Set `USE_CLAUDE=1` and `ANTHROPIC_API_KEY` to route NL search through Anthropic Claude
   (see `src/nl_search.py:parse_with_claude`).

## Config

- `AGG_DB` — path to the SQLite DB (default `data/aggregator.db`). Set this if your
  filesystem doesn't support SQLite locking (e.g. some network mounts).

## Layout

```
transport_aggregator/
├── run_all.py            # one-command end-to-end runner
├── requirements.txt
├── README.md
└── src/
    ├── generate_fares.py # stage 1 — synthetic per-carrier feeds
    ├── normalize.py      # stage 2 — unify into canonical offers
    ├── features.py       # shared feature engineering + label
    ├── train_model.py    # stage 3 — LightGBM training + scoring
    ├── nl_search.py      # NL query parser (Claude-ready)
    ├── api.py            # stage 4 — FastAPI service
    └── ui/index.html     # single-page search UI
```
