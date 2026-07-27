-- Migration 006: tenant-scoped royalty capture and reversal uniqueness.
--
-- Detect conflicts before replacing the original global constraints. A
-- conflict aborts the migration instead of deleting or merging financial
-- history.

BEGIN;

DO $$
BEGIN
  IF EXISTS (
    SELECT 1
    FROM royalty_obligations
    GROUP BY organization_id, idempotency_key
    HAVING count(*) > 1
  ) THEN
    RAISE EXCEPTION '006 blocked: duplicate royalty obligation tenant/idempotency rows';
  END IF;

  IF EXISTS (
    SELECT 1
    FROM royalty_obligations
    GROUP BY organization_id, event_id
    HAVING count(*) > 1
  ) THEN
    RAISE EXCEPTION '006 blocked: duplicate royalty obligation tenant/event rows';
  END IF;
END
$$;

ALTER TABLE royalty_obligations
  DROP CONSTRAINT royalty_obligations_idempotency_key_key;

ALTER TABLE royalty_obligations
  ADD CONSTRAINT royalty_obligations_tenant_idempotency_key
    UNIQUE (organization_id, idempotency_key),
  ADD CONSTRAINT royalty_obligations_tenant_event_id
    UNIQUE (organization_id, event_id);

ALTER TABLE royalty_obligations
  ADD COLUMN initial_ledger_transaction_id UUID,
  ADD COLUMN release_ledger_transaction_id UUID;

UPDATE royalty_obligations ro
SET initial_ledger_transaction_id = t.id
FROM transactions t
WHERE t.organization_id = ro.organization_id
  AND t.reference_id = ro.event_id;

UPDATE royalty_obligations ro
SET release_ledger_transaction_id = t.id
FROM transactions t
WHERE t.organization_id = ro.organization_id
  AND t.reference_id = ro.event_id || '-release';

CREATE TABLE royalty_releases (
  id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
  organization_id TEXT NOT NULL,
  royalty_obligation_id UUID NOT NULL REFERENCES royalty_obligations(id),
  release_idempotency_key VARCHAR(255) NOT NULL,
  release_ledger_transaction_id UUID NOT NULL,
  created_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT CURRENT_TIMESTAMP,
  UNIQUE (organization_id, royalty_obligation_id),
  UNIQUE (organization_id, release_idempotency_key)
);

INSERT INTO royalty_releases
  (organization_id, royalty_obligation_id, release_idempotency_key,
   release_ledger_transaction_id)
SELECT
  organization_id,
  id,
  'release-' || event_id,
  release_ledger_transaction_id
FROM royalty_obligations
WHERE release_ledger_transaction_id IS NOT NULL;

ALTER TABLE royalty_reversals
  ADD COLUMN organization_id TEXT;

UPDATE royalty_reversals rr
SET organization_id = ro.organization_id
FROM royalty_obligations ro
WHERE ro.id = rr.reversed_obligation_id;

ALTER TABLE royalty_reversals
  ADD COLUMN reversal_ledger_transaction_ids JSONB NOT NULL DEFAULT '[]',
  ALTER COLUMN organization_id SET NOT NULL,
  DROP CONSTRAINT royalty_reversals_reversal_idempotency_key_key;

UPDATE royalty_reversals
SET reversal_ledger_transaction_ids =
  jsonb_build_array(reversal_ledger_transaction_id)
WHERE reversal_ledger_transaction_id IS NOT NULL;

ALTER TABLE royalty_reversals
  ADD CONSTRAINT royalty_reversals_tenant_idempotency_key
    UNIQUE (organization_id, reversal_idempotency_key),
  ADD CONSTRAINT royalty_reversals_tenant_event_id
    UNIQUE (organization_id, reversal_event_id);

INSERT INTO schema_migrations (migration_id, name)
VALUES ('006', 'royalty_tenant_idempotency')
ON CONFLICT (migration_id) DO NOTHING;

COMMIT;
