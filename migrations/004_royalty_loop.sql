-- Migration 004: Royalty receipt loop (Lyrica <-> Archisynapse).
-- See spec/SPEC-royalty-loop-v1.md and spec/ACCEPTANCE-royalty-loop-v1.md.
--
-- organization_id here is TEXT (per migration 003 — merchant/tenant ids
-- are strings like "lyrica" or "mer_int_...", not UUIDs). The tenant_id
-- on a royalty event IS the organization_id passed to the transaction
-- service; see services/gateway/royalty_tenant_resolver.py.

BEGIN;

-- ============================================================
-- Transaction service: royalty obligation lifecycle (owns ledger posting)
-- ============================================================

CREATE TABLE royalty_obligations (
  id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
  organization_id TEXT NOT NULL,
  event_id VARCHAR(255) NOT NULL,
  correlation_id VARCHAR(255) NOT NULL,
  idempotency_key VARCHAR(255) NOT NULL UNIQUE,
  tenant_id VARCHAR(255) NOT NULL,
  track_id VARCHAR(255) NOT NULL,
  creator_id VARCHAR(255) NOT NULL,
  trigger_kind VARCHAR(20) NOT NULL CHECK (trigger_kind IN ('play', 'remix', 'license')),
  amount NUMERIC(19, 4) NOT NULL CHECK (amount > 0),
  currency VARCHAR(3) NOT NULL DEFAULT 'USD',
  splits JSONB NOT NULL DEFAULT '[]',  -- original [{ownerId, bps}] -- needed so a HELD
                                        -- obligation can be released later using the
                                        -- ORIGINAL splits, not ones re-supplied by the caller
  status VARCHAR(20) NOT NULL DEFAULT 'PENDING'
    CHECK (status IN ('PENDING', 'POSTED', 'HELD', 'BLOCKED', 'REVERSED')),
  decision_policy VARCHAR(50),
  risk_score NUMERIC(5, 4),
  status_reasons JSONB NOT NULL DEFAULT '[]',
  ledger_transaction_id UUID,
  request_hash VARCHAR(64) NOT NULL,
  created_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT CURRENT_TIMESTAMP,
  updated_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX idx_royalty_obligations_event_id ON royalty_obligations(event_id);
CREATE INDEX idx_royalty_obligations_organization_id ON royalty_obligations(organization_id);
CREATE INDEX idx_royalty_obligations_status ON royalty_obligations(status);

CREATE TABLE royalty_payouts (
  id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
  royalty_obligation_id UUID NOT NULL REFERENCES royalty_obligations(id),
  owner_id VARCHAR(255) NOT NULL,
  amount NUMERIC(19, 4) NOT NULL CHECK (amount > 0),
  state VARCHAR(20) NOT NULL DEFAULT 'PAID'
);

CREATE INDEX idx_royalty_payouts_obligation_id ON royalty_payouts(royalty_obligation_id);

-- Reversal linkage: which obligation reversed which.
CREATE TABLE royalty_reversals (
  id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
  reversed_obligation_id UUID NOT NULL REFERENCES royalty_obligations(id),
  reversal_event_id VARCHAR(255) NOT NULL,
  reversal_idempotency_key VARCHAR(255) NOT NULL UNIQUE,
  reversal_ledger_transaction_id UUID,
  created_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TRIGGER trigger_royalty_obligations_updated_at
BEFORE UPDATE ON royalty_obligations
FOR EACH ROW
EXECUTE FUNCTION update_updated_at();

-- ============================================================
-- Gateway: operational state (NOT financial truth -- that lives above
-- and in the ledger service only). Replaces .runtime/royalty_*.json.
-- ============================================================

CREATE TABLE royalty_receipts (
  receipt_id VARCHAR(255) PRIMARY KEY,
  event_id VARCHAR(255) NOT NULL,
  correlation_id VARCHAR(255) NOT NULL,
  tenant_id VARCHAR(255) NOT NULL,
  status VARCHAR(20) NOT NULL,
  payload JSONB NOT NULL,
  created_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT CURRENT_TIMESTAMP,
  updated_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX idx_royalty_receipts_event_id ON royalty_receipts(event_id);

-- Durable idempotency claim, gateway side. PROCESSING lets a concurrent
-- duplicate see "someone else has this key" instead of racing a second
-- call into the transaction service. completed_at/failed_at plus
-- claimed_at give abandoned-claim recovery a real signal to act on.
CREATE TABLE royalty_idempotency (
  tenant_id VARCHAR(255) NOT NULL,
  idempotency_key VARCHAR(255) NOT NULL,
  request_hash VARCHAR(64) NOT NULL,
  status VARCHAR(20) NOT NULL DEFAULT 'PROCESSING'
    CHECK (status IN ('PROCESSING', 'COMPLETED', 'FAILED')),
  receipt_id VARCHAR(255) REFERENCES royalty_receipts(receipt_id),
  failure_reason VARCHAR(255),
  claimed_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT CURRENT_TIMESTAMP,
  completed_at TIMESTAMP WITH TIME ZONE,
  failed_at TIMESTAMP WITH TIME ZONE,
  PRIMARY KEY (tenant_id, idempotency_key)
);

CREATE TABLE royalty_rejections (
  id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
  correlation_id VARCHAR(255),
  key_id VARCHAR(255),
  reason VARCHAR(100) NOT NULL,
  occurred_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE royalty_tenant_keys (
  tenant_id VARCHAR(255) NOT NULL,
  key_id VARCHAR(255) NOT NULL,
  public_key_b64 VARCHAR(255) NOT NULL,
  revoked_at TIMESTAMP WITH TIME ZONE,
  created_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT CURRENT_TIMESTAMP,
  PRIMARY KEY (tenant_id, key_id)
);

-- Never stores a plaintext API key -- api_key_hash is an argon2id hash.
CREATE TABLE royalty_tenant_api_keys (
  tenant_id VARCHAR(255) PRIMARY KEY,
  api_key_hash VARCHAR(255) NOT NULL,
  created_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT CURRENT_TIMESTAMP,
  rotated_at TIMESTAMP WITH TIME ZONE
);

-- ============================================================
-- Reference Lyrica-side transactional outbox (AT-12). This models what
-- Lyrica itself must build (spec/SPEC-royalty-loop-v1.md §8); it is
-- hosted here purely as a runnable reference implementation, not part
-- of the Archisynapse gateway's own state.
-- ============================================================

CREATE TABLE lyrica_outbox (
  event_id VARCHAR(255) PRIMARY KEY,
  idempotency_key VARCHAR(255) NOT NULL,
  correlation_id VARCHAR(255) NOT NULL,
  payload JSONB NOT NULL,
  private_key_b64 VARCHAR(255) NOT NULL,
  key_id VARCHAR(255) NOT NULL,
  state VARCHAR(20) NOT NULL DEFAULT 'pending'
    CHECK (state IN ('pending', 'sent', 'receipted', 'rejected')),
  attempts INT NOT NULL DEFAULT 0,
  next_attempt_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT now(),
  receipt JSONB,
  last_error TEXT,
  created_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT now(),
  updated_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT now()
);

CREATE INDEX idx_lyrica_outbox_state ON lyrica_outbox(state);

CREATE TRIGGER trigger_lyrica_outbox_updated_at
BEFORE UPDATE ON lyrica_outbox
FOR EACH ROW
EXECUTE FUNCTION update_updated_at();

INSERT INTO schema_migrations (migration_id, name) VALUES ('004', 'royalty_loop')
ON CONFLICT (migration_id) DO NOTHING;

COMMIT;
