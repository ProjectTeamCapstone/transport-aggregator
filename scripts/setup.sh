#!/bin/bash

set -e

echo "=== Setting up NaijaFares ==="

# Make sure we're running from the project root
cd "$(dirname "$0")/.."

# Activate virtual environment
source .venv/bin/activate

echo "=== Creating .env ==="
if [ ! -f .env ]; then
    cp .env.example .env
fi

echo "=== Installing Python dependencies ==="
pip install -r requirements.txt
pip install -r requirements-ml.txt

echo "=== Bootstrapping services ==="
chmod +x ./scripts/bootstrap.sh
./scripts/bootstrap.sh

echo "=== Generating market data ==="
python -m simulator.run --once

echo "=== Backfilling ML history ==="
python -m ml.backfill

echo "=== Setup complete ==="
