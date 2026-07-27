import { Pool, PoolClient } from 'pg';
import { Decimal } from 'decimal.js';
import { v4 as uuidv4 } from 'uuid';
import pino from 'pino';

import { RoyaltyLedgerClient } from './royalty-service-ledger-client';
import { computeRoyalty } from './royalty-money-math';
import {
  CreateRoyaltyObligationRequest,
  RoyaltyObligation,
  RoyaltyStatus,
  RoyaltyIdempotencyConflictError,
  RoyaltyObligationNotFoundError,
  RoyaltyInvalidStateError,
} from './royalty-service-types';

const logger = pino();

/**
 * RoyaltyService: owns the royalty obligation lifecycle and is the
 * SOLE caller of the ledger for this domain, mirroring the existing
 * card-payment TransactionService's relationship to the ledger
 * (transaction-service-core.ts line 36: "The Transaction Service is
 * the SOLE owner of ledger posting"). The gateway never posts here
 * directly and never invents transaction_id/ledger_transaction_id --
 * both come from this service.
 */
export class RoyaltyService {
  private pool: Pool;
  private ledgerClient: RoyaltyLedgerClient;

  constructor(pool: Pool, ledgerClient: RoyaltyLedgerClient) {
    this.pool = pool;
    this.ledgerClient = ledgerClient;
  }

  async createObligation(req: CreateRoyaltyObligationRequest): Promise<RoyaltyObligation> {
    const client = await this.pool.connect();
    try {
      await client.query('BEGIN');
      await client.query(
        `SELECT pg_advisory_xact_lock(hashtextextended($1, 0))`,
        [`royalty:${req.organizationId}:${req.eventId}`]
      );

      const existing = await client.query(
        `SELECT * FROM royalty_obligations
         WHERE organization_id = $1 AND idempotency_key = $2`,
        [req.organizationId, req.idempotencyKey]
      );
      if (existing.rows.length > 0) {
        const row = existing.rows[0];
        if (row.request_hash !== req.requestHash) {
          throw new RoyaltyIdempotencyConflictError(
            `idempotency_key ${req.idempotencyKey} reused with a different payload`
          );
        }
        await client.query('COMMIT');
        return this.loadObligation(client, row.id);
      }

      const capturedEvent = await client.query(
        `SELECT id FROM royalty_obligations
         WHERE organization_id = $1 AND event_id = $2`,
        [req.organizationId, req.eventId]
      );
      if (capturedEvent.rows.length > 0) {
        throw new RoyaltyIdempotencyConflictError(
          `event_id ${req.eventId} was already captured under a different idempotency key`
        );
      }

      let status: RoyaltyStatus;
      let ledgerTransactionId: string | null = null;
      let payouts: Array<{ ownerId: string; amount: Decimal }> = [];

      if (req.decision === 'block') {
        status = RoyaltyStatus.BLOCKED;
      } else if (req.decision === 'hold') {
        status = RoyaltyStatus.HELD;
        const ledgerTxn = await this.ledgerClient.postHold({
          organizationId: req.organizationId,
          eventId: req.eventId,
          correlationId: req.correlationId,
          idempotencyKey: req.idempotencyKey,
          gross: req.amount,
        });
        ledgerTransactionId = ledgerTxn.id;
      } else {
        const breakdown = computeRoyalty(req.amount, req.splits);
        payouts = breakdown.payouts;
        const ledgerTxn = await this.ledgerClient.postAllow({
          organizationId: req.organizationId,
          eventId: req.eventId,
          correlationId: req.correlationId,
          idempotencyKey: req.idempotencyKey,
          gross: req.amount,
          payouts: breakdown.payouts,
        });
        ledgerTransactionId = ledgerTxn.id;
        status = RoyaltyStatus.POSTED;
      }

      const id = uuidv4();
      await client.query(
        `INSERT INTO royalty_obligations
            (id, organization_id, event_id, correlation_id, idempotency_key, tenant_id,
             track_id, creator_id, trigger_kind, amount, currency, splits, status,
             decision_policy, risk_score, status_reasons, ledger_transaction_id,
             initial_ledger_transaction_id, request_hash)
           VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,$14,$15,$16,$17,$18,$19)`,
          [
            id,
            req.organizationId,
            req.eventId,
            req.correlationId,
            req.idempotencyKey,
            req.tenantId,
            req.trackId,
            req.creatorId,
            req.triggerKind,
            req.amount.toString(),
            req.currency,
            JSON.stringify(req.splits),
            status,
            req.decisionPolicy,
            req.riskScore,
            JSON.stringify(req.statusReasons),
            ledgerTransactionId,
            ledgerTransactionId,
            req.requestHash,
          ]
      );
      for (const payout of payouts) {
        await client.query(
          `INSERT INTO royalty_payouts (id, royalty_obligation_id, owner_id, amount, state)
             VALUES ($1,$2,$3,$4,'PAID')`,
          [uuidv4(), id, payout.ownerId, payout.amount.toString()]
        );
      }
      await client.query('COMMIT');

      return this.loadObligation(client, id);
    } catch (err) {
      await client.query('ROLLBACK').catch(() => undefined);
      throw err;
    } finally {
      client.release();
    }
  }

  async releaseObligation(
    organizationId: string,
    eventId: string,
    releaseIdempotencyKey: string
  ): Promise<RoyaltyObligation> {
    const client = await this.pool.connect();
    try {
      await client.query('BEGIN');
      const result = await client.query(
        `SELECT * FROM royalty_obligations
         WHERE organization_id = $1 AND event_id = $2 FOR UPDATE`,
        [organizationId, eventId]
      );
      if (result.rows.length === 0) {
        await client.query('ROLLBACK');
        throw new RoyaltyObligationNotFoundError(eventId);
      }
      const row = result.rows[0];

      const existingRelease = await client.query(
        `SELECT 1 FROM royalty_releases
         WHERE organization_id=$1 AND royalty_obligation_id=$2`,
        [organizationId, row.id]
      );
      if (existingRelease.rows.length > 0) {
        await client.query('COMMIT');
        return this.loadObligation(client, row.id);
      }
      if (row.status !== RoyaltyStatus.HELD) {
        await client.query('ROLLBACK');
        throw new RoyaltyInvalidStateError(`obligation ${eventId} is ${row.status}, not HELD`);
      }

      const splits = row.splits as Array<{ ownerId: string; bps: number }>;
      const breakdown = computeRoyalty(new Decimal(row.amount), splits);

      const ledgerTxn = await this.ledgerClient.postRelease({
        organizationId: row.organization_id,
        eventId: row.event_id,
        correlationId: row.correlation_id,
        idempotencyKey: releaseIdempotencyKey,
        gross: new Decimal(row.amount),
        payouts: breakdown.payouts,
      });

      await client.query(
        `UPDATE royalty_obligations
         SET status = 'POSTED',
             ledger_transaction_id = $2,
             release_ledger_transaction_id = $2
         WHERE id = $1`,
        [row.id, ledgerTxn.id]
      );
      await client.query(
        `INSERT INTO royalty_releases
          (id, organization_id, royalty_obligation_id, release_idempotency_key,
           release_ledger_transaction_id)
         VALUES ($1,$2,$3,$4,$5)`,
        [uuidv4(), organizationId, row.id, releaseIdempotencyKey, ledgerTxn.id]
      );
      for (const payout of breakdown.payouts) {
        await client.query(
          `INSERT INTO royalty_payouts (id, royalty_obligation_id, owner_id, amount, state)
           VALUES ($1,$2,$3,$4,'PAID')`,
          [uuidv4(), row.id, payout.ownerId, payout.amount.toString()]
        );
      }
      await client.query('COMMIT');

      return this.loadObligation(client, row.id);
    } catch (err) {
      await client.query('ROLLBACK').catch(() => undefined);
      throw err;
    } finally {
      client.release();
    }
  }

  async reverseObligation(
    organizationId: string,
    reversedEventId: string,
    reversalEventId: string,
    reversalIdempotencyKey: string,
    reason: string
  ): Promise<{ obligation: RoyaltyObligation; replayed: boolean }> {
    const client = await this.pool.connect();
    try {
      await client.query('BEGIN');

      const existingReversal = await client.query(
        `SELECT rr.*
         FROM royalty_reversals rr
         JOIN royalty_obligations ro ON ro.id = rr.reversed_obligation_id
         WHERE ro.organization_id = $1 AND rr.reversal_idempotency_key = $2`,
        [organizationId, reversalIdempotencyKey]
      );
      if (existingReversal.rows.length > 0) {
        const obligationRow = await client.query(
          `SELECT id FROM royalty_obligations WHERE id = $1`,
          [existingReversal.rows[0].reversed_obligation_id]
        );
        await client.query('COMMIT');
        return { obligation: await this.loadObligation(client, obligationRow.rows[0].id), replayed: true };
      }

      const result = await client.query(
        `SELECT * FROM royalty_obligations
         WHERE organization_id = $1 AND event_id = $2 FOR UPDATE`,
        [organizationId, reversedEventId]
      );
      if (result.rows.length === 0) {
        await client.query('ROLLBACK');
        throw new RoyaltyObligationNotFoundError(reversedEventId);
      }
      const row = result.rows[0];

      if (row.status === RoyaltyStatus.REVERSED) {
        await client.query('ROLLBACK');
        throw new RoyaltyInvalidStateError(`obligation ${reversedEventId} is already REVERSED`);
      }
      const ledgerTransactionIds = [
        row.initial_ledger_transaction_id || row.ledger_transaction_id,
        row.release_ledger_transaction_id,
      ].filter((value, index, values) => value && values.indexOf(value) === index);
      if (ledgerTransactionIds.length === 0) {
        await client.query('ROLLBACK');
        throw new RoyaltyInvalidStateError(
          `obligation ${reversedEventId} has no ledger transaction to reverse (status ${row.status})`
        );
      }

      const reversalTransactionIds: string[] = [];
      for (const [index, ledgerTransactionId] of ledgerTransactionIds.entries()) {
        const reversalTxn = await this.ledgerClient.postReversal(
          ledgerTransactionId,
          row.organization_id,
          reason,
          `${reversalIdempotencyKey}:${index}`
        );
        reversalTransactionIds.push(reversalTxn.id);
      }

      await client.query(`UPDATE royalty_obligations SET status = 'REVERSED' WHERE id = $1`, [row.id]);
      await client.query(
        `INSERT INTO royalty_reversals
          (id, organization_id, reversed_obligation_id, reversal_event_id,
           reversal_idempotency_key, reversal_ledger_transaction_id,
           reversal_ledger_transaction_ids)
         VALUES ($1,$2,$3,$4,$5,$6,$7)`,
        [
          uuidv4(),
          organizationId,
          row.id,
          reversalEventId,
          reversalIdempotencyKey,
          reversalTransactionIds[0],
          JSON.stringify(reversalTransactionIds),
        ]
      );
      await client.query('COMMIT');

      return { obligation: await this.loadObligation(client, row.id), replayed: false };
    } catch (err) {
      await client.query('ROLLBACK').catch(() => undefined);
      throw err;
    } finally {
      client.release();
    }
  }

  async getObligationByEventId(
    organizationId: string,
    eventId: string
  ): Promise<RoyaltyObligation> {
    const client = await this.pool.connect();
    try {
      const result = await client.query(
        `SELECT id FROM royalty_obligations
         WHERE organization_id = $1 AND event_id = $2`,
        [organizationId, eventId]
      );
      if (result.rows.length === 0) throw new RoyaltyObligationNotFoundError(eventId);
      return this.loadObligation(client, result.rows[0].id);
    } finally {
      client.release();
    }
  }

  private async loadObligation(client: PoolClient, id: string): Promise<RoyaltyObligation> {
    const result = await client.query(`SELECT * FROM royalty_obligations WHERE id = $1`, [id]);
    const row = result.rows[0];
    const payoutsResult = await client.query(
      `SELECT owner_id, amount, state FROM royalty_payouts WHERE royalty_obligation_id = $1`,
      [id]
    );
    return {
      id: row.id,
      organizationId: row.organization_id,
      eventId: row.event_id,
      correlationId: row.correlation_id,
      idempotencyKey: row.idempotency_key,
      tenantId: row.tenant_id,
      status: row.status,
      amount: new Decimal(row.amount),
      currency: row.currency,
      splits: row.splits,
      decisionPolicy: row.decision_policy,
      riskScore: row.risk_score === null ? null : Number(row.risk_score),
      statusReasons: row.status_reasons,
      ledgerTransactionId: row.ledger_transaction_id,
      payouts: payoutsResult.rows.map((p) => ({
        ownerId: p.owner_id,
        amount: new Decimal(p.amount),
        state: p.state,
      })),
      createdAt: row.created_at,
      updatedAt: row.updated_at,
    };
  }
}
