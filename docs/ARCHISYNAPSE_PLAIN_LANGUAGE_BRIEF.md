# Archisynapse

## What it is

Archisynapse is a payment and accounting system.

It is being built to help businesses:

- accept payment requests
- check whether a payment looks risky
- record payments and refunds
- keep balanced accounting records
- track revenue
- return a receipt showing what happened

Archisynapse is its own business. It can serve Empire-1 products and outside companies.

## Who it is for

Possible customers include:

- online stores
- software companies
- marketplaces
- gig-work platforms
- game platforms
- creator platforms
- fintech companies
- companies that need payment and ledger tools through an API

These are target customer groups, not claimed customers.

## What is built now

The current code includes:

- a transaction service for payments and refunds
- a double-entry ledger service
- a fraud and risk service
- a revenue analytics service
- an API gateway
- PostgreSQL and Redis support
- signed royalty-event handling
- duplicate-request protection
- stored receipts
- merchant API-key authentication

The gateway now identifies the merchant from its API key. A caller cannot put another merchant's ID in a payment request.

Merchant API keys are shown once and stored as hashes. Internal service keys are encrypted before they are stored.

## How a payment moves through the system

```text
Merchant sends request
  -> gateway checks merchant key
  -> gateway checks for a duplicate request
  -> fraud service checks risk
  -> transaction service records the payment
  -> transaction service sends the financial entries to the ledger
  -> analytics records the revenue event
  -> gateway stores and returns a receipt
```

The gateway does not create ledger entries directly.

## How Archisynapse can earn revenue

The planned revenue options are:

- monthly software subscriptions
- usage-based API charges
- transaction service fees
- paid reporting and reconciliation tools
- custom integrations
- white-label licensing
- fraud and revenue-analysis tools

These are business-model options. They are not current revenue claims.

## What is not proven yet

Archisynapse does not currently claim:

- connection to a live card network or bank rail
- a specific settlement speed
- lower fees than another payment company
- a specific transaction capacity
- a fraud detection percentage
- production uptime
- PCI-DSS, SOC 2, ISO 27001, or another certification
- a specific number of customers or pilots
- a specific amount of revenue
- production readiness

## What must happen before production

1. Connect an approved payment processor or banking partner.
2. Test duplicate requests under real PostgreSQL concurrency.
3. Move the remaining recovery files into PostgreSQL.
4. Add merchant key rotation, revocation, and suspension.
5. Sign payment receipts and add public verification.
6. Run full payment, refund, ledger, and failure tests.
7. Complete security and compliance work.
8. Measure performance before publishing performance numbers.

## Product boundary

Archisynapse does not depend on Lyrica 3.

Lyrica 3 may use Archisynapse for royalties and payments, just as another customer could. Southern Arcade and other Empire-1 businesses may also use it. Each business keeps its own product, customers, and revenue.

## Public rule

**Say what is built. Show the proof. Do not claim what has not been measured or verified.**
