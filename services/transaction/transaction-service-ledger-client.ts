import { Decimal } from 'decimal.js';

/**
 * LedgerClient: HTTP client for calling the Ledger Service.
 * The Transaction Service NEVER writes ledger data directly —
 * it always goes through this client, which calls the Ledger Service API.
 * This keeps the ledger as the single source of financial truth.
 */
export class LedgerClient {
  private baseUrl: string;

  constructor(baseUrl: string = process.env.LEDGER_SERVICE_URL || 'http://localhost:3001') {
    this.baseUrl = baseUrl;
  }

  /**
   * Post a payment-succeeded transaction to the ledger.
   * Debits Cash, credits Revenue (simplified two-account model —
   * extend with fee/tax accounts as needed).
   */
  async postPaymentSucceeded(params: {
    organizationId: string;
    paymentId: string;
    amount: Decimal;
    currency: string;
    cashAccountId: string;
    revenueAccountId: string;
    idempotencyKey: string;
  }): Promise<{ id: string }> {
    const res = await fetch(`${this.baseUrl}/transactions`, {
      method: 'POST',
      headers: {
        'Content-Type': 'application/json',
        'X-Organization-ID': params.organizationId,
      },
      body: JSON.stringify({
        type: 'PAYMENT',
        referenceId: params.paymentId,
        description: `Payment ${params.paymentId}`,
        amount: params.amount.toString(),
        currency: params.currency,
        idempotencyKey: params.idempotencyKey,
        entries: [
          {
            accountId: params.cashAccountId,
            debitCredit: 'DEBIT',
            amount: params.amount.toString(),
            description: 'Cash received',
          },
          {
            accountId: params.revenueAccountId,
            debitCredit: 'CREDIT',
            amount: params.amount.toString(),
            description: 'Revenue recognized',
          },
        ],
      }),
    });

    if (!res.ok) {
      const body = await res.text();
      throw new Error(`Ledger Service rejected transaction: ${res.status} ${body}`);
    }

    return res.json();
  }

  /**
   * Post a refund transaction to the ledger (reverses the original entries).
   */
  async postRefund(params: {
    organizationId: string;
    originalLedgerTransactionId: string;
    reason: string;
  }): Promise<{ id: string }> {
    const res = await fetch(
      `${this.baseUrl}/transactions/${params.originalLedgerTransactionId}/reverse`,
      {
        method: 'POST',
        headers: {
          'Content-Type': 'application/json',
          'X-Organization-ID': params.organizationId,
        },
        body: JSON.stringify({ reason: params.reason }),
      }
    );

    if (!res.ok) {
      const body = await res.text();
      throw new Error(`Ledger Service rejected reversal: ${res.status} ${body}`);
    }

    return res.json();
  }
}
