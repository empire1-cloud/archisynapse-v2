import express, { Request, Response, NextFunction } from 'express';
import { Decimal } from 'decimal.js';
import pino from 'pino';
import pinoHttp from 'pino-http';
import Joi from 'joi';

import { LedgerService } from './ledger-service-core';
import {
  AccountType,
  DebitCredit,
  PostJournalEntryRequest,
  PostTransactionRequest,
  TransactionType,
} from './ledger-service-types';

const logger = pino();
const app = express();

// Middleware
app.use(express.json());
app.use(pinoHttp({ logger }));

// Auth middleware (stub: replace with real auth)
const authenticateOrg = (req: Request, res: Response, next: NextFunction) => {
  if (req.path === '/health' || req.path === '/ready') {
    return next();
  }
  const orgId = req.headers['x-organization-id'] as string;
  if (!orgId) {
    return res.status(401).json({ error: 'Missing X-Organization-ID header' });
  }
  (req as any).organizationId = orgId;
  next();
};

app.use(authenticateOrg);

/**
 * Initialize ledger service (inject your DB pool here)
 */
export function initLedgerAPI(ledgerService: LedgerService) {
  app.get('/accounts', async (req: Request, res: Response) => {
    try {
      const accounts = await ledgerService.listAccounts((req as any).organizationId);
      res.json(accounts);
    } catch (err) {
      logger.error(err, 'Failed to list accounts');
      res.status(500).json({ error: 'Internal server error' });
    }
  });

  /**
   * POST /accounts
   * Create a new account in the chart of accounts
   */
  app.post('/accounts', async (req: Request, res: Response) => {
    try {
      const schema = Joi.object({
        code: Joi.string().max(20).required(),
        name: Joi.string().max(255).required(),
        type: Joi.string()
          .valid(...Object.values(AccountType))
          .required(),
        currency: Joi.string().length(3).default('USD'),
        metadata: Joi.object().optional(),
      });

      const { error, value } = schema.validate(req.body);
      if (error) {
        return res.status(400).json({ error: error.details[0].message });
      }

      const account = await ledgerService.createAccount(
        (req as any).organizationId,
        value.code,
        value.name,
        value.type,
        value.currency,
        value.metadata
      );

      res.status(201).json(account);
    } catch (err) {
      logger.error(err, 'Failed to create account');
      res.status(500).json({ error: 'Internal server error' });
    }
  });

  /**
   * POST /transactions
   * Post a transaction to the ledger
   * 
   * Example request:
   * {
   *   "type": "PAYMENT",
   *   "description": "Customer payment",
   *   "amount": "1000.00",
   *   "currency": "USD",
   *   "idempotencyKey": "unique-key-123",
   *   "entries": [
   *     {
   *       "accountId": "cash-account-uuid",
   *       "debitCredit": "DEBIT",
   *       "amount": "1000.00",
   *       "description": "Cash received"
   *     },
   *     {
   *       "accountId": "revenue-account-uuid",
   *       "debitCredit": "CREDIT",
   *       "amount": "1000.00",
   *       "description": "Revenue recognized"
   *     }
   *   ]
   * }
   */
  app.post('/transactions', async (req: Request, res: Response) => {
    try {
      const entrySchema = Joi.object({
        accountId: Joi.string().uuid().required(),
        debitCredit: Joi.string().valid('DEBIT', 'CREDIT').required(),
        amount: Joi.string().regex(/^\d+(\.\d{1,4})?$/).required(),
        description: Joi.string().required(),
        metadata: Joi.object().optional(),
      });

      const schema = Joi.object({
        type: Joi.string()
          .valid(...Object.values(TransactionType))
          .required(),
        referenceId: Joi.string().optional(),
        description: Joi.string().required(),
        amount: Joi.string().regex(/^\d+(\.\d{1,4})?$/).required(),
        currency: Joi.string().length(3).default('USD'),
        idempotencyKey: Joi.string().optional(),
        entries: Joi.array().items(entrySchema).min(2).required(),
        metadata: Joi.object().optional(),
      });

      const { error, value } = schema.validate(req.body);
      if (error) {
        return res.status(400).json({ error: error.details[0].message });
      }

      // Convert string amounts to Decimal
      const entries: PostJournalEntryRequest[] = value.entries.map((e: any) => ({
        ...e,
        amount: new Decimal(e.amount),
      }));

      const txnReq: PostTransactionRequest = {
        organizationId: (req as any).organizationId,
        type: value.type,
        referenceId: value.referenceId,
        description: value.description,
        amount: new Decimal(value.amount),
        currency: value.currency,
        entries,
        idempotencyKey: value.idempotencyKey,
        metadata: value.metadata,
      };

      const transaction = await ledgerService.postTransaction(txnReq);
      res.status(201).json(transaction);
    } catch (err: any) {
      logger.error(err, 'Failed to post transaction');

      // Return 400 for validation errors
      if (err.message.includes('does not balance') || err.message.includes('not found')) {
        return res.status(400).json({ error: err.message });
      }

      res.status(500).json({ error: 'Internal server error' });
    }
  });

  /**
   * POST /transactions/:id/reverse
   * Reverse (refund, chargeback) a transaction
   */
  app.post('/transactions/:id/reverse', async (req: Request, res: Response) => {
    try {
      const schema = Joi.object({
        reason: Joi.string().required(),
        idempotencyKey: Joi.string().optional(),
      });

      const { error, value } = schema.validate(req.body);
      if (error) {
        return res.status(400).json({ error: error.details[0].message });
      }

      const reversedTxn = await ledgerService.reverseTransaction(
        (req as any).organizationId,
        req.params.id as string,
        value.reason,
        value.idempotencyKey
      );

      res.status(201).json(reversedTxn);
    } catch (err: any) {
      logger.error(err, 'Failed to reverse transaction');

      if (err.message.includes('not found')) {
        return res.status(404).json({ error: err.message });
      }

      res.status(500).json({ error: 'Internal server error' });
    }
  });

  app.get('/transactions/:id', async (req: Request, res: Response) => {
    try {
      const transaction = await ledgerService.getTransaction(
        (req as any).organizationId,
        req.params.id as string
      );
      res.json(transaction);
    } catch (err: any) {
      logger.error(err, 'Failed to fetch transaction');
      if (err.message.includes('not found')) {
        return res.status(404).json({ error: err.message });
      }
      res.status(500).json({ error: 'Internal server error' });
    }
  });

  /**
   * GET /trial-balance
   * Get the trial balance (all accounts with debit/credit sums)
   */
  app.get('/trial-balance', async (req: Request, res: Response) => {
    try {
      const trialBalance = await ledgerService.getTrialBalance((req as any).organizationId);
      const totalBalance = trialBalance.reduce(
        (sum, tb) => sum.plus(tb.balance),
        new Decimal(0)
      );
      res.json({
        asOf: new Date(),
        accounts: trialBalance,
        isBalanced: totalBalance.equals(0),
      });
    } catch (err) {
      logger.error(err, 'Failed to fetch trial balance');
      res.status(500).json({ error: 'Internal server error' });
    }
  });

  /**
   * GET /reconciliation
   * Reconcile the ledger and return discrepancies (if any)
   */
  app.get('/reconciliation', async (req: Request, res: Response) => {
    try {
      const result = await ledgerService.reconcile((req as any).organizationId);
      res.json(result);
    } catch (err) {
      logger.error(err, 'Failed to reconcile ledger');
      res.status(500).json({ error: 'Internal server error' });
    }
  });

  /**
   * Health check
   */
  app.get('/health', (_req: Request, res: Response) => {
    res.json({ status: 'healthy' });
  });

  /**
   * Error handler
   */
  app.use((err: any, _req: Request, res: Response, _next: NextFunction) => {
    logger.error(err, 'Unhandled error');
    res.status(500).json({ error: 'Internal server error' });
  });

  return app;
}

export default app;
