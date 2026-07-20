# Acceptance Test Plan — Lyrica–Archisynapse Royalty Receipt Loop v1

**Passes when:** every test below is green against the real v2 services (PostgreSQL, real gateway, no mocks at the boundary), run twice consecutively.
**Fixtures:** tenant `lyrica` with registered ed25519 key `lyr-k1`; track `trk_9f3a2b1c` with valid DNA/Soulprint/VICS; creators `cre_a1b2c3` (A) and `cre_d4e5f6` (B); royalty amount $1.2500 unless stated — this is the creator payout pool in full, no platform fee is deducted from it (see spec/SPEC-royalty-loop-v1.md §4).

---

## AT-01 · Happy path, single owner

Remix in Lyrica → signed `royalty.obligation.created`, splits `[{A, 10000}]`.
**Expect:** HTTP 201. Receipt `status ∈ {processing, paid}`, `gross 1.2500`, `platform_fee 0.0000`, `net 1.2500`, one payout `{A, 1.2500}`. Exactly ONE transaction and ONE balanced ledger journal exist: DR royalty_expense 1.25 / CR creator_payable:A 1.25. Trial balance delta = 0. Lyrica outbox row → `receipted`; Earnings screen shows "Royalty earned $1.25".

## AT-02 · Idempotent retry

Re-send AT-01's exact request (same `idempotency_key`, same body) 3×, including once concurrently with the original.
**Expect:** every response 200 with a byte-identical receipt (`receipt_id` unchanged). Transaction count for the event = 1. Ledger journal count = 1. No duplicate payable.

## AT-03 · Idempotency conflict

Same `idempotency_key` as AT-01, amount changed to $2.0000.
**Expect:** 409 `idempotency_conflict`. No new transaction. Original receipt unchanged.

## AT-04 · 60/40 split with deterministic rounding

New event, splits `[{A, 6000}, {B, 4000}]`, amount $1.2500.
**Expect:** fee 0.0000, net 1.2500, payouts exactly `{A: 0.7500}`, `{B: 0.5000}` — 60/40 of $1.25 divides evenly, so no largest-remainder rounding is exercised here (see AT-04b below for that). `0.75 + 0.50 == 1.25`. Journal has one CR payable line per owner, DR royalty_expense 1.25; balanced. Re-running with a fresh idempotency key yields the same cents (determinism check).

## AT-04b · Split with a genuine remainder (rounding check)

Splits `[{A, 3333}, {B, 3333}, {C, 3334}]`, amount $1.2500 (raw shares $0.416625 / $0.416625 / $0.41675 — this does not divide evenly).
**Expect:** floor each to the cent — `{A: 0.41}, {B: 0.41}, {C: 0.41}` = $1.23 floored, 2 cents left over. C has the largest remainder (.00675 vs A/B's tied .006625) and gets the first leftover cent → $0.42. A and B are tied on both remainder and bps, so the second leftover cent goes to whichever has the lexicographically smaller `owner_id` → $0.42; the other stays at $0.41. Final payouts are some permutation of `{0.42, 0.42, 0.41}` summing to exactly `$1.2500`, deterministic given fixed owner_ids. This is the case the largest-remainder algorithm in spec §4 actually exists for — AT-01 and AT-04 both happen to divide evenly and never exercise it.

## AT-05 · Tampered signature

Valid body, signature computed over a different body (or flipped byte).
**Expect:** 401 `invalid_signature`. A rejection record exists carrying `correlation_id`, `key_id`, reason. Zero transactions, zero journals, zero receipts with `paid/processing` status.

## AT-06 · Wrong / unregistered key

Sign correctly with key `rogue-k9` not registered to tenant `lyrica`.
**Expect:** 403 `unknown_key`. Rejection recorded. No financial objects.

## AT-07 · Ownership invalid

Event whose `vics_proof.proof_id` fails verification (fixture: revoked proof).
**Expect:** 422 `ownership_invalid` with receipt-style body `status: "blocked"`, `status_reasons: ["vics_invalid"]`. No money movement, no payable. Lyrica Earnings shows "Blocked".

## AT-08 · High-risk hold

Event flagged by fraud policy (fixture: `trigger.actor_id` on the risk list, or risk_score ≥ threshold).
**Expect:** 201 with receipt `status: "held"`, `status_reasons` non-empty (e.g. `["sudden_usage_spike"]`). Ledger shows DR royalty_expense / CR royalty_held_liability 1.25. NO creator_payable recognized. Earnings shows "Held for risk review". (This is the productionization of the already-closed `ownership_valid_but_payout_held` creator-proof loop — same checks, now with financial consequences.)

## AT-09 · Release of a held event

`POST /api/v1/events/{AT-08 event_id}/release` with SLA113-authorized role. Then call release AGAIN (and a third time, for good measure).
**Expect:** first call → receipt `status: processing/paid`; reclass journal moves 1.25 out of held_liability into payables (by splits, no fee deducted); same `event_id` and `correlation_id`; total obligations for the event still exactly one. Every subsequent call → deterministically `200` with the *same stored receipt* — never a second payment, and `409 invalid_state` is never returned for a release-of-an-already-released event (that code is reserved for releasing something that was never held). Unauthorized role → 403, no state change.

## AT-10 · Block

Event breaching a hard policy rule (fixture: blocked tenant policy for `license` kind).
**Expect:** receipt `status: "blocked"` with machine-readable reasons. Zero financial entries. Earnings shows "Blocked". Retrying with same idempotency key returns the same blocked receipt, still zero entries.

## AT-11 · Reversal

Emit `royalty.obligation.reversed` referencing AT-01's `event_id` (own idempotency key, same correlation_id). Then retry it (same idempotency key, same body).
**Expect:** 201; linked reversing journal (existing v2 reversal semantics — mirrors the verified $100/full-refund behavior); original receipt retrievable with `status: "reversed"`; post-reversal trial balance delta for the event = 0 (the "zero post-refund trial balance" invariant, now via the event boundary). Retry deterministically returns `200` with the *original reversal receipt* — never a second reversing entry. Reversing an *already-reversed* event via a NEW, distinct reversal event (different idempotency_key) → 409 `invalid_state`. Earnings shows "Reversed".

## AT-12 · Outage durability

Stop the Archisynapse gateway. Trigger a remix in Lyrica. Confirm outbox row `pending/sent-failed` with the event fully persisted (id, key, correlation_id). Restart gateway; let outbox retry.
**Expect:** exactly one 201, one transaction, one journal. No royalty lost, none duplicated. Elapsed downtime appears only as latency, never as data difference.

## AT-13 · Correlation thread

Take AT-01's `correlation_id` and grep every layer.
**Expect:** the SAME id present in (1) Lyrica outbox row, (2) gateway request log, (3) transaction record, (4) ledger journal metadata, (5) receipt, (6) Lyrica Earnings data row. One id, six appearances, zero transformations.

## AT-14 · Earnings renders from receipt only

Point Lyrica's Earnings screen at a fixture receipt set covering all six statuses.
**Expect:** UI renders the exact mapping from spec/SPEC-royalty-loop-v1.md §6 — `processing`→Payout processing, `paid`→Paid (shown with the "Royalty earned $X" copy), `held`→Held for risk review, `blocked`→Blocked, `reversed`→Reversed, `rejected`→Rejected — sourced 100% from receipt fields (`status`, `amounts`, `payouts`). Mutating any non-receipt Lyrica-side data does NOT change displayed financial state. Receipt `signature` verifies against Archisynapse's published key.

## AT-15 · Stale event replay window

Re-send a correctly signed event with `occurred_at` 30 minutes old and a fresh idempotency key.
**Expect:** 422 `stale_event`. Zero rows in `royalty_obligations`/`royalty_idempotency`/ledger — the replay-window check happens before any financial side effect, not after. An auditable rejection record is written. (Guards captured-and-replayed requests that pass signature checks.)

Also test the boundary values, not just an obviously-stale timestamp, so the assertion isn't riding on wall-clock timing: `occurred_at` at exactly `now - 5:00` (accept — inclusive boundary) and `now - 5:01` (reject, `stale_event`) using an injectable clock rather than a real `sleep`.

---

## Definition of Done

All 15 tests (plus AT-04b) green, twice consecutively, against real services. The demo script for stakeholders is then simply AT-01 + AT-13 narrated: *remix happens in Lyrica → $1.25 obligation → policy and fraud checks → balanced ledger entries → signed receipt → creator sees "Royalty earned $1.25" — one correlation ID visible at every hop.* That is Create → Prove → Use → Earn → Check → Ledger → Pay/Hold → Receipt → Show creator, closed with real money math — and the full $1.25 lands with the creator, because that promise is the one thing this loop is not allowed to quietly renegotiate.

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
