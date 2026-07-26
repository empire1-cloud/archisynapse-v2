# Archisynapse

Archisynapse is a payment and accounting system being built under Empire-1.

It receives a payment request, checks risk, records the payment, posts balanced ledger entries, updates revenue records, and returns a receipt that shows what happened.

Archisynapse is its own business and system. Lyrica 3, Southern Arcade, other Empire-1 products, and outside companies may use it as customers.

## What exists in this repository

The repository contains separate services for:

- payment and refund handling
- double-entry ledger posting
- fraud and risk checks
- revenue analytics
- an authenticated API gateway
- PostgreSQL storage
- Redis support
- signed royalty-event processing
- idempotency and recovery paths

The payment flow is:

```text
Merchant request
  -> merchant authentication
  -> duplicate-request check
  -> risk check
  -> transaction service
  -> ledger posting
  -> analytics update
  -> stored receipt
```

The transaction service is the only service allowed to create financial ledger postings. The gateway coordinates the flow but does not write directly to the ledger.

## Current status

| Area | Current status | What still needs proof or work |
|---|---|---|
| Payment records | Implemented | Connect and verify a real external payment processor |
| Refund records | Implemented | Add processor-specific refund adapters and production tests |
| Double-entry ledger | Implemented | Continue reconciliation and failure testing |
| Fraud service | Implemented as a risk service | Do not claim a detection rate until measured with a documented dataset |
| Analytics service | Implemented | Validate reports against production-like transaction data |
| API gateway | Authenticated production entrypoint implemented | Run the full Docker and PostgreSQL boundary suite in CI |
| Merchant authentication | Implemented | Add API-key rotation and merchant suspension endpoints |
| Internal service credentials | Encrypted in PostgreSQL | Move the encryption key into a managed secret service before production |
| Durable idempotency | Implemented with request-hash conflict checks | Add concurrency tests against PostgreSQL |
| Durable payment receipts | Implemented in PostgreSQL | Add signed payment receipts and export verification |
| Royalty event path | Implemented behind a disabled-by-default flag | Complete deployment verification with approved tenant keys |
| External settlement | Not proven | Add an approved processor or banking partner adapter |
| Compliance certification | Not claimed | Complete the required audits before using certification language |
| Performance and uptime | Not claimed | Run repeatable load and reliability tests and publish the evidence |
| Customer and revenue traction | Not claimed in this repository | Use only founder-approved, documented records |

## What this repository does not claim

This project does not currently claim:

- a specific settlement speed
- lower fees than another company
- a transaction-per-second capacity
- a fraud detection percentage
- PCI-DSS, SOC 2, ISO 27001, or other certification
- a specific number of customers, pilots, developers, or revenue
- production readiness

Those statements may be used only after there is current evidence that can be reviewed.

## Run the development stack

Requirements:

- Docker with Compose support
- available local ports used by `docker-compose.yml`

Copy the local settings file and add secrets:

```bash
cp .env.example .env
```

Start the stack:

```bash
docker compose up --build
```

Main local services:

```text
Gateway:      http://127.0.0.1:9000
Transaction:  http://127.0.0.1:3000
Ledger:       http://127.0.0.1:3001
Fraud:        http://127.0.0.1:8000
Analytics:    http://127.0.0.1:8081
PostgreSQL:   127.0.0.1:5432
Redis:        127.0.0.1:6379
```

Check the gateway and its dependencies:

```bash
curl http://127.0.0.1:9000/health
```

Read the setup and verification flow in [`docs/GATEWAY_SETUP.md`](docs/GATEWAY_SETUP.md).

## Safety rules

- Never store raw card details in this system.
- Never place secrets in payment events, receipts, logs, or source control.
- Every money amount must use fixed precision or minor units.
- Every financial transaction must balance.
- Repeated requests with the same idempotency key must not create duplicate money movement.
- One idempotency key cannot be reused for a different request.
- A failed fraud check must fail closed.
- A merchant can access only its own payments and receipts.
- The gateway must not post directly to the ledger.
- A receipt must report partial or failed work honestly.
- A product claim must link to a current test, audit, benchmark, receipt, or customer record.

## Immediate build priorities

1. Add a real payment-processor adapter behind a clean interface.
2. Add PostgreSQL concurrency tests for duplicate payment requests.
3. Move the remaining recovery queues from local files to PostgreSQL.
4. Add merchant API-key rotation, revocation, and suspension.
5. Sign payment receipts and provide a verification endpoint.
6. Add repeatable end-to-end tests that prove balanced ledger entries and refunds.
7. Run measured load tests before publishing any performance numbers.

## Product boundary

Archisynapse provides payment orchestration, accounting, fraud checks, reconciliation, revenue records, and receipts.

It does not own Lyrica 3, its creator graph, or any other Empire-1 product. Each Empire-1 universe operates and earns independently.

**Build first. Measure second. Claim only what the evidence proves.**
