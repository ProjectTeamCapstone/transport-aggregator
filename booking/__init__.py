"""Phase 5: the booking orchestrator.

Three pieces, deliberately separated:

    ledger.py       remembers every booking attempt, in PostgreSQL
    providers.py    the two ways to book: Duffel sandbox, or a deep link
    orchestrator.py the order of operations that makes double-booking impossible

Nothing in this package touches money. Duffel is used in sandbox only, and only
for HOLD orders, which reserve a seat without payment. Payment processing is out
of scope for this project and there is no code path that could reach it.
"""
