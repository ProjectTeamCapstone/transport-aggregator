"""
Stage 4 — FastAPI search + booking-orchestrator (the presentation/serving layer).

Endpoints:
  GET  /health
  GET  /routes                      list available routes
  GET  /metrics                     model evaluation metrics
  GET  /search?origin=&destination=&date=&sort=&mode=
  POST /search/nl        {"q": "cheapest Lagos to Abuja tomorrow morning"}
  POST /book             {"trip_id": "..."}   (simulated carrier booking call)
  GET  /                             single-page search UI

Serving reads the `predictions` table (latest snapshot per trip + rise_prob),
mirroring a Redis-cached PostgreSQL read in production.
"""
from __future__ import annotations
import os
import json
import sqlite3
from typing import Optional

from fastapi import FastAPI, Query
from fastapi.responses import HTMLResponse, JSONResponse
from pydantic import BaseModel

import nl_search

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
DB_PATH = os.environ.get("AGG_DB", os.path.join(ROOT, "data", "aggregator.db"))
MODEL_DIR = os.path.join(ROOT, "models")
UI_PATH = os.path.join(HERE, "ui", "index.html")

CITY = {"LOS": "Lagos", "ABV": "Abuja", "ONI": "Onitsha",
        "PHC": "Port Harcourt", "LON": "London"}

app = FastAPI(title="Skyscanner-for-Nigeria — Multi-Modal Aggregator", version="1.0")


def db():
    # Open read-only so the app works on Vercel's read-only serverless filesystem.
    # Falls back to a normal connection on writable local filesystems.
    uri = f"file:{DB_PATH}?mode=ro"
    try:
        con = sqlite3.connect(uri, uri=True)
    except sqlite3.OperationalError:
        con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row
    return con


def _fmt(row: sqlite3.Row) -> dict:
    prob = float(row["rise_prob"])
    return {
        "trip_id": row["trip_id"],
        "route": row["route_id"],
        "carrier": row["carrier"],
        "mode": row["mode"],
        "departure_date": row["departure_date"],
        "departure_hour": row["departure_hour"],
        "duration_min": row["duration_min"],
        "price_ngn": round(row["price_ngn"]),
        "rise_prob": round(prob, 3),
        "advice": "Book now — likely to rise" if prob >= 0.6
                  else "Prices stable" if prob >= 0.4
                  else "Can wait — unlikely to rise",
    }


@app.get("/health")
def health():
    ok = os.path.exists(DB_PATH) and os.path.exists(os.path.join(MODEL_DIR, "price_rise_lgbm.txt"))
    return {"status": "ok" if ok else "not_ready", "db": os.path.exists(DB_PATH)}


@app.get("/routes")
def routes():
    con = db()
    rows = con.execute("SELECT * FROM routes ORDER BY route_id").fetchall()
    con.close()
    return [{"route_id": r["route_id"], "origin": r["origin_city"],
             "destination": r["dest_city"]} for r in rows]


@app.get("/metrics")
def metrics():
    p = os.path.join(MODEL_DIR, "metrics.json")
    if not os.path.exists(p):
        return JSONResponse({"error": "model not trained"}, status_code=404)
    with open(p) as f:
        return json.load(f)


def _search(origin, destination, date, sort, mode, part_of_day=None, limit=10):
    con = db()
    q = "SELECT * FROM predictions WHERE 1=1"
    args = []
    if origin and destination:
        q += " AND route_id = ?"
        args.append(f"{origin}-{destination}")
    if date:
        q += " AND departure_date = ?"
        args.append(date)
    if mode:
        q += " AND mode = ?"
        args.append(mode)
    if part_of_day and part_of_day in nl_search.PART_OF_DAY:
        lo, hi = nl_search.PART_OF_DAY[part_of_day]
        q += " AND departure_hour BETWEEN ? AND ?"
        args += [lo, hi]
    if sort == "time":
        order = "departure_date ASC, departure_hour ASC"
    elif sort == "fastest":
        order = "duration_min ASC"
    else:
        order = "price_ngn ASC"
    q += f" ORDER BY {order} LIMIT ?"
    args.append(limit)
    try:
        rows = con.execute(q, args).fetchall()
    except sqlite3.OperationalError:
        con.close()
        return None  # predictions table not built yet
    con.close()
    return [_fmt(r) for r in rows]


@app.get("/search")
def search(
    origin: Optional[str] = Query(None),
    destination: Optional[str] = Query(None),
    date: Optional[str] = Query(None),
    sort: str = Query("time"),
    mode: Optional[str] = Query(None),
    limit: int = Query(10, le=50),
):
    results = _search(origin, destination, date, sort, mode, limit=limit)
    if results is None:
        return JSONResponse(
            {"detail": "Model not trained yet — run `python run_all.py` first."},
            status_code=503)
    return {"count": len(results), "sort": sort, "results": results}


class NLQuery(BaseModel):
    q: str


@app.post("/search/nl")
def search_nl(body: NLQuery):
    parsed = nl_search.route_query(body.q)
    results = _search(parsed["origin"], parsed["destination"], parsed["date"],
                      parsed["sort"], parsed["mode"], parsed.get("part_of_day"))
    if results is None:
        return JSONResponse(
            {"detail": "Model not trained yet — run `python run_all.py` first."},
            status_code=503)
    return {"query": body.q, "parsed": parsed, "count": len(results), "results": results}


class BookRequest(BaseModel):
    trip_id: str


@app.post("/book")
def book(body: BookRequest):
    """Simulated booking-orchestrator call to the carrier API."""
    con = db()
    row = con.execute("SELECT * FROM predictions WHERE trip_id = ?",
                      (body.trip_id,)).fetchone()
    con.close()
    if not row:
        return JSONResponse({"error": "trip not found"}, status_code=404)
    import uuid
    return {
        "status": "confirmed",
        "booking_ref": "NG-" + uuid.uuid4().hex[:8].upper(),
        "trip_id": body.trip_id,
        "carrier": row["carrier"],
        "price_ngn": round(row["price_ngn"]),
        "note": "Simulated. In production this calls the carrier's booking API.",
    }


@app.get("/", response_class=HTMLResponse)
def home():
    if os.path.exists(UI_PATH):
        with open(UI_PATH, encoding="utf-8") as f:
            return f.read()
    return "<h1>UI not found</h1>"


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="127.0.0.1", port=8000)
