# Archisynapse Processor Proof

This runbook proves the current test-mode boundary. It does not prove live settlement, production readiness, uptime, fee savings, compliance certification, or customer traction.

## What this proof covers

A successful run shows that:

1. A merchant authenticates with an Archisynapse API key.
2. Repeated requests use one durable idempotency record.
3. The risk service runs before the payment call.
4. The transaction service calls a configured processor adapter.
5. The processor receives an integer amount in minor units and its own idempotency key.
6. Only a processor `succeeded` result can move the payment to `SUCCEEDED`.
7. The transaction service posts the accounting entries.
8. The gateway stores the receipt in PostgreSQL.
9. The gateway signs the receipt with Ed25519 when a signing key is configured.
10. Changing the stored receipt causes signature verification to fail.
11. Refunds call the original processor payment reference before reversing the ledger.
12. A processor-success / ledger-pending refund is kept as durable recovery state.

## What this proof does not cover

- live card, bank, wallet, or stablecoin settlement
- merchant onboarding or underwriting by a regulated processor
- chargebacks and disputes
- SCA or other customer-interaction flows
- production key management
- processor webhook verification
- measured throughput, latency, availability, or fraud accuracy
- compliance certification

## Configuration

Copy the environment template:

```bash
cp .env.example .env
```

Generate two separate 32-byte values:

```bash
python - <<'PY'
import base64
import os
for name in (
    "ARCHISYNAPSE_GATEWAY_MASTER_KEY",
    "ARCHISYNAPSE_RECEIPT_SIGNING_PRIVATE_KEY",
):
    value = base64.urlsafe_b64encode(os.urandom(32)).decode().rstrip("=")
    print(f"{name}={value}")
PY
```

Set a long admin token. For the processor proof lane, use a Stripe test key only:

```dotenv
ARCHISYNAPSE_PROCESSOR=stripe_test
STRIPE_SECRET_KEY=sk_test_...
```

The adapter rejects `sk_live_` keys.

## Start the stack

```bash
docker compose down -v
docker compose up --build
```

Check status:

```bash
curl -s http://127.0.0.1:9000/health | python -m json.tool
curl -s http://127.0.0.1:9000/status | python -m json.tool
```

The status response must continue to say:

```json
{
  "production_ready": false,
  "processor": {
    "test_mode": true,
    "live_money": false
  }
}
```

## Create a merchant

```bash
curl -sS -X POST http://127.0.0.1:9000/admin/merchants \
  -H "Content-Type: application/json" \
  -H "X-Archisynapse-Admin-Token: $ARCHISYNAPSE_ADMIN_TOKEN" \
  -d '{"name":"Processor Proof Merchant","plan":"test","environment":"test"}'
```

Save the returned API key. It is revealed once and stored only as a hash.

## Submit a test payment

Use a tokenized test PaymentMethod reference. Raw card numbers must never enter this API.

```bash
curl -sS -X POST http://127.0.0.1:9000/v1/payments \
  -H "Authorization: Bearer $ARCHISYNAPSE_MERCHANT_API_KEY" \
  -H "Idempotency-Key: processor-proof-payment-001" \
  -H "Content-Type: application/json" \
  -d '{
    "customer_id":"customer-proof-001",
    "amount":"12.50",
    "fee_amount":"0.00",
    "currency":"USD",
    "payment_method_type":"CARD",
    "payment_method_token":"pm_card_visa",
    "description":"Archisynapse processor proof"
  }'
```

Record these fields from the response and stored evidence:

- `event_id`
- `correlation_id`
- `transaction_id`
- `ledger_transaction_id`
- `status`
- processor payment reference from the transaction service record

## Prove duplicate-request safety

Send the exact same request again with the same idempotency key. It must return the existing receipt rather than creating a second payment.

Then change the amount while reusing the same key. The gateway must return HTTP 409 because one idempotency key cannot represent two different requests.

The CI concurrency test also sends 20 simultaneous claims against PostgreSQL and requires exactly one winner.

## Verify the signed receipt

```bash
curl -sS \
  -H "Authorization: Bearer $ARCHISYNAPSE_MERCHANT_API_KEY" \
  http://127.0.0.1:9000/v1/receipts/$EVENT_ID/evidence \
  | python -m json.tool
```

Required result:

```json
{
  "signature_valid": true,
  "verification": "receipt signature is valid"
}
```

The public verification key is available at:

```bash
curl -sS http://127.0.0.1:9000/v1/proof/key | python -m json.tool
```

## Refund the test payment

```bash
curl -sS -X POST \
  http://127.0.0.1:9000/v1/payments/$TRANSACTION_ID/refund \
  -H "Authorization: Bearer $ARCHISYNAPSE_MERCHANT_API_KEY" \
  -H "Idempotency-Key: processor-proof-refund-001" \
  -H "Content-Type: application/json" \
  -d '{"amount":"12.50","reason":"customer_requested"}'
```

The transaction service must use the original processor payment reference. It must not reverse the ledger before the processor accepts the refund.

If the processor refund succeeds but ledger reversal fails, inspect:

```sql
SELECT *
FROM processor_refund_attempts
WHERE status = 'PROCESSOR_SUCCEEDED';
```

That row is the recovery proof. It prevents the system from silently treating a processor refund as fully reconciled.

## Automated proof checks

The pull-request workflow runs:

```text
TypeScript build
processor adapter tests
Python compile checks
receipt signature tests
PostgreSQL migration checks
20-request idempotency concurrency test
```

A green workflow proves the code paths and database behavior covered by those tests. It does not prove live money movement.

## Evidence package

For a founder-approved proof run, preserve:

- the exact commit SHA
- the workflow run URL
- `/status` output
- the merchant ID, with API keys redacted
- the payment receipt
- the receipt proof envelope
- the processor test object ID
- the ledger transaction ID and balanced entries
- the refund object ID
- the reversal ledger transaction ID
- the PostgreSQL idempotency row

Never place secret keys, raw card data, or unredacted credentials in the evidence package.
