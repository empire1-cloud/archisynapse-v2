-- Migration 005: remove plaintext outbox signing keys and add worker leases.
--
-- Existing raw keys must be imported into the configured secret provider
-- before this migration runs. The migration intentionally refuses to copy,
-- log, or silently discard those secrets.

BEGIN;

DO $$
BEGIN
  IF EXISTS (
    SELECT 1
    FROM lyrica_outbox
    WHERE private_key_b64 IS NOT NULL AND private_key_b64 <> ''
  ) THEN
    RAISE EXCEPTION
      '005 blocked: import existing lyrica_outbox private keys into the secret provider, set signing references, and clear private_key_b64 before retrying';
  END IF;
END
$$;

ALTER TABLE lyrica_outbox
  ADD COLUMN signing_key_ref VARCHAR(512),
  ADD COLUMN lease_owner VARCHAR(255),
  ADD COLUMN lease_expires_at TIMESTAMP WITH TIME ZONE;

UPDATE lyrica_outbox
SET signing_key_ref = 'migration-required://' || event_id
WHERE signing_key_ref IS NULL;

ALTER TABLE lyrica_outbox
  ALTER COLUMN signing_key_ref SET NOT NULL;

ALTER TABLE lyrica_outbox
  DROP CONSTRAINT lyrica_outbox_state_check;

ALTER TABLE lyrica_outbox
  ADD CONSTRAINT lyrica_outbox_state_check
  CHECK (state IN ('pending', 'sent', 'processing', 'receipted', 'rejected'));

ALTER TABLE lyrica_outbox
  DROP COLUMN private_key_b64;

CREATE INDEX idx_lyrica_outbox_due_lease
  ON lyrica_outbox (next_attempt_at, lease_expires_at)
  WHERE state IN ('pending', 'sent', 'processing');

INSERT INTO schema_migrations (migration_id, name)
VALUES ('005', 'royalty_outbox_hardening')
ON CONFLICT (migration_id) DO NOTHING;

COMMIT;
