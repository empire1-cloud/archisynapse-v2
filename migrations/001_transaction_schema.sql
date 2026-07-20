-- Migration 001: Transaction Service Schema
-- Owns the customer-facing payment lifecycle.
-- Does NOT own financial bookkeeping — that's the Ledger Service's job.
-- Repeated execution is safe (IF NOT EXISTS).

CREATE EXTENSION IF NOT EXISTS "uuid-ossp";

CREATE TABLE IF NOT EXISTS payments (
  id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
  organization_id UUID NOT NULL,
  customer_id UUID,
  amount NUMERIC(19, 4) NOT NULL CHECK (amount > 0),
  currency VARCHAR(3) NOT NULL DEFAULT 'USD',
  status VARCHAR(20) NOT NULL DEFAULT 'PENDING'
    CHECK (status IN ('PENDING', 'AUTHORIZED', 'SUCCEEDED', 'FAILED', 'REFUNDED', 'PARTIALLY_REFUNDED', 'DISPUTED')),
  payment_method_type VARCHAR(20) NOT NULL CHECK (payment_method_type IN ('CARD', 'BANK_TRANSFER', 'WALLET')),
  payment_method_token VARCHAR(255) NOT NULL,
  payment_method_last4 VARCHAR(4),
  payment_method_brand VARCHAR(50),
  description TEXT,
  idempotency_key VARCHAR(255) NOT NULL UNIQUE,
  ledger_transaction_id UUID,
  processor_transaction_id VARCHAR(255),
  failure_reason TEXT,
  fee_amount NUMERIC(19, 4) NOT NULL DEFAULT 0,
  metadata JSONB,
  created_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT CURRENT_TIMESTAMP,
  updated_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT CURRENT_TIMESTAMP
);

DO $$ BEGIN
  CREATE INDEX idx_payments_organization_id ON payments(organization_id);
EXCEPTION WHEN duplicate_table THEN NULL;
END $$;

DO $$ BEGIN
  CREATE INDEX idx_payments_customer_id ON payments(customer_id);
EXCEPTION WHEN duplicate_table THEN NULL;
END $$;

DO $$ BEGIN
  CREATE INDEX idx_payments_status ON payments(status);
EXCEPTION WHEN duplicate_table THEN NULL;
END $$;

DO $$ BEGIN
  CREATE INDEX idx_payments_idempotency_key ON payments(idempotency_key);
EXCEPTION WHEN duplicate_table THEN NULL;
END $$;

DO $$ BEGIN
  CREATE INDEX idx_payments_created_at ON payments(created_at);
EXCEPTION WHEN duplicate_table THEN NULL;
END $$;

CREATE TABLE IF NOT EXISTS refunds (
  id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
  payment_id UUID NOT NULL REFERENCES payments(id),
  organization_id UUID NOT NULL,
  amount NUMERIC(19, 4) NOT NULL CHECK (amount > 0),
  reason TEXT NOT NULL,
  status VARCHAR(20) NOT NULL CHECK (status IN ('SUCCEEDED', 'FAILED')),
  idempotency_key VARCHAR(255) NOT NULL UNIQUE,
  ledger_transaction_id UUID,
  created_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT CURRENT_TIMESTAMP
);

DO $$ BEGIN
  CREATE INDEX idx_refunds_payment_id ON refunds(payment_id);
EXCEPTION WHEN duplicate_table THEN NULL;
END $$;

DO $$ BEGIN
  CREATE INDEX idx_refunds_organization_id ON refunds(organization_id);
EXCEPTION WHEN duplicate_table THEN NULL;
END $$;

CREATE OR REPLACE FUNCTION update_updated_at()
RETURNS TRIGGER AS $$
BEGIN
  NEW.updated_at = CURRENT_TIMESTAMP;
  RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS trigger_payments_updated_at ON payments;
CREATE TRIGGER trigger_payments_updated_at
BEFORE UPDATE ON payments
FOR EACH ROW
EXECUTE FUNCTION update_updated_at();

INSERT INTO schema_migrations (migration_id, name)
VALUES ('001', 'transaction_schema')
ON CONFLICT (migration_id) DO NOTHING;
