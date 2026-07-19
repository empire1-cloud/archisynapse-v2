import { Pool, PoolClient } from 'pg';
import { Decimal } from 'decimal.js';
import { v4 as uuidv4 } from 'uuid';
import { createLogger } from 'pino';

import { LedgerClient } from './transaction-service-ledger-client';
import {
  Payment,
  PaymentStatus,
  CreatePaymentRequest,
  RefundRequest,
  Refund,
  ProcessorResult,
  DuplicatePaymentError,
  PaymentNotFoundError,
  InsufficientFundsError,
} from './transaction-service-types';

const logger = createLogger();

/**
 * Chart of account IDs used for posting to the ledger.
 * In a real system these would be looked up per-organization, not hardcoded.
 * Set via environment/config per deployment.
 */
interface LedgerAccountConfig {
  cashAccountId: string;
  revenueAccountId: string;
}

/**
 * TransactionService: Owns the customer-facing payment lifecycle.
 *
 * Responsibilities:
 * - Validate and create payments
 * - Call out to a payment processor (card network, bank rail, etc.)
 * - On success, post the transaction to the Ledger Service (source of truth for money)
 * - Handle refunds by reversing the ledger entry
 *
 * Explicitly NOT responsible for:
 * - Bookkeeping / balance calculation (Ledger Service's job)
 * - Fraud scoring (Fraud Service's job — this service can consult it, not replace it)
 */
export class TransactionService {
  private pool: Pool;
  private ledgerClient: LedgerClient;
  private ledgerAccounts: LedgerAccountConfig;

  constructor(pool: Pool, ledgerClient: LedgerClient, ledgerAccounts: LedgerAccountConfig) {
    this.pool = pool;
    this.ledgerClient = ledgerClient;
    this.ledgerAccounts = ledgerAccounts;
  }

  /**
   * Create and process a payment.
   *
   * Flow:
   * 1. Idempotency check (return existing payment if key already used)
   * 2. Create payment record (status: PENDING)
   * 3. Call payment processor (stubbed here — plug in Stripe/Adyen/etc.)
   * 4. On processor success: mark SUCCEEDED, post to Ledger Service
   * 5. On processor failure: mark FAILED, store reason
   *
   * Note: this method does NOT roll back the payment row if the ledger post fails —
   * instead it leaves the payment in a recoverable "SUCCEEDED but not yet posted" state
   * (ledger_transaction_id is null) so a reconciliation job can retry the ledger post.
   * Money should never be "un-charged" just because our bookkeeping call failed.
   */
  async createPayment(req: CreatePaymentRequest): Promise<Payment> {
    const client = await this.pool.connect();
    try {
      // 1. Idempotency check
      const existing = await client.query(
        `SELECT * FROM payments WHERE idempotency_key = $1`,
        [req.idempotencyKey]
      );
      if (existing.rows.length > 0) {
        logger.info({ idempotencyKey: req.idempotencyKey }, 'Returning existing payment (idempotent)');
        return this.rowToPayment(existing.rows[0]);
      }

      // 2. Create payment record as PENDING
      const paymentId = uuidv4();
      await client.query(
        `INSERT INTO payments
         (id, organization_id, customer_id, amount, currency, status,
          payment_method_type, payment_method_token, payment_method_last4, payment_method_brand,
          description, idempotency_key, metadata)
         VALUES ($1,$2,$3,$4,$5,'PENDING',$6,$7,$8,$9,$10,$11,$12)`,
        [
          paymentId,
          req.organizationId,
          req.customerId || null,
          req.amount.toString(),
          req.currency,
          req.paymentMethod.type,
          req.paymentMethod.token,
          req.paymentMethod.last4 || null,
          req.paymentMethod.brand || null,
          req.description || null,
          req.idempotencyKey,
          JSON.stringify(req.metadata || {}),
        ]
      );

      // 3. Call payment processor
      const processorResult = await this.callProcessor(req);

      if (!processorResult.success) {
        await client.query(
          `UPDATE payments SET status = 'FAILED', failure_reason = $1 WHERE id = $2`,
          [processorResult.failureMessage || 'Processor declined', paymentId]
        );
        logger.warn({ paymentId, reason: processorResult.failureMessage }, 'Payment failed at processor');
        return this.getPayment(req.organizationId, paymentId);
      }

      // 4. Mark succeeded
      await client.query(
        `UPDATE payments SET status = 'SUCCEEDED', processor_transaction_id = $1 WHERE id = $2`,
        [processorResult.processorTransactionId || null, paymentId]
      );

      // 5. Post to ledger (best-effort; failure here doesn't undo the charge)
      try {
        const ledgerTxn = await this.ledgerClient.postPaymentSucceeded({
          organizationId: req.organizationId,
          paymentId,
          amount: req.amount,
          currency: req.currency,
          cashAccountId: this.ledgerAccounts.cashAccountId,
          revenueAccountId: this.ledgerAccounts.revenueAccountId,
          idempotencyKey: `payment-${paymentId}`,
        });

        await client.query(
          `UPDATE payments SET ledger_transaction_id = $1 WHERE id = $2`,
          [ledgerTxn.id, paymentId]
        );
      } catch (ledgerError) {
        // Critical: log loudly. A reconciliation job MUST pick this up and retry.
        logger.error(
          { paymentId, error: ledgerError },
          'CRITICAL: Payment succeeded but ledger post failed — needs reconciliation'
        );
      }

      return this.getPayment(req.organizationId, paymentId);
    } finally {
      client.release();
    }
  }

  /**
   * Refund a payment (full or partial).
   */
  async refundPayment(req: RefundRequest): Promise<Refund> {
    const client = await this.pool.connect();
    try {
      // Idempotency
      const existing = await client.query(
        `SELECT * FROM refunds WHERE idempotency_key = $1`,
        [req.idempotencyKey]
      );
      if (existing.rows.length > 0) {
        return this.rowToRefund(existing.rows[0]);
      }

      const paymentResult = await client.query(`SELECT * FROM payments WHERE id = $1`, [req.paymentId]);
      if (paymentResult.rows.length === 0) {
        throw new PaymentNotFoundError();
      }
      const payment = this.rowToPayment(paymentResult.rows[0]);

      if (payment.status !== PaymentStatus.SUCCEEDED && payment.status !== PaymentStatus.PARTIALLY_REFUNDED) {
        throw new Error(`Cannot refund payment with status ${payment.status}`);
      }

      const refundAmount = req.amount || payment.amount;

      // Ensure we're not refunding more than what remains
      const priorRefunds = await client.query(
        `SELECT COALESCE(SUM(amount), 0) as total FROM refunds WHERE payment_id = $1 AND status = 'SUCCEEDED'`,
        [req.paymentId]
      );
      const alreadyRefunded = new Decimal(priorRefunds.rows[0].total);
      if (alreadyRefunded.plus(refundAmount).greaterThan(payment.amount)) {
        throw new InsufficientFundsError('Refund amount exceeds remaining payment balance');
      }

      const refundId = uuidv4();

      if (!payment.ledgerTransactionId) {
        throw new Error('Cannot refund: payment has not been posted to the ledger yet. Retry after reconciliation.');
      }

      const ledgerReversal = await this.ledgerClient.postRefund({
        organizationId: req.paymentId ? payment.organizationId : req.paymentId,
        originalLedgerTransactionId: payment.ledgerTransactionId,
        reason: req.reason,
      });

      await client.query(
        `INSERT INTO refunds (id, payment_id, organization_id, amount, reason, status, idempotency_key, ledger_transaction_id)
         VALUES ($1,$2,$3,$4,$5,'SUCCEEDED',$6,$7)`,
        [refundId, req.paymentId, payment.organizationId, refundAmount.toString(), req.reason, req.idempotencyKey, ledgerReversal.id]
      );

      const isFullRefund = alreadyRefunded.plus(refundAmount).equals(payment.amount);
      await client.query(
        `UPDATE payments SET status = $1 WHERE id = $2`,
        [isFullRefund ? 'REFUNDED' : 'PARTIALLY_REFUNDED', req.paymentId]
      );

      return {
        id: refundId,
        paymentId: req.paymentId,
        organizationId: payment.organizationId,
        amount: refundAmount,
        reason: req.reason,
        status: 'SUCCEEDED',
        ledgerTransactionId: ledgerReversal.id,
        createdAt: new Date(),
      };
    } finally {
      client.release();
    }
  }

  async getPayment(organizationId: string, paymentId: string): Promise<Payment> {
    const result = await this.pool.query(
      `SELECT * FROM payments WHERE id = $1 AND organization_id = $2`,
      [paymentId, organizationId]
    );
    if (result.rows.length === 0) {
      throw new PaymentNotFoundError();
    }
    return this.rowToPayment(result.rows[0]);
  }

  async listPayments(
    organizationId: string,
    opts: { limit?: number; cursor?: string; status?: PaymentStatus } = {}
  ): Promise<{ payments: Payment[]; nextCursor: string | null }> {
    const limit = Math.min(opts.limit || 50, 100);
    const params: any[] = [organizationId];
    let query = `SELECT * FROM payments WHERE organization_id = $1`;

    if (opts.status) {
      params.push(opts.status);
      query += ` AND status = $${params.length}`;
    }
    if (opts.cursor) {
      params.push(opts.cursor);
      query += ` AND id < $${params.length}`;
    }

    params.push(limit + 1);
    query += ` ORDER BY created_at DESC, id DESC LIMIT $${params.length}`;

    const result = await this.pool.query(query, params);
    const hasMore = result.rows.length > limit;
    const rows = hasMore ? result.rows.slice(0, limit) : result.rows;

    return {
      payments: rows.map((r) => this.rowToPayment(r)),
      nextCursor: hasMore ? rows[rows.length - 1].id : null,
    };
  }

  /**
   * Reconciliation helper: find payments marked SUCCEEDED that never got
   * a ledger_transaction_id (i.e. the ledger post failed). Call this from
   * a background job every few minutes.
   */
  async findUnpostedPayments(organizationId: string): Promise<Payment[]> {
    const result = await this.pool.query(
      `SELECT * FROM payments
       WHERE organization_id = $1 AND status = 'SUCCEEDED' AND ledger_transaction_id IS NULL
       ORDER BY created_at ASC`,
      [organizationId]
    );
    return result.rows.map((r) => this.rowToPayment(r));
  }

  /**
   * Payment processor stub. Replace with a real adapter (Stripe, Adyen, etc.)
   * Kept deliberately simple and isolated so swapping providers doesn't
   * touch the rest of the service.
   */
  private async callProcessor(req: CreatePaymentRequest): Promise<ProcessorResult> {
    // TODO: integrate real processor. This stub always succeeds so the
    // rest of the pipeline (ledger posting, refunds) can be exercised end-to-end.
    return {
      success: true,
      processorTransactionId: `proc_${uuidv4()}`,
    };
  }

  private rowToPayment(row: any): Payment {
    return {
      id: row.id,
      organizationId: row.organization_id,
      customerId: row.customer_id,
      amount: new Decimal(row.amount),
      currency: row.currency,
      status: row.status,
      paymentMethod: {
        type: row.payment_method_type,
        token: row.payment_method_token,
        last4: row.payment_method_last4,
        brand: row.payment_method_brand,
      },
      description: row.description,
      idempotencyKey: row.idempotency_key,
      ledgerTransactionId: row.ledger_transaction_id,
      failureReason: row.failure_reason,
      metadata: row.metadata,
      createdAt: row.created_at,
      updatedAt: row.updated_at,
    };
  }

  private rowToRefund(row: any): Refund {
    return {
      id: row.id,
      paymentId: row.payment_id,
      organizationId: row.organization_id,
      amount: new Decimal(row.amount),
      reason: row.reason,
      status: row.status,
      ledgerTransactionId: row.ledger_transaction_id,
      createdAt: row.created_at,
    };
  }
}
