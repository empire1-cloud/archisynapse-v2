# Local proof run — 2026-10-04

Recorded output of `tools/proof-run/proof_run.py` against `main` at commit
`1aa7b67`. Every step passed (`99-summary.json`).

## What ran

- Gateway, fraud, transaction, ledger and analytics services from this repo,
  started locally (not Docker), on PostgreSQL 16 with migrations 000–007.
- Processor adapter: `stripe_test`, pointed at
  `tools/proof-run/stripe_stand_in.py`, a local stand-in for the Stripe test
  API. **No Stripe account, card network or money was involved.**
- Gateway `/status`: `test_mode: true`, `live_money: false`,
  `production_ready: false`.

## Files

| File | Proves |
| --- | --- |
| `00-environment.json` | Run conditions, gateway status, public receipt key |
| `01-merchants.json` | Two test merchants (ids only; API keys are never recorded) |
| `02-authorized-payment.json` | Completed payment, signed receipt, `signature_valid: true`, balanced ledger lines, row counts |
| `03-offline-verification.json` | Stored receipt verifies offline; a copy with a changed amount fails |
| `04-idempotency-replay.json` | Same key replays the same receipt; changed request with same key → 409; no new rows |
| `05-denied-actions.json` | No key / forged key → 401; cross-merchant read and refund → 404; missing Idempotency-Key → 422; row counts unchanged |
| `06-refund-reversal.json` | Refund → balanced reversal, original `REVERSED`, payment `REFUNDED`, refund attempt `LEDGER_SUCCEEDED` |
| `07-ledger-balance.json` | Every organization's journal nets to zero |
| `99-summary.json` | Step-by-step pass/fail |

## What this does not prove

- Payment through the real Stripe test API (needs a real `sk_test` key).
- A fraud **block** decision (this payment scored approve, 0).
- Load, latency, uptime, settlement or live money movement.
- Signed refund receipts (refunds are evidenced by ledger and refund records).

## Defects observed while preparing the run

- The gateway accepts any `customer_id`; the transaction service requires a
  UUID and fails the payment otherwise. The PROCESSOR_PROOF runbook example
  (`customer-proof-001`) hits this.
- The gateway sends an empty `last4` when none is given; the transaction
  service rejects it. The run supplied `last4` and `brand`.
- Fraud and analytics both create a `merchants` table. In one shared database
  (as in `docker-compose.yml`) analytics fails; the run gave analytics its own
  database.

## Reproduce

See `tools/proof-run/README.md`.
