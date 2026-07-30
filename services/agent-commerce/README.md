# Archisynapse Agent Commerce Rail

A fail-closed financial control and evidence service for AI agents, agent-to-agent payments, and AI model consumption.

## Guarantees

- No paid agent call without a scoped, expiring, revocable authorization.
- Atomic budget reservation prevents concurrent overspend.
- Invoice amount and payment hash come from LND's decode endpoint, not an advertised price.
- Price bait-and-switch is blocked before payment.
- Raw preimages are used only in memory for L402 delivery and are never stored.
- Paid-but-undelivered work still produces a signed receipt and consumes the real spend.
- Crash recovery looks up a known payment hash; it never blindly pays twice.
- Reputation changes only from a signed receipt plus a separate validator outcome.
- Discovery profiles cannot publish localhost/private endpoints in production.
- AI model calls require pre-call token and cost authorization.
- Provider/model prices come from versioned rate cards with source references, not hard-coded marketing claims.
- Actual input, output, cached-input, and reasoning usage produce signed receipts.
- Runaway calls, actual-over-estimate anomalies, provider mismatches, and emergency stops pause spend fail-closed.
- Provider usage events reconcile against internal request IDs; orphaned charges are surfaced as critical alerts.
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
python -m compileall -q agent_commerce
pytest -q
```

See `docs/AGENT_COMMERCE_RAIL.md` for the payment state machine and `docs/AI_TOKEN_SPEND_CONTROLS.md` for the token-consumption control contract.
