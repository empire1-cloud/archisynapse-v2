-- Migration 006: External processor proof and signed payment receipts.
--
-- This migration does not claim that production money movement is live. It adds
-- the durable state required to prove processor test-mode calls, recover refunds
-- when processor and ledger steps split, and attach verifiable signatures to
-- gateway receipts.

BEGIN;

ALTER TABLE refunds
  ADD COLUMN IF NOT EXISTS processor_refund_id VARCHAR(255);

CREATE INDEX IF NOT EXISTS idx_refunds_processor_refund_id
  ON refunds(processor_refund_id)
  WHERE processor_refund_id IS NOT NULL;

CREATE TABLE IF NOT EXISTS processor_refund_attempts (
  id UUID PRIMARY KEY,
  payment_id UUID NOT NULL REFERENCES payments(id),
  organization_id TEXT NOT NULL,
  idempotency_key VARCHAR(255) NOT NULL UNIQUE,
  processor_payment_id VARCHAR(255) NOT NULL,
  processor_refund_id VARCHAR(255),
  amount NUMERIC(19, 4) NOT NULL CHECK (amount > 0),
  currency VARCHAR(3) NOT NULL,
  reason TEXT NOT NULL,
  status VARCHAR(30) NOT NULL DEFAULT 'PROCESSING'
    CHECK (status IN ('PROCESSING', 'PROCESSOR_SUCCEEDED', 'LEDGER_SUCCEEDED', 'FAILED')),
  ledger_transaction_id UUID,
  failure_reason TEXT,
  processor_succeeded_at TIMESTAMP WITH TIME ZONE,
  ledger_succeeded_at TIMESTAMP WITH TIME ZONE,
  created_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT CURRENT_TIMESTAMP,
  updated_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_processor_refund_attempts_payment
  ON processor_refund_attempts(payment_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_processor_refund_attempts_status
  ON processor_refund_attempts(status, updated_at);

DROP TRIGGER IF EXISTS trigger_processor_refund_attempts_updated_at
  ON processor_refund_attempts;
CREATE TRIGGER trigger_processor_refund_attempts_updated_at
BEFORE UPDATE ON processor_refund_attempts
FOR EACH ROW EXECUTE FUNCTION update_updated_at();

ALTER TABLE gateway_payment_receipts
  ADD COLUMN IF NOT EXISTS proof_key_id VARCHAR(100),
  ADD COLUMN IF NOT EXISTS payload_sha256 VARCHAR(64),
  ADD COLUMN IF NOT EXISTS signature_b64 TEXT;

CREATE INDEX IF NOT EXISTS idx_gateway_payment_receipts_proof_key
  ON gateway_payment_receipts(proof_key_id)
  WHERE proof_key_id IS NOT NULL;

INSERT INTO schema_migrations (migration_id, name)
VALUES ('006', 'processor_proof')
ON CONFLICT (migration_id) DO NOTHING;

COMMIT;
