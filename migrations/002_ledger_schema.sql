-- Migration 002: Ledger Service Schema
-- Double-entry bookkeeping with immutable ledger entries.
-- Financial amounts stored as NUMERIC for precision.
-- Adds unposted_payments table for durable reconciliation.
-- Repeated execution is safe (IF NOT EXISTS).

CREATE EXTENSION IF NOT EXISTS "uuid-ossp";

CREATE TABLE IF NOT EXISTS accounts (
  id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
  organization_id UUID NOT NULL,
  code VARCHAR(20) NOT NULL,
  name VARCHAR(255) NOT NULL,
  type VARCHAR(20) NOT NULL CHECK (type IN ('ASSET', 'LIABILITY', 'EQUITY', 'REVENUE', 'EXPENSE')),
  balance NUMERIC(19, 4) NOT NULL DEFAULT 0,
  currency VARCHAR(3) NOT NULL DEFAULT 'USD',
  is_active BOOLEAN NOT NULL DEFAULT true,
  metadata JSONB,
  created_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT CURRENT_TIMESTAMP,
  updated_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT CURRENT_TIMESTAMP,
  UNIQUE(organization_id, code)
);

DO $$ BEGIN
  CREATE INDEX idx_accounts_organization_id ON accounts(organization_id);
EXCEPTION WHEN duplicate_table THEN NULL;
END $$;

DO $$ BEGIN
  CREATE INDEX idx_accounts_type ON accounts(type);
EXCEPTION WHEN duplicate_table THEN NULL;
END $$;

CREATE TABLE IF NOT EXISTS journal_entries (
  id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
  transaction_id UUID NOT NULL,
  organization_id UUID NOT NULL,
  account_id UUID NOT NULL REFERENCES accounts(id),
  debit_credit VARCHAR(6) NOT NULL CHECK (debit_credit IN ('DEBIT', 'CREDIT')),
  amount NUMERIC(19, 4) NOT NULL CHECK (amount > 0),
  description TEXT NOT NULL,
  metadata JSONB,
  created_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT CURRENT_TIMESTAMP
);

DO $$ BEGIN
  CREATE INDEX idx_journal_entries_transaction_id ON journal_entries(transaction_id);
EXCEPTION WHEN duplicate_table THEN NULL;
END $$;

DO $$ BEGIN
  CREATE INDEX idx_journal_entries_account_id ON journal_entries(account_id);
EXCEPTION WHEN duplicate_table THEN NULL;
END $$;

DO $$ BEGIN
  CREATE INDEX idx_journal_entries_organization_id ON journal_entries(organization_id);
EXCEPTION WHEN duplicate_table THEN NULL;
END $$;

DO $$ BEGIN
  CREATE INDEX idx_journal_entries_created_at ON journal_entries(created_at);
EXCEPTION WHEN duplicate_table THEN NULL;
END $$;

CREATE TABLE IF NOT EXISTS transactions (
  id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
  organization_id UUID NOT NULL,
  type VARCHAR(20) NOT NULL CHECK (type IN ('PAYMENT', 'PAYOUT', 'REFUND', 'CHARGEBACK', 'FEE', 'REVERSAL', 'ADJUSTMENT')),
  reference_id VARCHAR(100),
  description TEXT NOT NULL,
  amount NUMERIC(19, 4) NOT NULL,
  currency VARCHAR(3) NOT NULL DEFAULT 'USD',
  status VARCHAR(20) NOT NULL DEFAULT 'POSTED' CHECK (status IN ('PENDING', 'POSTED', 'FAILED', 'REVERSED')),
  idempotency_key VARCHAR(255) UNIQUE,
  metadata JSONB,
  posted_at TIMESTAMP WITH TIME ZONE,
  created_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT CURRENT_TIMESTAMP,
  updated_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT CURRENT_TIMESTAMP
);

DO $$ BEGIN
  CREATE INDEX idx_transactions_organization_id ON transactions(organization_id);
EXCEPTION WHEN duplicate_table THEN NULL;
END $$;

DO $$ BEGIN
  CREATE INDEX idx_transactions_type ON transactions(type);
EXCEPTION WHEN duplicate_table THEN NULL;
END $$;

DO $$ BEGIN
  CREATE INDEX idx_transactions_status ON transactions(status);
EXCEPTION WHEN duplicate_table THEN NULL;
END $$;

DO $$ BEGIN
  CREATE INDEX idx_transactions_reference_id ON transactions(reference_id);
EXCEPTION WHEN duplicate_table THEN NULL;
END $$;

DO $$ BEGIN
  CREATE INDEX idx_transactions_posted_at ON transactions(posted_at);
EXCEPTION WHEN duplicate_table THEN NULL;
END $$;

DO $$ BEGIN
  CREATE INDEX idx_transactions_idempotency_key ON transactions(idempotency_key);
EXCEPTION WHEN duplicate_table THEN NULL;
END $$;

CREATE TABLE IF NOT EXISTS audit_logs (
  id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
  organization_id UUID NOT NULL,
  action VARCHAR(20) NOT NULL CHECK (action IN ('CREATE', 'POST', 'REVERSE', 'UPDATE', 'DELETE')),
  entity_type VARCHAR(20) NOT NULL CHECK (entity_type IN ('ACCOUNT', 'TRANSACTION', 'ENTRY')),
  entity_id VARCHAR(100) NOT NULL,
  previous_state JSONB,
  new_state JSONB NOT NULL,
  actor_id UUID,
  ip_address VARCHAR(45),
  created_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT CURRENT_TIMESTAMP
);

DO $$ BEGIN
  CREATE INDEX idx_audit_logs_organization_id ON audit_logs(organization_id);
EXCEPTION WHEN duplicate_table THEN NULL;
END $$;

DO $$ BEGIN
  CREATE INDEX idx_audit_logs_entity_id ON audit_logs(entity_id);
EXCEPTION WHEN duplicate_table THEN NULL;
END $$;

DO $$ BEGIN
  CREATE INDEX idx_audit_logs_created_at ON audit_logs(created_at);
EXCEPTION WHEN duplicate_table THEN NULL;
END $$;

CREATE TABLE IF NOT EXISTS idempotency_store (
  idempotency_key VARCHAR(255) PRIMARY KEY,
  organization_id UUID NOT NULL,
  request_hash VARCHAR(64) NOT NULL,
  response JSONB NOT NULL,
  created_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT CURRENT_TIMESTAMP,
  expires_at TIMESTAMP WITH TIME ZONE NOT NULL
);

DO $$ BEGIN
  CREATE INDEX idx_idempotency_store_expires_at ON idempotency_store(expires_at);
EXCEPTION WHEN duplicate_table THEN NULL;
END $$;

-- Reconciliation table: tracks payments that succeeded but ledger posting is pending
CREATE TABLE IF NOT EXISTS unposted_payments (
  id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
  organization_id UUID NOT NULL,
  payment_id UUID NOT NULL,
  idempotency_key VARCHAR(255) NOT NULL,
  gross_amount NUMERIC(19, 4) NOT NULL,
  fee_amount NUMERIC(19, 4) NOT NULL DEFAULT 0,
  net_amount NUMERIC(19, 4) NOT NULL,
  currency VARCHAR(3) NOT NULL DEFAULT 'USD',
  reference_id VARCHAR(100) NOT NULL,
  status VARCHAR(20) NOT NULL DEFAULT 'PENDING' CHECK (status IN ('PENDING', 'PROCESSING', 'COMPLETED', 'FAILED')),
  attempts INTEGER NOT NULL DEFAULT 0,
  last_attempt_at TIMESTAMP WITH TIME ZONE,
  next_attempt_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT CURRENT_TIMESTAMP,
  error_message TEXT,
  metadata JSONB,
  created_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT CURRENT_TIMESTAMP,
  updated_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT CURRENT_TIMESTAMP
);

DO $$ BEGIN
  CREATE INDEX idx_unposted_payments_status ON unposted_payments(status);
EXCEPTION WHEN duplicate_table THEN NULL;
END $$;

DO $$ BEGIN
  CREATE INDEX idx_unposted_payments_next_attempt ON unposted_payments(next_attempt_at) WHERE status = 'PENDING';
EXCEPTION WHEN duplicate_table THEN NULL;
END $$;

DO $$ BEGIN
  CREATE INDEX idx_unposted_payments_reference_id ON unposted_payments(reference_id);
EXCEPTION WHEN duplicate_table THEN NULL;
END $$;

-- Trial balance view
CREATE OR REPLACE VIEW trial_balance AS
SELECT
  a.id as account_id,
  a.code as account_code,
  a.name as account_name,
  a.organization_id,
  COALESCE(SUM(CASE WHEN je.debit_credit = 'DEBIT' THEN je.amount ELSE 0 END), 0) as debit_sum,
  COALESCE(SUM(CASE WHEN je.debit_credit = 'CREDIT' THEN je.amount ELSE 0 END), 0) as credit_sum,
  COALESCE(SUM(CASE WHEN je.debit_credit = 'DEBIT' THEN je.amount ELSE 0 END), 0) -
  COALESCE(SUM(CASE WHEN je.debit_credit = 'CREDIT' THEN je.amount ELSE 0 END), 0) as balance
FROM accounts a
LEFT JOIN journal_entries je ON a.id = je.account_id
GROUP BY a.id, a.code, a.name, a.organization_id;

-- Function: Check transaction balance
CREATE OR REPLACE FUNCTION check_transaction_balanced(p_transaction_id UUID)
RETURNS BOOLEAN AS $$
DECLARE
  v_debit_sum NUMERIC;
  v_credit_sum NUMERIC;
BEGIN
  SELECT
    COALESCE(SUM(CASE WHEN debit_credit = 'DEBIT' THEN amount ELSE 0 END), 0),
    COALESCE(SUM(CASE WHEN debit_credit = 'CREDIT' THEN amount ELSE 0 END), 0)
  INTO v_debit_sum, v_credit_sum
  FROM journal_entries
  WHERE transaction_id = p_transaction_id;

  RETURN v_debit_sum = v_credit_sum AND v_debit_sum > 0;
END;
$$ LANGUAGE plpgsql STABLE;

-- Function: Update account balance after entry insertion
CREATE OR REPLACE FUNCTION update_account_balance()
RETURNS TRIGGER AS $$
BEGIN
  UPDATE accounts
  SET balance = (
    SELECT COALESCE(SUM(CASE WHEN debit_credit = 'DEBIT' THEN amount ELSE -amount END), 0)
    FROM journal_entries
    WHERE account_id = NEW.account_id
  ),
  updated_at = CURRENT_TIMESTAMP
  WHERE id = NEW.account_id;
  RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS trigger_update_account_balance ON journal_entries;
CREATE TRIGGER trigger_update_account_balance
AFTER INSERT ON journal_entries
FOR EACH ROW
EXECUTE FUNCTION update_account_balance();

INSERT INTO schema_migrations (migration_id, name)
VALUES ('002', 'ledger_schema')
ON CONFLICT (migration_id) DO NOTHING;
