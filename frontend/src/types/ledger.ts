/**
 * Ledger domain types.
 *
 * Mirrors services/ledger/ledger-service-types.ts field-for-field so this
 * console can point at the real ledger-service API with zero shape drift.
 * Decimal amounts travel as strings over the wire (decimal.js on the
 * backend) — this app never does float math on money, only display
 * formatting.
 */

export type AccountType = 'ASSET' | 'LIABILITY' | 'EQUITY' | 'REVENUE' | 'EXPENSE';

export type DebitCredit = 'DEBIT' | 'CREDIT';

export interface Account {
  id: string;
  organizationId: string;
  code: string;
  name: string;
  type: AccountType;
  balance: string;
  currency: string;
  isActive: boolean;
  metadata?: Record<string, unknown>;
  createdAt: string;
  updatedAt: string;
}

export interface JournalEntry {
  id: string;
  transactionId: string;
  organizationId: string;
  accountId: string;
  debitCredit: DebitCredit;
  amount: string;
  description: string;
  metadata?: Record<string, unknown>;
  createdAt: string;
}

export type TransactionType =
  | 'PAYMENT'
  | 'PAYOUT'
  | 'REFUND'
  | 'CHARGEBACK'
  | 'FEE'
  | 'REVERSAL'
  | 'ADJUSTMENT';

export type TransactionStatus = 'PENDING' | 'POSTED' | 'FAILED' | 'REVERSED';

export interface Transaction {
  id: string;
  organizationId: string;
  type: TransactionType;
  referenceId?: string;
  description: string;
  amount: string;
  currency: string;
  status: TransactionStatus;
  entries: JournalEntry[];
  idempotencyKey?: string;
  metadata?: Record<string, unknown>;
  postedAt: string;
  createdAt: string;
  updatedAt: string;
}

export interface TrialBalanceRow {
  accountId: string;
  accountCode: string;
  accountName: string;
  debitSum: string;
  creditSum: string;
  balance: string;
  asOf: string;
}
