import express, { Request, RequestHandler, Response, NextFunction } from 'express';
import { Decimal } from 'decimal.js';
import pino from 'pino';
import pinoHttp from 'pino-http';
import Joi from 'joi';

import { TransactionService } from './transaction-service-core';
import {
  ServiceAuthConfig,
  createServiceAuthMiddleware,
  serviceAuthFromEnv,
} from './service-auth';
import {
  PaymentMethodType,
  PaymentStatus,
  DuplicatePaymentError,
  PaymentNotFoundError,
  InsufficientFundsError,
} from './transaction-service-types';

// pino-http logs request headers. Never write the service token (or any
// other credential header) to the logs.
const logger = pino({
  redact: {
    paths: ['req.headers.authorization', 'req.headers.cookie', 'req.headers["x-api-key"]'],
    censor: '[redacted]',
  },
});
const app = express();

app.use(pinoHttp({ logger }));

// Service authentication runs first: the caller must prove which internal
// service it is before X-Organization-ID is trusted (service-auth.ts).
// Until initTransactionAPI configures it, every non-health request is refused.
// Covers the royalty routes too: they are mounted on this same app.
let serviceAuth: RequestHandler | null = null;
app.use((req: Request, res: Response, next: NextFunction) => {
  if (serviceAuth) {
    return serviceAuth(req, res, next);
  }
  if (req.path === '/health' || req.path === '/ready') {
    return next();
  }
  return res.status(503).json({ error: 'Service authentication not configured' });
});

// Bodies are parsed only after the caller is authenticated, so an
// unauthenticated request cannot make the service parse or log anything.
app.use(express.json());

// Organization context. Trusted only after service authentication above.
const authenticateOrg = (req: Request, res: Response, next: NextFunction) => {
  if (req.path === '/health' || req.path === '/ready') {
    return next();
  }
  const orgId = req.headers['x-organization-id'] as string;
  if (!orgId) {
    return res.status(401).json({ error: 'Missing X-Organization-ID header' });
  }
  // organization_id is TEXT in Postgres (migration 003 -- merchant/tenant
  // ids are strings like "mer_int_..." or "lyrica", not UUIDs). A plain
  // non-empty string check is a strict superset of the old UUID-only
  // check: every valid UUID still passes; royalty tenant ids now do too.
  const { error } = Joi.string().min(1).max(255).validate(orgId);
  if (error) {
    return res.status(400).json({ error: 'X-Organization-ID must be a non-empty string' });
  }
  (req as any).organizationId = orgId;
  next();
};

app.use(authenticateOrg);

export function initTransactionAPI(
  transactionService: TransactionService,
  options: { serviceAuth?: ServiceAuthConfig } = {}
) {
  serviceAuth = createServiceAuthMiddleware(options.serviceAuth ?? serviceAuthFromEnv());

  /**
   * POST /payments
   * Create and process a payment. Idempotent via required Idempotency-Key header
   * (falls back to idempotencyKey in body if header absent).
   */
  app.post('/payments', async (req: Request, res: Response) => {
    try {
      const schema = Joi.object({
        customerId: Joi.string().uuid().optional(),
        amount: Joi.string().regex(/^\d+(\.\d{1,4})?$/).required(),
        feeAmount: Joi.string().regex(/^\d+(\.\d{1,4})?$/).optional().default('0'),
        currency: Joi.string().length(3).default('USD'),
        paymentMethod: Joi.object({
          type: Joi.string().valid(...Object.values(PaymentMethodType)).required(),
          token: Joi.string().required(),
          last4: Joi.string().length(4).optional(),
          brand: Joi.string().optional(),
        }).required(),
        description: Joi.string().optional(),
        idempotencyKey: Joi.string().optional(),
        metadata: Joi.object().optional(),
      });

      const { error, value } = schema.validate(req.body);
      if (error) {
        return res.status(400).json({ error: error.details[0].message });
      }

      const idempotencyKey =
        (req.headers['idempotency-key'] as string) || value.idempotencyKey;

      if (!idempotencyKey) {
        return res.status(400).json({
          error: 'Idempotency-Key header (or idempotencyKey in body) is required',
        });
      }

      const payment = await transactionService.createPayment({
        organizationId: (req as any).organizationId,
        customerId: value.customerId,
        amount: new Decimal(value.amount),
        feeAmount: new Decimal(value.feeAmount),
        currency: value.currency,
        paymentMethod: value.paymentMethod,
        description: value.description,
        idempotencyKey,
        metadata: value.metadata,
      });

      const statusCode = payment.status === PaymentStatus.FAILED ? 402 : 201;
      res.status(statusCode).json(payment);
    } catch (err: any) {
      logger.error(err, 'Failed to create payment');

      if (err instanceof DuplicatePaymentError) {
        return res.status(409).json({ error: err.message });
      }

      res.status(500).json({ error: 'Internal server error' });
    }
  });

  /**
   * GET /payments/:id
   */
  app.get('/payments/:id', async (req: Request, res: Response) => {
    try {
      const payment = await transactionService.getPayment(
        (req as any).organizationId,
        req.params.id as string
      );
      res.json(payment);
    } catch (err: any) {
      if (err instanceof PaymentNotFoundError) {
        return res.status(404).json({ error: err.message });
      }
      logger.error(err, 'Failed to fetch payment');
      res.status(500).json({ error: 'Internal server error' });
    }
  });

  /**
   * GET /payments
   * List payments with cursor pagination.
   */
  app.get('/payments', async (req: Request, res: Response) => {
    try {
      const limit = req.query.limit ? parseInt(req.query.limit as string, 10) : undefined;
      const cursor = req.query.cursor as string | undefined;
      const status = req.query.status as PaymentStatus | undefined;

      const result = await transactionService.listPayments(
        (req as any).organizationId,
        { limit, cursor, status }
      );
      res.json(result);
    } catch (err) {
      logger.error(err, 'Failed to list payments');
      res.status(500).json({ error: 'Internal server error' });
    }
  });

  /**
   * POST /payments/:id/refund
   */
  app.post('/payments/:id/refund', async (req: Request, res: Response) => {
    try {
      const schema = Joi.object({
        amount: Joi.string().regex(/^\d+(\.\d{1,4})?$/).optional(),
        reason: Joi.string().required(),
      });

      const { error, value } = schema.validate(req.body);
      if (error) {
        return res.status(400).json({ error: error.details[0].message });
      }

      const idempotencyKey =
        (req.headers['idempotency-key'] as string) || `refund-${req.params.id as string}-${Date.now()}`;

      const refund = await transactionService.refundPayment({
        organizationId: (req as any).organizationId,
        paymentId: req.params.id as string,
        amount: value.amount ? new Decimal(value.amount) : undefined,
        reason: value.reason,
        idempotencyKey,
      });

      res.status(201).json(refund);
    } catch (err: any) {
      logger.error(err, 'Failed to refund payment');

      if (err instanceof PaymentNotFoundError) {
        return res.status(404).json({ error: err.message });
      }
      if (err instanceof InsufficientFundsError) {
        return res.status(400).json({ error: err.message });
      }

      res.status(500).json({ error: err.message || 'Internal server error' });
    }
  });

  /**
   * GET /reconciliation/unposted
   * Ops endpoint: find payments that succeeded at the processor but never
   * made it into the ledger. Poll this from a background job.
   */
  app.get('/reconciliation/unposted', async (req: Request, res: Response) => {
    try {
      const unposted = await transactionService.findUnpostedPayments(
        (req as any).organizationId
      );
      res.json({ count: unposted.length, payments: unposted });
    } catch (err) {
      logger.error(err, 'Failed to fetch unposted payments');
      res.status(500).json({ error: 'Internal server error' });
    }
  });

  app.get('/health', (_req: Request, res: Response) => {
    res.json({ status: 'healthy' });
  });

  app.use((err: any, _req: Request, res: Response, _next: NextFunction) => {
    logger.error(err, 'Unhandled error');
    res.status(500).json({ error: 'Internal server error' });
  });

  return app;
}

export default app;
