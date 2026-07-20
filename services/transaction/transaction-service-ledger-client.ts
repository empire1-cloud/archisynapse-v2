import { Decimal } from 'decimal.js';

/**
 * LedgerClient: HTTP client for calling the Ledger Service.
 * The Transaction Service NEVER writes ledger data directly —
 * it always goes through this client, which calls the Ledger Service API.
 * This keeps the ledger as the single source of financial truth.
 *
 * Accounting model (platform facilitating merchant payments):
 *   Debit  Processor Clearing     gross amount
 *   Credit Merchant Payable        net amount
 *   Credit Platform Fee Revenue    platform fee
 */
export class LedgerClient {
  private baseUrl: string;

  constructor(baseUrl: string = process.env.LEDGER_SERVICE_URL || 'http://localhost:3001') {
    this.baseUrl = baseUrl;
  }

  async listAccounts(params: { organizationId: string }): Promise<Array<{ id: string; code: string; name: string; type: string; currency: string }>> {
    const res = await fetch(`${this.baseUrl}/accounts`, {
      headers: {
        'X-Organization-ID': params.organizationId,
      },
    });

    if (!res.ok) {
      const body = await res.text();
      throw new Error(`Ledger Service rejected account list: ${res.status} ${body}`);
    }

    return res.json() as Promise<Array<{ id: string; code: string; name: string; type: string; currency: string }>>;
  }

  async createAccount(params: {
    organizationId: string;
    code: string;
    name: string;
    type: string;
    currency: string;
  }): Promise<{ id: string; code: string }> {
    const res = await fetch(`${this.baseUrl}/accounts`, {
      method: 'POST',
      headers: {
        'Content-Type': 'application/json',
        'X-Organization-ID': params.organizationId,
      },
      body: JSON.stringify({
        code: params.code,
        name: params.name,
        type: params.type,
        currency: params.currency,
      }),
    });

    if (!res.ok) {
      const body = await res.text();
      throw new Error(`Ledger Service rejected account create: ${res.status} ${body}`);
    }

    return res.json() as Promise<{ id: string; code: string }>;
  }

  /**
   * Post a payment-succeeded transaction to the ledger.
   *
   * Accounting entries:
   *   Debit  Processor Clearing     gross amount
   *   Credit Merchant Payable        net amount (gross - fee)
   *   Credit Platform Fee Revenue    platform fee
   *
   * One idempotency key derived from the payment ID is used for
   * initial posting and every retry.
   */
  async postPaymentSucceeded(params: {
    organizationId: string;
    paymentId: string;
    grossAmount: Decimal;
    feeAmount: Decimal;
    netAmount: Decimal;
    currency: string;
    processorClearingAccountId: string;
    merchantPayableAccountId: string;
    platformFeeRevenueAccountId: string;
    idempotencyKey: string;
    metadata?: Record<string, unknown>;
  }): Promise<{ id: string }> {
    const entries = [
      {
        accountId: params.processorClearingAccountId,
        debitCredit: 'DEBIT',
        amount: params.grossAmount.toString(),
        description: 'Gross payment received from processor',
      },
      {
        accountId: params.merchantPayableAccountId,
        debitCredit: 'CREDIT',
        amount: params.netAmount.toString(),
        description: 'Net amount payable to merchant',
      },
    ];

    if (params.feeAmount.greaterThan(0)) {
      entries.push({
        accountId: params.platformFeeRevenueAccountId,
        debitCredit: 'CREDIT',
        amount: params.feeAmount.toString(),
        description: 'Platform fee revenue',
      });
    }

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
        amount: params.grossAmount.toString(),
        currency: params.currency,
        idempotencyKey: params.idempotencyKey,
        entries,
        metadata: params.metadata,
      }),
    });

    if (!res.ok) {
      const body = await res.text();
      throw new Error(`Ledger Service rejected transaction: ${res.status} ${body}`);
    }

    return res.json() as Promise<{ id: string }>;
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

    return res.json() as Promise<{ id: string }>;
  }
}
