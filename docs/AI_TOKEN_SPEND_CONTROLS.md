# AI Token Spend Controls

Archisynapse governs AI model consumption before, during, and after a provider call. It does not ingest prompts or model responses; it stores usage counts, cost evidence, response digests, provider request identifiers, policy state, and signed receipts.

## Control chain

```text
rate-card evidence
  -> scoped spend policy
  -> pre-call token estimate
  -> atomic cost reservation
  -> approved or fallback provider/model route
  -> provider call
  -> actual token metering
  -> signed usage receipt
  -> provider usage reconciliation
  -> confirmed, disputed, or orphaned state
```

## Core guarantees

1. **No model call without preflight.** The policy must be active, inside its time window, and have enough unspent and unreserved budget.
2. **Atomic reservations.** Concurrent callers cannot reserve more than the same policy budget.
3. **Configurable prices.** Rate cards are versioned by provider, model, effective time, and source reference. No provider price is treated as permanent.
4. **Model and provider allowlists.** A requested route may be replaced only by an explicitly allowed fallback route.
5. **Actual spend is never erased.** If a model uses more than estimated, the real internally metered cost is recorded even when that pauses the policy.
6. **Runaway shutdown.** Call-rate spikes, token overruns, cost overruns, budget overruns, and reconciliation mismatches pause the policy fail-closed.
7. **Independent reconciliation.** Provider usage events match internal reservations through provider request IDs. Unknown charges become critical orphan alerts.
8. **Immutable evidence.** Final usage receipts are canonical JSON, SHA-256 bound, and HMAC signed.
9. **No prompt collection.** Only operational usage and financial evidence are stored by this service.

## API surface

All routes require the existing internal bearer token.

- `POST /v1/ai-spend/rate-cards`
- `POST /v1/ai-spend/policies`
- `GET /v1/ai-spend/policies/{policy_id}`
- `POST /v1/ai-spend/policies/{policy_id}/stop`
- `GET /v1/ai-spend/policies/{policy_id}/summary`
- `POST /v1/ai-spend/preflight`
- `POST /v1/ai-spend/reservations/{reservation_id}/finalize`
- `GET /v1/ai-spend/receipts/{receipt_id}`
- `POST /v1/ai-spend/reconcile`

## Money representation

Costs are stored as integer **micro-USD** values, where `1 USD = 1,000,000 microusd`. Rate cards are expressed as micro-USD per one million tokens. This avoids binary floating-point accounting drift.

## Preflight example

```json
{
  "policy_id": "aipol_...",
  "idempotency_key": "workflow-step-0001",
  "provider": "provider-a",
  "model": "model-premium",
  "estimated_input_tokens": 1200,
  "estimated_output_tokens": 800,
  "estimated_cached_input_tokens": 0,
  "estimated_reasoning_tokens": 300
}
```

The response records the requested route, selected route, rate-card ID, estimated cost, and reserved amount. A cheaper configured fallback can be selected when the requested route exceeds policy.

## Finalization example

The caller supplies actual token categories, the provider request ID, an outcome label, and a SHA-256 digest of the response artifact. Archisynapse computes actual cost from the rate card, releases the estimate, debits actual usage, signs the receipt, and pauses the policy when anomaly limits are crossed.

## Reconciliation states

- `confirmed`: provider cost is within the configured tolerance of internal metering.
- `disputed`: provider and internal cost differ beyond tolerance; the policy is paused.
- `orphaned`: the provider reports a charge with no matching internal request ID.

Provider invoices and Admin API exports remain external evidence. Archisynapse does not claim a provider integration is live until its collector is separately configured and validated.
