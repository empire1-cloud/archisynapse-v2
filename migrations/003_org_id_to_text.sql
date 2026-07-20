-- Migration 003: Convert organization_id from UUID to TEXT
-- Merchant IDs are strings like "mer_int_1234567890", not UUIDs.
-- This is a non-destructive ALTER: PostgreSQL casts existing UUIDs to TEXT seamlessly.

BEGIN;

-- Helper: drop dependent views first
DROP VIEW IF EXISTS trial_balance;

-- Convert each table's organization_id from uuid to text
ALTER TABLE accounts ALTER COLUMN organization_id TYPE text USING organization_id::text;
ALTER TABLE audit_logs ALTER COLUMN organization_id TYPE text USING organization_id::text;
ALTER TABLE idempotency_store ALTER COLUMN organization_id TYPE text USING organization_id::text;
ALTER TABLE journal_entries ALTER COLUMN organization_id TYPE text USING organization_id::text;
ALTER TABLE payments ALTER COLUMN organization_id TYPE text USING organization_id::text;
ALTER TABLE refunds ALTER COLUMN organization_id TYPE text USING organization_id::text;
ALTER TABLE transactions ALTER COLUMN organization_id TYPE text USING organization_id::text;
ALTER TABLE unposted_payments ALTER COLUMN organization_id TYPE text USING organization_id::text;

-- Rebuild trial balance view with TEXT organization_id
CREATE OR REPLACE VIEW trial_balance AS
SELECT
    a.id AS account_id,
    a.code AS account_code,
    a.name AS account_name,
    a.organization_id,
    COALESCE(SUM(CASE WHEN je.debit_credit = 'DEBIT' THEN je.amount ELSE 0 END), 0) AS debit_sum,
    COALESCE(SUM(CASE WHEN je.debit_credit = 'CREDIT' THEN je.amount ELSE 0 END), 0) AS credit_sum,
    COALESCE(SUM(CASE WHEN je.debit_credit = 'DEBIT' THEN je.amount ELSE -je.amount END), 0) AS balance
FROM accounts a
LEFT JOIN journal_entries je ON je.account_id = a.id
WHERE a.is_active = true
GROUP BY a.id, a.code, a.name, a.organization_id;

-- Record migration
INSERT INTO schema_migrations (migration_id, name) VALUES ('003', 'org_id_to_text')
ON CONFLICT (migration_id) DO NOTHING;

COMMIT;
