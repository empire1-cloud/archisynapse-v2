import { Decimal } from 'decimal.js';
import crypto from 'crypto';

/**
 * Ledger HTTP client for the royalty domain. Separate from the
 * card-payment LedgerClient (transaction-service-ledger-client.ts)
 * because royalty accounts are per-owner and dynamic (creator payables
 * aren't pre-provisioned the way merchant payable is) -- this client
 * finds-or-creates them, since the ledger service itself does NOT
 * dedupe accounts by (organizationId, code) server-side.
 *
 * Accounting model (spec/SPEC-royalty-loop-v1.md §5):
 *   Allow:   DR royalty_expense        / CR creator_payable:{owner} (per owner)
 *   Hold:    DR royalty_expense        / CR royalty_held_liab
 *   Release: DR royalty_held_liab / CR creator_payable:{owner} (per owner)
 */
export class RoyaltyLedgerClient {
  private baseUrl: string;

  constructor(baseUrl: string = process.env.LEDGER_SERVICE_URL || 'http://localhost:3001') {
    this.baseUrl = baseUrl;
  }

  private scopedIdempotencyKey(
    organizationId: string,
    operation: string,
    idempotencyKey: string
  ): string {
    const digest = crypto
      .createHash('sha256')
      .update(`${organizationId}\u0000${operation}\u0000${idempotencyKey}`)
      .digest('hex');
    return `royalty:${operation}:${digest}`;
  }

  private async listAccounts(organizationId: string): Promise<Array<{ id: string; code: string }>> {
    const res = await fetch(`${this.baseUrl}/accounts`, {
      headers: { 'X-Organization-ID': organizationId },
    });
    if (!res.ok) {
      throw new Error(`Ledger Service rejected account list: ${res.status} ${await res.text()}`);
    }
    return res.json() as Promise<Array<{ id: string; code: string }>>;
  }

  private async createAccount(
    organizationId: string,
    code: string,
    name: string,
    type: string
  ): Promise<{ id: string }> {
    const res = await fetch(`${this.baseUrl}/accounts`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', 'X-Organization-ID': organizationId },
      body: JSON.stringify({ code, name, type, currency: 'USD' }),
    });
    if (!res.ok) {
      throw new Error(`Ledger Service rejected account create: ${res.status} ${await res.text()}`);
    }
    return res.json() as Promise<{ id: string }>;
  }

  async ensureAccount(organizationId: string, code: string, name: string, type: string): Promise<string> {
    const existing = await this.listAccounts(organizationId);
    const found = existing.find((a) => a.code === code);
    if (found) return found.id;
    const created = await this.createAccount(organizationId, code, name, type);
    return created.id;
  }

  private async postTransaction(
    organizationId: string,
    body: Record<string, unknown>
  ): Promise<{ id: string }> {
    const res = await fetch(`${this.baseUrl}/transactions`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', 'X-Organization-ID': organizationId },
      body: JSON.stringify(body),
    });
    if (!res.ok) {
      throw new Error(`Ledger Service rejected transaction: ${res.status} ${await res.text()}`);
    }
    return res.json() as Promise<{ id: string }>;
  }

  async postAllow(params: {
    organizationId: string;
    eventId: string;
    correlationId: string;
    idempotencyKey: string;
    gross: Decimal;
    payouts: Array<{ ownerId: string; amount: Decimal }>;
  }): Promise<{ id: string }> {
    const expenseAccountId = await this.ensureAccount(
      params.organizationId,
      'royalty_expense',
      'Royalty Expense',
      'EXPENSE'
    );
    const entries: Array<Record<string, unknown>> = [
      {
        accountId: expenseAccountId,
        debitCredit: 'DEBIT',
        amount: params.gross.toString(),
        description: `Royalty obligation ${params.eventId}`,
        metadata: { correlationId: params.correlationId, royaltyEventId: params.eventId },
      },
    ];
    for (const payout of params.payouts) {
      const payableAccountId = await this.ensureAccount(
        params.organizationId,
        payout.ownerId,
        `Creator Payable: ${payout.ownerId}`,
        'LIABILITY'
      );
      entries.push({
        accountId: payableAccountId,
        debitCredit: 'CREDIT',
        amount: payout.amount.toString(),
        description: `Royalty payable to ${payout.ownerId} for ${params.eventId}`,
        metadata: { correlationId: params.correlationId, royaltyEventId: params.eventId },
      });
    }

    return this.postTransaction(params.organizationId, {
      type: 'PAYOUT',
      referenceId: params.eventId,
      description: `Royalty obligation ${params.eventId}`,
      amount: params.gross.toString(),
      currency: 'USD',
      idempotencyKey: this.scopedIdempotencyKey(
        params.organizationId,
        'allow',
        params.idempotencyKey
      ),
      metadata: {
        sourceIdempotencyKey: params.idempotencyKey,
        royaltyEventId: params.eventId,
        correlationId: params.correlationId,
      },
      entries,
    });
  }

  async postHold(params: {
    organizationId: string;
    eventId: string;
    correlationId: string;
    idempotencyKey: string;
    gross: Decimal;
  }): Promise<{ id: string }> {
    const expenseAccountId = await this.ensureAccount(
      params.organizationId,
      'royalty_expense',
      'Royalty Expense',
      'EXPENSE'
    );
    const heldAccountId = await this.ensureAccount(
      params.organizationId,
      'royalty_held_liab',
      'Royalty Held Liability',
      'LIABILITY'
    );

    return this.postTransaction(params.organizationId, {
      type: 'ADJUSTMENT',
      referenceId: params.eventId,
      description: `Royalty obligation held: ${params.eventId}`,
      amount: params.gross.toString(),
      currency: 'USD',
      idempotencyKey: this.scopedIdempotencyKey(
        params.organizationId,
        'hold',
        params.idempotencyKey
      ),
      metadata: {
        sourceIdempotencyKey: params.idempotencyKey,
        royaltyEventId: params.eventId,
        correlationId: params.correlationId,
      },
      entries: [
        {
          accountId: expenseAccountId,
          debitCredit: 'DEBIT',
          amount: params.gross.toString(),
          description: `Royalty obligation ${params.eventId} (held)`,
          metadata: { correlationId: params.correlationId, royaltyEventId: params.eventId },
        },
        {
          accountId: heldAccountId,
          debitCredit: 'CREDIT',
          amount: params.gross.toString(),
          description: `Held pending risk review: ${params.eventId}`,
          metadata: { correlationId: params.correlationId, royaltyEventId: params.eventId },
        },
      ],
    });
  }

  async postRelease(params: {
    organizationId: string;
    eventId: string;
    correlationId: string;
    idempotencyKey: string;
    gross: Decimal;
    payouts: Array<{ ownerId: string; amount: Decimal }>;
  }): Promise<{ id: string }> {
    const heldAccountId = await this.ensureAccount(
      params.organizationId,
      'royalty_held_liab',
      'Royalty Held Liability',
      'LIABILITY'
    );
    const entries: Array<Record<string, unknown>> = [
      {
        accountId: heldAccountId,
        debitCredit: 'DEBIT',
        amount: params.gross.toString(),
        description: `Release held royalty ${params.eventId}`,
        metadata: { correlationId: params.correlationId, royaltyEventId: params.eventId },
      },
    ];
    for (const payout of params.payouts) {
      const payableAccountId = await this.ensureAccount(
        params.organizationId,
        payout.ownerId,
        `Creator Payable: ${payout.ownerId}`,
        'LIABILITY'
      );
      entries.push({
        accountId: payableAccountId,
        debitCredit: 'CREDIT',
        amount: payout.amount.toString(),
        description: `Royalty payable to ${payout.ownerId} for ${params.eventId} (released)`,
        metadata: { correlationId: params.correlationId, royaltyEventId: params.eventId },
      });
    }

    return this.postTransaction(params.organizationId, {
      type: 'PAYOUT',
      referenceId: `${params.eventId}-release`,
      description: `Release of held royalty ${params.eventId}`,
      amount: params.gross.toString(),
      currency: 'USD',
      idempotencyKey: this.scopedIdempotencyKey(
        params.organizationId,
        'release',
        params.idempotencyKey
      ),
      metadata: {
        sourceIdempotencyKey: params.idempotencyKey,
        royaltyEventId: params.eventId,
        correlationId: params.correlationId,
      },
      entries,
    });
  }

  async postReversal(
    originalLedgerTransactionId: string,
    organizationId: string,
    reason: string,
    reversalIdempotencyKey: string
  ): Promise<{ id: string }> {
    const res = await fetch(`${this.baseUrl}/transactions/${originalLedgerTransactionId}/reverse`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', 'X-Organization-ID': organizationId },
      body: JSON.stringify({
        reason,
        idempotencyKey: this.scopedIdempotencyKey(
          organizationId,
          'reversal',
          reversalIdempotencyKey
        ),
      }),
    });
    if (!res.ok) {
      throw new Error(`Ledger Service rejected reversal: ${res.status} ${await res.text()}`);
    }
    return res.json() as Promise<{ id: string }>;
  }
}
