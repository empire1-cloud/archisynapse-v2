# Acceptance Test Plan — Lyrica–Archisynapse Royalty Receipt Loop v1

**Passes when:** every test below is green against the real v2 services (PostgreSQL, real gateway, no mocks at the boundary), run twice consecutively.
**Fixtures:** tenant `lyrica` with registered ed25519 key `lyr-k1`; track `trk_9f3a2b1c` with valid DNA/Soulprint/VICS; creators `cre_a1b2c3` (A) and `cre_d4e5f6` (B); platform fee 290 bps; royalty amount $1.2500 unless stated.

---

## AT-01 · Happy path, single owner

Remix in Lyrica → signed `royalty.obligation.created`, splits `[{A, 10000}]`.
**Expect:** HTTP 201. Receipt `status ∈ {processing, paid}`, `gross 1.2500`, `platform_fee 0.0400`, `net 1.2100`, one payout `{A, 1.2100}`. Exactly ONE transaction and ONE balanced ledger journal exist: DR royalty_clearing 1.25 / CR creator_payable:A 1.21 / CR platform_fee_revenue 0.04. Trial balance delta = 0. Lyrica outbox row → `receipted`; Earnings screen shows "Royalty earned $1.21".

## AT-02 · Idempotent retry

Re-send AT-01's exact request (same `idempotency_key`, same body) 3×, including once concurrently with the original.
**Expect:** every response 200 with a byte-identical receipt (`receipt_id` unchanged). Transaction count for the event = 1. Ledger journal count = 1. No duplicate payable.

## AT-03 · Idempotency conflict

Same `idempotency_key` as AT-01, amount changed to $2.0000.
**Expect:** 409 `idempotency_conflict`. No new transaction. Original receipt unchanged.

## AT-04 · 60/40 split with deterministic rounding

New event, splits `[{A, 6000}, {B, 4000}]`, amount $1.2500.
**Expect:** fee 0.0400, net 1.2100, payouts exactly `{A: 0.7300}`, `{B: 0.4800}` (largest-remainder; A's raw 0.7260 remainder .006 beats B's .004). `0.73 + 0.48 + 0.04 == 1.25`. Journal has one CR payable line per owner; balanced. Re-running with a fresh idempotency key yields the same cents (determinism check).

## AT-05 · Tampered signature

Valid body, signature computed over a different body (or flipped byte).
**Expect:** 401 `invalid_signature`. A rejection record exists carrying `correlation_id`, `key_id`, reason. Zero transactions, zero journals, zero receipts with `paid/processing` status.

## AT-06 · Wrong / unregistered key

Sign correctly with key `rogue-k9` not registered to tenant `lyrica`.
**Expect:** 403 `unknown_key`. Rejection recorded. No financial objects.

## AT-07 · Ownership invalid

Event whose `vics_proof.proof_id` fails verification (fixture: revoked proof).
**Expect:** 422 `ownership_invalid` with receipt-style body `status: "blocked"`, `status_reasons: ["vics_invalid"]`. No money movement, no payable. Lyrica Earnings shows "Blocked". (This is the productionization of the already-closed `ownership_valid_but_payout_held` creator-proof loop — same checks, now with financial consequences.)

## AT-08 · High-risk hold

Event flagged by fraud policy (fixture: `trigger.actor_id` on the risk list, or risk_score ≥ threshold).
**Expect:** 201 with receipt `status: "held"`, `status_reasons` non-empty (e.g. `["sudden_usage_spike"]`). Ledger shows DR royalty_clearing / CR royalty_held_liability 1.25. NO creator_payable, NO fee revenue recognized. Earnings shows "Held for risk review".

## AT-09 · Release of a held event

`POST /api/v1/events/{AT-08 event_id}/release` with SLA113-authorized role. Then call release AGAIN.
**Expect:** first call → receipt `status: processing/paid`; reclass journal moves 1.25 out of held_liability into payables (1.21 by splits) + fee 0.04; same `event_id` and `correlation_id`; total obligations for the event still exactly one. Second call → 409 `invalid_state` or idempotent 200 with same receipt — never a second payment. Unauthorized role → 403, no state change.

## AT-10 · Block

Event breaching a hard policy rule (fixture: blocked tenant policy for `license` kind).
**Expect:** receipt `status: "blocked"` with machine-readable reasons. Zero financial entries. Earnings shows "Blocked". Retrying with same idempotency key returns the same blocked receipt, still zero entries.

## AT-11 · Reversal

Emit `royalty.obligation.reversed` referencing AT-01's `event_id` (own idempotency key, same correlation_id). Then retry it.
**Expect:** 201; linked reversing journal (existing v2 reversal semantics — mirrors the verified $100/full-refund behavior); original receipt retrievable with `status: "reversed"`; post-reversal trial balance delta for the event = 0 (the "zero post-refund trial balance" invariant, now via the event boundary). Retry creates no second reversal (409 or idempotent 200). Reversing a reversed event → 409 `invalid_state`. Earnings shows "Reversed".

## AT-12 · Outage durability

Stop the Archisynapse gateway. Trigger a remix in Lyrica. Confirm outbox row `pending/sent-failed` with the event fully persisted (id, key, correlation_id). Restart gateway; let outbox retry.
**Expect:** exactly one 201, one transaction, one journal. No royalty lost, none duplicated. Elapsed downtime appears only as latency, never as data difference.

## AT-13 · Correlation thread

Take AT-01's `correlation_id` and grep every layer.
**Expect:** the SAME id present in (1) Lyrica outbox row, (2) gateway request log, (3) transaction record, (4) ledger journal metadata, (5) receipt, (6) Lyrica Earnings data row. One id, six appearances, zero transformations.

## AT-14 · Earnings renders from receipt only

Point Lyrica's Earnings screen at a fixture receipt set covering all six statuses.
**Expect:** UI shows Royalty earned / Payout processing / Paid / Held for risk review / Blocked / Reversed — sourced 100% from receipt fields (`status`, `amounts`, `payouts`). Mutating any non-receipt Lyrica-side data does NOT change displayed financial state. Receipt `signature` verifies against Archisynapse's published key.

## AT-15 · Stale event replay window

Re-send a correctly signed event with `occurred_at` 30 minutes old and a fresh idempotency key.
**Expect:** 422 `stale_event`, no financial objects. (Guards captured-and-replayed requests that pass signature checks.)

---

## Definition of Done

All 15 tests green, twice consecutively, against real services. The demo script for stakeholders is then simply AT-01 + AT-13 narrated: *remix happens in Lyrica → $1.25 obligation → policy and fraud checks → balanced ledger entries → signed receipt → creator sees "Royalty earned $1.21" — one correlation ID visible at every hop.* That is Create → Prove → Use → Earn → Check → Ledger → Pay/Hold → Receipt → Show creator, closed with real money math.

## Build Order Suggestion (maps to /spec → /plan → /build)

1. Event + receipt schema validators (AT: 01 partial, 03, 15)
2. Signature verification + rejection recording (AT-05, 06)
3. Money math module with rounding rules — pure functions, unit-test first (AT-01, 04)
4. `/api/v1/events` happy path → transaction + journal (AT-01, 02)
5. Decision engine wiring: hold/block + held-liability posting (AT-07, 08, 10)
6. Release endpoint (AT-09)
7. Reversal event (AT-11)
8. Lyrica outbox + retry (AT-12)
9. Correlation propagation audit + Earnings rendering (AT-13, 14)
