import { Pool, PoolClient } from 'pg';
import { Decimal } from 'decimal.js';
import { v4 as uuidv4 } from 'uuid';
import pino from 'pino';
import crypto from 'crypto';

import {
  Account,
  JournalEntry,
  Transaction,
  TransactionType,
  TransactionStatus,
  DebitCredit,
  AccountType,
  PostTransactionRequest,
  TrialBalance,
  ReconciliationResult,
  Discrepancy,
  AuditAction,
} from './ledger-service-types';

const logger = pino();

/**
 * LedgerService: Core double-entry bookkeeping engine
 * 
 * Enforces:
 * 1. Every transaction balances to zero (total debits = total credits)
 * 2. Ledger entries are immutable (insert-only, no updates)
 * 3. Account balances are always in sync (denormalized, updated by trigger)
 * 4. Idempotency: same request returns same result
 * 5. Transaction isolation: concurrent posts don't corrupt state
 */
export class LedgerService {
  private pool: Pool;

  constructor(pool: Pool) {
    this.pool = pool;
  }

  /**
   * Create an account in the chart of accounts.
   * Accounts are the foundation of double-entry bookkeeping.
   */
  async createAccount(
    organizationId: string,
    code: string,
    name: string,
    type: AccountType,
    currency: string = 'USD',
    metadata?: Record<string, unknown>
  ): Promise<Account> {
    const client = await this.pool.connect();
    try {
      const id = uuidv4();

      const result = await client.query(
        `INSERT INTO accounts (id, organization_id, code, name, type, currency, metadata)
         VALUES ($1, $2, $3, $4, $5, $6, $7)
         RETURNING id, organization_id, code, name, type, balance, currency, is_active, metadata, created_at, updated_at`,
        [id, organizationId, code, name, type, currency, JSON.stringify(metadata || {})]
      );

      const account = this.rowToAccount(result.rows[0]);

      // Audit log
      await this.auditLog(
        client,
        organizationId,
        AuditAction.CREATE,
        'ACCOUNT',
        account.id,
        null,
        account
      );

      logger.info({ accountId: account.id, code }, 'Account created');
      return account;
    } finally {
      client.release();
    }
  }

  async listAccounts(organizationId: string): Promise<Account[]> {
    const result = await this.pool.query(
      `SELECT id, organization_id, code, name, type, balance, currency, is_active, metadata, created_at, updated_at
       FROM accounts
       WHERE organization_id = $1
       ORDER BY code`,
      [organizationId]
    );

    return result.rows.map((row) => this.rowToAccount(row));
  }

  /**
   * Post a transaction to the ledger.
   * 
   * This is the critical operation: it validates the transaction is balanced,
   * inserts all journal entries atomically, and updates account balances.
   * 
   * Returns the posted transaction with all its entries, or throws if validation fails.
   */
  async postTransaction(req: PostTransactionRequest): Promise<Transaction> {
    const client = await this.pool.connect();
    try {
      await client.query('BEGIN');

      // 1. Idempotency check: if same key already posted, return stored result
      if (req.idempotencyKey) {
        const idempotentResult = await this.checkIdempotency(client, req.idempotencyKey);
        if (idempotentResult) {
          logger.info(
            { idempotencyKey: req.idempotencyKey },
            'Transaction already posted, returning cached result'
          );
          return idempotentResult;
        }
      }

      // 2. Validate the transaction balances (all debits = all credits)
      if (!this.validateTransactionBalance(req.entries)) {
        throw new Error(
          `Transaction does not balance. Debits must equal credits.`
        );
      }

      // 3. Create the transaction record
      const transactionId = uuidv4();
      const now = new Date();

      await client.query(
        `INSERT INTO transactions (id, organization_id, type, reference_id, description, amount, currency, status, idempotency_key, metadata, posted_at)
         VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11)`,
        [
          transactionId,
          req.organizationId,
          req.type,
          req.referenceId || null,
          req.description,
          req.amount.toString(),
          req.currency,
          TransactionStatus.POSTED,
          req.idempotencyKey || null,
          JSON.stringify(req.metadata || {}),
          now,
        ]
      );

      // 4. Create journal entries (immutable ledger lines)
      const entries: JournalEntry[] = [];
      for (const entryReq of req.entries) {
        const entryId = uuidv4();

        // Verify account exists and belongs to the organization
        const accountCheck = await client.query(
          `SELECT id FROM accounts WHERE id = $1 AND organization_id = $2`,
          [entryReq.accountId, req.organizationId]
        );

        if (accountCheck.rows.length === 0) {
          throw new Error(`Account ${entryReq.accountId} not found or does not belong to this organization`);
        }

        const entryResult = await client.query(
          `INSERT INTO journal_entries (id, transaction_id, organization_id, account_id, debit_credit, amount, description, metadata)
           VALUES ($1, $2, $3, $4, $5, $6, $7, $8)
           RETURNING id, transaction_id, organization_id, account_id, debit_credit, amount, description, metadata, created_at`,
          [
            entryId,
            transactionId,
            req.organizationId,
            entryReq.accountId,
            entryReq.debitCredit,
            entryReq.amount.toString(),
            entryReq.description,
            JSON.stringify(entryReq.metadata || {}),
          ]
        );

        entries.push(this.rowToJournalEntry(entryResult.rows[0]));
      }

      // 5. Verify the transaction is still balanced (sanity check)
      const isBalanced = await this.verifyTransactionBalance(client, transactionId);
      if (!isBalanced) {
        throw new Error('Transaction validation failed after insert (database state corruption detected)');
      }

      // 6. Store idempotency response
      if (req.idempotencyKey) {
        const transaction: Transaction = {
          id: transactionId,
          organizationId: req.organizationId,
          type: req.type,
          referenceId: req.referenceId,
          description: req.description,
          amount: req.amount,
          currency: req.currency,
          status: TransactionStatus.POSTED,
          entries,
          metadata: req.metadata,
          postedAt: now,
          createdAt: now,
          updatedAt: now,
        };

        await this.storeIdempotencyResponse(client, req.idempotencyKey, req, transaction);
      }

      // 7. Audit log
      await this.auditLog(
        client,
        req.organizationId,
        AuditAction.POST,
        'TRANSACTION',
        transactionId,
        null,
        { transactionId, entryCount: entries.length }
      );

      await client.query('COMMIT');

      logger.info(
        { transactionId, type: req.type, amount: req.amount.toString(), entryCount: entries.length },
        'Transaction posted successfully'
      );

      return {
        id: transactionId,
        organizationId: req.organizationId,
        type: req.type,
        referenceId: req.referenceId,
        description: req.description,
        amount: req.amount,
        currency: req.currency,
        status: TransactionStatus.POSTED,
        entries,
        metadata: req.metadata,
        postedAt: now,
        createdAt: now,
        updatedAt: now,
      };
    } catch (error) {
      await client.query('ROLLBACK');
      logger.error({ error, req }, 'Failed to post transaction');
      throw error;
    } finally {
      client.release();
    }
  }

  /**
   * Reverse a transaction by posting its opposite entries.
   * Used for refunds, chargebacks, and corrections.
   * 
   * The original transaction remains immutable; a new REVERSAL transaction is posted.
   */
  async reverseTransaction(
    organizationId: string,
    transactionId: string,
    reason: string
  ): Promise<Transaction> {
    const client = await this.pool.connect();
    try {
      await client.query('BEGIN');

      // Fetch the original transaction
      const originalResult = await client.query(
        `SELECT id, type, description, amount, currency
         FROM transactions
         WHERE id = $1 AND organization_id = $2`,
        [transactionId, organizationId]
      );

      if (originalResult.rows.length === 0) {
        throw new Error(`Transaction ${transactionId} not found`);
      }

      const original = originalResult.rows[0];

      // Fetch all entries from the original transaction
      const entriesResult = await client.query(
        `SELECT account_id, debit_credit, amount, description
         FROM journal_entries
         WHERE transaction_id = $1`,
        [transactionId]
      );

      if (entriesResult.rows.length === 0) {
        throw new Error(`Transaction ${transactionId} has no entries (corrupted state)`);
      }

      // Create reversal entries (swap debit/credit for each entry)
      const reversalEntries = entriesResult.rows.map((row) => ({
        accountId: row.account_id,
        debitCredit: (row.debit_credit === 'DEBIT' ? 'CREDIT' : 'DEBIT') as DebitCredit,
        amount: new Decimal(row.amount),
        description: `Reversal: ${row.description}`,
      }));

      // Post the reversal transaction
      const reversalTxn = await this.postTransaction({
        organizationId,
        type: TransactionType.REVERSAL,
        referenceId: transactionId,
        description: `Reversal of ${original.type}: ${reason}`,
        amount: new Decimal(original.amount),
        currency: original.currency,
        entries: reversalEntries,
        metadata: { originalTransactionId: transactionId },
      });

      // Update original transaction status
      await client.query(
        `UPDATE transactions SET status = $1, updated_at = CURRENT_TIMESTAMP WHERE id = $2`,
        [TransactionStatus.REVERSED, transactionId]
      );

      await client.query('COMMIT');
      return reversalTxn;
    } catch (error) {
      await client.query('ROLLBACK');
      logger.error({ error, transactionId }, 'Failed to reverse transaction');
      throw error;
    } finally {
      client.release();
    }
  }

  async getTransaction(organizationId: string, transactionId: string): Promise<Transaction> {
    const txnResult = await this.pool.query(
      `SELECT id, organization_id, type, reference_id, description, amount, currency, status, metadata, posted_at, created_at, updated_at
       FROM transactions
       WHERE id = $1 AND organization_id = $2`,
      [transactionId, organizationId]
    );

    if (txnResult.rows.length === 0) {
      throw new Error(`Transaction ${transactionId} not found`);
    }

    const entryResult = await this.pool.query(
      `SELECT id, transaction_id, organization_id, account_id, debit_credit, amount, description, metadata, created_at
       FROM journal_entries
       WHERE transaction_id = $1
       ORDER BY created_at ASC, id ASC`,
      [transactionId]
    );

    const row = txnResult.rows[0];
    return {
      id: row.id,
      organizationId: row.organization_id,
      type: row.type,
      referenceId: row.reference_id,
      description: row.description,
      amount: new Decimal(row.amount),
      currency: row.currency,
      status: row.status,
      entries: entryResult.rows.map((entry) => this.rowToJournalEntry(entry)),
      metadata: row.metadata,
      postedAt: row.posted_at,
      createdAt: row.created_at,
      updatedAt: row.updated_at,
    };
  }

  /**
   * Get trial balance: sum of all debits and credits per account.
   * Trial balance should always sum to zero if the ledger is correct.
   */
  async getTrialBalance(organizationId: string): Promise<TrialBalance[]> {
    const result = await this.pool.query(
      `SELECT account_id, account_code, account_name, debit_sum, credit_sum, balance
       FROM trial_balance
       WHERE organization_id = $1
       ORDER BY account_code`,
      [organizationId]
    );

    return result.rows.map((row) => ({
      accountId: row.account_id,
      accountCode: row.account_code,
      accountName: row.account_name,
      debitSum: new Decimal(row.debit_sum),
      creditSum: new Decimal(row.credit_sum),
      balance: new Decimal(row.balance),
      asOf: new Date(),
    }));
  }

  /**
   * Reconciliation: verify the ledger is balanced.
   * Returns discrepancies (if any) and overall balance status.
   */
  async reconcile(organizationId: string, asOfDate?: Date): Promise<ReconciliationResult> {
    const trialBalance = await this.getTrialBalance(organizationId);

    // Check if trial balance sums to zero
    const totalBalance = trialBalance.reduce((sum, tb) => sum.plus(tb.balance), new Decimal(0));
    const isBalanced = totalBalance.equals(0);

    // Find transactions with unbalanced entries
    const discrepancies: Discrepancy[] = [];

    const result = await this.pool.query(
      `SELECT
         t.id,
         t.description,
         SUM(CASE WHEN je.debit_credit = 'DEBIT' THEN je.amount ELSE -je.amount END) as actual_balance,
         0 as expected_balance
       FROM transactions t
       LEFT JOIN journal_entries je ON t.id = je.transaction_id
       WHERE t.organization_id = $1 AND t.status = 'POSTED'
       GROUP BY t.id, t.description
       HAVING SUM(CASE WHEN je.debit_credit = 'DEBIT' THEN je.amount ELSE -je.amount END) <> 0`,
      [organizationId]
    );

    for (const row of result.rows) {
      discrepancies.push({
        transactionId: row.id,
        description: row.description,
        expectedBalance: new Decimal(0),
        actualBalance: new Decimal(row.actual_balance),
        difference: new Decimal(row.actual_balance),
      });
    }

    // Count total and balanced transactions
    const txnResult = await this.pool.query(
      `SELECT COUNT(*) as total, 
              SUM(CASE WHEN status = 'POSTED' THEN 1 ELSE 0 END) as posted
       FROM transactions
       WHERE organization_id = $1`,
      [organizationId]
    );

    const totalTransactions = parseInt(txnResult.rows[0].total, 10);
    const postedTransactions = parseInt(txnResult.rows[0].posted, 10);

    return {
      asOf: asOfDate || new Date(),
      totalTransactions,
      balancedTransactions: postedTransactions - discrepancies.length,
      unbalancedTransactions: [],
      trialBalance,
      isBalanced,
      discrepancies,
    };
  }

  /**
   * Private helpers
   */

  private validateTransactionBalance(entries: any[]): boolean {
    let debitSum = new Decimal(0);
    let creditSum = new Decimal(0);

    for (const entry of entries) {
      const amount = new Decimal(entry.amount);
      if (entry.debitCredit === DebitCredit.DEBIT) {
        debitSum = debitSum.plus(amount);
      } else {
        creditSum = creditSum.plus(amount);
      }
    }

    return debitSum.equals(creditSum) && debitSum.greaterThan(0);
  }

  private async verifyTransactionBalance(client: PoolClient, transactionId: string): Promise<boolean> {
    const result = await client.query(
      `SELECT check_transaction_balanced($1)`,
      [transactionId]
    );
    return result.rows[0].check_transaction_balanced;
  }

  private async checkIdempotency(
    client: PoolClient,
    idempotencyKey: string
  ): Promise<Transaction | null> {
    const result = await client.query(
      `SELECT response FROM idempotency_store WHERE idempotency_key = $1 AND expires_at > NOW()`,
      [idempotencyKey]
    );

    if (result.rows.length === 0) {
      return null;
    }

    return result.rows[0].response;
  }

  private async storeIdempotencyResponse(
    client: PoolClient,
    idempotencyKey: string,
    request: PostTransactionRequest,
    response: Transaction
  ): Promise<void> {
    const requestHash = crypto.createHash('sha256').update(JSON.stringify(request)).digest('hex');
    const expiresAt = new Date(Date.now() + 24 * 60 * 60 * 1000); // 24 hours

    await client.query(
      `INSERT INTO idempotency_store (idempotency_key, organization_id, request_hash, response, expires_at)
       VALUES ($1, $2, $3, $4, $5)
       ON CONFLICT (idempotency_key) DO UPDATE
       SET response = $4, expires_at = $5`,
      [idempotencyKey, request.organizationId, requestHash, JSON.stringify(response), expiresAt]
    );
  }

  private async auditLog(
    client: PoolClient,
    organizationId: string,
    action: AuditAction,
    entityType: 'ACCOUNT' | 'TRANSACTION' | 'ENTRY',
    entityId: string,
    previousState: any,
    newState: any
  ): Promise<void> {
    await client.query(
      `INSERT INTO audit_logs (organization_id, action, entity_type, entity_id, previous_state, new_state)
       VALUES ($1, $2, $3, $4, $5, $6)`,
      [organizationId, action, entityType, entityId, previousState, JSON.stringify(newState)]
    );
  }

  private rowToAccount(row: any): Account {
    return {
      id: row.id,
      organizationId: row.organization_id,
      code: row.code,
      name: row.name,
      type: row.type,
      balance: new Decimal(row.balance),
      currency: row.currency,
      isActive: row.is_active,
      metadata: row.metadata,
      createdAt: row.created_at,
      updatedAt: row.updated_at,
    };
  }

  private rowToJournalEntry(row: any): JournalEntry {
    return {
      id: row.id,
      transactionId: row.transaction_id,
      organizationId: row.organization_id,
      accountId: row.account_id,
      debitCredit: row.debit_credit,
      amount: new Decimal(row.amount),
      description: row.description,
      metadata: row.metadata,
      createdAt: row.created_at,
    };
  }
}
