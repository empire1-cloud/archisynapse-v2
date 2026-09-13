import { describe, expect, it } from 'vitest';
import { Pool } from 'pg';

import { LedgerService } from './ledger-service-core';
import { DebitCredit } from './ledger-service-types';

/**
 * The double-entry invariant is the rule the whole payment system rests on:
 * a transaction may only post when its debits equal its credits, and a
 * zero-value transaction is not a transaction at all.
 *
 * `validateTransactionBalance` is the gate that enforces it. It is pure and
 * touches no database, so it is exercised here directly. The service holds the
 * pool only to hand it to query paths that these cases never reach.
 */
function balanceGate() {
  const service = new LedgerService({} as Pool);
  return (entries: Array<{ debitCredit: DebitCredit; amount: string | number }>): boolean =>
    (service as unknown as {
      validateTransactionBalance(entries: unknown[]): boolean;
    }).validateTransactionBalance(entries);
}

const debit = (amount: string | number) => ({ debitCredit: DebitCredit.DEBIT, amount });
const credit = (amount: string | number) => ({ debitCredit: DebitCredit.CREDIT, amount });

describe('double-entry balance validation', () => {
  const balances = balanceGate();

  it('accepts a simple two-sided entry', () => {
    expect(balances([debit('100.00'), credit('100.00')])).toBe(true);
  });

  it('accepts many debits against many credits when the totals agree', () => {
    expect(
      balances([debit('70.00'), debit('30.00'), credit('25.00'), credit('75.00')])
    ).toBe(true);
  });

  it('refuses a transaction whose debits exceed its credits', () => {
    expect(balances([debit('100.01'), credit('100.00')])).toBe(false);
  });

  it('refuses a transaction whose credits exceed its debits', () => {
    expect(balances([debit('100.00'), credit('100.01')])).toBe(false);
  });

  it('refuses a one-sided transaction', () => {
    expect(balances([debit('100.00')])).toBe(false);
    expect(balances([credit('100.00')])).toBe(false);
  });

  it('refuses an empty entry list', () => {
    expect(balances([])).toBe(false);
  });

  it('refuses a balanced transaction that moves no money', () => {
    // Debits equal credits, but posting zero would create an audit record
    // for an event that never happened.
    expect(balances([debit('0'), credit('0')])).toBe(false);
  });

  it('holds the invariant at a scale where float arithmetic would drift', () => {
    // 0.1 + 0.2 !== 0.3 in IEEE-754. Decimal keeps the books exact.
    expect(balances([debit('0.1'), debit('0.2'), credit('0.3')])).toBe(true);
  });

  it('keeps precision across many small entries', () => {
    const entries = [
      ...Array.from({ length: 10 }, () => debit('0.01')),
      credit('0.10'),
    ];
    expect(balances(entries)).toBe(true);
  });

  it('detects a shortfall smaller than a cent', () => {
    expect(balances([debit('100.000001'), credit('100.00')])).toBe(false);
  });

  it('reads numeric amounts as well as string amounts', () => {
    expect(balances([debit(100), credit(100)])).toBe(true);
    expect(balances([debit(100), credit(99)])).toBe(false);
  });
});
