-- Migration 008: Least-privilege UPDATE for the tenant role; ledger and audit
-- history stay immutable.
--
-- Follow-up to migration 007 (review findings on PR #16). 007 granted UPDATE
-- on every column of every tenant table. A tenant-scoped session could then:
--   * rewrite its own journal entries (amount, direction, account) while the
--     account-balance trigger, which runs only AFTER INSERT, left balances stale;
--   * rewrite its own audit records (action, previous_state, new_state);
--   * change amounts on payments, transactions, refunds and royalty rows.
--
-- No service runs under `archisynapse_tenant` yet, so none of this was
-- reachable in production. It is fixed before the wiring lands.
--
-- Additive: 007 is not edited. This migration only narrows the tenant role.
-- Owner and superuser connections are unaffected.
--
-- After this migration the tenant role can:
--   * INSERT and SELECT on every tenant table (unchanged);
--   * UPDATE only the columns the services actually update (listed below,
--     taken from the UPDATE and ON CONFLICT DO UPDATE statements in
--     services/ledger, services/transaction and services/gateway);
--   * never UPDATE append-only history: journal_entries, audit_logs, refunds,
--     unposted_payments, royalty_payouts, royalty_reversals, gateway_audit_events;
--   * never set an account balance directly: balances change only through the
--     existing ledger trigger after a journal entry insert.
--
-- Repeated execution is safe.

BEGIN;

-- 1. Remove every table-wide UPDATE grant made by 007.
REVOKE UPDATE ON
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
FROM archisynapse_tenant;

-- 2. Grant back only the columns service code updates. updated_at columns
--    maintained by BEFORE UPDATE triggers need no grant.

-- transaction-service-core.ts: payment outcome and ledger link
GRANT UPDATE (status, processor_transaction_id, failure_reason, ledger_transaction_id)
  ON payments TO archisynapse_tenant;

-- transaction-service-core.ts: refund attempt state machine
GRANT UPDATE (status, processor_refund_id, failure_reason, processor_succeeded_at,
              ledger_transaction_id, ledger_succeeded_at, updated_at)
  ON processor_refund_attempts TO archisynapse_tenant;

-- ledger-service-core.ts: reversal marks the original transaction
GRANT UPDATE (status, updated_at)
  ON transactions TO archisynapse_tenant;

-- ledger-service-core.ts: idempotent response cache (ON CONFLICT DO UPDATE)
GRANT UPDATE (response, expires_at)
  ON idempotency_store TO archisynapse_tenant;

-- royalty-service-core.ts: obligation lifecycle
GRANT UPDATE (status, ledger_transaction_id)
  ON royalty_obligations TO archisynapse_tenant;

-- gateway_store.py: idempotency claim lifecycle
GRANT UPDATE (status, claimed_at, failed_at, failure_reason, event_id, completed_at)
  ON gateway_payment_idempotency TO archisynapse_tenant;

-- gateway_store.py: receipt upsert (ON CONFLICT DO UPDATE)
GRANT UPDATE (status, payload, updated_at)
  ON gateway_payment_receipts TO archisynapse_tenant;

-- royalty_state.py: idempotency claim lifecycle
GRANT UPDATE (status, claimed_at, failure_reason, receipt_id, completed_at, failed_at)
  ON royalty_idempotency TO archisynapse_tenant;

-- royalty_state.py: receipt upsert (ON CONFLICT DO UPDATE)
GRANT UPDATE (status, payload, updated_at)
  ON royalty_receipts TO archisynapse_tenant;

-- 002 update_account_balance trigger: the only writer of balances
GRANT UPDATE (balance, updated_at)
  ON accounts TO archisynapse_tenant;

-- 3. The balance grant exists only for the ledger trigger. Block a tenant-role
--    session from using it directly. pg_trigger_depth() is 1 when this guard
--    fires for a direct UPDATE and 2 or more when the UPDATE comes from inside
--    update_account_balance after a journal entry insert.
CREATE OR REPLACE FUNCTION archisynapse_guard_tenant_account_update()
RETURNS TRIGGER
LANGUAGE plpgsql
AS $$
BEGIN
  IF current_user = 'archisynapse_tenant' AND pg_trigger_depth() <= 1 THEN
    RAISE EXCEPTION 'accounts are updated only by ledger postings'
      USING ERRCODE = 'insufficient_privilege';
  END IF;
  RETURN NEW;
END;
$$;

DO $$
BEGIN
  IF NOT EXISTS (
    SELECT 1 FROM pg_trigger
    WHERE tgname = 'trigger_accounts_tenant_update_guard'
      AND tgrelid = 'accounts'::regclass
  ) THEN
    CREATE TRIGGER trigger_accounts_tenant_update_guard
    BEFORE UPDATE ON accounts
    FOR EACH ROW
    EXECUTE FUNCTION archisynapse_guard_tenant_account_update();
  END IF;
END $$;

INSERT INTO schema_migrations (migration_id, name)
VALUES ('008', 'tenant_ledger_immutability')
ON CONFLICT (migration_id) DO NOTHING;

COMMIT;
