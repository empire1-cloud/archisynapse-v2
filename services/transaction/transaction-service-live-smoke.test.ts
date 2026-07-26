import { describe, expect, it } from 'vitest';

import {
  ProcessorConfigurationError,
  ProcessorFetch,
  ProcessorProtocolError,
  StripeLiveSmokeProcessor,
  buildProcessorFromEnv,
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

describe('one-dollar live smoke processor guardrails', () => {
  it('requires the explicit enable flag and exact confirmation phrase', () => {
    expect(() =>
      buildProcessorFromEnv({
        ARCHISYNAPSE_PROCESSOR: 'stripe_live_smoke',
        STRIPE_SECRET_KEY: 'sk_live_example',
      })
    ).toThrow(ProcessorConfigurationError);

    expect(() =>
      buildProcessorFromEnv({
        ARCHISYNAPSE_PROCESSOR: 'stripe_live_smoke',
        STRIPE_SECRET_KEY: 'sk_live_example',
        ARCHISYNAPSE_LIVE_SMOKE_TEST_ENABLED: 'true',
        ARCHISYNAPSE_LIVE_SMOKE_TEST_CONFIRM: 'wrong',
      })
    ).toThrow(ProcessorConfigurationError);
  });

  it('rejects test keys in live smoke mode', () => {
    expect(
      () => new StripeLiveSmokeProcessor({ secretKey: 'sk_test_not_live' })
    ).toThrow(ProcessorConfigurationError);
  });

  it('allows exactly one dollar USD with a token and smoke metadata', async () => {
    let seen: { url: string; init: Parameters<ProcessorFetch>[1] } | undefined;
    const processor = new StripeLiveSmokeProcessor({
      secretKey: 'sk_live_example',
      baseUrl: 'http://127.0.0.1:9999/v1',
      fetchImpl: async (url, init) => {
        seen = { url, init };
        return response(200, { id: 'pi_live_smoke', status: 'succeeded' });
      },
    });

    const result = await processor.charge({
      amountMinor: 100,
      currency: 'USD',
      paymentMethodToken: 'pm_live_tokenized',
      idempotencyKey: 'live-smoke-test-1',
      metadata: { live_smoke_test: true },
    });

    expect(result.status).toBe('succeeded');
    expect(seen?.init.body).toContain('amount=100');
    expect(seen?.init.body).toContain('currency=usd');
    expect(seen?.init.body).toContain('payment_method=pm_live_tokenized');
  });

  it('rejects every amount except one dollar', async () => {
    const processor = new StripeLiveSmokeProcessor({
      secretKey: 'sk_live_example',
      baseUrl: 'http://127.0.0.1:9999/v1',
      fetchImpl: async () => response(200, { id: 'pi_nope', status: 'succeeded' }),
    });

    await expect(
      processor.charge({
        amountMinor: 101,
        currency: 'USD',
        paymentMethodToken: 'pm_live_tokenized',
        idempotencyKey: 'live-smoke-test-2',
        metadata: { live_smoke_test: true },
      })
    ).rejects.toBeInstanceOf(ProcessorProtocolError);
  });

  it('rejects missing smoke metadata and bad idempotency prefixes', async () => {
    const processor = new StripeLiveSmokeProcessor({
      secretKey: 'sk_live_example',
      baseUrl: 'http://127.0.0.1:9999/v1',
      fetchImpl: async () => response(200, { id: 'pi_nope', status: 'succeeded' }),
    });

    await expect(
      processor.charge({
        amountMinor: 100,
        currency: 'USD',
        paymentMethodToken: 'pm_live_tokenized',
        idempotencyKey: 'ordinary-key',
        metadata: { live_smoke_test: true },
      })
    ).rejects.toBeInstanceOf(ProcessorProtocolError);

    await expect(
      processor.charge({
        amountMinor: 100,
        currency: 'USD',
        paymentMethodToken: 'pm_live_tokenized',
        idempotencyKey: 'live-smoke-test-3',
      })
    ).rejects.toBeInstanceOf(ProcessorProtocolError);
  });

  it('allows only a one-dollar refund with the refund prefix', async () => {
    let seen: { url: string; init: Parameters<ProcessorFetch>[1] } | undefined;
    const processor = new StripeLiveSmokeProcessor({
      secretKey: 'sk_live_example',
      baseUrl: 'http://127.0.0.1:9999/v1',
      fetchImpl: async (url, init) => {
        seen = { url, init };
        return response(200, { id: 're_live_smoke', status: 'succeeded' });
      },
    });

    const result = await processor.refund({
      processorTransactionId: 'pi_live_smoke',
      amountMinor: 100,
      reason: 'customer_requested',
      idempotencyKey: 'live-smoke-refund-test-1',
    });

    expect(result.status).toBe('succeeded');
    expect(seen?.init.body).toContain('amount=100');

    await expect(
      processor.refund({
        processorTransactionId: 'pi_live_smoke',
        amountMinor: 99,
        reason: 'customer_requested',
        idempotencyKey: 'live-smoke-refund-test-2',
      })
    ).rejects.toBeInstanceOf(ProcessorProtocolError);
  });
});
