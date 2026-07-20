import { Decimal } from 'decimal.js';

export enum RoyaltyStatus {
  PENDING = 'PENDING',
  POSTED = 'POSTED',
  HELD = 'HELD',
  BLOCKED = 'BLOCKED',
  REVERSED = 'REVERSED',
}

export type RoyaltyDecision = 'allow' | 'hold' | 'block';

export interface RoyaltySplit {
  ownerId: string;
  bps: number;
}

export interface CreateRoyaltyObligationRequest {
  organizationId: string;
  eventId: string;
  correlationId: string;
  idempotencyKey: string;
  tenantId: string;
  trackId: string;
  creatorId: string;
  triggerKind: 'play' | 'remix' | 'license';
  amount: Decimal;
  currency: string;
  splits: RoyaltySplit[];
  decision: RoyaltyDecision;
  decisionPolicy: string;
  riskScore: number;
  statusReasons: string[];
  requestHash: string;
}

export interface RoyaltyPayout {
  ownerId: string;
  amount: Decimal;
  state: string;
}

export interface RoyaltyObligation {
  id: string;
  organizationId: string;
  eventId: string;
  correlationId: string;
  idempotencyKey: string;
  tenantId: string;
  status: RoyaltyStatus;
  amount: Decimal;
  currency: string;
  splits: RoyaltySplit[];
  decisionPolicy: string | null;
  riskScore: number | null;
  statusReasons: string[];
  ledgerTransactionId: string | null;
  payouts: RoyaltyPayout[];
  createdAt: Date;
  updatedAt: Date;
}

export class RoyaltyIdempotencyConflictError extends Error {}
export class RoyaltyObligationNotFoundError extends Error {}
export class RoyaltyInvalidStateError extends Error {}
