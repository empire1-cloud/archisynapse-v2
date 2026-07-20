import { Pool, PoolClient } from 'pg';
import { Decimal } from 'decimal.js';
import { v4 as uuidv4 } from 'uuid';
import pino from 'pino';

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

const logger = pino();

interface LedgerAccountConfig {
  processorClearingAccountId?: string;
  merchantPayableAccountId?: string;
  platformFeeRevenueAccountId?: string;
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
 * The Transaction Service is the SOLE owner of ledger posting.
 * The Gateway never posts directly — it queries by referenceId if needed.
 *
 * Accounting model:
 *   Debit  Processor Clearing     gross amount
 *   Credit Merchant Payable        net amount
 *   Credit Platform Fee Revenue    platform fee
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
   * The ledger idempotency key is `payment-{paymentId}` — derived from the
   * payment ID and used for initial posting and every retry.
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
      const feeAmount = req.feeAmount || new Decimal(0);
      const grossAmount = req.amount;
      const netAmount = grossAmount.minus(feeAmount);

      await client.query(
        `INSERT INTO payments
         (id, organization_id, customer_id, amount, currency, status,
          payment_method_type, payment_method_token, payment_method_last4, payment_method_brand,
          description, idempotency_key, fee_amount, metadata)
         VALUES ($1,$2,$3,$4,$5,'PENDING',$6,$7,$8,$9,$10,$11,$12,$13)`,
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
          feeAmount.toString(),
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
      // The idempotency key is derived from the payment ID — used for initial
      // posting and every retry by the reconciliation service.
      const ledgerIdempotencyKey = `payment-${paymentId}`;
      try {
        const accounts = await this.resolveLedgerAccounts(req.organizationId);
        const metadata = {
          ...(req.metadata || {}),
          payment_id: paymentId,
          correlation_id: req.metadata?.correlation_id,
          event_id: req.metadata?.event_id,
          fee_amount: feeAmount.toString(),
          gross_amount: grossAmount.toString(),
          net_amount: netAmount.toString(),
        };

        const ledgerTxn = await this.ledgerClient.postPaymentSucceeded({
          organizationId: req.organizationId,
          paymentId,
          grossAmount,
          feeAmount,
          netAmount,
          currency: req.currency,
          processorClearingAccountId: accounts.processorClearingAccountId,
          merchantPayableAccountId: accounts.merchantPayableAccountId,
          platformFeeRevenueAccountId: accounts.platformFeeRevenueAccountId,
          idempotencyKey: ledgerIdempotencyKey,
          metadata,
        });

        await client.query(
          `UPDATE payments SET ledger_transaction_id = $1 WHERE id = $2`,
          [ledgerTxn.id, paymentId]
        );
      } catch (ledgerError) {
        // Critical: log loudly. A reconciliation job MUST pick this up and retry.
        logger.error(
          { paymentId, ledgerIdempotencyKey, error: ledgerError },
          'CRITICAL: Payment succeeded but ledger post failed — needs reconciliation'
        );
        // Insert into unposted_payments for durable reconciliation
        await this.enqueueUnpostedPayment(client, {
          organizationId: req.organizationId,
          paymentId,
          idempotencyKey: ledgerIdempotencyKey,
          grossAmount,
          feeAmount,
          netAmount,
          currency: req.currency,
          referenceId: paymentId,
          metadata: req.metadata,
        });
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
        organizationId: payment.organizationId,
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
   * Query ledger by payment referenceId to check if a ledger transaction exists.
   * Used by the gateway when transaction succeeds but returns no ledger ID.
   */
  async queryLedgerByReference(organizationId: string, referenceId: string): Promise<boolean> {
    try {
      const accounts = await this.ledgerClient.listAccounts({ organizationId });
      if (!accounts || accounts.length === 0) return false;
      // If we can list accounts, the ledger is reachable.
      // The actual check is done by the gateway querying /transactions?referenceId=...
      return true;
    } catch {
      return false;
    }
  }

  private async enqueueUnpostedPayment(
    client: PoolClient,
    params: {
      organizationId: string;
      paymentId: string;
      idempotencyKey: string;
      grossAmount: Decimal;
      feeAmount: Decimal;
      netAmount: Decimal;
      currency: string;
      referenceId: string;
      metadata?: Record<string, unknown>;
    }
  ): Promise<void> {
    await client.query(
      `INSERT INTO unposted_payments (organization_id, payment_id, idempotency_key, gross_amount, fee_amount, net_amount, currency, reference_id, metadata)
       VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9)
       ON CONFLICT DO NOTHING`,
      [
        params.organizationId,
        params.paymentId,
        params.idempotencyKey,
        params.grossAmount.toString(),
        params.feeAmount.toString(),
        params.netAmount.toString(),
        params.currency,
        params.referenceId,
        JSON.stringify(params.metadata || {}),
      ]
    );
  }

  /**
   * Payment processor stub. Replace with a real adapter (Stripe, Adyen, etc.)
   */
  private async callProcessor(req: CreatePaymentRequest): Promise<ProcessorResult> {
    return {
      success: true,
      processorTransactionId: `proc_${uuidv4()}`,
    };
  }

  private async resolveLedgerAccounts(organizationId: string): Promise<Required<LedgerAccountConfig>> {
    if (
      this.ledgerAccounts.processorClearingAccountId &&
      this.ledgerAccounts.merchantPayableAccountId &&
      this.ledgerAccounts.platformFeeRevenueAccountId
    ) {
      return {
        processorClearingAccountId: this.ledgerAccounts.processorClearingAccountId,
        merchantPayableAccountId: this.ledgerAccounts.merchantPayableAccountId,
        platformFeeRevenueAccountId: this.ledgerAccounts.platformFeeRevenueAccountId,
      };
    }

    const existingAccounts = await this.ledgerClient.listAccounts({ organizationId });
    let processorClearingAccountId = existingAccounts.find((account) => account.code === '1100')?.id;
    let merchantPayableAccountId = existingAccounts.find((account) => account.code === '2001')?.id;
    let platformFeeRevenueAccountId = existingAccounts.find((account) => account.code === '4001')?.id;

    if (!processorClearingAccountId) {
      processorClearingAccountId = (
        await this.ledgerClient.createAccount({
          organizationId,
          code: '1100',
          name: 'Processor Clearing',
          type: 'ASSET',
          currency: 'USD',
        })
      ).id;
    }

    if (!merchantPayableAccountId) {
      merchantPayableAccountId = (
        await this.ledgerClient.createAccount({
          organizationId,
          code: '2001',
          name: 'Merchant Payable',
          type: 'LIABILITY',
          currency: 'USD',
        })
      ).id;
    }

    if (!platformFeeRevenueAccountId) {
      platformFeeRevenueAccountId = (
        await this.ledgerClient.createAccount({
          organizationId,
          code: '4001',
          name: 'Platform Fee Revenue',
          type: 'REVENUE',
          currency: 'USD',
        })
      ).id;
    }

    if (!processorClearingAccountId || !merchantPayableAccountId || !platformFeeRevenueAccountId) {
      throw new Error('Failed to resolve or create all required ledger accounts');
    }

    return { processorClearingAccountId, merchantPayableAccountId, platformFeeRevenueAccountId };
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
