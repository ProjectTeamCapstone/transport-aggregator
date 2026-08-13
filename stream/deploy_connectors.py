"""Deploy every Kafka Connect sink in stream/connectors/.

Idempotent: PUT .../config creates or updates in place, so re-running is safe.
Keys beginning with '//' are comments and are stripped before sending.

Credentials are NOT stored in the connector JSON. Each file carries
{{PLACEHOLDER}} markers that are filled from the environment at deploy time,
the same way stream/deploy.py substitutes {{GBP_NGN_RATE}} into offers.sql.
.env is the single source of truth, and it is the only file holding a real
password - which matters because .env is gitignored and stream/connectors/ is not.

    python stream/deploy_connectors.py            # apply and report status
    python stream/deploy_connectors.py --dry-run  # print what would be sent
    python stream/deploy_connectors.py --delete   # remove them
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import time
from pathlib import Path

import requests

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from common.config import (  # noqa: E402
    POSTGRES_DB,
    POSTGRES_PASSWORD,
    POSTGRES_USER,
)

CONNECT_URL = "http://localhost:8083"
CONNECTOR_DIR = Path(__file__).resolve().parent / "connectors"

# Deliberately no POSTGRES_HOST or POSTGRES_PORT. Connect runs *inside* the
# Docker network and must reach the database as postgres:5432, whereas
# POSTGRES_HOST is localhost - correct for host-side Python, wrong here.
# Templating it would substitute a value that cannot resolve from a container.
SUBSTITUTIONS = {
    "POSTGRES_USER": POSTGRES_USER,
    "POSTGRES_PASSWORD": POSTGRES_PASSWORD,
    "POSTGRES_DB": POSTGRES_DB,
}

_PLACEHOLDER = re.compile(r"\{\{(\w+)\}\}")


def substitute(value: str, source: Path) -> str:
    """Fill {{PLACEHOLDER}} markers, refusing to send any that remain.

    Substitution happens on the parsed values rather than the raw file text so
    a password containing a quote or backslash cannot break JSON parsing.

    An unrecognised placeholder is a hard error. Posting a literal
    '{{POSTGRES_PASSWORD}}' as the password would leave Connect RUNNING while
    every record failed authentication - precisely the silent-success failure
    mode that D-009 cost a day to find.
    """
    filled = _PLACEHOLDER.sub(
        lambda m: SUBSTITUTIONS.get(m.group(1), m.group(0)), value
    )
    unknown = sorted(set(_PLACEHOLDER.findall(filled)))
    if unknown:
        raise SystemExit(
            f"{source.name}: no value for placeholder(s) "
            f"{', '.join('{{' + u + '}}' for u in unknown)}. "
            f"Known: {', '.join(sorted(SUBSTITUTIONS))}."
        )
    return filled


def load(path: Path) -> tuple[str, dict]:
    spec = json.loads(path.read_text(encoding="utf-8"))
    config = {
        k: substitute(v, path) if isinstance(v, str) else v
        for k, v in spec["config"].items()
        if not k.startswith("//")
    }
    return spec["name"], config


def redacted(config: dict) -> dict:
    """A copy safe to print - --dry-run must not spill the password."""
    return {
        k: "***" if "password" in k.lower() else v
        for k, v in config.items()
    }


def wait_for_connect(timeout: int = 180) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            if requests.get(f"{CONNECT_URL}/connectors", timeout=5).status_code == 200:
                return
        except requests.RequestException:
            pass
        time.sleep(3)
    raise RuntimeError(f"Kafka Connect not reachable at {CONNECT_URL}")


def status(name: str) -> tuple[str, list[str]]:
    resp = requests.get(f"{CONNECT_URL}/connectors/{name}/status", timeout=15)
    if resp.status_code != 200:
        return f"HTTP {resp.status_code}", []
    body = resp.json()
    problems = [
        t["trace"].strip().splitlines()[0]
        for t in body.get("tasks", [])
        if t.get("state") == "FAILED" and t.get("trace")
    ]
    states = [t.get("state", "?") for t in body.get("tasks", [])]
    return f"{body['connector']['state']} (tasks: {', '.join(states) or 'none'})", problems


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--delete", action="store_true")
    parser.add_argument("--dry-run", action="store_true",
                        help="print the substituted config without deploying")
    args = parser.parse_args()

    specs = sorted(CONNECTOR_DIR.glob("*.json"))
    if not specs:
        print(f"no connector definitions in {CONNECTOR_DIR}")
        return 1

    # Before wait_for_connect: --dry-run is for checking substitution, and it
    # should work whether or not the stack is running.
    if args.dry_run:
        for path in specs:
            name, config = load(path)
            print(f"\n--- {name} ({path.name})")
            print(json.dumps(redacted(config), indent=2))
        return 0

    wait_for_connect()

    if args.delete:
        for path in specs:
            name, _ = load(path)
            requests.delete(f"{CONNECT_URL}/connectors/{name}", timeout=30)
            print(f"  deleted {name}")
        return 0

    for path in specs:
        name, config = load(path)
        resp = requests.put(
            f"{CONNECT_URL}/connectors/{name}/config",
            json=config, timeout=60,
        )
        if resp.status_code not in (200, 201):
            print(f"FAILED to deploy {name}: HTTP {resp.status_code}")
            print(json.dumps(resp.json(), indent=2)[:2000])
            return 1
        print(f"  deployed {name}")

    print("\nWaiting for tasks to settle...")
    time.sleep(15)

    failed = 0
    for path in specs:
        name, _ = load(path)
        state, problems = status(name)
        print(f"  {name:<22} {state}")
        for problem in problems:
            print(f"      ! {problem[:220]}")
            failed += 1
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
