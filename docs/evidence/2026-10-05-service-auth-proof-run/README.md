# Proof run with service-to-service authentication enforced — 2026-10-05

Recorded output of `tools/proof-run/proof_run.py` (steps 00–07) and
`tools/proof-run/service_auth_probe.py` (step 08) against `main` at `1f31934`
plus the service-authentication change in the same pull request as this
folder. Every step passed (`99-summary.json`, `08-service-auth-denials.json`).
The `git_commit` field in the JSON is the local commit the run was made from,
before the files were uploaded to GitHub, so it does not appear in this
repository's history.

## What ran

- Gateway, fraud, transaction, ledger and analytics services from this repo,
  started locally (not in Docker), on PostgreSQL 16 with migrations 000–008.
- Ledger and transaction started the way their Docker images start them:
  `node dist/index.js`, configured with `DATABASE_URL`.
- `ARCHISYNAPSE_SERVICE_AUTH=enforce` on both. Tokens created by
  `infra/service-auth/generate_service_tokens.sh` (the same script the
  `service-tokens` compose service runs) and passed as files, each service
  getting only its own: the gateway its two outbound tokens, the transaction
  service its ledger token, and both receiving services only SHA-256 digests
  of their callers' tokens.
- Processor adapter: `stripe_test`, pointed at a local stand-in for the Stripe
  test API. **No Stripe account, card network or money was involved.**

## Files

| File | Proves |
| --- | --- |
| `00`–`07`, `99-summary.json` | The full payment, receipt, idempotency, denial, refund and ledger-balance run (same steps as `2026-10-04-local-proof-run`) still passes with every gateway → transaction → ledger call authenticated |
| `08-service-auth-denials.json` | Calling the ledger or transaction service directly, bypassing the gateway: only `X-Organization-ID` → 401 (reads, writes, account create, payment, refund, royalty); made-up token → 401; a token for another hop → 401; gateway token posting to the ledger → 403; gateway token reading the ledger → 200; zero rows written by the refused calls; no token value in any service log |

Also checked during this run (not in the JSON): with
`ARCHISYNAPSE_SERVICE_AUTH=enforce` and no tokens configured, the ledger
service exits with status 1 and never opens its port. An unauthenticated
request with a malformed body gets 401, not 500 (covered by the unit tests).

## What this does not prove

- The Docker images themselves (this run used the same entry point and
  configuration, but not containers). The `Docker stack` CI job covers that.
- Payment through the real Stripe test API (needs a real `sk_test` key).
- Protection against a compromised gateway: the gateway holds a valid token,
  so it can still act for any merchant. Merchant authentication at the gateway
  is what stops that.
- Encryption of internal traffic. Tokens travel in plain HTTP between services
  on the private network; use TLS or a private network you control in
  production.
- Load, latency, uptime, settlement or live money movement.
