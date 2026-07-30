# Archisynapse Agent Commerce Rail

A fail-closed payment and evidence service for AI agents that hire other agents.

## Guarantees

- No paid call without a scoped, expiring, revocable authorization.
- Atomic budget reservation prevents concurrent overspend.
- Invoice amount and payment hash come from LND's decode endpoint, not an advertised price.
- Price bait-and-switch is blocked before payment.
- Raw preimages are used only in memory for L402 delivery and are never stored.
- Paid-but-undelivered work still produces a signed receipt and consumes the real spend.
- Crash recovery looks up a known payment hash; it never blindly pays twice.
- Reputation changes only from a signed receipt plus a separate validator outcome.
- Discovery profiles cannot publish localhost/private endpoints in production.
- Every state transition is chained into an append-only audit log.

## Run

```bash
cp .env.example .env
set -a; source .env; set +a
uvicorn agent_commerce.api:app --host 0.0.0.0 --port 8090
```

## Test

```bash
python -m pip install -e '.[test]'
pytest
```

See `docs/AGENT_COMMERCE_RAIL.md` for the state machine and API contract.
