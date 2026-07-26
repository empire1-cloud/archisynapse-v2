# ADR-001: Canonical Archisynapse Runtime Boundary

- **Status:** Accepted
- **Date:** 2026-07-26
- **Decision owner:** Empire-1
- **Applies to:** `empire1-cloud/archisynapse-v2`, `empire1-cloud/Archisynapse-`, and Lyrica 3 integrations

## Context

Empire-1 currently preserves two Archisynapse implementations:

1. `empire1-cloud/Archisynapse-` — the original monolith and compatibility API.
2. `empire1-cloud/archisynapse-v2` — the microservices runtime with gateway, transaction, ledger, fraud, analytics, PostgreSQL durability, and the Lyrica royalty receipt loop.

Both repositories contain useful work. The operating rule is **WE EVOLVE. NEVER DELETE.** The two systems must therefore be assigned explicit responsibilities rather than flattened, overwritten, or ambiguously treated as equal financial sources of truth.

## Decision

### 1. Archisynapse v2 is the canonical production financial runtime

`empire1-cloud/archisynapse-v2` is the only runtime authorized to create new production financial truth for Empire-1 product universes.

Canonical flow:

```text
Product universe signed event
  -> Archisynapse v2 gateway verifies identity, signature, policy, and risk
  -> transaction service owns obligation/payment state
  -> transaction service alone requests ledger posting
  -> ledger service records balanced double-entry truth
  -> gateway persists and signs the unified receipt
  -> product universe renders status from the receipt
```

The gateway must never post directly to the ledger. Product universes must never write directly to the ledger.

### 2. The original Archisynapse monolith is preserved as the legacy compatibility system

`empire1-cloud/Archisynapse-` remains active for:

- legacy API compatibility during migration;
- historical demonstrations and preserved behavior;
- blueprint/registry capabilities not yet migrated;
- comparison and reconciliation evidence;
- emergency read-only access to pre-v2 records where required.

It must not become a second production financial source of truth for new Lyrica royalty obligations after cutover.

New financial features belong in v2. Critical security and data-integrity fixes may still be applied to the legacy system. Nothing is deleted merely because v2 is canonical.

### 3. Lyrica creates obligations; Archisynapse moves money

For the Lyrica royalty loop:

- Lyrica determines that a creative event occurred and determines the creator split.
- Lyrica persists a durable outbox record before sending.
- Lyrica emits a signed `royalty.obligation.created` event to `POST /api/v1/events`.
- Archisynapse v2 verifies the event, makes the financial/risk decision, posts through the transaction service, and returns a signed unified receipt.
- Lyrica's creator-facing earnings and payout status must come from the persisted Archisynapse receipt, not locally inferred booleans or simulated balances.

### 4. The $1.25 remix pool is creator money

For the v1 Lyrica remix contract, `amount.value = "1.2500"` is the full creator payout pool.

Archisynapse deducts no platform fee from that pool. `platform_fee` remains `"0.0000"` in the receipt. Empire-1's separate 70/30 commercial policy is a different contract and cannot be silently applied to a creator royalty obligation.

### 5. Proof and receipt boundaries fail closed

A structurally valid event is not automatically a valid ownership claim.

Production payout requires:

- a registered SLA113 tenant API key;
- a registered Ed25519 Lyrica event-signing public key;
- a valid signature over exact request bytes;
- a verifiable Lyrica VICS/DNA/Soulprint proof;
- an accepted Archisynapse risk decision;
- a balanced transaction-service/ledger result;
- a persisted, signed Archisynapse receipt.

Missing or unavailable proof dependencies result in rejection or hold, never a fabricated paid state.

## Migration sequence

### Phase A — boundary lock

- Mark v2 as canonical in architecture and deployment documentation.
- Preserve the legacy monolith and document its compatibility role.
- Stop adding new Lyrica financial truth to the legacy API.

### Phase B — proof connection

- Implement a real v2 ownership verifier against a stable Lyrica proof contract.
- Register Lyrica tenant/API/signing keys through approved admin paths.
- Keep production fail-closed until the verifier is live.

### Phase C — Lyrica outbox and receipt cutover

- Add a durable Lyrica royalty outbox.
- Emit signed v1 obligations using stable event, correlation, and idempotency identifiers.
- Persist the exact response receipt.
- Drive the Earnings UI from receipt fields only.

### Phase D — reconciliation and compatibility

- Reconcile any legacy Lyrica financial references against v2 records.
- Retain a compatibility adapter for legacy reads while preventing dual writes.
- Publish migration evidence and balanced-ledger test receipts.

## Required invariants

1. Exactly one canonical financial effect per idempotency key.
2. One correlation ID across Lyrica outbox, gateway, transaction, ledger metadata, and receipt.
3. No direct product-to-ledger or gateway-to-ledger writes.
4. No creator-facing `paid` state without a valid signed receipt.
5. No fee erosion of the $1.25 creator remix pool.
6. Every reversal links to the original event and leaves a balanced net ledger delta.
7. Both Archisynapse repositories remain preserved with explicit, non-overlapping roles.

## Consequences

### Positive

- Removes ambiguity about which Archisynapse owns financial truth.
- Preserves all prior work without allowing dual-ledger drift.
- Gives Lyrica an auditable proof-to-payout contract.
- Makes the infrastructure credible for future labels, distributors, platforms, and additional Empire-1 universes.

### Costs

- Lyrica must implement a durable outbox and receipt-driven UI.
- v2 must connect a real VICS ownership verifier before production payouts can be allowed.
- Legacy callers require an explicit migration or compatibility path.
- Key registration, rotation, and deployment secrets require operational discipline.

## Non-goals

This ADR does not delete, archive, or rename either repository. It does not claim the v2 royalty loop is production-ready before the real VICS verifier, tenant keys, deployment configuration, and end-to-end receipts are verified.
