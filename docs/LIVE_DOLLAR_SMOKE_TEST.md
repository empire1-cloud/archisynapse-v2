# $1 Live Card Smoke Test

This runbook performs one real **$1.00 USD** card payment through the guarded
Archisynapse processor lane and immediately requests a full refund.

It is not a production launch. It is a founder-controlled proof run.

## What this proves

A successful run produces evidence for this path:

```text
real card
  -> Stripe.js tokenized PaymentMethod
  -> authenticated Archisynapse gateway
  -> fraud decision
  -> Stripe live PaymentIntent
  -> transaction record
  -> balanced ledger posting
  -> analytics record
  -> signed Archisynapse receipt
  -> receipt verification
  -> Stripe refund
  -> ledger reversal
```

## Hard safety limits

The `stripe_live_smoke` adapter refuses to run unless all of these are true:

- `ARCHISYNAPSE_LIVE_SMOKE_TEST_ENABLED=true`
- `ARCHISYNAPSE_LIVE_SMOKE_TEST_CONFIRM=CHARGE_AND_REFUND_ONE_USD`
- `STRIPE_SECRET_KEY` begins with `sk_live_`
- the amount is exactly 100 cents
- the currency is exactly USD
- the payment method is a tokenized `pm_...` value
- the payment idempotency key begins with `live-smoke-`
- `metadata.live_smoke_test` is true
- the refund is exactly 100 cents
- the refund idempotency key begins with `live-smoke-refund-`

The local runner permits one attempt per process and binds only to `127.0.0.1`.

## Never put these in chat or Git

- card number
- expiration date
- CVC
- Stripe secret key
- Archisynapse merchant API key
- gateway encryption key
- receipt signing private key

The browser sends card details directly to Stripe through Stripe.js. The local
runner and Archisynapse receive only a PaymentMethod token.

## Prerequisites

1. A Stripe account activated for live payments.
2. Live Stripe publishable and secret keys.
3. Docker with Compose.
4. This branch checked out.
5. At least $1.00 plus any issuer conversion or card fees available on the card.

A prepaid or crypto-linked card may decline the payment, require additional
authentication, place a temporary hold, or take time to show the refund.

## 1. Check out the branch

```bash
git fetch origin
git switch agent/live-dollar-smoke-test
```

## 2. Create the local environment file

```bash
cp .env.example .env
```

Generate two different 32-byte values:

```bash
python - <<'PY'
import base64, os
for label in ("gateway", "receipt"):
    value = base64.urlsafe_b64encode(os.urandom(32)).decode().rstrip("=")
    print(f"{label}: {value}")
PY
```

Set these values in `.env`:

```dotenv
ARCHISYNAPSE_ADMIN_TOKEN=<long-random-admin-token>
ARCHISYNAPSE_GATEWAY_MASTER_KEY=<gateway-value>
ARCHISYNAPSE_RECEIPT_SIGNING_PRIVATE_KEY=<receipt-value>
ARCHISYNAPSE_PEPPER=<long-random-pepper>

ARCHISYNAPSE_PROCESSOR=stripe_live_smoke
STRIPE_SECRET_KEY=<your sk_live_ key>
STRIPE_PUBLISHABLE_KEY=<your pk_live_ key>
ARCHISYNAPSE_LIVE_SMOKE_TEST_ENABLED=true
ARCHISYNAPSE_LIVE_SMOKE_TEST_CONFIRM=CHARGE_AND_REFUND_ONE_USD
```

Do not commit `.env`.

## 3. Start Archisynapse

```bash
docker compose up --build
```

In a second terminal, check the gateway:

```bash
curl -s http://127.0.0.1:9000/health | python -m json.tool
```

Do not continue unless PostgreSQL, transaction, ledger, fraud, and analytics are
healthy and receipt signing is configured.

## 4. Create a live Archisynapse merchant key

Run this locally. The returned API key is shown once.

```bash
curl -s -X POST http://127.0.0.1:9000/admin/merchants \
  -H "X-Archisynapse-Admin-Token: $ARCHISYNAPSE_ADMIN_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"name":"Founder $1 Live Smoke","plan":"internal","environment":"live"}' \
  | tee /tmp/archisynapse-live-merchant.json
```

Copy only the returned `api_key` into your local shell environment:

```bash
export ARCHISYNAPSE_MERCHANT_API_KEY='arch_live_...'
export STRIPE_PUBLISHABLE_KEY='pk_live_...'
export ARCHISYNAPSE_GATEWAY_URL='http://127.0.0.1:9000'
export ARCHISYNAPSE_LIVE_SMOKE_TEST_CONFIRM='CHARGE_AND_REFUND_ONE_USD'
```

Delete the temporary file after the run:

```bash
shred -u /tmp/archisynapse-live-merchant.json 2>/dev/null || rm -f /tmp/archisynapse-live-merchant.json
```

## 5. Start the local card page

Use the same Python environment as the gateway dependencies:

```bash
python -m pip install -r services/gateway/requirements.txt
python tools/live-dollar-smoke/app.py
```

Open:

```text
http://127.0.0.1:9010
```

Enter the cardholder name and card details in the Stripe-hosted card element.
Press **Charge $1 and refund it** once.

## 6. Save the proof

A successful response includes:

- Archisynapse event ID
- correlation ID
- transaction ID
- ledger transaction ID
- fraud decision
- analytics status
- valid receipt signature
- refund result

Save the JSON response without card data or secrets.

Also verify in the Stripe Dashboard that:

1. the $1 PaymentIntent exists;
2. the PaymentIntent succeeded;
3. a $1 refund was created for that PaymentIntent.

## Manual refund fallback

If the charge succeeds but the local runner reports that receipt verification or
refund failed, open the Stripe Dashboard immediately, locate the $1 PaymentIntent,
and issue a full refund manually.

Do not retry the browser button from the same process. Stop the runner, investigate
the receipt and ledger state, and start a new process only after the first payment
is accounted for.

## Honest result labels

- **PASS:** live charge succeeded, ledger posted, signed receipt verified, refund succeeded.
- **PARTIAL:** live charge succeeded but ledger, receipt verification, analytics, or refund needs recovery.
- **DECLINED:** issuer or processor rejected the card; no proof of successful money movement.
- **ACTION REQUIRED:** the issuer required an authentication flow not supported by this first smoke lane.

A PASS proves one controlled live transaction. It does not prove production
readiness, scale, uptime, compliance certification, or customer demand.
