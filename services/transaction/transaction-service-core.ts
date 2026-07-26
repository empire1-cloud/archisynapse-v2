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
  PaymentNotFoundError,
  InsufficientFundsError,
} from './transaction-service-types';
import {
  PaymentProcessor,
  buildProcessorFromEnv,
  decimalStringToMinorUnits,
} from './transaction-service-processor';

const logger = pino();

interface LedgerAccountConfig {
  processorClearingAccountId?: string;
  merchantPayableAccountId?: string;
  platformFeeRevenueAccountId?: string;
}

interface RefundAttemptRow {
  id: string;
  status: 'PROCESSING' | 'PROCESSOR_SUCCEEDED' | 'LEDGER_SUCCEEDED' | 'FAILED';
  processor_refund_id?: string | null;
  ledger_transaction_id?: string | null;
  failure_reason?: string | null;
}

/**
 * Owns the customer-facing payment lifecycle and is the only service allowed
 * to create financial ledger postings.
 */
export class TransactionService {
  private pool: Pool;
  private ledgerClient: LedgerClient;
  private ledgerAccounts: LedgerAccountConfig;
  private processor: PaymentProcessor;

  constructor(
    pool: Pool,
    ledgerClient: LedgerClient,
    ledgerAccounts: LedgerAccountConfig,
    processor: PaymentProcessor = buildProcessorFromEnv()
  ) {
    this.pool = pool;
    this.ledgerClient = ledgerClient;
    this.ledgerAccounts = ledgerAccounts;
    this.processor = processor;
  }

  getProcessorHealth() {
    return this.processor.health();
  }

  async createPayment(req: CreatePaymentRequest): Promise<Payment> {
    const client = await this.pool.connect();
    try {
      const existing = await client.query(
        `SELECT * FROM payments WHERE idempotency_key = $1`,
        [req.idempotencyKey]
      );
      if (existing.rows.length > 0) {
        logger.info(
          { idempotencyKey: req.idempotencyKey },
          'Returning existing payment (idempotent)'
        );
        return this.rowToPayment(existing.rows[0]);
      }

      const paymentId = uuidv4();
      const feeAmount = req.feeAmount || new Decimal(0);
      const grossAmount = req.amount;
      const netAmount = grossAmount.minus(feeAmount);

      try {
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
      } catch (error: any) {
        if (error?.code === '23505') {
          const raced = await client.query(
            `SELECT * FROM payments WHERE idempotency_key = $1`,
            [req.idempotencyKey]
          );
          if (raced.rows.length > 0) return this.rowToPayment(raced.rows[0]);
        }
        throw error;
      }

      let processorResult: ProcessorResult;
      try {
        processorResult = await this.callProcessor(req);
      } catch (error) {
        processorResult = {
          success: false,
          failureCode: error instanceof Error ? error.name : 'processor_error',
          failureMessage: error instanceof Error ? error.message : String(error),
        };
      }

      if (!processorResult.success) {
        await client.query(
          `UPDATE payments
              SET status = 'FAILED', processor_transaction_id = $1, failure_reason = $2
            WHERE id = $3`,
          [
            processorResult.processorTransactionId || null,
            processorResult.failureMessage || 'Processor declined',
            paymentId,
          ]
        );
        logger.warn(
          {
            paymentId,
            code: processorResult.failureCode,
            reason: processorResult.failureMessage,
          },
          'Payment failed at processor'
        );
        return this.getPayment(req.organizationId, paymentId);
      }

      await client.query(
        `UPDATE payments
            SET status = 'SUCCEEDED', processor_transaction_id = $1, failure_reason = NULL
          WHERE id = $2`,
        [processorResult.processorTransactionId || null, paymentId]
      );

      const ledgerIdempotencyKey = `payment-${paymentId}`;
      try {
        const accounts = await this.resolveLedgerAccounts(req.organizationId);
        const metadata = {
          ...(req.metadata || {}),
          payment_id: paymentId,
          processor_transaction_id: processorResult.processorTransactionId,
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
        logger.error(
          { paymentId, ledgerIdempotencyKey, error: ledgerError },
          'CRITICAL: Payment succeeded but ledger post failed — needs reconciliation'
        );
        await this.enqueueUnpostedPayment(client, {
          organizationId: req.organizationId,
          paymentId,
          idempotencyKey: ledgerIdempotencyKey,
          grossAmount,
          feeAmount,
          netAmount,
          currency: req.currency,
          referenceId: paymentId,
          metadata: {
            ...(req.metadata || {}),
            processor_transaction_id: processorResult.processorTransactionId,
          },
        });
      }

      return this.getPayment(req.organizationId, paymentId);
    } finally {
      client.release();
    }
  }

  async refundPayment(req: RefundRequest): Promise<Refund> {
    const client = await this.pool.connect();
    try {
      const existing = await client.query(
        `SELECT * FROM refunds WHERE idempotency_key = $1`,
        [req.idempotencyKey]
      );
      if (existing.rows.length > 0) return this.rowToRefund(existing.rows[0]);

      const paymentResult = await client.query(
        `SELECT * FROM payments WHERE id = $1`,
        [req.paymentId]
      );
      if (paymentResult.rows.length === 0) throw new PaymentNotFoundError();
      const payment = this.rowToPayment(paymentResult.rows[0]);

      if (
        payment.status !== PaymentStatus.SUCCEEDED &&
        payment.status !== PaymentStatus.PARTIALLY_REFUNDED
      ) {
        throw new Error(`Cannot refund payment with status ${payment.status}`);
      }
      if (!payment.processorTransactionId) {
        throw new Error('Cannot refund: payment has no processor transaction reference');
      }
      if (!payment.ledgerTransactionId) {
        throw new Error(
          'Cannot refund: payment has not been posted to the ledger yet. Retry after reconciliation.'
        );
      }

      const refundAmount = req.amount || payment.amount;
      const priorRefunds = await client.query(
        `SELECT COALESCE(SUM(amount), 0) AS total
           FROM refunds
          WHERE payment_id = $1 AND status = 'SUCCEEDED'`,
        [req.paymentId]
      );
      const alreadyRefunded = new Decimal(priorRefunds.rows[0].total);
      if (alreadyRefunded.plus(refundAmount).greaterThan(payment.amount)) {
        throw new InsufficientFundsError(
          'Refund amount exceeds remaining payment balance'
        );
      }

      const refundId = uuidv4();
      const inserted = await client.query(
        `INSERT INTO processor_refund_attempts
           (id, payment_id, organization_id, idempotency_key, processor_payment_id,
            amount, currency, reason, status)
         VALUES ($1,$2,$3,$4,$5,$6,$7,$8,'PROCESSING')
         ON CONFLICT (idempotency_key) DO NOTHING
         RETURNING *`,
        [
          refundId,
          req.paymentId,
          payment.organizationId,
          req.idempotencyKey,
          payment.processorTransactionId,
          refundAmount.toString(),
          payment.currency,
          req.reason,
        ]
      );

      let attempt: RefundAttemptRow;
      if (inserted.rows.length > 0) {
        attempt = inserted.rows[0] as RefundAttemptRow;
      } else {
        const attemptResult = await client.query(
          `SELECT * FROM processor_refund_attempts WHERE idempotency_key = $1`,
          [req.idempotencyKey]
        );
        if (attemptResult.rows.length === 0) {
          throw new Error('refund idempotency record disappeared');
        }
        attempt = attemptResult.rows[0] as RefundAttemptRow;
        if (attempt.status === 'PROCESSING') {
          throw new Error('another refund request is already processing this idempotency key');
        }
        if (attempt.status === 'LEDGER_SUCCEEDED') {
          const completed = await client.query(
            `SELECT * FROM refunds WHERE idempotency_key = $1`,
            [req.idempotencyKey]
          );
          if (completed.rows.length > 0) return this.rowToRefund(completed.rows[0]);
        }
        if (attempt.status === 'FAILED') {
          await client.query(
            `UPDATE processor_refund_attempts
                SET status = 'PROCESSING', failure_reason = NULL, updated_at = now()
              WHERE id = $1`,
            [attempt.id]
          );
          attempt.status = 'PROCESSING';
        }
      }

      let processorRefundId = attempt.processor_refund_id || undefined;
      if (attempt.status !== 'PROCESSOR_SUCCEEDED') {
        const processorRefund = await this.processor.refund({
          processorTransactionId: payment.processorTransactionId,
          amountMinor: decimalStringToMinorUnits(refundAmount.toFixed(2), payment.currency),
          reason: req.reason,
          idempotencyKey: req.idempotencyKey,
        });

        if (processorRefund.status !== 'succeeded') {
          const reason =
            processorRefund.failureMessage ||
            `processor refund status ${processorRefund.rawStatus || processorRefund.status}`;
          await client.query(
            `UPDATE processor_refund_attempts
                SET status = 'FAILED', processor_refund_id = $1,
                    failure_reason = $2, updated_at = now()
              WHERE id = $3`,
            [processorRefund.processorRefundId || null, reason, attempt.id]
          );
          throw new Error(reason);
        }

        processorRefundId = processorRefund.processorRefundId;
        await client.query(
          `UPDATE processor_refund_attempts
              SET status = 'PROCESSOR_SUCCEEDED', processor_refund_id = $1,
                  processor_succeeded_at = now(), updated_at = now()
            WHERE id = $2`,
          [processorRefundId || null, attempt.id]
        );
      }

      let ledgerReversal;
      try {
        ledgerReversal = await this.ledgerClient.postRefund({
          organizationId: payment.organizationId,
          originalLedgerTransactionId: payment.ledgerTransactionId,
          reason: req.reason,
        });
      } catch (error) {
        await client.query(
          `UPDATE processor_refund_attempts
              SET failure_reason = $1, updated_at = now()
            WHERE id = $2`,
          [
            `Processor refund succeeded; ledger reversal pending: ${
              error instanceof Error ? error.message : String(error)
            }`,
            attempt.id,
          ]
        );
        throw error;
      }

      await client.query(
        `INSERT INTO refunds
           (id, payment_id, organization_id, amount, reason, status,
            idempotency_key, processor_refund_id, ledger_transaction_id)
         VALUES ($1,$2,$3,$4,$5,'SUCCEEDED',$6,$7,$8)
         ON CONFLICT (idempotency_key) DO NOTHING`,
        [
          refundId,
          req.paymentId,
          payment.organizationId,
          refundAmount.toString(),
          req.reason,
          req.idempotencyKey,
          processorRefundId || null,
          ledgerReversal.id,
        ]
      );

      await client.query(
        `UPDATE processor_refund_attempts
            SET status = 'LEDGER_SUCCEEDED', ledger_transaction_id = $1,
                ledger_succeeded_at = now(), failure_reason = NULL, updated_at = now()
          WHERE id = $2`,
        [ledgerReversal.id, attempt.id]
      );

      const isFullRefund = alreadyRefunded.plus(refundAmount).equals(payment.amount);
      await client.query(
        `UPDATE payments SET status = $1 WHERE id = $2`,
        [isFullRefund ? 'REFUNDED' : 'PARTIALLY_REFUNDED', req.paymentId]
      );

      const saved = await client.query(
        `SELECT * FROM refunds WHERE idempotency_key = $1`,
        [req.idempotencyKey]
      );
      return this.rowToRefund(saved.rows[0]);
    } finally {
      client.release();
    }
  }

  async getPayment(organizationId: string, paymentId: string): Promise<Payment> {
    const result = await this.pool.query(
      `SELECT * FROM payments WHERE id = $1 AND organization_id = $2`,
      [paymentId, organizationId]
    );
    if (result.rows.length === 0) throw new PaymentNotFoundError();
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
      payments: rows.map((row) => this.rowToPayment(row)),
      nextCursor: hasMore ? rows[rows.length - 1].id : null,
    };
  }

  async findUnpostedPayments(organizationId: string): Promise<Payment[]> {
    const result = await this.pool.query(
      `SELECT * FROM payments
        WHERE organization_id = $1 AND status = 'SUCCEEDED'
          AND ledger_transaction_id IS NULL
        ORDER BY created_at ASC`,
      [organizationId]
    );
    return result.rows.map((row) => this.rowToPayment(row));
  }

  async queryLedgerByReference(
    organizationId: string,
    _referenceId: string
  ): Promise<boolean> {
    try {
      const accounts = await this.ledgerClient.listAccounts({ organizationId });
      return Boolean(accounts && accounts.length > 0);
    } catch {
      return false;
    }
  }

  private async callProcessor(req: CreatePaymentRequest): Promise<ProcessorResult> {
    const result = await this.processor.charge({
      amountMinor: decimalStringToMinorUnits(req.amount.toFixed(2), req.currency),
      currency: req.currency,
      paymentMethodToken: req.paymentMethod.token,
      description: req.description,
      idempotencyKey: req.idempotencyKey,
      metadata: req.metadata,
    });
    return {
      success: result.status === 'succeeded',
      processorTransactionId: result.processorTransactionId,
      failureCode: result.failureCode || result.rawStatus,
      failureMessage:
        result.failureMessage ||
        (result.status === 'succeeded'
          ? undefined
          : `processor returned ${result.rawStatus || result.status}`),
    };
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
      `INSERT INTO unposted_payments
         (organization_id, payment_id, idempotency_key, gross_amount,
          fee_amount, net_amount, currency, reference_id, metadata)
       VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9)
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

  private async resolveLedgerAccounts(
    organizationId: string
  ): Promise<Required<LedgerAccountConfig>> {
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
    let processorClearingAccountId = existingAccounts.find(
      (account) => account.code === '1100'
    )?.id;
    let merchantPayableAccountId = existingAccounts.find(
      (account) => account.code === '2001'
    )?.id;
    let platformFeeRevenueAccountId = existingAccounts.find(
      (account) => account.code === '4001'
    )?.id;

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

    if (
      !processorClearingAccountId ||
      !merchantPayableAccountId ||
      !platformFeeRevenueAccountId
    ) {
      throw new Error('Failed to resolve or create all required ledger accounts');
    }
    return {
      processorClearingAccountId,
      merchantPayableAccountId,
      platformFeeRevenueAccountId,
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
      processorTransactionId: row.processor_transaction_id,
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
      processorRefundId: row.processor_refund_id,
      ledgerTransactionId: row.ledger_transaction_id,
      createdAt: row.created_at,
    };
  }
}
