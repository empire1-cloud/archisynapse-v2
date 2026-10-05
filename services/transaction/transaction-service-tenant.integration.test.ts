import { afterAll, afterEach, beforeAll, beforeEach, describe, expect, it } from 'vitest';
import { Pool } from 'pg';
import { Decimal } from 'decimal.js';
import { randomUUID } from 'crypto';

import { TransactionService } from './transaction-service-core';
import { LedgerClient } from './transaction-service-ledger-client';
import { PaymentProcessor, ProcessorRefundRequest } from './transaction-service-processor';
import { PaymentNotFoundError, PaymentStatus } from './transaction-service-types';
import { TENANT_ROLE } from './tenant-session';

/**
 * The transaction service runs organization-scoped work under the tenant role
 * (row-level security, migrations 007+). The processor and ledger are fakes;
 * the database is real. Runs when DATABASE_URL is set (CI: tenant-services).
 */
const DATABASE_URL = process.env.DATABASE_URL;

class FakeProcessor implements PaymentProcessor {
  readonly name = 'fake';
  readonly mode = 'test' as any;
  refunds: ProcessorRefundRequest[] = [];
  async charge() {
    return { status: 'succeeded' as const, processorTransactionId: `pi_${randomUUID()}` };
  }
  async refund(request: ProcessorRefundRequest) {
    this.refunds.push(request);
    return { status: 'succeeded' as const, processorRefundId: `re_${randomUUID()}` };
  }
  health() {
    return { status: 'ok' } as any;
  }
}

const fakeLedger = {
  async listAccounts() {
    return [];
  },
  async postPaymentSucceeded() {
    return { id: randomUUID() };
  },
  async postRefund() {
    return { id: randomUUID() };
  },
} as unknown as LedgerClient;

const accounts = {
  processorClearingAccountId: randomUUID(),
  merchantPayableAccountId: randomUUID(),
  platformFeeRevenueAccountId: randomUUID(),
};

describe.skipIf(!DATABASE_URL)('transaction service under the tenant role', () => {
  let pool: Pool;
  let processor: FakeProcessor;
  let service: TransactionService;
  let orgA: string;
  let orgB: string;

  beforeAll(() => {
    // One connection, so a leaked role would show up on the next statement.
    pool = new Pool({ connectionString: DATABASE_URL, max: 1 });
  });

  afterAll(async () => {
    await pool.end();
  });

  beforeEach(() => {
    processor = new FakeProcessor();
    service = new TransactionService(pool, fakeLedger, accounts, processor, { tenantIsolation: true });
    orgA = `org_txn_a_${randomUUID().slice(0, 8)}`;
    orgB = `org_txn_b_${randomUUID().slice(0, 8)}`;
  });

  afterEach(async () => {
    const orgs = [orgA, orgB];
    for (const table of ['refunds', 'processor_refund_attempts', 'unposted_payments', 'payments']) {
      await pool.query(`DELETE FROM ${table} WHERE organization_id = ANY($1)`, [orgs]);
    }
  });

  const pay = (organizationId: string, idempotencyKey = `pay-${randomUUID()}`) =>
    service.createPayment({
      organizationId,
      amount: new Decimal('40.00'),
      currency: 'USD',
      paymentMethod: { type: 'CARD', token: 'tok_test' } as any,
      idempotencyKey,
    });

  it('creates and reads a payment under the role', async () => {
    const payment = await pay(orgA);
    expect(payment.status).toBe(PaymentStatus.SUCCEEDED);
    expect(payment.ledgerTransactionId).toBeTruthy();
    const listed = await service.listPayments(orgA);
    expect(listed.payments.map((p) => p.id)).toEqual([payment.id]);
  });

  it('cannot read or list another organization\'s payment', async () => {
    const payment = await pay(orgA);
    await expect(service.getPayment(orgB, payment.id)).rejects.toBeInstanceOf(PaymentNotFoundError);
    expect((await service.listPayments(orgB)).payments).toEqual([]);
  });

  it('cannot refund another organization\'s payment, and the processor is never called', async () => {
    const payment = await pay(orgA);
    await expect(
      service.refundPayment({
        organizationId: orgB,
        paymentId: payment.id,
        reason: 'cross-tenant attempt',
        idempotencyKey: `ref-${randomUUID()}`,
      })
    ).rejects.toBeInstanceOf(PaymentNotFoundError);
    expect(processor.refunds).toHaveLength(0);
    expect((await service.getPayment(orgA, payment.id)).status).toBe(PaymentStatus.SUCCEEDED);
  });

  it('refuses a refund that does not name the caller\'s organization', async () => {
    const payment = await pay(orgA);
    await expect(
      service.refundPayment({ paymentId: payment.id, reason: 'no org', idempotencyKey: `ref-${randomUUID()}` })
    ).rejects.toBeInstanceOf(PaymentNotFoundError);
    expect(processor.refunds).toHaveLength(0);
  });

  it('refunds its own payment under the role', async () => {
    const payment = await pay(orgA);
    const refund = await service.refundPayment({
      organizationId: orgA,
      paymentId: payment.id,
      reason: 'customer request',
      idempotencyKey: `ref-${randomUUID()}`,
    });
    expect(refund.status).toBe('SUCCEEDED');
    expect(processor.refunds).toHaveLength(1);
    expect((await service.getPayment(orgA, payment.id)).status).toBe(PaymentStatus.REFUNDED);
  });

  it('a reused idempotency key never returns another organization\'s payment', async () => {
    const key = `shared-${randomUUID()}`;
    const paymentA = await pay(orgA, key);
    // Before row-level security this returned organization A's payment to B.
    const outcome = await pay(orgB, key).then(
      (p) => p,
      (e) => e
    );
    if (!(outcome instanceof Error)) {
      expect(outcome.id).not.toBe(paymentA.id);
    }
  });

  it('the pooled connection is back to the owner afterwards', async () => {
    await pay(orgA);
    const state = await pool.query(
      "SELECT current_user AS role, current_setting('archisynapse.tenant_id', true) AS tenant"
    );
    expect(state.rows[0].role).not.toBe(TENANT_ROLE);
    expect(state.rows[0].tenant ?? '').toBe('');
  });
});
