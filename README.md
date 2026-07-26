# Archisynapse

Archisynapse is a payment and accounting system being built under Empire-1.

It is designed to receive a payment request, check risk, record the payment, post balanced ledger entries, update revenue records, and return a receipt that shows what happened.

Archisynapse is its own business and system. Lyrica 3, Southern Arcade, other Empire-1 products, and outside companies may use it as customers.

## What exists in this repository

The repository contains separate services for:

- payment and refund handling
- double-entry ledger posting
- fraud and risk checks
- revenue analytics
- an API gateway
- PostgreSQL storage
- Redis support
- signed royalty-event processing
- idempotency and recovery paths

The intended payment flow is:

```text
Merchant request
  -> API gateway
  -> risk check
  -> transaction service
  -> ledger posting
  -> analytics update
  -> receipt
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
| API gateway | Implemented | Finish merchant authentication and remove local JSON state |
| Royalty event path | Implemented behind a disabled-by-default flag | Complete deployment verification with approved tenant keys |
| Merchant authentication | In progress | Store hashed merchant keys and bind each request to one merchant |
| Durable receipts | Partly implemented | Move all remaining payment receipts and idempotency state to PostgreSQL |
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

## Safety rules

- Never store raw card details in this system.
- Never place secrets in payment events, receipts, logs, or source control.
- Every money amount must use fixed precision or minor units.
- Every financial transaction must balance.
- Repeated requests with the same idempotency key must not create duplicate money movement.
- A failed fraud check must fail closed.
- The gateway must not post directly to the ledger.
- A receipt must report partial or failed work honestly.
- A product claim must link to current test, audit, benchmark, receipt, or customer evidence.

## Immediate build priorities

1. Replace local merchant credentials and payment receipt files with PostgreSQL records.
2. Add hashed merchant API keys with one-time key reveal.
3. Bind every payment request to its authenticated merchant.
4. Add durable idempotency with request-hash conflict detection.
5. Add a processor adapter boundary without pretending a live banking rail is connected.
6. Add repeatable end-to-end tests that produce receipts and balanced-ledger evidence.
7. Publish measured results only after the tests are run.

## Product boundary

Archisynapse provides payment orchestration, accounting, fraud checks, reconciliation, revenue records, and receipts.

It does not own Lyrica 3, its creator graph, or any other Empire-1 product. Each Empire-1 universe operates and earns independently.

**Build first. Measure second. Claim only what the evidence proves.**
