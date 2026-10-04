-- Migration 007: Tenant isolation with PostgreSQL row-level security (RLS).
--
-- Additive only. No table, column, row, or existing policy is dropped or
-- changed, and current services keep working exactly as before:
--
--   * Services connect as the table owner / superuser today. RLS is ENABLED,
--     not FORCED, so owner and superuser connections bypass it as they did
--     before this migration.
--   * Isolation applies to the new NOLOGIN role `archisynapse_tenant`. A
--     tenant-scoped unit of work opts in with, inside one transaction:
--
--         SET LOCAL ROLE archisynapse_tenant;
--         SELECT set_config('archisynapse.tenant_id', '<merchant id>', true);
--
--     Under that role a tenant sees and writes only its own rows. With no
--     tenant set, it sees nothing and can write nothing.
--
-- What this migration does NOT prove: that the services already run their
-- tenant queries under this role. That wiring is the next step; until then
-- the protection is available in the database but not yet in use.
--
-- Deliberately out of scope for the tenant role (no grant at all, so any
-- access from it is denied):
--   gateway_merchant_api_keys, royalty_tenant_keys, royalty_tenant_api_keys
--     (credential lookups that happen before the tenant is known)
--   royalty_rejections, economic_truth_outbox, lyrica_outbox, schema_migrations
--     (no tenant column; system-level state)
--   trial_balance view (views run with owner rights and would bypass RLS)
--
-- Repeated execution is safe.

BEGIN;

-- ------------------------------------------------------------------
-- Role and tenant lookup
-- ------------------------------------------------------------------

DO $$
BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'archisynapse_tenant') THEN
    CREATE ROLE archisynapse_tenant NOLOGIN;
  END IF;
END $$;

GRANT USAGE ON SCHEMA public TO archisynapse_tenant;

-- Empty or unset setting -> NULL -> every policy below evaluates to false.
CREATE OR REPLACE FUNCTION archisynapse_current_tenant()
RETURNS TEXT
LANGUAGE sql
STABLE
AS $$
  SELECT NULLIF(current_setting('archisynapse.tenant_id', true), '')
$$;

-- ------------------------------------------------------------------
-- Tables with their own tenant column: one permissive policy each
-- ------------------------------------------------------------------

DO $$
DECLARE
  target RECORD;
BEGIN
  FOR target IN
    SELECT * FROM (VALUES
      ('payments',                    'organization_id'),
      ('refunds',                     'organization_id'),
      ('processor_refund_attempts',   'organization_id'),
      ('accounts',                    'organization_id'),
      ('journal_entries',             'organization_id'),
      ('transactions',                'organization_id'),
      ('audit_logs',                  'organization_id'),
      ('idempotency_store',           'organization_id'),
      ('unposted_payments',           'organization_id'),
      ('royalty_obligations',         'organization_id'),
      ('gateway_merchants',           'merchant_id'),
      ('gateway_payment_receipts',    'merchant_id'),
      ('gateway_payment_idempotency', 'merchant_id'),
      ('gateway_audit_events',        'merchant_id'),
      ('royalty_receipts',            'tenant_id'),
      ('royalty_idempotency',         'tenant_id')
    ) AS t(table_name, tenant_column)
  LOOP
    EXECUTE format('ALTER TABLE %I ENABLE ROW LEVEL SECURITY', target.table_name);

    IF NOT EXISTS (
      SELECT 1 FROM pg_policies
      WHERE schemaname = 'public'
        AND tablename = target.table_name
        AND policyname = 'tenant_isolation'
    ) THEN
      EXECUTE format(
        'CREATE POLICY tenant_isolation ON %I AS PERMISSIVE FOR ALL '
        'TO archisynapse_tenant '
        'USING (%I = archisynapse_current_tenant()) '
        'WITH CHECK (%I = archisynapse_current_tenant())',
        target.table_name, target.tenant_column, target.tenant_column
      );
    END IF;
  END LOOP;
END $$;

-- ------------------------------------------------------------------
-- Child tables with no tenant column: visible only through their parent
-- ------------------------------------------------------------------

ALTER TABLE royalty_payouts ENABLE ROW LEVEL SECURITY;
ALTER TABLE royalty_reversals ENABLE ROW LEVEL SECURITY;

DO $$
BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_policies WHERE schemaname = 'public'
                 AND tablename = 'royalty_payouts' AND policyname = 'tenant_isolation') THEN
    CREATE POLICY tenant_isolation ON royalty_payouts AS PERMISSIVE FOR ALL
      TO archisynapse_tenant
      USING (EXISTS (
        SELECT 1 FROM royalty_obligations o
        WHERE o.id = royalty_payouts.royalty_obligation_id
          AND o.organization_id = archisynapse_current_tenant()))
      WITH CHECK (EXISTS (
        SELECT 1 FROM royalty_obligations o
        WHERE o.id = royalty_payouts.royalty_obligation_id
          AND o.organization_id = archisynapse_current_tenant()));
  END IF;

  IF NOT EXISTS (SELECT 1 FROM pg_policies WHERE schemaname = 'public'
                 AND tablename = 'royalty_reversals' AND policyname = 'tenant_isolation') THEN
    CREATE POLICY tenant_isolation ON royalty_reversals AS PERMISSIVE FOR ALL
      TO archisynapse_tenant
      USING (EXISTS (
        SELECT 1 FROM royalty_obligations o
        WHERE o.id = royalty_reversals.reversed_obligation_id
          AND o.organization_id = archisynapse_current_tenant()))
      WITH CHECK (EXISTS (
        SELECT 1 FROM royalty_obligations o
        WHERE o.id = royalty_reversals.reversed_obligation_id
          AND o.organization_id = archisynapse_current_tenant()));
  END IF;
END $$;

-- ------------------------------------------------------------------
-- Cross-tenant references: a row may only point at a parent row owned by
-- the same tenant. Foreign-key checks ignore RLS, so without these a
-- tenant could attach its own row to another tenant's account or payment.
-- RESTRICTIVE = must pass in addition to tenant_isolation.
-- ------------------------------------------------------------------

DO $$
BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_policies WHERE schemaname = 'public'
                 AND tablename = 'journal_entries' AND policyname = 'same_tenant_account') THEN
    CREATE POLICY same_tenant_account ON journal_entries AS RESTRICTIVE FOR ALL
      TO archisynapse_tenant
      USING (true)
      WITH CHECK (EXISTS (
        SELECT 1 FROM accounts a
        WHERE a.id = journal_entries.account_id
          AND a.organization_id = journal_entries.organization_id));
  END IF;

  IF NOT EXISTS (SELECT 1 FROM pg_policies WHERE schemaname = 'public'
                 AND tablename = 'refunds' AND policyname = 'same_tenant_payment') THEN
    CREATE POLICY same_tenant_payment ON refunds AS RESTRICTIVE FOR ALL
      TO archisynapse_tenant
      USING (true)
      WITH CHECK (EXISTS (
        SELECT 1 FROM payments p
        WHERE p.id = refunds.payment_id
          AND p.organization_id = refunds.organization_id));
  END IF;

  IF NOT EXISTS (SELECT 1 FROM pg_policies WHERE schemaname = 'public'
                 AND tablename = 'processor_refund_attempts' AND policyname = 'same_tenant_payment') THEN
    CREATE POLICY same_tenant_payment ON processor_refund_attempts AS RESTRICTIVE FOR ALL
      TO archisynapse_tenant
      USING (true)
      WITH CHECK (EXISTS (
        SELECT 1 FROM payments p
        WHERE p.id = processor_refund_attempts.payment_id
          AND p.organization_id = processor_refund_attempts.organization_id));
  END IF;

  IF NOT EXISTS (SELECT 1 FROM pg_policies WHERE schemaname = 'public'
                 AND tablename = 'gateway_payment_idempotency' AND policyname = 'same_tenant_receipt') THEN
    CREATE POLICY same_tenant_receipt ON gateway_payment_idempotency AS RESTRICTIVE FOR ALL
      TO archisynapse_tenant
      USING (true)
      WITH CHECK (
        event_id IS NULL OR EXISTS (
          SELECT 1 FROM gateway_payment_receipts r
          WHERE r.event_id = gateway_payment_idempotency.event_id
            AND r.merchant_id = gateway_payment_idempotency.merchant_id));
  END IF;

  IF NOT EXISTS (SELECT 1 FROM pg_policies WHERE schemaname = 'public'
                 AND tablename = 'royalty_idempotency' AND policyname = 'same_tenant_receipt') THEN
    CREATE POLICY same_tenant_receipt ON royalty_idempotency AS RESTRICTIVE FOR ALL
      TO archisynapse_tenant
      USING (true)
      WITH CHECK (
        receipt_id IS NULL OR EXISTS (
          SELECT 1 FROM royalty_receipts r
          WHERE r.receipt_id = royalty_idempotency.receipt_id
            AND r.tenant_id = royalty_idempotency.tenant_id));
  END IF;
END $$;

-- ------------------------------------------------------------------
-- Grants. No DELETE anywhere: ledger history is append-only, and no
-- service code deletes these rows today. Merchant records are read-only
-- to the tenant role (onboarding stays an admin action).
-- ------------------------------------------------------------------

GRANT SELECT ON gateway_merchants TO archisynapse_tenant;

GRANT SELECT, INSERT, UPDATE ON
  payments,
  refunds,
  processor_refund_attempts,
  accounts,
  journal_entries,
  transactions,
  audit_logs,
  idempotency_store,
  unposted_payments,
  royalty_obligations,
  royalty_payouts,
  royalty_reversals,
  gateway_payment_receipts,
  gateway_payment_idempotency,
  gateway_audit_events,
  royalty_receipts,
  royalty_idempotency
TO archisynapse_tenant;

INSERT INTO schema_migrations (migration_id, name)
VALUES ('007', 'tenant_isolation_rls')
ON CONFLICT (migration_id) DO NOTHING;

COMMIT;
