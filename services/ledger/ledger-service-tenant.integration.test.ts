import { afterAll, afterEach, beforeAll, beforeEach, describe, expect, it } from 'vitest';
import { Pool } from 'pg';
import { Decimal } from 'decimal.js';
import { randomUUID } from 'crypto';

import { LedgerService } from './ledger-service-core';
import { AccountType, DebitCredit, TransactionType } from './ledger-service-types';
import { TENANT_ROLE, openTenantSession, tenantIsolationFromEnv } from './tenant-session';

/**
 * The ledger service runs organization-scoped work under the tenant role
 * (row-level security, migrations 007+). Runs against a real PostgreSQL
 * database when DATABASE_URL is set (CI: tenant-services workflow).
 */
const DATABASE_URL = process.env.DATABASE_URL;

describe.skipIf(!DATABASE_URL)('ledger service under the tenant role', () => {
  let pool: Pool;
  let ledger: LedgerService;
  let orgA: string;
  let orgB: string;
  let cashA: string;
  let revenueA: string;
  let cashB: string;

  beforeAll(() => {
    // One connection: every call reuses it, so a leaked role or open
    // transaction would be visible to the next statement.
    pool = new Pool({ connectionString: DATABASE_URL, max: 1 });
    ledger = new LedgerService(pool, { tenantIsolation: true });
  });

  afterAll(async () => {
    await pool.end();
  });

  beforeEach(async () => {
    orgA = `org_led_a_${randomUUID().slice(0, 8)}`;
    orgB = `org_led_b_${randomUUID().slice(0, 8)}`;
    cashA = (await ledger.createAccount(orgA, '1000', 'Cash', AccountType.ASSET)).id;
    revenueA = (await ledger.createAccount(orgA, '4000', 'Revenue', AccountType.REVENUE)).id;
    cashB = (await ledger.createAccount(orgB, '1000', 'Cash', AccountType.ASSET)).id;
  });

  afterEach(async () => {
    const orgs = [orgA, orgB];
    for (const table of ['journal_entries', 'transactions', 'audit_logs', 'idempotency_store', 'accounts']) {
      await pool.query(`DELETE FROM ${table} WHERE organization_id = ANY($1)`, [orgs]);
    }
  });

  const post = (organizationId: string, debitAccount: string, creditAccount: string, idempotencyKey?: string) =>
    ledger.postTransaction({
      organizationId,
      type: TransactionType.PAYMENT,
      description: 'test sale',
      amount: new Decimal('25.00'),
      currency: 'USD',
      idempotencyKey,
      entries: [
        { accountId: debitAccount, debitCredit: DebitCredit.DEBIT, amount: new Decimal('25.00'), description: 'cash in' },
        { accountId: creditAccount, debitCredit: DebitCredit.CREDIT, amount: new Decimal('25.00'), description: 'revenue' },
      ],
    } as any);

  it('lists only the calling organization\'s accounts', async () => {
    const accounts = await ledger.listAccounts(orgA);
    expect(new Set(accounts.map((a) => a.organizationId))).toEqual(new Set([orgA]));
    expect(accounts.map((a) => a.id).sort()).toEqual([cashA, revenueA].sort());
  });

  it('posts a balanced transaction and updates balances under the role', async () => {
    const txn = await post(orgA, cashA, revenueA);
    expect(txn.entries).toHaveLength(2);
    const accounts = await ledger.listAccounts(orgA);
    expect(accounts.find((a) => a.id === cashA)!.balance.toString()).toBe('25');
  });

  it('refuses to post against another organization\'s account', async () => {
    await expect(post(orgA, cashB, revenueA)).rejects.toThrow();
    const balanceB = await pool.query('SELECT balance FROM accounts WHERE id = $1', [cashB]);
    expect(balanceB.rows[0].balance).toBe('0.0000');
  });

  it('cannot read another organization\'s transaction', async () => {
    const txn = await post(orgA, cashA, revenueA);
    await expect(ledger.getTransaction(orgB, txn.id)).rejects.toThrow('not found');
    const own = await ledger.getTransaction(orgA, txn.id);
    expect(own.entries).toHaveLength(2);
  });

  it('row-level security is in force on the service path, not just the WHERE clauses', async () => {
    // getTransaction reads journal entries by transaction id only. Plant a
    // row for another organization under the same transaction id (as the
    // owner). With the tenant role in force the service still sees only its
    // own two entries; on an owner connection it would return three.
    const txn = await post(orgA, cashA, revenueA);
    await pool.query(
      `INSERT INTO journal_entries (transaction_id, organization_id, account_id, debit_credit, amount, description)
       VALUES ($1, $2, $3, 'DEBIT', 1.00, 'planted')`,
      [txn.id, orgB, cashB]
    );
    const seen = await ledger.getTransaction(orgA, txn.id);
    expect(seen.entries.map((e) => e.organizationId)).toEqual([orgA, orgA]);
    const owner = new LedgerService(pool, { tenantIsolation: false });
    expect((await owner.getTransaction(orgA, txn.id)).entries).toHaveLength(3);
  });

  it('reverses a transaction under the role', async () => {
    const txn = await post(orgA, cashA, revenueA);
    const reversal = await ledger.reverseTransaction(orgA, txn.id, 'refund');
    expect(reversal.type).toBe(TransactionType.REVERSAL);
    const original = await pool.query('SELECT status FROM transactions WHERE id = $1', [txn.id]);
    expect(original.rows[0].status).toBe('REVERSED');
  });

  it('idempotent replay returns the same transaction and leaves the connection clean', async () => {
    const key = `led-${randomUUID()}`;
    const first = await post(orgA, cashA, revenueA, key);
    const replay = await post(orgA, cashA, revenueA, key);
    expect(replay.id).toBe(first.id);
    const state = await pool.query(
      "SELECT current_user AS role, current_setting('archisynapse.tenant_id', true) AS tenant, now() = statement_timestamp() AS fresh"
    );
    expect(state.rows[0].role).not.toBe(TENANT_ROLE);
    expect(state.rows[0].tenant ?? '').toBe('');
    expect(state.rows[0].fresh).toBe(true);
  });

  it('a query without an organization filter sees one organization', async () => {
    const session = await openTenantSession(pool, orgA, true);
    try {
      const rows = await session.client.query(
        'SELECT DISTINCT organization_id FROM accounts WHERE organization_id = ANY($1)',
        [[orgA, orgB]]
      );
      expect(rows.rows.map((r) => r.organization_id)).toEqual([orgA]);
      const role = await session.client.query('SELECT current_user AS role');
      expect(role.rows[0].role).toBe(TENANT_ROLE);
    } finally {
      await session.close();
    }
  });

  it('trial balance still works (owner read, filtered by organization)', async () => {
    await post(orgA, cashA, revenueA);
    const balance = await ledger.getTrialBalance(orgA);
    expect(balance.map((b) => b.accountId).sort()).toEqual([cashA, revenueA].sort());
  });

  it('isolation off uses the owner connection', async () => {
    const session = await openTenantSession(pool, orgA, false);
    try {
      const role = await session.client.query('SELECT current_user AS role');
      expect(role.rows[0].role).not.toBe(TENANT_ROLE);
    } finally {
      await session.close();
    }
  });
});

describe('tenant isolation setting', () => {
  it('defaults to enforce and accepts off', () => {
    expect(tenantIsolationFromEnv({})).toBe(true);
    expect(tenantIsolationFromEnv({ ARCHISYNAPSE_TENANT_ISOLATION: 'off' })).toBe(false);
    expect(() => tenantIsolationFromEnv({ ARCHISYNAPSE_TENANT_ISOLATION: 'maybe' })).toThrow();
  });
});
