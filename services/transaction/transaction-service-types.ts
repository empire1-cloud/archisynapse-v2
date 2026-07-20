import { Decimal } from 'decimal.js';

/**
 * Transaction Service Types
 * Handles the customer-facing payment lifecycle: authorize -> capture -> settle
 * Delegates all financial bookkeeping to the Ledger Service.
 */

export enum PaymentStatus {
  PENDING = 'PENDING',
  AUTHORIZED = 'AUTHORIZED',
  SUCCEEDED = 'SUCCEEDED',
  FAILED = 'FAILED',
  REFUNDED = 'REFUNDED',
  PARTIALLY_REFUNDED = 'PARTIALLY_REFUNDED',
  DISPUTED = 'DISPUTED',
}

export enum PaymentMethodType {
  CARD = 'CARD',
  BANK_TRANSFER = 'BANK_TRANSFER',
  WALLET = 'WALLET',
}

export interface PaymentMethod {
  type: PaymentMethodType;
  // Tokenized reference only — never store raw card/account numbers
  token: string;
  last4?: string;
  brand?: string;
}

export interface Payment {
  id: string;
  organizationId: string;
  customerId?: string;
  amount: Decimal;
  currency: string;
  status: PaymentStatus;
  paymentMethod: PaymentMethod;
  description?: string;
  idempotencyKey: string;
  ledgerTransactionId?: string;    // Set once posted to the Ledger Service
  failureReason?: string;
  metadata?: Record<string, unknown>;
  createdAt: Date;
  updatedAt: Date;
}

export interface CreatePaymentRequest {
  organizationId: string;
  customerId?: string;
  amount: Decimal;
  feeAmount?: Decimal;
  currency: string;
  paymentMethod: PaymentMethod;
  description?: string;
  idempotencyKey: string;
  metadata?: Record<string, unknown>;
}

export interface RefundRequest {
  paymentId: string;
  amount?: Decimal;  // Partial refund if specified; full refund otherwise
  reason: string;
  idempotencyKey: string;
}

export interface Refund {
  id: string;
  paymentId: string;
  organizationId: string;
  amount: Decimal;
  reason: string;
  status: 'SUCCEEDED' | 'FAILED';
  ledgerTransactionId?: string;
  createdAt: Date;
}

/**
 * Result of calling out to a payment processor (Stripe-like gateway).
 * This is an abstraction — swap in a real processor adapter later.
 */
export interface ProcessorResult {
  success: boolean;
  processorTransactionId?: string;
  failureCode?: string;
  failureMessage?: string;
}

export class InsufficientFundsError extends Error {
  constructor(message = 'Insufficient funds') {
    super(message);
    this.name = 'InsufficientFundsError';
  }
}

export class DuplicatePaymentError extends Error {
  constructor(message = 'Payment with this idempotency key already exists') {
    super(message);
    this.name = 'DuplicatePaymentError';
  }
}

export class PaymentNotFoundError extends Error {
  constructor(message = 'Payment not found') {
    super(message);
    this.name = 'PaymentNotFoundError';
  }
}
