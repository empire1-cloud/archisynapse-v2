import { createHash } from 'crypto';
import { AddressInfo } from 'net';
import { Server } from 'http';
import { afterAll, beforeAll, beforeEach, describe, expect, it } from 'vitest';

import { initLedgerAPI } from './ledger-service-api';
import { LedgerService } from './ledger-service-core';
import {
  MIN_SERVICE_TOKEN_LENGTH,
  ServiceAuthConfig,
  outboundServiceToken,
  parseServiceTokens,
  serviceAuthFromEnv,
} from './service-auth';

/**
 * Service-to-service authentication for the ledger.
 *
 * Before: any request carrying X-Organization-ID was trusted, so anything able
 * to reach the port could read or post ledger entries as any merchant.
 * After: the caller must present a token issued to it by name, and a
 * read-only caller (the gateway) can never post.
 *
 * The HTTP checks run against the real Express app with a recording stand-in
 * for LedgerService, so they prove that a rejected request never reaches the
 * ledger at all.
 */

const TRANSACTION_TOKEN = 't'.repeat(40) + '-transaction';
const GATEWAY_TOKEN = 'g'.repeat(40) + '-gateway';
const ROTATED_TRANSACTION_TOKEN = 'r'.repeat(40) + '-transaction-next';

const calls: Array<{ method: string; organizationId: string }> = [];
const ledgerStub = {
  async listAccounts(organizationId: string) {
    calls.push({ method: 'listAccounts', organizationId });
    return [];
  },
  async createAccount(organizationId: string, code: string, name: string, type: string) {
    calls.push({ method: 'createAccount', organizationId });
    return { id: 'acct-1', organizationId, code, name, type };
  },
} as unknown as LedgerService;

const enforced: ServiceAuthConfig = serviceAuthFromEnv({
  ARCHISYNAPSE_INBOUND_SERVICE_TOKENS: [
    `transaction:write:${TRANSACTION_TOKEN}`,
    `transaction:write:${ROTATED_TRANSACTION_TOKEN}`,
    `gateway:read:${GATEWAY_TOKEN}`,
  ].join(','),
});

let server: Server;
let base: string;

async function call(
  method: string,
  path: string,
  headers: Record<string, string> = {},
  body?: unknown
): Promise<Response> {
  return fetch(`${base}${path}`, {
    method,
    headers: { 'Content-Type': 'application/json', ...headers },
    body: body === undefined ? undefined : JSON.stringify(body),
  });
}

const org = { 'X-Organization-ID': 'mer_auth_test' };
const bearer = (token: string) => ({ Authorization: `Bearer ${token}` });
const newAccount = { code: '1000', name: 'Cash', type: 'ASSET' };

beforeAll(async () => {
  const app = initLedgerAPI(ledgerStub, { serviceAuth: enforced });
  server = app.listen(0);
  await new Promise((resolve) => server.once('listening', resolve));
  base = `http://127.0.0.1:${(server.address() as AddressInfo).port}`;
});

afterAll(async () => {
  await new Promise((resolve) => server.close(resolve));
});

beforeEach(() => {
  calls.length = 0;
  initLedgerAPI(ledgerStub, { serviceAuth: enforced });
});

describe('ledger HTTP: service authentication', () => {
  it('leaves /health open', async () => {
    expect((await call('GET', '/health')).status).toBe(200);
  });

  it('rejects a request with only X-Organization-ID (the old trust model)', async () => {
    const res = await call('GET', '/accounts', org);
    expect(res.status).toBe(401);
    expect(calls).toEqual([]);
  });

  it('rejects a wrong token and a malformed Authorization header', async () => {
    expect((await call('GET', '/accounts', { ...org, ...bearer('x'.repeat(48)) })).status).toBe(401);
    expect((await call('GET', '/accounts', { ...org, Authorization: TRANSACTION_TOKEN })).status).toBe(401);
    expect((await call('GET', '/accounts', { ...org, Authorization: 'Basic abc' })).status).toBe(401);
    expect(calls).toEqual([]);
  });

  it('rejects an unauthenticated write before it reaches the ledger', async () => {
    const res = await call('POST', '/accounts', org, newAccount);
    expect(res.status).toBe(401);
    expect(calls).toEqual([]);
  });

  it('lets the transaction service read and write for the organization it names', async () => {
    const read = await call('GET', '/accounts', { ...org, ...bearer(TRANSACTION_TOKEN) });
    expect(read.status).toBe(200);
    const write = await call('POST', '/accounts', { ...org, ...bearer(TRANSACTION_TOKEN) }, newAccount);
    expect(write.status).toBe(201);
    expect(calls).toEqual([
      { method: 'listAccounts', organizationId: 'mer_auth_test' },
      { method: 'createAccount', organizationId: 'mer_auth_test' },
    ]);
  });

  it('accepts either token during a rotation', async () => {
    const res = await call('GET', '/accounts', { ...org, ...bearer(ROTATED_TRANSACTION_TOKEN) });
    expect(res.status).toBe(200);
  });

  it('lets the gateway read but never post to the ledger', async () => {
    expect((await call('GET', '/accounts', { ...org, ...bearer(GATEWAY_TOKEN) })).status).toBe(200);
    const write = await call('POST', '/accounts', { ...org, ...bearer(GATEWAY_TOKEN) }, newAccount);
    expect(write.status).toBe(403);
    expect(calls).toEqual([{ method: 'listAccounts', organizationId: 'mer_auth_test' }]);
  });

  it('still requires X-Organization-ID from an authenticated caller', async () => {
    const res = await call('GET', '/accounts', bearer(TRANSACTION_TOKEN));
    expect(res.status).toBe(401);
    expect(calls).toEqual([]);
  });

  it('refuses an unauthenticated request before reading its body (malformed JSON -> 401, not 500)', async () => {
    const res = await fetch(`${base}/accounts`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', ...org },
      body: '{bad',
    });
    expect(res.status).toBe(401);
    expect(calls).toEqual([]);
  });

  it('accepts a caller whose entry is stored only as a SHA-256 digest', async () => {
    const hex = createHash('sha256').update(TRANSACTION_TOKEN).digest('hex');
    initLedgerAPI(ledgerStub, {
      serviceAuth: serviceAuthFromEnv({ ARCHISYNAPSE_INBOUND_SERVICE_TOKENS: `transaction:write:sha256:${hex}` }),
    });
    expect((await call('GET', '/accounts', { ...org, ...bearer(TRANSACTION_TOKEN) })).status).toBe(200);
    expect((await call('GET', '/accounts', { ...org, ...bearer(GATEWAY_TOKEN) })).status).toBe(401);
    // Presenting the digest itself as the token does not work.
    expect((await call('GET', '/accounts', { ...org, ...bearer(hex) })).status).toBe(401);
  });

  it('ARCHISYNAPSE_SERVICE_AUTH=off restores the previous behaviour', async () => {
    initLedgerAPI(ledgerStub, { serviceAuth: serviceAuthFromEnv({ ARCHISYNAPSE_SERVICE_AUTH: 'off' }) });
    expect((await call('GET', '/accounts', org)).status).toBe(200);
  });
});

describe('service token configuration', () => {
  it('parses comma and newline separated entries and skips comments', () => {
    const parsed = parseServiceTokens(
      `# rotation in progress\ngateway:read:${GATEWAY_TOKEN}, transaction:write:${TRANSACTION_TOKEN}\n`
    );
    expect(parsed.map((c) => [c.caller, c.scope])).toEqual([
      ['gateway', 'read'],
      ['transaction', 'write'],
    ]);
  });

  it('never keeps the token itself, only a digest', () => {
    const [credential] = parseServiceTokens(`gateway:read:${GATEWAY_TOKEN}`);
    expect(JSON.stringify(credential)).not.toContain(GATEWAY_TOKEN);
  });

  it('refuses short tokens, unknown scopes, bad caller names and malformed entries', () => {
    const short = 'a'.repeat(MIN_SERVICE_TOKEN_LENGTH - 1);
    expect(() => parseServiceTokens(`gateway:read:${short}`)).toThrow(/at least/);
    expect(() => parseServiceTokens(`gateway:admin:${GATEWAY_TOKEN}`)).toThrow(/read or write/);
    expect(() => parseServiceTokens(`Gateway Service:read:${GATEWAY_TOKEN}`)).toThrow(/caller name/);
    expect(() => parseServiceTokens(GATEWAY_TOKEN)).toThrow(/caller:scope:token/);
  });

  it('refuses a malformed digest entry', () => {
    expect(() => parseServiceTokens('gateway:read:sha256:abc')).toThrow(/64 hex/);
  });

  it('enforces by default and refuses to start with no tokens', () => {
    expect(() => serviceAuthFromEnv({})).toThrow(/refusing to start/);
    expect(() => serviceAuthFromEnv({ ARCHISYNAPSE_SERVICE_AUTH: 'enforce' })).toThrow(/refusing to start/);
  });

  it('rejects an unknown mode', () => {
    expect(() => serviceAuthFromEnv({ ARCHISYNAPSE_SERVICE_AUTH: 'maybe' })).toThrow(/enforce' or 'off/);
  });

  it('reads entries from a file and combines them with the variable', () => {
    const config = serviceAuthFromEnv(
      {
        ARCHISYNAPSE_INBOUND_SERVICE_TOKENS: `gateway:read:${GATEWAY_TOKEN}`,
        ARCHISYNAPSE_INBOUND_SERVICE_TOKENS_FILE: '/run/service-tokens/inbound.tokens',
      },
      (path) => {
        expect(path).toBe('/run/service-tokens/inbound.tokens');
        return `transaction:write:${TRANSACTION_TOKEN}\n`;
      }
    );
    expect(config.enforce).toBe(true);
    expect(config.credentials.map((c) => c.caller)).toEqual(['gateway', 'transaction']);
  });

  it('outbound token: direct value, then file, else none', () => {
    expect(outboundServiceToken('X', { X_TOKEN: ` ${TRANSACTION_TOKEN} ` })).toBe(TRANSACTION_TOKEN);
    expect(outboundServiceToken('X', { X_TOKEN_FILE: '/f' }, () => `${GATEWAY_TOKEN}\n`)).toBe(GATEWAY_TOKEN);
    expect(outboundServiceToken('X', {})).toBeNull();
  });
});
