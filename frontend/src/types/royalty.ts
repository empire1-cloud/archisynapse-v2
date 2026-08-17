/**
 * Royalty receipt-loop domain types.
 *
 * Mirrors services/gateway/royalty_events.py and the receipt shape built
 * in royalty_orchestrator.py::_build_receipt. This is the "real" event
 * envelope Lyrica (or any tenant) POSTs to POST /api/v1/events, and the
 * signed receipt the gateway returns/persists.
 */

export type ReceiptStatus = 'processing' | 'paid' | 'held' | 'blocked' | 'reversed' | 'rejected';

export interface VicsProof {
  proof_id: string;
  issued_at: string;
  chain_ref: string;
}

export interface TrackInfo {
  track_id: string;
  dna_tag: string;
  soulprint_hash: string;
  vics_proof: VicsProof;
}

export interface CreatorRef {
  creator_id: string;
  identity_ref: string;
}

export interface Split {
  owner_id: string;
  bps: number; // basis points; splits[].bps must sum to exactly 10000
}

export interface Trigger {
  kind: 'play' | 'remix' | 'license';
  source_ref: string;
  actor_id: string;
}

export interface Amount {
  currency: string;
  value: string; // fixed-point string, exactly 4 decimal places
}

/** The inbound event: royalty.obligation.created, schema_version 1.0 */
export interface RoyaltyObligationCreated {
  schema_version: string;
  event_id: string;
  event_type: 'royalty.obligation.created';
  occurred_at: string;
  correlation_id: string;
  idempotency_key: string; // tenant-scoped: (tenant_id, idempotency_key) is the dedupe key
  tenant_id: string;
  track: TrackInfo;
  creator: CreatorRef;
  splits: Split[];
  trigger: Trigger;
  amount: Amount;
}

export interface Payout {
  owner_id: string;
  amount: string;
  state: string; // lowercased obligation payout state, e.g. "posted" | "held" | "blocked"
}

export interface Amounts {
  currency: string;
  gross: string;
  platform_fee: string;
  net: string;
}

export interface Decision {
  policy: string; // e.g. "allow", "fraud_block_payout", "ownership_invalid", "fraud_service_error"
  risk_score: number; // 0.0 - 1.0
  checks: string[]; // e.g. ["ownership_verified", "dna_match", "vics_valid"]
}

export interface Signature {
  alg: string; // "ed25519"
  key_id: string;
  value: string;
}

/** The signed receipt: GET /api/v1/receipts/{id} and the sync response body */
export interface UnifiedReceipt {
  schema_version: string;
  receipt_id: string;
  status: ReceiptStatus;
  status_reasons: string[];
  event_id: string;
  correlation_id: string;
  tenant_id: string;
  transaction_id: string | null;
  ledger_transaction_id: string | null;
  amounts: Amounts;
  payouts: Payout[];
  decision: Decision;
  issued_at: string;
  signature: Signature;
}

/** POST /api/v1/events error envelope for 409s (royalty_orchestrator.py) */
export interface RoyaltyRejectionBody {
  code: 'idempotency_conflict' | 'processing' | 'retry_later' | string;
  message: string;
  retryable?: boolean;
}

/**
 * Outbox row shape — lyrica_outbox (royalty_outbox_simulator.py). This is
 * the reference outbox Lyrica itself must build against the gateway.
 * State machine: pending -> processing -> receipted
 *                                       -> sent (retryable: 503 retry_later,
 *                                          connection error, or 409 "processing")
 *                                       -> rejected (terminal: validation/auth
 *                                          failure or 409 "idempotency_conflict")
 */
export type OutboxState = 'pending' | 'sent' | 'processing' | 'receipted' | 'rejected';

export interface OutboxRow {
  event_id: string;
  idempotency_key: string;
  correlation_id: string;
  tenant_id: string;
  key_id: string;
  state: OutboxState;
  attempts: number;
  last_error: string | null;
  next_attempt_at: string;
  created_at: string;
  receipt: UnifiedReceipt | null;
}
