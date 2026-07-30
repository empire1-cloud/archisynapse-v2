export type ProcessorMode = 'disabled' | 'test' | 'live_smoke';
export type ProcessorStatus = 'succeeded' | 'processing' | 'requires_action' | 'failed';

export interface ProcessorChargeRequest {
  amountMinor: number;
  currency: string;
  paymentMethodToken: string;
  description?: string;
  idempotencyKey: string;
  metadata?: Record<string, unknown>;
}

export interface ProcessorChargeResult {
  status: ProcessorStatus;
  processorTransactionId?: string;
  failureCode?: string;
  failureMessage?: string;
  rawStatus?: string;
}

export interface ProcessorRefundRequest {
  processorTransactionId: string;
  amountMinor: number;
  reason: string;
  idempotencyKey: string;
}

export interface ProcessorRefundResult {
  status: 'succeeded' | 'pending' | 'failed';
  processorRefundId?: string;
  failureCode?: string;
  failureMessage?: string;
  rawStatus?: string;
}

export interface ProcessorHealth {
  provider: string;
  mode: ProcessorMode;
  configured: boolean;
}

export interface PaymentProcessor {
  readonly name: string;
  readonly mode: ProcessorMode;
  charge(request: ProcessorChargeRequest): Promise<ProcessorChargeResult>;
  refund(request: ProcessorRefundRequest): Promise<ProcessorRefundResult>;
  health(): ProcessorHealth;
}

export class ProcessorNotConfiguredError extends Error {
  constructor(message = 'payment processor is not configured') {
    super(message);
    this.name = 'ProcessorNotConfiguredError';
  }
}

export class ProcessorProtocolError extends Error {
  constructor(message: string) {
    super(message);
    this.name = 'ProcessorProtocolError';
  }
}

export class ProcessorConfigurationError extends Error {
  constructor(message: string) {
    super(message);
    this.name = 'ProcessorConfigurationError';
  }
}

interface FetchResponse {
  ok: boolean;
  status: number;
  json(): Promise<unknown>;
  text(): Promise<string>;
}

export type ProcessorFetch = (
  input: string,
  init: {
    method: string;
    headers: Record<string, string>;
    body?: string;
  }
) => Promise<FetchResponse>;

function defaultFetch(input: string, init: Parameters<ProcessorFetch>[1]): Promise<FetchResponse> {
  const runtimeFetch = (globalThis as unknown as { fetch?: ProcessorFetch }).fetch;
  if (!runtimeFetch) {
    throw new ProcessorConfigurationError('global fetch is unavailable');
  }
  return runtimeFetch(input, init);
}

export class DisabledProcessor implements PaymentProcessor {
  readonly name = 'disabled';
  readonly mode: ProcessorMode = 'disabled';

  async charge(_request: ProcessorChargeRequest): Promise<ProcessorChargeResult> {
    throw new ProcessorNotConfiguredError();
  }

  async refund(_request: ProcessorRefundRequest): Promise<ProcessorRefundResult> {
    throw new ProcessorNotConfiguredError();
  }

  health(): ProcessorHealth {
    return { provider: this.name, mode: this.mode, configured: false };
  }
}

interface StripeProcessorOptions {
  secretKey: string;
  baseUrl?: string;
  fetchImpl?: ProcessorFetch;
}

abstract class StripeProcessorBase implements PaymentProcessor {
  readonly name = 'stripe';
  abstract readonly mode: ProcessorMode;

  private readonly secretKey: string;
  private readonly baseUrl: string;
  private readonly fetchImpl: ProcessorFetch;

  protected constructor(options: StripeProcessorOptions, expectedPrefix: 'sk_test_' | 'sk_live_') {
    if (!options.secretKey.startsWith(expectedPrefix)) {
      throw new ProcessorConfigurationError(
        `Stripe ${expectedPrefix === 'sk_test_' ? 'test' : 'live smoke'} adapter requires ${expectedPrefix} keys`
      );
    }
    this.secretKey = options.secretKey;
    this.baseUrl = (options.baseUrl || 'https://api.stripe.com/v1').replace(/\/$/, '');
    if (!this.baseUrl.startsWith('https://') && !this.baseUrl.startsWith('http://127.0.0.1')) {
      throw new ProcessorConfigurationError(
        'processor base URL must use HTTPS (localhost is allowed for tests)'
      );
    }
    this.fetchImpl = options.fetchImpl || defaultFetch;
  }

  health(): ProcessorHealth {
    return { provider: this.name, mode: this.mode, configured: true };
  }

  protected validateCharge(_request: ProcessorChargeRequest): void {}
  protected validateRefund(_request: ProcessorRefundRequest): void {}

  async charge(request: ProcessorChargeRequest): Promise<ProcessorChargeResult> {
    validateMinorAmount(request.amountMinor);
    validateCurrency(request.currency);
    validateToken(request.paymentMethodToken);
    validateIdempotencyKey(request.idempotencyKey);
    this.validateCharge(request);

    const body = new URLSearchParams();
    body.set('amount', String(request.amountMinor));
    body.set('currency', request.currency.toLowerCase());
    body.set('payment_method', request.paymentMethodToken);
    body.set('confirm', 'true');
    body.set('automatic_payment_methods[enabled]', 'true');
    body.set('automatic_payment_methods[allow_redirects]', 'never');
    if (request.description) body.set('description', request.description);
    appendMetadata(body, request.metadata);

    const payload = await this.post('/payment_intents', body, request.idempotencyKey);
    const id = stringField(payload, 'id');
    const status = stringField(payload, 'status');

    switch (status) {
      case 'succeeded':
        return { status: 'succeeded', processorTransactionId: id, rawStatus: status };
      case 'processing':
        return { status: 'processing', processorTransactionId: id, rawStatus: status };
      case 'requires_action':
      case 'requires_confirmation':
      case 'requires_capture':
        return { status: 'requires_action', processorTransactionId: id, rawStatus: status };
      default:
        return {
          status: 'failed',
          processorTransactionId: id,
          rawStatus: status,
          failureCode: nestedString(payload, ['last_payment_error', 'code']),
          failureMessage:
            nestedString(payload, ['last_payment_error', 'message']) ||
            `processor returned status ${status || 'unknown'}`,
        };
    }
  }

  async refund(request: ProcessorRefundRequest): Promise<ProcessorRefundResult> {
    validateMinorAmount(request.amountMinor);
    validateIdempotencyKey(request.idempotencyKey);
    this.validateRefund(request);
    if (!request.processorTransactionId.startsWith('pi_')) {
      throw new ProcessorProtocolError('Stripe refund requires a PaymentIntent id (pi_)');
    }

    const body = new URLSearchParams();
    body.set('payment_intent', request.processorTransactionId);
    body.set('amount', String(request.amountMinor));
    body.set('reason', normalizeRefundReason(request.reason));

    const payload = await this.post('/refunds', body, request.idempotencyKey);
    const id = stringField(payload, 'id');
    const status = stringField(payload, 'status');
    if (status === 'succeeded') {
      return { status: 'succeeded', processorRefundId: id, rawStatus: status };
    }
    if (status === 'pending' || status === 'requires_action') {
      return { status: 'pending', processorRefundId: id, rawStatus: status };
    }
    return {
      status: 'failed',
      processorRefundId: id,
      rawStatus: status,
      failureCode: stringField(payload, 'failure_reason') || undefined,
      failureMessage: `processor refund returned status ${status || 'unknown'}`,
    };
  }

  private async post(
    path: string,
    body: URLSearchParams,
    idempotencyKey: string
  ): Promise<Record<string, unknown>> {
    let response: FetchResponse;
    try {
      response = await this.fetchImpl(`${this.baseUrl}${path}`, {
        method: 'POST',
        headers: {
          Authorization: `Bearer ${this.secretKey}`,
          'Content-Type': 'application/x-www-form-urlencoded',
          'Idempotency-Key': idempotencyKey,
        },
        body: body.toString(),
      });
    } catch (error) {
      throw new ProcessorProtocolError(
        `processor request failed: ${error instanceof Error ? error.message : String(error)}`
      );
    }

    const text = await response.text();
    let payload: unknown;
    try {
      payload = text ? JSON.parse(text) : await response.json();
    } catch {
      throw new ProcessorProtocolError(
        `processor returned invalid JSON (HTTP ${response.status})`
      );
    }
    if (!isRecord(payload)) {
      throw new ProcessorProtocolError(
        `processor returned a non-object response (HTTP ${response.status})`
      );
    }
    if (!response.ok) {
      const message =
        nestedString(payload, ['error', 'message']) || `processor HTTP ${response.status}`;
      const code = nestedString(payload, ['error', 'code']);
      throw new ProcessorProtocolError(code ? `${code}: ${message}` : message);
    }
    return payload;
  }
}

export interface StripeTestProcessorOptions extends StripeProcessorOptions {}

export class StripeTestProcessor extends StripeProcessorBase {
  readonly mode: ProcessorMode = 'test';

  constructor(options: StripeTestProcessorOptions) {
    super(options, 'sk_test_');
  }
}

export interface StripeLiveSmokeProcessorOptions extends StripeProcessorOptions {}

export class StripeLiveSmokeProcessor extends StripeProcessorBase {
  readonly mode: ProcessorMode = 'live_smoke';

  constructor(options: StripeLiveSmokeProcessorOptions) {
    super(options, 'sk_live_');
  }

  protected validateCharge(request: ProcessorChargeRequest): void {
    if (request.amountMinor !== 100 || request.currency.toUpperCase() !== 'USD') {
      throw new ProcessorProtocolError(
        'live smoke mode permits exactly 100 cents USD and no other amount or currency'
      );
    }
    if (!request.idempotencyKey.startsWith('live-smoke-')) {
      throw new ProcessorProtocolError(
        'live smoke mode requires an Idempotency-Key beginning with live-smoke-'
      );
    }
    if (request.metadata?.live_smoke_test !== true) {
      throw new ProcessorProtocolError(
        'live smoke mode requires metadata.live_smoke_test=true'
      );
    }
  }

  protected validateRefund(request: ProcessorRefundRequest): void {
    if (request.amountMinor !== 100) {
      throw new ProcessorProtocolError('live smoke refunds must be exactly 100 cents');
    }
    if (!request.idempotencyKey.startsWith('live-smoke-refund-')) {
      throw new ProcessorProtocolError(
        'live smoke refund idempotency keys must begin with live-smoke-refund-'
      );
    }
  }
}

export function buildProcessorFromEnv(
  env: Record<string, string | undefined> = process.env
): PaymentProcessor {
  const provider = (env.ARCHISYNAPSE_PROCESSOR || 'disabled').trim().toLowerCase();
  if (provider === 'disabled' || provider === '') return new DisabledProcessor();

  const secretKey = (env.STRIPE_SECRET_KEY || '').trim();
  if (!secretKey) {
    throw new ProcessorConfigurationError('STRIPE_SECRET_KEY is required');
  }

  if (provider === 'stripe_test') {
    return new StripeTestProcessor({
      secretKey,
      baseUrl: env.STRIPE_API_BASE_URL || undefined,
    });
  }

  if (provider === 'stripe_live_smoke') {
    if ((env.ARCHISYNAPSE_LIVE_SMOKE_TEST_ENABLED || '').trim().toLowerCase() !== 'true') {
      throw new ProcessorConfigurationError(
        'stripe_live_smoke requires ARCHISYNAPSE_LIVE_SMOKE_TEST_ENABLED=true'
      );
    }
    if (
      (env.ARCHISYNAPSE_LIVE_SMOKE_TEST_CONFIRM || '').trim() !==
      'CHARGE_AND_REFUND_ONE_USD'
    ) {
      throw new ProcessorConfigurationError(
        'stripe_live_smoke requires the exact live smoke confirmation phrase'
      );
    }
    return new StripeLiveSmokeProcessor({
      secretKey,
      baseUrl: env.STRIPE_API_BASE_URL || undefined,
    });
  }

  throw new ProcessorConfigurationError(`unsupported processor adapter: ${provider}`);
}

export function decimalStringToMinorUnits(amount: string, currency: string): number {
  validateCurrency(currency);
  if (!/^\d+(\.\d{1,2})?$/.test(amount)) {
    throw new ProcessorProtocolError(
      'processor proof lane currently accepts positive amounts with at most two decimal places'
    );
  }
  const [whole, fraction = ''] = amount.split('.');
  const value = Number(whole) * 100 + Number((fraction + '00').slice(0, 2));
  validateMinorAmount(value);
  return value;
}

function validateMinorAmount(value: number): void {
  if (!Number.isSafeInteger(value) || value <= 0) {
    throw new ProcessorProtocolError(
      'processor amount must be a positive safe integer in minor units'
    );
  }
}

function validateCurrency(value: string): void {
  if (!/^[A-Z]{3}$/i.test(value)) {
    throw new ProcessorProtocolError('currency must be a three-letter code');
  }
}

function validateToken(value: string): void {
  if (!/^pm_[A-Za-z0-9_]+$/.test(value)) {
    throw new ProcessorProtocolError(
      'processor lanes accept tokenized PaymentMethod ids only (pm_); raw card data is forbidden'
    );
  }
}

function validateIdempotencyKey(value: string): void {
  if (!value || value.length > 255) {
    throw new ProcessorProtocolError('processor idempotency key must be 1-255 characters');
  }
}

function appendMetadata(body: URLSearchParams, metadata?: Record<string, unknown>): void {
  if (!metadata) return;
  for (const [key, value] of Object.entries(metadata)) {
    if (!/^[A-Za-z0-9_.-]{1,40}$/.test(key)) continue;
    if (!['string', 'number', 'boolean'].includes(typeof value)) continue;
    body.set(`metadata[archisynapse_${key}]`, String(value).slice(0, 500));
  }
}

function normalizeRefundReason(reason: string): string {
  const normalized = reason.trim().toLowerCase();
  if (normalized === 'duplicate') return 'duplicate';
  if (normalized === 'fraudulent' || normalized === 'fraud') return 'fraudulent';
  return 'requested_by_customer';
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === 'object' && value !== null && !Array.isArray(value);
}

function stringField(value: Record<string, unknown>, key: string): string {
  return typeof value[key] === 'string' ? (value[key] as string) : '';
}

function nestedString(value: Record<string, unknown>, path: string[]): string {
  let current: unknown = value;
  for (const key of path) {
    if (!isRecord(current)) return '';
    current = current[key];
  }
  return typeof current === 'string' ? current : '';
}
