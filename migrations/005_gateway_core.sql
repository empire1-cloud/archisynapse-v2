-- Migration 005: Gateway merchant identity, durable idempotency, and receipts.
-- This is gateway operational state. Financial truth remains in the transaction
-- and ledger services.

BEGIN;

CREATE TABLE IF NOT EXISTS gateway_merchants (
  merchant_id TEXT PRIMARY KEY,
  name VARCHAR(200) NOT NULL,
  plan VARCHAR(50) NOT NULL DEFAULT 'growth',
  status VARCHAR(20) NOT NULL DEFAULT 'ACTIVE'
    CHECK (status IN ('ACTIVE', 'SUSPENDED', 'CLOSED')),
  encrypted_service_credentials BYTEA,
  credentials_key_id VARCHAR(100),
  created_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT CURRENT_TIMESTAMP,
  updated_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS gateway_merchant_api_keys (
  key_id VARCHAR(32) PRIMARY KEY,
  merchant_id TEXT NOT NULL REFERENCES gateway_merchants(merchant_id),
  key_prefix VARCHAR(64) NOT NULL,
  api_key_hash VARCHAR(255) NOT NULL,
  environment VARCHAR(10) NOT NULL CHECK (environment IN ('test', 'live')),
  status VARCHAR(20) NOT NULL DEFAULT 'ACTIVE'
    CHECK (status IN ('ACTIVE', 'REVOKED')),
  created_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT CURRENT_TIMESTAMP,
  last_used_at TIMESTAMP WITH TIME ZONE,
  revoked_at TIMESTAMP WITH TIME ZONE
);

CREATE INDEX IF NOT EXISTS idx_gateway_merchant_api_keys_merchant
  ON gateway_merchant_api_keys(merchant_id);
CREATE INDEX IF NOT EXISTS idx_gateway_merchant_api_keys_status
  ON gateway_merchant_api_keys(status);

CREATE TABLE IF NOT EXISTS gateway_payment_receipts (
  event_id VARCHAR(255) PRIMARY KEY,
  merchant_id TEXT NOT NULL REFERENCES gateway_merchants(merchant_id),
  correlation_id VARCHAR(255) NOT NULL,
  idempotency_key VARCHAR(255) NOT NULL,
  request_hash VARCHAR(64) NOT NULL,
  status VARCHAR(30) NOT NULL,
  payload JSONB NOT NULL,
  created_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT CURRENT_TIMESTAMP,
  updated_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT CURRENT_TIMESTAMP,
  UNIQUE (merchant_id, idempotency_key)
);

CREATE INDEX IF NOT EXISTS idx_gateway_payment_receipts_merchant
  ON gateway_payment_receipts(merchant_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_gateway_payment_receipts_correlation
  ON gateway_payment_receipts(correlation_id);
CREATE INDEX IF NOT EXISTS idx_gateway_payment_receipts_transaction
  ON gateway_payment_receipts((payload->>'transaction_id'));
CREATE INDEX IF NOT EXISTS idx_gateway_payment_receipts_status
  ON gateway_payment_receipts(merchant_id, status);

CREATE TABLE IF NOT EXISTS gateway_payment_idempotency (
  merchant_id TEXT NOT NULL REFERENCES gateway_merchants(merchant_id),
  idempotency_key VARCHAR(255) NOT NULL,
  request_hash VARCHAR(64) NOT NULL,
  status VARCHAR(20) NOT NULL DEFAULT 'PROCESSING'
    CHECK (status IN ('PROCESSING', 'COMPLETED', 'FAILED')),
  event_id VARCHAR(255) REFERENCES gateway_payment_receipts(event_id),
  failure_reason VARCHAR(500),
  claimed_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT CURRENT_TIMESTAMP,
  completed_at TIMESTAMP WITH TIME ZONE,
  failed_at TIMESTAMP WITH TIME ZONE,
  PRIMARY KEY (merchant_id, idempotency_key)
);

CREATE INDEX IF NOT EXISTS idx_gateway_payment_idempotency_status
  ON gateway_payment_idempotency(status, claimed_at);

CREATE TABLE IF NOT EXISTS gateway_audit_events (
  id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
  merchant_id TEXT REFERENCES gateway_merchants(merchant_id),
  event_type VARCHAR(100) NOT NULL,
  details JSONB NOT NULL DEFAULT '{}',
  occurred_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_gateway_audit_events_merchant
  ON gateway_audit_events(merchant_id, occurred_at DESC);
CREATE INDEX IF NOT EXISTS idx_gateway_audit_events_type
  ON gateway_audit_events(event_type, occurred_at DESC);

DROP TRIGGER IF EXISTS trigger_gateway_merchants_updated_at ON gateway_merchants;
CREATE TRIGGER trigger_gateway_merchants_updated_at
BEFORE UPDATE ON gateway_merchants
FOR EACH ROW EXECUTE FUNCTION update_updated_at();

DROP TRIGGER IF EXISTS trigger_gateway_payment_receipts_updated_at ON gateway_payment_receipts;
CREATE TRIGGER trigger_gateway_payment_receipts_updated_at
BEFORE UPDATE ON gateway_payment_receipts
FOR EACH ROW EXECUTE FUNCTION update_updated_at();

INSERT INTO schema_migrations (migration_id, name)
VALUES ('005', 'gateway_core')
ON CONFLICT (migration_id) DO NOTHING;

COMMIT;
