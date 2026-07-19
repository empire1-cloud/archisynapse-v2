import { Decimal } from 'decimal.js';

/**
 * Core ledger types ensuring financial correctness
 */

export enum AccountType {
  ASSET = 'ASSET',        // Cash, bank accounts
  LIABILITY = 'LIABILITY', // Credit, owed amounts
  EQUITY = 'EQUITY',      // Owner's stake
  REVENUE = 'REVENUE',    // Income
  EXPENSE = 'EXPENSE'     // Costs
}

export enum DebitCredit {
  DEBIT = 'DEBIT',
  CREDIT = 'CREDIT'
}

/**
 * Account: Represents a ledger account.
 * Every transaction affects at least two accounts (double-entry principle).
 */
export interface Account {
  id: string;
  organizationId: string;
  code: string;                    // e.g., "1000", "2000" - unique per org
  name: string;
  type: AccountType;
  balance: Decimal;                // Always up-to-date; denormalized for query speed
  currency: string;                // ISO 4217 (USD, EUR, etc)
  isActive: boolean;
  metadata?: Record<string, unknown>;
  createdAt: Date;
  updatedAt: Date;
}

/**
 * JournalEntry: An immutable ledger line item.
 * Every entry is debit or credit to a specific account.
 * Entries are never modified; corrections are made via reversing entries.
 */
export interface JournalEntry {
  id: string;
  transactionId: string;           // Links back to the transaction that caused this
  organizationId: string;
  accountId: string;
  debitCredit: DebitCredit;
  amount: Decimal;                 // Amount in the account's currency
  description: string;
  metadata?: Record<string, unknown>;
  createdAt: Date;                 // Immutable
}

/**
 * Transaction: A logical business event (payment, payout, fee, etc).
 * A transaction groups multiple journal entries and enforces they balance to zero.
 */
export interface Transaction {
  id: string;
  organizationId: string;
  type: TransactionType;
  referenceId?: string;             // Links to external transaction ID (e.g., payment.id)
  description: string;
  amount: Decimal;
  currency: string;
  status: TransactionStatus;
  entries: JournalEntry[];
  idempotencyKey?: string;           // Prevents duplicate postings
  metadata?: Record<string, unknown>;
  postedAt: Date;                    // When the transaction was committed to ledger
  createdAt: Date;
  updatedAt: Date;
}

export enum TransactionType {
  PAYMENT = 'PAYMENT',             // Customer payment in
  PAYOUT = 'PAYOUT',               // Merchant payout out
  REFUND = 'REFUND',               // Payment reversal
  CHARGEBACK = 'CHARGEBACK',       // Disputed payment
  FEE = 'FEE',                     // Platform/processing fee
  REVERSAL = 'REVERSAL',           // Reverse incorrect entry
  ADJUSTMENT = 'ADJUSTMENT'        // Manual correction
}

export enum TransactionStatus {
  PENDING = 'PENDING',
  POSTED = 'POSTED',
  FAILED = 'FAILED',
  REVERSED = 'REVERSED'
}

/**
 * TrialBalance: Sum of all debits and credits per account.
 * Should always sum to zero for a balanced ledger.
 */
export interface TrialBalance {
  accountId: string;
  accountCode: string;
  accountName: string;
  debitSum: Decimal;
  creditSum: Decimal;
  balance: Decimal;
  asOf: Date;
}

/**
 * Post request to create and immediately post a transaction.
 * Returns the transaction with all journal entries if successful.
 */
export interface PostTransactionRequest {
  organizationId: string;
  type: TransactionType;
  referenceId?: string;
  description: string;
  amount: Decimal;
  currency: string;
  entries: PostJournalEntryRequest[];
  idempotencyKey?: string;
  metadata?: Record<string, unknown>;
}

export interface PostJournalEntryRequest {
  accountId: string;
  debitCredit: DebitCredit;
  amount: Decimal;
  description: string;
  metadata?: Record<string, unknown>;
}

/**
 * Reconciliation result: identifies unmatched transactions.
 */
export interface ReconciliationResult {
  asOf: Date;
  totalTransactions: number;
  balancedTransactions: number;
  unbalancedTransactions: Transaction[];
  trialBalance: TrialBalance[];
  isBalanced: boolean;
  discrepancies: Discrepancy[];
}

export interface Discrepancy {
  transactionId: string;
  description: string;
  expectedBalance: Decimal;
  actualBalance: Decimal;
  difference: Decimal;
}

/**
 * Financial statement: standard accounting report.
 */
export interface FinancialStatement {
  organizationId: string;
  period: {
    startDate: Date;
    endDate: Date;
  };
  incomeStatement: IncomeStatement;
  balanceSheet: BalanceSheet;
  generatedAt: Date;
}

export interface IncomeStatement {
  revenue: Decimal;
  expenses: Decimal;
  netIncome: Decimal;
}

export interface BalanceSheet {
  assets: Decimal;
  liabilities: Decimal;
  equity: Decimal;
}

/**
 * Audit log: immutable record of all ledger mutations.
 */
export interface AuditLog {
  id: string;
  organizationId: string;
  action: AuditAction;
  entityType: 'ACCOUNT' | 'TRANSACTION' | 'ENTRY';
  entityId: string;
  previousState?: Record<string, unknown>;
  newState: Record<string, unknown>;
  actorId?: string;
  ipAddress?: string;
  createdAt: Date;
}

export enum AuditAction {
  CREATE = 'CREATE',
  POST = 'POST',
  REVERSE = 'REVERSE',
  UPDATE = 'UPDATE',
  DELETE = 'DELETE'
}
