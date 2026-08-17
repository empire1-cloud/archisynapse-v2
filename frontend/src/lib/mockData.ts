/**
 * ============================================================================
 * MOCK DATA — clearly labeled, never presented as live.
 * ============================================================================
 *
 * Every shape here matches the REAL backend 1:1 (see src/types/*.ts, each
 * annotated with the source file it mirrors):
 *   - Account / JournalEntry / Transaction  <- services/ledger/ledger-service-types.ts
 *   - RoyaltyObligationCreated / UnifiedReceipt <- services/gateway/royalty_events.py,
 *     services/gateway/royalty_orchestrator.py::_build_receipt
 *   - OutboxRow state machine <- services/gateway/royalty_outbox_simulator.py
 *   - RiskAssessment <- services/gateway/royalty_decision.py (fraud-service call)
 *
 * This module is the fallback data source when no live gateway is reachable
 * at VITE_API_BASE_URL (see src/lib/apiClient.ts). Swap-in for live data is
 * a matter of pointing that env var at a running gateway — the UI layer
 * never branches on mock-vs-live beyond the banner it renders.
 */

import type { Account, JournalEntry, Transaction } from '../types/ledger';
import type { OutboxRow, RoyaltyObligationCreated, UnifiedReceipt } from '../types/royalty';
import type { RiskAssessment } from '../types/risk';

const ORG_ID = 'org_lyrica_9f21';
const TENANT_ID = 'lyrica-music-group';

// ---------------------------------------------------------------------------
// Chart of accounts
// ---------------------------------------------------------------------------

export const mockAccounts: Account[] = [
  {
    id: 'acct_cash_op',
    organizationId: ORG_ID,
    code: '1000',
    name: 'Cash — Operating',
    type: 'ASSET',
    balance: '18420.6600',
    currency: 'USD',
    isActive: true,
    createdAt: '2026-06-01T00:00:00Z',
    updatedAt: '2026-08-15T22:14:03Z',
  },
  {
    id: 'acct_payout_clearing',
    organizationId: ORG_ID,
    code: '1010',
    name: 'Payout Clearing',
    type: 'ASSET',
    balance: '2140.0000',
    currency: 'USD',
    isActive: true,
    createdAt: '2026-06-01T00:00:00Z',
    updatedAt: '2026-08-15T20:02:11Z',
  },
  {
    id: 'acct_creator_payable',
    organizationId: ORG_ID,
    code: '2000',
    name: 'Creator Royalties Payable',
    type: 'LIABILITY',
    balance: '3186.4000',
    currency: 'USD',
    isActive: true,
    createdAt: '2026-06-01T00:00:00Z',
    updatedAt: '2026-08-15T22:14:03Z',
  },
  {
    id: 'acct_chargeback_reserve',
    organizationId: ORG_ID,
    code: '2010',
    name: 'Chargeback Reserve',
    type: 'LIABILITY',
    balance: '412.0000',
    currency: 'USD',
    isActive: true,
    createdAt: '2026-06-01T00:00:00Z',
    updatedAt: '2026-08-14T09:00:00Z',
  },
  {
    id: 'acct_platform_revenue',
    organizationId: ORG_ID,
    code: '4000',
    name: 'Platform Fee Revenue',
    type: 'REVENUE',
    balance: '2618.5000',
    currency: 'USD',
    isActive: true,
    createdAt: '2026-06-01T00:00:00Z',
    updatedAt: '2026-08-15T22:14:03Z',
  },
];

const acct = (code: string) => mockAccounts.find((a) => a.code === code)!.id;

// ---------------------------------------------------------------------------
// Double-entry journal — every transaction balances to zero
// ---------------------------------------------------------------------------

function entry(
  id: string,
  transactionId: string,
  accountId: string,
  debitCredit: 'DEBIT' | 'CREDIT',
  amount: string,
  description: string,
  createdAt: string,
): JournalEntry {
  return {
    id,
    transactionId,
    organizationId: ORG_ID,
    accountId,
    debitCredit,
    amount,
    description,
    createdAt,
  };
}

export const mockTransactions: Transaction[] = [
  {
    id: 'txn_8a41f2',
    organizationId: ORG_ID,
    type: 'PAYMENT',
    referenceId: 'evt_7f3a9c2d1e4b',
    description: 'Streaming pool ingest — remix trigger, track NEON_BLOOM',
    amount: '128.4000',
    currency: 'USD',
    status: 'POSTED',
    idempotencyKey: 'lyrica:remix:trk_neon_bloom:2026-08-15T18Z',
    entries: [
      entry('je_001', 'txn_8a41f2', acct('1000'), 'DEBIT', '128.4000', 'Cash received from streaming pool', '2026-08-15T18:02:11Z'),
      entry('je_002', 'txn_8a41f2', acct('2000'), 'CREDIT', '128.4000', 'Royalty obligation recognized', '2026-08-15T18:02:11Z'),
    ],
    postedAt: '2026-08-15T18:02:11Z',
    createdAt: '2026-08-15T18:02:10Z',
    updatedAt: '2026-08-15T18:02:11Z',
  },
  {
    id: 'txn_c910de',
    organizationId: ORG_ID,
    type: 'PAYOUT',
    referenceId: 'evt_7f3a9c2d1e4b',
    description: 'Royalty payout — NEON_BLOOM, 2 owners',
    amount: '115.5600',
    currency: 'USD',
    status: 'POSTED',
    idempotencyKey: 'lyrica:remix:trk_neon_bloom:2026-08-15T18Z:payout',
    entries: [
      entry('je_003', 'txn_c910de', acct('2000'), 'DEBIT', '115.5600', 'Payable cleared on payout', '2026-08-15T18:04:47Z'),
      entry('je_004', 'txn_c910de', acct('1000'), 'CREDIT', '115.5600', 'Cash disbursed to payout clearing', '2026-08-15T18:04:47Z'),
    ],
    postedAt: '2026-08-15T18:04:47Z',
    createdAt: '2026-08-15T18:04:46Z',
    updatedAt: '2026-08-15T18:04:47Z',
  },
  {
    id: 'txn_2be701',
    organizationId: ORG_ID,
    type: 'FEE',
    referenceId: 'evt_7f3a9c2d1e4b',
    description: 'Platform fee — NEON_BLOOM royalty event',
    amount: '12.8400',
    currency: 'USD',
    status: 'POSTED',
    idempotencyKey: 'lyrica:remix:trk_neon_bloom:2026-08-15T18Z:fee',
    entries: [
      entry('je_005', 'txn_2be701', acct('2000'), 'DEBIT', '12.8400', 'Platform fee split from obligation', '2026-08-15T18:04:47Z'),
      entry('je_006', 'txn_2be701', acct('4000'), 'CREDIT', '12.8400', 'Platform fee revenue recognized', '2026-08-15T18:04:47Z'),
    ],
    postedAt: '2026-08-15T18:04:47Z',
    createdAt: '2026-08-15T18:04:47Z',
    updatedAt: '2026-08-15T18:04:47Z',
  },
  {
    id: 'txn_51ac9f',
    organizationId: ORG_ID,
    type: 'PAYMENT',
    referenceId: 'evt_1c8d4e7a2f90',
    description: 'Streaming pool ingest — play trigger, track SOUTHERN_GLOW',
    amount: '64.2000',
    currency: 'USD',
    status: 'POSTED',
    idempotencyKey: 'lyrica:play:trk_southern_glow:2026-08-15T14Z',
    entries: [
      entry('je_007', 'txn_51ac9f', acct('1000'), 'DEBIT', '64.2000', 'Cash received from streaming pool', '2026-08-15T14:31:02Z'),
      entry('je_008', 'txn_51ac9f', acct('2000'), 'CREDIT', '64.2000', 'Royalty obligation recognized', '2026-08-15T14:31:02Z'),
    ],
    postedAt: '2026-08-15T14:31:02Z',
    createdAt: '2026-08-15T14:31:01Z',
    updatedAt: '2026-08-15T14:31:02Z',
  },
  {
    id: 'txn_f30a11',
    organizationId: ORG_ID,
    type: 'PAYMENT',
    referenceId: 'evt_9b2f6c1a4d33',
    description: 'Streaming pool ingest — license trigger, track DUSTBOWL_REVIVAL',
    amount: '340.0000',
    currency: 'USD',
    status: 'PENDING',
    idempotencyKey: 'lyrica:license:trk_dustbowl_revival:2026-08-15T21Z',
    entries: [
      entry('je_009', 'txn_f30a11', acct('1000'), 'DEBIT', '340.0000', 'Cash pending clearance', '2026-08-15T21:47:00Z'),
      entry('je_010', 'txn_f30a11', acct('2000'), 'CREDIT', '340.0000', 'Royalty obligation held pending fraud review', '2026-08-15T21:47:00Z'),
    ],
    postedAt: '2026-08-15T21:47:00Z',
    createdAt: '2026-08-15T21:46:59Z',
    updatedAt: '2026-08-15T21:47:00Z',
  },
  {
    id: 'txn_7dd420',
    organizationId: ORG_ID,
    type: 'CHARGEBACK',
    referenceId: 'evt_44a1b9e0f612',
    description: 'Chargeback — disputed license trigger, track VELVET_UNDERGRD',
    amount: '212.0000',
    currency: 'USD',
    status: 'POSTED',
    idempotencyKey: 'lyrica:chargeback:trk_velvet_undergrd:2026-08-13T10Z',
    entries: [
      entry('je_011', 'txn_7dd420', acct('1000'), 'CREDIT', '212.0000', 'Cash reversed on dispute', '2026-08-13T10:15:44Z'),
      entry('je_012', 'txn_7dd420', acct('2010'), 'DEBIT', '212.0000', 'Chargeback reserve funded', '2026-08-13T10:15:44Z'),
    ],
    postedAt: '2026-08-13T10:15:44Z',
    createdAt: '2026-08-13T10:15:43Z',
    updatedAt: '2026-08-13T10:15:44Z',
  },
  {
    id: 'txn_0f8b6a',
    organizationId: ORG_ID,
    type: 'REVERSAL',
    referenceId: 'evt_2d7e5f8a1b40',
    description: 'Reversal — obligation.reversed for track BROKEN_COMPASS',
    amount: '88.0000',
    currency: 'USD',
    status: 'REVERSED',
    idempotencyKey: 'lyrica:reverse:trk_broken_compass:2026-08-12T09Z',
    entries: [
      entry('je_013', 'txn_0f8b6a', acct('2000'), 'DEBIT', '88.0000', 'Obligation reversed at source', '2026-08-12T09:22:18Z'),
      entry('je_014', 'txn_0f8b6a', acct('1000'), 'CREDIT', '88.0000', 'Cash returned to pool', '2026-08-12T09:22:18Z'),
    ],
    postedAt: '2026-08-12T09:22:18Z',
    createdAt: '2026-08-12T09:22:17Z',
    updatedAt: '2026-08-12T09:22:18Z',
  },
  {
    id: 'txn_3e19cd',
    organizationId: ORG_ID,
    type: 'PAYOUT',
    referenceId: 'evt_c5a8b2e19f04',
    description: 'Royalty payout — blocked, high-risk device fingerprint',
    amount: '96.0000',
    currency: 'USD',
    status: 'FAILED',
    idempotencyKey: 'lyrica:play:trk_static_wire:2026-08-15T11Z:payout',
    entries: [
      entry('je_015', 'txn_3e19cd', acct('2000'), 'DEBIT', '96.0000', 'Payout attempted — blocked pre-post', '2026-08-15T11:09:33Z'),
      entry('je_016', 'txn_3e19cd', acct('1000'), 'CREDIT', '96.0000', 'Cash hold released back to pool', '2026-08-15T11:09:33Z'),
    ],
    postedAt: '2026-08-15T11:09:33Z',
    createdAt: '2026-08-15T11:09:32Z',
    updatedAt: '2026-08-15T11:09:33Z',
  },
];

// ---------------------------------------------------------------------------
// Royalty obligation events (inbound, signed by tenant)
// ---------------------------------------------------------------------------

export const mockObligationEvents: RoyaltyObligationCreated[] = [
  {
    schema_version: '1.0',
    event_id: 'evt_7f3a9c2d1e4b',
    event_type: 'royalty.obligation.created',
    occurred_at: '2026-08-15T18:02:09Z',
    correlation_id: 'corr_2a91c4de88f1',
    idempotency_key: 'lyrica:remix:trk_neon_bloom:2026-08-15T18Z',
    tenant_id: TENANT_ID,
    track: {
      track_id: 'trk_neon_bloom',
      dna_tag: 'dna_8841aa02',
      soulprint_hash: 'sp_9e21f7c4a6b8d0',
      vics_proof: { proof_id: 'vics_f02c91', issued_at: '2026-07-02T00:00:00Z', chain_ref: 'chain_ref_44a1' },
    },
    creator: { creator_id: 'creator_marisol_v', identity_ref: 'idref_marisol_v' },
    splits: [
      { owner_id: 'creator_marisol_v', bps: 7000 },
      { owner_id: 'creator_dj_kilo', bps: 3000 },
    ],
    trigger: { kind: 'remix', source_ref: 'stream_session_9f21', actor_id: 'actor_platform_lyrica' },
    amount: { currency: 'USD', value: '128.4000' },
  },
  {
    schema_version: '1.0',
    event_id: 'evt_1c8d4e7a2f90',
    event_type: 'royalty.obligation.created',
    occurred_at: '2026-08-15T14:30:58Z',
    correlation_id: 'corr_8e10bf22a173',
    idempotency_key: 'lyrica:play:trk_southern_glow:2026-08-15T14Z',
    tenant_id: TENANT_ID,
    track: {
      track_id: 'trk_southern_glow',
      dna_tag: 'dna_1102be55',
      soulprint_hash: 'sp_3a71c8e0f429',
      vics_proof: { proof_id: 'vics_ba2201', issued_at: '2026-06-11T00:00:00Z', chain_ref: 'chain_ref_09b7' },
    },
    creator: { creator_id: 'creator_lil_soleil', identity_ref: 'idref_lil_soleil' },
    splits: [{ owner_id: 'creator_lil_soleil', bps: 10000 }],
    trigger: { kind: 'play', source_ref: 'stream_session_3d81', actor_id: 'actor_platform_lyrica' },
    amount: { currency: 'USD', value: '64.2000' },
  },
  {
    schema_version: '1.0',
    event_id: 'evt_9b2f6c1a4d33',
    event_type: 'royalty.obligation.created',
    occurred_at: '2026-08-15T21:46:55Z',
    correlation_id: 'corr_f4901ac823de',
    idempotency_key: 'lyrica:license:trk_dustbowl_revival:2026-08-15T21Z',
    tenant_id: TENANT_ID,
    track: {
      track_id: 'trk_dustbowl_revival',
      dna_tag: 'dna_66e0aa19',
      soulprint_hash: 'sp_c891f0e7b213',
      vics_proof: { proof_id: 'vics_77d940', issued_at: '2026-05-20T00:00:00Z', chain_ref: 'chain_ref_c2f8' },
    },
    creator: { creator_id: 'creator_huxley', identity_ref: 'idref_huxley' },
    splits: [
      { owner_id: 'creator_huxley', bps: 6000 },
      { owner_id: 'label_dustbowl_records', bps: 4000 },
    ],
    trigger: { kind: 'license', source_ref: 'license_req_ab12', actor_id: 'actor_licensing_desk' },
    amount: { currency: 'USD', value: '340.0000' },
  },
  {
    schema_version: '1.0',
    event_id: 'evt_c5a8b2e19f04',
    event_type: 'royalty.obligation.created',
    occurred_at: '2026-08-15T11:09:20Z',
    correlation_id: 'corr_5b381eac9f02',
    idempotency_key: 'lyrica:play:trk_static_wire:2026-08-15T11Z',
    tenant_id: TENANT_ID,
    track: {
      track_id: 'trk_static_wire',
      dna_tag: 'dna_ff019cc2',
      soulprint_hash: 'sp_00a4e8c1d7f6',
      vics_proof: { proof_id: 'vics_9c1027', issued_at: '2026-08-01T00:00:00Z', chain_ref: 'chain_ref_71ab' },
    },
    creator: { creator_id: 'creator_static_wire', identity_ref: 'idref_static_wire' },
    splits: [{ owner_id: 'creator_static_wire', bps: 10000 }],
    trigger: { kind: 'play', source_ref: 'stream_session_ffa2', actor_id: 'actor_platform_lyrica' },
    amount: { currency: 'USD', value: '96.0000' },
  },
];

// ---------------------------------------------------------------------------
// Signed receipts — UnifiedReceipt, one per obligation event above
// ---------------------------------------------------------------------------

export const mockReceipts: UnifiedReceipt[] = [
  {
    schema_version: '1.0',
    receipt_id: 'rcp_a71f0c9d2e4b5f608a1c',
    status: 'paid',
    status_reasons: [],
    event_id: 'evt_7f3a9c2d1e4b',
    correlation_id: 'corr_2a91c4de88f1',
    tenant_id: TENANT_ID,
    transaction_id: 'txn_8a41f2',
    ledger_transaction_id: 'txn_c910de',
    amounts: { currency: 'USD', gross: '128.4000', platform_fee: '12.8400', net: '115.5600' },
    payouts: [
      { owner_id: 'creator_marisol_v', amount: '80.8920', state: 'posted' },
      { owner_id: 'creator_dj_kilo', amount: '34.6680', state: 'posted' },
    ],
    decision: { policy: 'allow', risk_score: 0.06, checks: ['ownership_verified', 'dna_match', 'vics_valid'] },
    issued_at: '2026-08-15T18:04:47Z',
    signature: { alg: 'ed25519', key_id: 'gw_key_2026_08', value: 'x9K2m4P8qR1sT7vW3yZ6bC0dE5fG8hJ1kL4nO7pQ==' },
  },
  {
    schema_version: '1.0',
    receipt_id: 'rcp_5e2b8a1c4d90f3372b6e',
    status: 'paid',
    status_reasons: [],
    event_id: 'evt_1c8d4e7a2f90',
    correlation_id: 'corr_8e10bf22a173',
    tenant_id: TENANT_ID,
    transaction_id: 'txn_51ac9f',
    ledger_transaction_id: 'txn_51ac9f',
    amounts: { currency: 'USD', gross: '64.2000', platform_fee: '6.4200', net: '57.7800' },
    payouts: [{ owner_id: 'creator_lil_soleil', amount: '57.7800', state: 'posted' }],
    decision: { policy: 'allow', risk_score: 0.11, checks: ['ownership_verified', 'dna_match', 'vics_valid'] },
    issued_at: '2026-08-15T14:31:40Z',
    signature: { alg: 'ed25519', key_id: 'gw_key_2026_08', value: 'a4D7fH1jK3mN6pQ9sU2wY5bE8gJ0lO3rT6vX9zC1AA==' },
  },
  {
    schema_version: '1.0',
    receipt_id: 'rcp_c19d4f7a0b2e8c5163fa',
    status: 'held',
    status_reasons: ['manual_review_required', 'high_value_license'],
    event_id: 'evt_9b2f6c1a4d33',
    correlation_id: 'corr_f4901ac823de',
    tenant_id: TENANT_ID,
    transaction_id: 'txn_f30a11',
    ledger_transaction_id: null,
    amounts: { currency: 'USD', gross: '340.0000', platform_fee: '34.0000', net: '306.0000' },
    payouts: [
      { owner_id: 'creator_huxley', amount: '183.6000', state: 'held' },
      { owner_id: 'label_dustbowl_records', amount: '122.4000', state: 'held' },
    ],
    decision: { policy: 'fraud_hold_payout', risk_score: 0.52, checks: ['ownership_verified', 'dna_match', 'vics_valid'] },
    issued_at: '2026-08-15T21:47:33Z',
    signature: { alg: 'ed25519', key_id: 'gw_key_2026_08', value: 'p2R5tV8wZ1cF4hK7mP0sU3xA6dG9jL2nQ5tW8yB0BB==' },
  },
  {
    schema_version: '1.0',
    receipt_id: 'rcp_00e8b1a4d7f2c9536b1d',
    status: 'blocked',
    status_reasons: ['device_fingerprint_mismatch', 'velocity_threshold_exceeded'],
    event_id: 'evt_c5a8b2e19f04',
    correlation_id: 'corr_5b381eac9f02',
    tenant_id: TENANT_ID,
    transaction_id: 'txn_3e19cd',
    ledger_transaction_id: null,
    amounts: { currency: 'USD', gross: '96.0000', platform_fee: '9.6000', net: '86.4000' },
    payouts: [{ owner_id: 'creator_static_wire', amount: '86.4000', state: 'blocked' }],
    decision: { policy: 'fraud_block_payout', risk_score: 0.91, checks: ['ownership_verified', 'dna_match', 'vics_valid'] },
    issued_at: '2026-08-15T11:09:52Z',
    signature: { alg: 'ed25519', key_id: 'gw_key_2026_08', value: 'q6W9zB2eH5kM8pS1uY4aD7gJ0lN3rT6xC9fI2oR5DD==' },
  },
];

// ---------------------------------------------------------------------------
// Outbox — Lyrica-side transactional outbox rows against the gateway
// ---------------------------------------------------------------------------

export const mockOutboxRows: OutboxRow[] = [
  {
    event_id: 'evt_7f3a9c2d1e4b',
    idempotency_key: 'lyrica:remix:trk_neon_bloom:2026-08-15T18Z',
    correlation_id: 'corr_2a91c4de88f1',
    tenant_id: TENANT_ID,
    key_id: 'lyrica_key_01',
    state: 'receipted',
    attempts: 1,
    last_error: null,
    next_attempt_at: '2026-08-15T18:02:09Z',
    created_at: '2026-08-15T18:02:08Z',
    receipt: mockReceipts[0],
  },
  {
    event_id: 'evt_1c8d4e7a2f90',
    idempotency_key: 'lyrica:play:trk_southern_glow:2026-08-15T14Z',
    correlation_id: 'corr_8e10bf22a173',
    tenant_id: TENANT_ID,
    key_id: 'lyrica_key_01',
    state: 'receipted',
    attempts: 1,
    last_error: null,
    next_attempt_at: '2026-08-15T14:30:58Z',
    created_at: '2026-08-15T14:30:57Z',
    receipt: mockReceipts[1],
  },
  {
    event_id: 'evt_9b2f6c1a4d33',
    idempotency_key: 'lyrica:license:trk_dustbowl_revival:2026-08-15T21Z',
    correlation_id: 'corr_f4901ac823de',
    tenant_id: TENANT_ID,
    key_id: 'lyrica_key_01',
    state: 'receipted',
    attempts: 1,
    last_error: null,
    next_attempt_at: '2026-08-15T21:46:55Z',
    created_at: '2026-08-15T21:46:54Z',
    receipt: mockReceipts[2],
  },
  {
    event_id: 'evt_c5a8b2e19f04',
    idempotency_key: 'lyrica:play:trk_static_wire:2026-08-15T11Z',
    correlation_id: 'corr_5b381eac9f02',
    tenant_id: TENANT_ID,
    key_id: 'lyrica_key_01',
    state: 'receipted',
    attempts: 1,
    last_error: null,
    next_attempt_at: '2026-08-15T11:09:20Z',
    created_at: '2026-08-15T11:09:19Z',
    receipt: mockReceipts[3],
  },
  {
    // Retryable: gateway returned 409 processing — original claim on this
    // idempotency_key is still in flight. Outbox retries the SAME row.
    event_id: 'evt_e0a4c7f1b9d2',
    idempotency_key: 'lyrica:play:trk_amber_wake:2026-08-15T22Z',
    correlation_id: 'corr_a017cf4e92bb',
    tenant_id: TENANT_ID,
    key_id: 'lyrica_key_01',
    state: 'sent',
    attempts: 2,
    last_error: '409 processing: retry original claim',
    next_attempt_at: '2026-08-15T22:31:04Z',
    created_at: '2026-08-15T22:30:41Z',
    receipt: null,
  },
  {
    // Currently leased by a worker, mid-attempt.
    event_id: 'evt_3f6a0d2c8b17',
    idempotency_key: 'lyrica:remix:trk_glass_horizon:2026-08-15T22Z',
    correlation_id: 'corr_6c209ab4e7f1',
    tenant_id: TENANT_ID,
    key_id: 'lyrica_key_01',
    state: 'processing',
    attempts: 1,
    last_error: null,
    next_attempt_at: '2026-08-15T22:33:10Z',
    created_at: '2026-08-15T22:33:08Z',
    receipt: null,
  },
  {
    // Terminal: same idempotency_key replayed with a different payload —
    // NOT retried by the outbox (only 409 "processing" is retryable).
    event_id: 'evt_9d1b4a7f2e05',
    idempotency_key: 'lyrica:play:trk_southern_glow:2026-08-15T14Z',
    correlation_id: 'corr_b8f430e19ac2',
    tenant_id: TENANT_ID,
    key_id: 'lyrica_key_01',
    state: 'rejected',
    attempts: 1,
    last_error: '409 idempotency_conflict: same idempotency_key, different payload',
    next_attempt_at: '2026-08-15T14:31:05Z',
    created_at: '2026-08-15T14:31:04Z',
    receipt: null,
  },
  {
    // Freshly persisted, not yet attempted.
    event_id: 'evt_b60d3e9a1c4f',
    idempotency_key: 'lyrica:play:trk_low_tide:2026-08-15T22Z',
    correlation_id: 'corr_0d92fe4b81ac',
    tenant_id: TENANT_ID,
    key_id: 'lyrica_key_01',
    state: 'pending',
    attempts: 0,
    last_error: null,
    next_attempt_at: '2026-08-15T22:34:00Z',
    created_at: '2026-08-15T22:34:00Z',
    receipt: null,
  },
];

// ---------------------------------------------------------------------------
// Risk assessments
// ---------------------------------------------------------------------------

export const mockRiskAssessments: RiskAssessment[] = [
  {
    transaction_id: 'txn_8a41f2',
    tenant_id: TENANT_ID,
    fraud_decision: 'allow_payout',
    risk_score: 6,
    reasons: [],
    policy: 'allow',
    evaluated_at: '2026-08-15T18:02:31Z',
  },
  {
    transaction_id: 'txn_51ac9f',
    tenant_id: TENANT_ID,
    fraud_decision: 'allow_payout',
    risk_score: 11,
    reasons: [],
    policy: 'allow',
    evaluated_at: '2026-08-15T14:31:12Z',
  },
  {
    transaction_id: 'txn_f30a11',
    tenant_id: TENANT_ID,
    fraud_decision: 'hold_payout',
    risk_score: 52,
    reasons: ['high_value_license', 'first_license_from_licensor'],
    policy: 'fraud_hold_payout',
    evaluated_at: '2026-08-15T21:47:10Z',
  },
  {
    transaction_id: 'txn_3e19cd',
    tenant_id: TENANT_ID,
    fraud_decision: 'block_payout',
    risk_score: 91,
    reasons: ['device_fingerprint_mismatch', 'velocity_threshold_exceeded', 'geo_mismatch_payout_account'],
    policy: 'fraud_block_payout',
    evaluated_at: '2026-08-15T11:09:41Z',
  },
  {
    transaction_id: 'txn_7dd420',
    tenant_id: TENANT_ID,
    fraud_decision: 'hold_payout',
    risk_score: 47,
    reasons: ['prior_chargeback_on_track', 'disputed_license_terms'],
    policy: 'fraud_hold_payout',
    evaluated_at: '2026-08-13T10:15:20Z',
  },
];

export const MOCK_TENANT_ID = TENANT_ID;
export const MOCK_ORG_ID = ORG_ID;
