/**
 * API client — live-if-reachable, mock-if-not. Never silently mislabels one
 * as the other.
 *
 * Live endpoints actually exist on the real services today for only part of
 * this console's surface:
 *   - ledger-service (default :3001): GET /accounts, GET /trial-balance,
 *     GET /reconciliation, GET /transactions/:id  (services/ledger/ledger-service-api.ts)
 *   - gateway (default :8000): GET /api/v1/receipts/:id, GET /api/v1/keys/:key_id
 *     (services/gateway/royalty_routes.py)
 *
 * There is currently no "list all transactions" or "list all receipts"
 * endpoint on the real backend — this console's Ledger Journal, Receipts,
 * Royalty, Risk and Outbox list views are mock-only until those list
 * endpoints exist upstream. Accounts / trial balance / reconciliation will
 * go live automatically the moment a ledger-service is reachable at
 * VITE_LEDGER_SERVICE_URL.
 */

import type { Account, TrialBalanceRow } from '../types/ledger';
import {
  mockAccounts,
  mockObligationEvents,
  mockOutboxRows,
  mockReceipts,
  mockRiskAssessments,
  mockTransactions,
} from './mockData';

const LEDGER_SERVICE_URL = import.meta.env.VITE_LEDGER_SERVICE_URL || 'http://127.0.0.1:3001';
const GATEWAY_URL = import.meta.env.VITE_GATEWAY_URL || 'http://127.0.0.1:8000';
const PROBE_TIMEOUT_MS = 1200;
const DEMO_ORG_ID = 'org_lyrica_9f21';

export type DataSourceMode = 'live' | 'mock';

export interface DataSourceStatus {
  ledger: DataSourceMode;
  gateway: DataSourceMode;
}

async function probe(url: string): Promise<boolean> {
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), PROBE_TIMEOUT_MS);
  try {
    const res = await fetch(`${url}/health`, { signal: controller.signal });
    return res.ok;
  } catch {
    return false;
  } finally {
    clearTimeout(timer);
  }
}

let cachedStatus: DataSourceStatus | null = null;

/** Probes both services once per session. Call before rendering data views. */
export async function detectDataSources(): Promise<DataSourceStatus> {
  if (cachedStatus) return cachedStatus;
  const [ledgerUp, gatewayUp] = await Promise.all([probe(LEDGER_SERVICE_URL), probe(GATEWAY_URL)]);
  cachedStatus = {
    ledger: ledgerUp ? 'live' : 'mock',
    gateway: gatewayUp ? 'live' : 'mock',
  };
  return cachedStatus;
}

export function getCachedStatus(): DataSourceStatus {
  return cachedStatus ?? { ledger: 'mock', gateway: 'mock' };
}

// ---------------------------------------------------------------------------
// Ledger — accounts + trial balance are live-able today
// ---------------------------------------------------------------------------

export async function fetchAccounts(): Promise<{ data: Account[]; source: DataSourceMode }> {
  const status = await detectDataSources();
  if (status.ledger === 'live') {
    try {
      const res = await fetch(`${LEDGER_SERVICE_URL}/accounts`, {
        headers: { 'X-Organization-ID': DEMO_ORG_ID },
      });
      if (res.ok) return { data: await res.json(), source: 'live' };
    } catch {
      // fall through to mock
    }
  }
  return { data: mockAccounts, source: 'mock' };
}

export async function fetchTrialBalance(): Promise<{ data: TrialBalanceRow[] | null; source: DataSourceMode }> {
  const status = await detectDataSources();
  if (status.ledger === 'live') {
    try {
      const res = await fetch(`${LEDGER_SERVICE_URL}/trial-balance`, {
        headers: { 'X-Organization-ID': DEMO_ORG_ID },
      });
      if (res.ok) return { data: await res.json(), source: 'live' };
    } catch {
      // fall through
    }
  }
  return { data: null, source: 'mock' }; // computed client-side from mock journal instead
}

/** No list-all-transactions endpoint exists upstream yet — always mock. */
export async function fetchTransactions() {
  return { data: mockTransactions, source: 'mock' as DataSourceMode };
}

/** No list-all-events endpoint exists upstream yet — always mock. */
export async function fetchObligationEvents() {
  return { data: mockObligationEvents, source: 'mock' as DataSourceMode };
}

/** No list-all-receipts endpoint exists upstream yet — always mock. */
export async function fetchReceipts() {
  return { data: mockReceipts, source: 'mock' as DataSourceMode };
}

/** Reference outbox implementation is Lyrica-side, not gateway-hosted — always mock. */
export async function fetchOutboxRows() {
  return { data: mockOutboxRows, source: 'mock' as DataSourceMode };
}

/** Risk assessments are embedded in receipts today, no standalone list endpoint — always mock. */
export async function fetchRiskAssessments() {
  return { data: mockRiskAssessments, source: 'mock' as DataSourceMode };
}
