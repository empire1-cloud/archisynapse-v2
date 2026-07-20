import { Decimal } from 'decimal.js';

/**
 * Money math for the Lyrica royalty receipt loop -- see
 * spec/SPEC-royalty-loop-v1.md §4. Owned by the transaction service
 * (which owns ledger posting), not the gateway -- the gateway only
 * relays what this module (via royalty-service-core.ts) computes.
 *
 * The pool (event.amount.value) belongs to the creator(s) in full.
 * There is no platform fee in this loop.
 */

export interface Split {
  ownerId: string;
  bps: number;
}

export interface Payout {
  ownerId: string;
  amount: Decimal;
}

const CENT = new Decimal('0.01');
const BPS_DENOMINATOR = new Decimal(10000);

export function distributeSplits(pool: Decimal, splits: Split[]): Payout[] {
  const totalBps = splits.reduce((sum, s) => sum + s.bps, 0);
  if (totalBps !== 10000) {
    throw new Error(`splits[].bps must sum to exactly 10000, got ${totalBps}`);
  }

  const raw = splits.map((s) => ({
    ownerId: s.ownerId,
    bps: s.bps,
    raw: pool.mul(s.bps).div(BPS_DENOMINATOR),
  }));

  const floored = new Map<string, Decimal>();
  const remainder = new Map<string, Decimal>();
  const bpsByOwner = new Map<string, number>();
  for (const r of raw) {
    const f = r.raw.toDecimalPlaces(2, Decimal.ROUND_DOWN);
    floored.set(r.ownerId, f);
    remainder.set(r.ownerId, r.raw.minus(f));
    bpsByOwner.set(r.ownerId, r.bps);
  }

  const poolCents = pool.div(CENT).toDecimalPlaces(0, Decimal.ROUND_DOWN);
  let floorCentsTotal = new Decimal(0);
  for (const f of floored.values()) {
    floorCentsTotal = floorCentsTotal.plus(f.div(CENT));
  }
  const leftoverCents = poolCents.minus(floorCentsTotal).toNumber();

  const ordering = splits
    .map((s) => s.ownerId)
    .sort((a, b) => {
      const remainderCompare = remainder.get(b)!.comparedTo(remainder.get(a)!); // descending remainder
      if (remainderCompare !== 0) return remainderCompare;
      const bpsCompare = bpsByOwner.get(b)! - bpsByOwner.get(a)!; // descending bps
      if (bpsCompare !== 0) return bpsCompare;
      return a < b ? -1 : a > b ? 1 : 0; // ascending owner_id
    });

  for (let i = 0; i < leftoverCents; i++) {
    const ownerId = ordering[i];
    floored.set(ownerId, floored.get(ownerId)!.plus(CENT));
  }

  return splits.map((s) => ({ ownerId: s.ownerId, amount: floored.get(s.ownerId)! }));
}

export interface RoyaltyBreakdown {
  gross: Decimal;
  platformFee: Decimal;
  net: Decimal;
  payouts: Payout[];
}

export function computeRoyalty(gross: Decimal, splits: Split[]): RoyaltyBreakdown {
  const pool = gross.toDecimalPlaces(2);
  const payouts = distributeSplits(pool, splits);

  const totalPayout = payouts.reduce((sum, p) => sum.plus(p.amount), new Decimal(0));
  if (!totalPayout.equals(pool)) {
    throw new Error(`balanced-journal invariant violated: payouts(${totalPayout}) != pool(${pool})`);
  }

  return { gross, platformFee: new Decimal('0.00'), net: gross, payouts };
}
