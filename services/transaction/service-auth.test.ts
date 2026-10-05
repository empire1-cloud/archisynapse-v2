import { AddressInfo } from 'net';
import { IncomingHttpHeaders, Server, createServer } from 'http';
import { afterAll, beforeAll, beforeEach, describe, expect, it } from 'vitest';

import { RoyaltyLedgerClient } from './royalty-service-ledger-client';
import { initRoyaltyAPI } from './royalty-service-api';
import { RoyaltyService } from './royalty-service-core';
import { serviceAuthFromEnv } from './service-auth';
import { initTransactionAPI } from './transaction-service-api';
import { TransactionService } from './transaction-service-core';
import { LedgerClient } from './transaction-service-ledger-client';

/**
 * Service-to-service authentication for the transaction service, both ways:
 *   inbound  - only the gateway (by token) may call it, payment and royalty
 *              routes alike; a rejected request never reaches the service;
 *   outbound - its ledger clients send the transaction service's own token,
 *              and send nothing (so the ledger refuses) when none is configured.
 */

const GATEWAY_TOKEN = 'g'.repeat(40) + '-gateway';
const LEDGER_TOKEN = 'l'.repeat(40) + '-transaction-to-ledger';

const calls: string[] = [];
const transactionStub = {
  async getPayment(organizationId: string, id: string) {
    calls.push(`getPayment:${id}:${organizationId}`);
    return { id, organizationId, status: 'SUCCEEDED' };
  },
} as unknown as TransactionService;
const royaltyStub = {
  async getObligationByEventId(eventId: string) {
    calls.push(`getObligation:${eventId}`);
    return null;
  },
} as unknown as RoyaltyService;

const enforced = serviceAuthFromEnv({
  ARCHISYNAPSE_INBOUND_SERVICE_TOKENS: `gateway:write:${GATEWAY_TOKEN}`,
});

let server: Server;
let base: string;
const org = { 'X-Organization-ID': 'mer_auth_test' };
const bearer = (token: string) => ({ Authorization: `Bearer ${token}` });

beforeAll(async () => {
  const app = initTransactionAPI(transactionStub, { serviceAuth: enforced });
  app.use(initRoyaltyAPI(royaltyStub));
  server = app.listen(0);
  await new Promise((resolve) => server.once('listening', resolve));
  base = `http://127.0.0.1:${(server.address() as AddressInfo).port}`;
});

afterAll(async () => {
  await new Promise((resolve) => server.close(resolve));
});

beforeEach(() => {
  calls.length = 0;
});

describe('transaction HTTP: service authentication', () => {
  it('leaves /health open', async () => {
    expect((await fetch(`${base}/health`)).status).toBe(200);
  });

  it('rejects a payment lookup with only X-Organization-ID', async () => {
    const res = await fetch(`${base}/payments/pay_1`, { headers: org });
    expect(res.status).toBe(401);
    expect(calls).toEqual([]);
  });

  it('rejects an unauthenticated payment or refund before it reaches the service', async () => {
    const payment = await fetch(`${base}/payments`, {
      method: 'POST',
      headers: { ...org, 'Content-Type': 'application/json', 'Idempotency-Key': 'k1' },
      body: JSON.stringify({ amount: '1.00', paymentMethod: { type: 'CARD', token: 'tok' } }),
    });
    const refund = await fetch(`${base}/payments/pay_1/refund`, {
      method: 'POST',
      headers: { ...org, 'Content-Type': 'application/json' },
      body: JSON.stringify({ amount: '1.00' }),
    });
    expect([payment.status, refund.status]).toEqual([401, 401]);
    expect(calls).toEqual([]);
  });

  it('refuses an unauthenticated request before reading its body (malformed JSON -> 401)', async () => {
    const res = await fetch(`${base}/payments`, {
      method: 'POST',
      headers: { ...org, 'Content-Type': 'application/json' },
      body: '{bad',
    });
    expect(res.status).toBe(401);
    expect(calls).toEqual([]);
  });

  it('protects the royalty routes mounted on the same app', async () => {
    const res = await fetch(`${base}/royalties/evt_1`, { headers: org });
    expect(res.status).toBe(401);
    expect(calls).toEqual([]);
  });

  it('serves the gateway for the organization it names', async () => {
    const res = await fetch(`${base}/payments/pay_1`, { headers: { ...org, ...bearer(GATEWAY_TOKEN) } });
    expect(res.status).toBe(200);
    expect(calls).toEqual(['getPayment:pay_1:mer_auth_test']);
  });

  it('rejects a token issued for a different hop (transaction -> ledger)', async () => {
    const res = await fetch(`${base}/payments/pay_1`, { headers: { ...org, ...bearer(LEDGER_TOKEN) } });
    expect(res.status).toBe(401);
  });
});

describe('outbound: ledger clients send the transaction service token', () => {
  let ledger: Server;
  let ledgerUrl: string;
  const seen: IncomingHttpHeaders[] = [];

  beforeAll(async () => {
    ledger = createServer((req, res) => {
      seen.push(req.headers);
      res.setHeader('Content-Type', 'application/json');
      res.end('[]');
    });
    ledger.listen(0);
    await new Promise((resolve) => ledger.once('listening', resolve));
    ledgerUrl = `http://127.0.0.1:${(ledger.address() as AddressInfo).port}`;
  });

  afterAll(async () => {
    await new Promise((resolve) => ledger.close(resolve));
  });

  beforeEach(() => {
    seen.length = 0;
  });

  it('payment ledger client', async () => {
    await new LedgerClient(ledgerUrl, LEDGER_TOKEN).listAccounts({ organizationId: 'mer_a' });
    expect(seen[0].authorization).toBe(`Bearer ${LEDGER_TOKEN}`);
    expect(seen[0]['x-organization-id']).toBe('mer_a');
  });

  it('royalty ledger client', async () => {
    await new RoyaltyLedgerClient(ledgerUrl, LEDGER_TOKEN).ensureAccount('mer_a', '1', 'x', 'ASSET').catch(() => {});
    expect(seen[0].authorization).toBe(`Bearer ${LEDGER_TOKEN}`);
  });

  it('reads ARCHISYNAPSE_TRANSACTION_TO_LEDGER_TOKEN on every call when no token is passed', async () => {
    const saved = process.env.ARCHISYNAPSE_TRANSACTION_TO_LEDGER_TOKEN;
    try {
      const client = new LedgerClient(ledgerUrl);
      process.env.ARCHISYNAPSE_TRANSACTION_TO_LEDGER_TOKEN = LEDGER_TOKEN;
      await client.listAccounts({ organizationId: 'mer_a' });
      process.env.ARCHISYNAPSE_TRANSACTION_TO_LEDGER_TOKEN = LEDGER_TOKEN + '-rotated';
      await client.listAccounts({ organizationId: 'mer_a' });
      expect(seen.map((h) => h.authorization)).toEqual([
        `Bearer ${LEDGER_TOKEN}`,
        `Bearer ${LEDGER_TOKEN}-rotated`,
      ]);
    } finally {
      if (saved === undefined) delete process.env.ARCHISYNAPSE_TRANSACTION_TO_LEDGER_TOKEN;
      else process.env.ARCHISYNAPSE_TRANSACTION_TO_LEDGER_TOKEN = saved;
    }
  });

  it('sends no Authorization header when no token is configured (ledger then refuses)', async () => {
    await new LedgerClient(ledgerUrl, null).listAccounts({ organizationId: 'mer_a' });
    expect(seen[0].authorization).toBeUndefined();
  });
});
