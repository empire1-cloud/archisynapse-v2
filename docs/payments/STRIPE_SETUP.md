# Stripe Card-Acquiring Layer

This wires **real card charges** into Archisynapse's Revenue Assurance Loop.
Before this, every "payment" used a fake `tok_test_card` and nothing actually moved.

```
Customer  ->  Stripe Checkout (hosted card form, PCI on Stripe)
          ->  Stripe webhook  ->  /v1/stripe/webhook  (signature verified)
          ->  /v1/revenue/process  ->  fraud -> transaction -> ledger -> analytics
          ->  receipt   (loop closed, charge posted to the double-entry ledger)
```

Fails **closed**: if `STRIPE_SECRET_KEY` is unset, every Stripe endpoint returns a
clear `503`. No silent no-ops. No secrets live in the repo — everything is env-driven.

## 1. Get your keys (Stripe Dashboard)

| Env var | Where | Notes |
|---|---|---|
| `STRIPE_SECRET_KEY` | Developers → API keys | `sk_test_...` to rehearse, `sk_live_...` for a real charge |
| `STRIPE_PUBLISHABLE_KEY` | Developers → API keys | `pk_test_...` / `pk_live_...` |
| `STRIPE_WEBHOOK_SECRET` | Developers → Webhooks → your endpoint | `whsec_...` |
| `PUBLIC_BASE_URL` | your public https URL | needed for redirect + webhook |

## 2. Rehearse in TEST mode first (do this before any real card)

```bash
export STRIPE_SECRET_KEY=sk_test_xxx
export PUBLIC_BASE_URL=http://localhost:9000

# Terminal A: forward webhooks to the gateway and print the signing secret
stripe listen --forward-to localhost:9000/v1/stripe/webhook
#   -> copy the whsec_... it prints:
export STRIPE_WEBHOOK_SECRET=whsec_xxx

# Terminal B: boot the stack
docker compose up -d

# Open the pay page and use Stripe's test card 4242 4242 4242 4242, any future date/CVC
open http://localhost:9000/v1/stripe/pay
```

Verify the loop closed:

```bash
curl localhost:9000/v1/stripe/config          # ready_to_charge: true, mode: TEST
curl localhost:9000/v1/revenue/receipts        # your $1 should appear, posted to ledger
```

## 3. The real $1 charge (LIVE mode)

Only after the test above works end-to-end:

1. Swap the three keys to their `..._live_...` / live-webhook values.
2. `PUBLIC_BASE_URL` must be a real public **https** URL (Stripe won't hit localhost live).
   Register the live webhook endpoint `https://<host>/v1/stripe/webhook` in the Dashboard.
3. Open `/v1/stripe/pay`, pay $1 with a real card.
4. Confirm in Stripe Dashboard → Payments **and** in `/v1/revenue/receipts` (ledger).
5. Refund if you want your dollar back: `POST /v1/revenue/refund/{transaction_id}`
   or refund the PaymentIntent in the Stripe Dashboard.

## Endpoints added

| Method | Path | Purpose |
|---|---|---|
| GET | `/v1/stripe/config` | Non-secret readiness/mode check |
| GET | `/v1/stripe/pay` | One-button hosted pay page |
| POST | `/v1/stripe/checkout` | Create a Checkout Session, returns `checkout_url` |
| GET | `/v1/stripe/success` / `/cancel` | Redirect landing pages |
| POST | `/v1/stripe/webhook` | Signature-verified; closes the loop into the ledger |

Idempotency: the loop post is keyed on `stripe_<session_id>`, so Stripe retries never
double-post to the ledger.
