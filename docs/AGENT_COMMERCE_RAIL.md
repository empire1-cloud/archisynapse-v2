# Archisynapse Agent Commerce Rail

## Product boundary

Nostr or another registry may announce an agent. It is not trusted to authorize
spending, prove settlement, or establish reputation. Archisynapse owns those controls.

## Paid-call state machine

```text
authorization.active
  -> budget.reserved
  -> invoice.bound
  -> payment.initiated
  -> payment.settled
  -> receipt.finalized
  -> validator outcome
  -> verified reputation update
```

A failure before settlement releases the reservation. A failure after settlement does
not release it: the payment is real and a `delivery_failed` receipt is finalized.

## Authorization scope

An authorization binds tenant, orchestrator, expiration, total budget, per-call budget,
route-fee reserve, maximum calls, allowed agent npubs, and allowed specialties. Revocation
blocks new reservations immediately. `BEGIN IMMEDIATE` serialization prevents two calls
from spending the same remaining budget.

## Receipt evidence

Each receipt binds:

- authorization id and version;
- reservation and idempotency key;
- orchestrator, tenant, agent identity, specialty, and endpoint;
- advertised quote;
- invoice digest, decoded amount, and payment hash;
- provider payment id, settlement state, fee, time, and hashed preimage;
- delivery HTTP status, content type, size, body hash, latency, and failure state;
- total debit;
- canonical receipt SHA-256 and Archisynapse HMAC signature.

The raw Lightning preimage is never persisted.

## Recovery

If the process crashes after sending payment but before recording settlement,
`reconcile_initiated_payment` queries the provider using the already-bound payment hash.
It records a settlement only when the returned preimage proves that exact hash. It never
creates a second invoice or retries payment blindly. Operators invoke this through
`POST /v1/reservations/{reservation_id}/reconcile`; an unproven payment remains reserved
rather than being falsely released or paid again.

## Reputation

Self-advertised reputation seeds are ignored. A score starts neutral at 50 and changes
only once per signed receipt after a separate validator supplies outcome evidence.
Duplicate or conflicting outcomes are rejected.

## Deployment identity

Production profiles require HTTPS and reject loopback, private, link-local, metadata,
reserved, and credential-bearing endpoints. Archisynapse signs a short-lived profile
attestation that can be included in Nostr metadata tags.

## Client choice

Clients can preserve the money-spend option while choosing policy:

- do not create an authorization: no spend is possible;
- narrow one-call authorization: human approval per payment;
- bounded multi-call authorization: automatic spend under a task cap;
- recurring policy issuance: automatic spend under tenant limits.

The service never silently upgrades one mode into another.
