# Archisynapse Gateway Setup

This guide starts the development system and proves the internal payment flow.

It does not connect Archisynapse to a card network, bank, or live settlement rail.

## 1. Create local secrets

Copy the example file:

```bash
cp .env.example .env
```

Create a gateway encryption key:

```bash
python -c "import base64, os; print(base64.urlsafe_b64encode(os.urandom(32)).decode().rstrip('='))"
```

Put that value in:

```text
ARCHISYNAPSE_GATEWAY_MASTER_KEY=
```

Add a separate random admin token:

```text
ARCHISYNAPSE_ADMIN_TOKEN=
```

Do not commit `.env`.

## 2. Start the services

```bash
docker compose up --build
```

Check the gateway:

```bash
curl http://127.0.0.1:9000/health
```

The response reports the database and each internal service separately. A degraded response is honest evidence that one or more parts are unavailable.

## 3. Create a test merchant

```bash
curl -X POST http://127.0.0.1:9000/admin/merchants \
  -H "Content-Type: application/json" \
  -H "X-Archisynapse-Admin-Token: $ARCHISYNAPSE_ADMIN_TOKEN" \
  -d '{
    "name": "Local Test Merchant",
    "plan": "growth",
    "environment": "test"
  }'
```

The response contains one merchant API key. It is shown once. Store it outside the repository.

The database stores only an Argon2 hash of that API key. Internal fraud and analytics keys are encrypted before storage and are never returned to the merchant.

## 4. Check merchant identity

```bash
curl http://127.0.0.1:9000/v1/merchant/me \
  -H "Authorization: Bearer $MERCHANT_API_KEY"
```

The merchant ID comes from the API key. A caller cannot choose another merchant ID in the payment body.

## 5. Send a development payment request

```bash
curl -X POST http://127.0.0.1:9000/v1/payments \
  -H "Authorization: Bearer $MERCHANT_API_KEY" \
  -H "Idempotency-Key: local-payment-001" \
  -H "Content-Type: application/json" \
  -d '{
    "customer_id": "customer-001",
    "amount": "100.00",
    "fee_amount": "2.90",
    "currency": "USD",
    "payment_method_type": "CARD",
    "payment_method_token": "tok_test_card",
    "payment_method_last4": "4242",
    "payment_method_brand": "VISA",
    "description": "Development payment"
  }'
```

This exercises Archisynapse's internal flow:

```text
merchant authentication
  -> idempotency claim
  -> fraud check
  -> transaction record
  -> ledger posting
  -> analytics record
  -> stored receipt
```

The token in this example is a development token. It does not charge a real card.

## 6. Test duplicate protection

Send the exact request again with the same `Idempotency-Key`.

The gateway should return the stored receipt instead of creating a second payment flow.

Then change the amount while keeping the same `Idempotency-Key`.

The gateway should reject it with `409 Conflict` because the key was already bound to a different request.

## 7. Read stored receipts

```bash
curl http://127.0.0.1:9000/v1/receipts \
  -H "Authorization: Bearer $MERCHANT_API_KEY"
```

Read one receipt:

```bash
curl http://127.0.0.1:9000/v1/receipts/EVENT_ID \
  -H "Authorization: Bearer $MERCHANT_API_KEY"
```

A merchant can read only its own receipts.

## 8. Read the honest system status

```bash
curl http://127.0.0.1:9000/status
```

The status endpoint currently reports:

- no live payment processor connection
- not production-ready
- no measured settlement-speed claim
- no measured fee-savings claim
- no measured throughput claim
- no certifications claimed

Those values should change only after current evidence exists.

## Automated checks

From the gateway directory:

```bash
cd services/gateway
python -m unittest discover -s tests -p "test_gateway_store.py"
python -m py_compile gateway_store.py production_main.py
```

These checks cover merchant API-key parsing and hashing, credential encryption, merchant binding, and stable request hashing. Full PostgreSQL and service-boundary tests remain required before deployment.
