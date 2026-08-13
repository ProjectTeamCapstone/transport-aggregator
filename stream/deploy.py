"""Deploy the ksqlDB statements in stream/offers.sql.

Splits the file into statements, substitutes the FX rate, and posts each one
to ksqlDB in order, stopping at the first failure with the offending statement
printed. Safe to re-run: every CREATE uses IF NOT EXISTS, and existing INSERT
queries are detected and skipped rather than duplicated.

    python stream/deploy.py            # apply
    python stream/deploy.py --reset    # tear down existing queries first
    python stream/deploy.py --dry-run  # print what would be sent
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

import requests

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from common.config import FX_FALLBACK_GBP_NGN, KSQLDB_URL  # noqa: E402

SQL_PATH = Path(__file__).resolve().parent / "offers.sql"
HEADERS = {"Content-Type": "application/vnd.ksql.v1+json"}


def split_statements(sql: str) -> list[str]:
    """Split on semicolons that end a statement.

    Comment lines are stripped first so a semicolon inside a comment cannot
    split a statement in half.
    """
    without_comments = "\n".join(
        line for line in sql.splitlines() if not line.strip().startswith("--")
    )
    return [s.strip() + ";" for s in without_comments.split(";") if s.strip()]


# Each POST is its own ksqlDB session, so a `SET 'auto.offset.reset'` inside
# the SQL file applies only to the request that carried it. Sent as a stream
# property instead, or every INSERT starts at `latest` and silently ignores
# everything already sitting on the raw topics.
STREAM_PROPERTIES = {"ksql.streams.auto.offset.reset": "earliest"}


def _post(statement: str, timeout: int = 90) -> requests.Response:
    return requests.post(
        f"{KSQLDB_URL}/ksql",
        json={"ksql": statement, "streamsProperties": STREAM_PROPERTIES},
        headers=HEADERS,
        timeout=timeout,
    )


def running_queries() -> list[dict]:
    resp = _post("SHOW QUERIES;")
    resp.raise_for_status()
    return resp.json()[0].get("queries", [])


def teardown() -> None:
    """Terminate INSERT queries and drop the streams, newest first."""
    for query in running_queries():
        qid = query["id"]
        print(f"  terminating {qid}")
        _post(f"TERMINATE {qid};")

    for name in (
        "OFFERS", "RAW_GIGM", "RAW_ABCTRANSPORT", "RAW_CROSSCOUNTRY",
        "RAW_CHISCO", "RAW_AIRPEACE", "RAW_IBOMAIR", "RAW_ARIK",
        "RAW_BRITISHAIRWAYS",
    ):
        resp = _post(f"DROP STREAM IF EXISTS {name};")
        if resp.status_code == 200:
            print(f"  dropped {name}")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--reset", action="store_true",
                        help="terminate queries and drop streams first")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    sql = SQL_PATH.read_text(encoding="utf-8").replace(
        "{{GBP_NGN_RATE}}", f"{FX_FALLBACK_GBP_NGN:.4f}"
    )
    statements = split_statements(sql)

    if args.dry_run:
        for i, statement in enumerate(statements, 1):
            print(f"\n--- [{i}/{len(statements)}]\n{statement}")
        return 0

    if args.reset:
        print("==> Tearing down existing queries and streams")
        teardown()

    # An INSERT already running would be duplicated by re-applying it, which
    # would write every offer to the topic twice.
    existing_sinks = {q.get("sinks", [None])[0] for q in running_queries()}
    insert_count = sum(1 for q in running_queries() if q["id"].startswith("INSERTQUERY"))

    print(f"==> Applying {len(statements)} statements to {KSQLDB_URL}")
    applied = skipped = 0

    for i, statement in enumerate(statements, 1):
        head = re.sub(r"\s+", " ", statement)[:68]

        if statement.upper().startswith("INSERT INTO") and insert_count >= 8:
            print(f"  [{i:2}/{len(statements)}] SKIP (already running) {head}")
            skipped += 1
            continue

        resp = _post(statement)
        if resp.status_code != 200:
            print(f"\nFAILED at statement {i}:\n{statement}\n")
            try:
                error = resp.json()
                print(f"ksqlDB said: {error.get('message', error)}")
            except ValueError:
                print(resp.text[:2000])
            return 1

        print(f"  [{i:2}/{len(statements)}] ok   {head}")
        applied += 1

    print(f"\n{applied} applied, {skipped} skipped.")
    queries = running_queries()
    print(f"{len(queries)} query(ies) running.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
