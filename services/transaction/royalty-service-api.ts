import { Router, Request, Response } from 'express';
import { Decimal } from 'decimal.js';
import pino from 'pino';
import Joi from 'joi';

import { RoyaltyService } from './royalty-service-core';
import {
  RoyaltyIdempotencyConflictError,
  RoyaltyObligationNotFoundError,
  RoyaltyInvalidStateError,
} from './royalty-service-types';

const logger = pino();

const splitSchema = Joi.object({
  ownerId: Joi.string().required(),
  bps: Joi.number().integer().min(1).max(10000).required(),
});

const createSchema = Joi.object({
  eventId: Joi.string().required(),
  correlationId: Joi.string().required(),
  idempotencyKey: Joi.string().required(),
  tenantId: Joi.string().required(),
  trackId: Joi.string().required(),
  creatorId: Joi.string().required(),
  triggerKind: Joi.string().valid('play', 'remix', 'license').required(),
  amount: Joi.string().regex(/^\d+\.\d{4}$/).required(),
  currency: Joi.string().length(3).default('USD'),
  splits: Joi.array().items(splitSchema).min(1).required(),
  decision: Joi.string().valid('allow', 'hold', 'block').required(),
  decisionPolicy: Joi.string().required(),
  riskScore: Joi.number().min(0).max(1).required(),
  statusReasons: Joi.array().items(Joi.string()).default([]),
  requestHash: Joi.string().required(),
});

/**
 * initRoyaltyAPI: returns an Express Router mounted at the app root
 * (see royalty-service-index wiring in transaction-service-index.ts).
 * Shares the parent app's authenticateOrg middleware -- it does NOT
 * register its own auth, by design, so there is exactly one auth gate
 * for this service, not two divergent ones.
 */
export function initRoyaltyAPI(royaltyService: RoyaltyService): Router {
  const router = Router();

  router.post('/royalties', async (req: Request, res: Response) => {
    try {
      const { error, value } = createSchema.validate(req.body);
      if (error) {
        return res.status(400).json({ error: error.details[0].message });
      }

      const obligation = await royaltyService.createObligation({
        organizationId: (req as any).organizationId,
        eventId: value.eventId,
        correlationId: value.correlationId,
        idempotencyKey: value.idempotencyKey,
        tenantId: value.tenantId,
        trackId: value.trackId,
        creatorId: value.creatorId,
        triggerKind: value.triggerKind,
        amount: new Decimal(value.amount),
        currency: value.currency,
        splits: value.splits,
        decision: value.decision,
        decisionPolicy: value.decisionPolicy,
        riskScore: value.riskScore,
        statusReasons: value.statusReasons,
        requestHash: value.requestHash,
      });

      res.status(201).json(obligation);
    } catch (err: any) {
      if (err instanceof RoyaltyIdempotencyConflictError) {
        return res.status(409).json({ error: err.message, code: 'idempotency_conflict' });
      }
      logger.error(err, 'Failed to create royalty obligation');
      res.status(500).json({ error: 'Internal server error' });
    }
  });

  router.get('/royalties/:eventId', async (req: Request, res: Response) => {
    try {
      const obligation = await royaltyService.getObligationByEventId(req.params.eventId as string);
      res.json(obligation);
    } catch (err: any) {
      if (err instanceof RoyaltyObligationNotFoundError) {
        return res.status(404).json({ error: err.message });
      }
      logger.error(err, 'Failed to fetch royalty obligation');
      res.status(500).json({ error: 'Internal server error' });
    }
  });

  router.post('/royalties/:eventId/release', async (req: Request, res: Response) => {
    try {
      const idempotencyKey =
        (req.headers['idempotency-key'] as string) || `release-${req.params.eventId}`;
      const obligation = await royaltyService.releaseObligation(
        req.params.eventId as string,
        idempotencyKey
      );
      res.status(200).json(obligation);
    } catch (err: any) {
      if (err instanceof RoyaltyObligationNotFoundError) {
        return res.status(404).json({ error: err.message });
      }
      if (err instanceof RoyaltyInvalidStateError) {
        return res.status(409).json({ error: err.message, code: 'invalid_state' });
      }
      logger.error(err, 'Failed to release royalty obligation');
      res.status(500).json({ error: 'Internal server error' });
    }
  });

  router.post('/royalties/:eventId/reverse', async (req: Request, res: Response) => {
    try {
      const schema = Joi.object({
        reversalEventId: Joi.string().required(),
        reversalIdempotencyKey: Joi.string().required(),
        reason: Joi.string().required(),
      });
      const { error, value } = schema.validate(req.body);
      if (error) {
        return res.status(400).json({ error: error.details[0].message });
      }

      const { obligation, replayed } = await royaltyService.reverseObligation(
        req.params.eventId as string,
        value.reversalEventId,
        value.reversalIdempotencyKey,
        value.reason
      );
      res.status(replayed ? 200 : 201).json(obligation);
    } catch (err: any) {
      if (err instanceof RoyaltyObligationNotFoundError) {
        return res.status(404).json({ error: err.message });
      }
      if (err instanceof RoyaltyInvalidStateError) {
        return res.status(409).json({ error: err.message, code: 'invalid_state' });
      }
      logger.error(err, 'Failed to reverse royalty obligation');
      res.status(500).json({ error: 'Internal server error' });
    }
  });

  return router;
}
