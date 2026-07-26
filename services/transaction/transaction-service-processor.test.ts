import { describe, expect, it } from 'vitest';

import {
  DisabledProcessor,
  ProcessorConfigurationError,
  ProcessorNotConfiguredError,
  ProcessorFetch,
  StripeTestProcessor,
  buildProcessorFromEnv,
  decimalStringToMinorUnits,
} from './transaction-service-processor';

function response(status: number, payload: Record<string, unknown>) {
  const text = JSON.stringify(payload);
  return {
    ok: status >= 200 && status < 300,
    status,
    async text() {
      return text;
    },
    async json() {
      return payload;
    },
  };
}

describe('transaction processor proof adapter', () => {
  it('converts two-decimal amounts to minor units', () => {
    expect(decimalStringToMinorUnits('10.25', 'USD')).toBe(1025);
    expect(() => decimalStringToMinorUnits('1.234', 'USD')).toThrow();
  });

  it('rejects live Stripe keys', () => {
    expect(
      () => new StripeTestProcessor({ secretKey: 'sk_live_not_allowed' })
    ).toThrow(ProcessorConfigurationError);
  });

  it('fails closed when no processor is configured', async () => {
    const processor = buildProcessorFromEnv({
      ARCHISYNAPSE_PROCESSOR: 'disabled',
    });
    expect(processor).toBeInstanceOf(DisabledProcessor);
    await expect(
      processor.charge({
        amountMinor: 100,
        currency: 'USD',
        paymentMethodToken: 'pm_card_visa',
        idempotencyKey: 'proof-disabled',
      })
    ).rejects.toBeInstanceOf(ProcessorNotConfiguredError);
  });

  it('sends provider idempotency and tokenized payment data', async () => {
    let seen:
      | { url: string; init: Parameters<ProcessorFetch>[1] }
      | undefined;
    const processor = new StripeTestProcessor({
      secretKey: 'sk_test_example',
      baseUrl: 'http://127.0.0.1:9999/v1',
      fetchImpl: async (url, init) => {
        seen = { url, init };
        return response(200, { id: 'pi_test', status: 'succeeded' });
      },
    });

    const result = await processor.charge({
      amountMinor: 1250,
      currency: 'USD',
      paymentMethodToken: 'pm_card_visa',
      idempotencyKey: 'proof-charge-1',
      description: 'proof payment',
      metadata: {
        order_id: 'order_1',
        unsafe_nested: { ignored: true },
      },
    });

    expect(result.status).toBe('succeeded');
    expect(result.processorTransactionId).toBe('pi_test');
    expect(seen?.init.headers['Idempotency-Key']).toBe('proof-charge-1');
    expect(seen?.init.body).toContain('amount=1250');
    expect(seen?.init.body).toContain('payment_method=pm_card_visa');
    expect(seen?.init.body).toContain(
      'metadata%5Barchisynapse_order_id%5D=order_1'
    );
    expect(seen?.init.body).not.toContain('unsafe_nested');
  });

  it('does not report action-required payments as succeeded', async () => {
    const processor = new StripeTestProcessor({
      secretKey: 'sk_test_example',
      baseUrl: 'http://127.0.0.1:9999/v1',
      fetchImpl: async () =>
        response(200, { id: 'pi_action', status: 'requires_action' }),
    });

    const result = await processor.charge({
      amountMinor: 100,
      currency: 'USD',
      paymentMethodToken: 'pm_card_visa',
      idempotencyKey: 'proof-action',
    });
    expect(result.status).toBe('requires_action');
  });

  it('refunds the original processor PaymentIntent idempotently', async () => {
    let seen:
      | { url: string; init: Parameters<ProcessorFetch>[1] }
      | undefined;
    const processor = new StripeTestProcessor({
      secretKey: 'sk_test_example',
      baseUrl: 'http://127.0.0.1:9999/v1',
      fetchImpl: async (url, init) => {
        seen = { url, init };
        return response(200, { id: 're_test', status: 'succeeded' });
      },
    });

    const result = await processor.refund({
      processorTransactionId: 'pi_test',
      amountMinor: 500,
      reason: 'customer_requested',
      idempotencyKey: 'proof-refund-1',
    });

    expect(result.status).toBe('succeeded');
    expect(result.processorRefundId).toBe('re_test');
    expect(seen?.init.headers['Idempotency-Key']).toBe('proof-refund-1');
    expect(seen?.init.body).toContain('payment_intent=pi_test');
    expect(seen?.init.body).toContain('reason=requested_by_customer');
  });
});
