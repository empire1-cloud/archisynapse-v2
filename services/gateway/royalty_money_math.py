"""
Money math for the Lyrica royalty receipt loop — see spec/SPEC-royalty-loop-v1.md §4.

Pure functions only: no I/O, no FastAPI, no gateway/orchestrator imports.
All arithmetic happens in Decimal; amounts are formatted as fixed-point
strings with exactly 4 decimal places to match the receipt schema and
the v1 monolith's BigInt ledger units (value * 10000).

`amount.value` on the incoming event IS the creator royalty pool, in
full. Archisynapse deducts nothing from it — there is no platform fee
in this loop. Empire's separate 70/30 platform-revenue-share policy
governs a different pricing contract and must not be applied here
unless a distinct, founder-approved contract says otherwise.
"""

from decimal import Decimal, ROUND_DOWN
from typing import Iterable, TypedDict

CENT = Decimal("0.01")
LEDGER_PLACES = Decimal("0.0001")
BPS_DENOMINATOR = Decimal(10000)


class Split(TypedDict):
    owner_id: str
    bps: int


class Payout(TypedDict):
    owner_id: str
    amount: Decimal


class RoyaltyBreakdown(TypedDict):
    gross: Decimal
    platform_fee: Decimal
    net: Decimal
    payouts: list[Payout]


def format_ledger_amount(amount: Decimal) -> str:
    """Fixed-point string, exactly 4 decimal places (matches amount.value)."""
    return str(amount.quantize(LEDGER_PLACES))


def distribute_splits(pool: Decimal, splits: Iterable[Split]) -> list[Payout]:
    """
    Floor each owner's share of the pool to the cent, then hand out the
    leftover cents one at a time by largest fractional remainder. Ties
    break by larger bps, then lexicographically smaller owner_id —
    deterministic, same event always yields the same cents. Splits that
    divide evenly (e.g. 60/40 of $1.25) leave nothing to distribute.
    """
    splits = list(splits)
    total_bps = sum(s["bps"] for s in splits)
    if total_bps != 10000:
        raise ValueError(f"splits[].bps must sum to exactly 10000, got {total_bps}")

    raw_shares = [
        (s["owner_id"], s["bps"], pool * Decimal(s["bps"]) / BPS_DENOMINATOR)
        for s in splits
    ]
    floored = {
        owner_id: raw.quantize(CENT, rounding=ROUND_DOWN)
        for owner_id, _, raw in raw_shares
    }
    remainders = {owner_id: raw - floored[owner_id] for owner_id, _, raw in raw_shares}
    bps_by_owner = {owner_id: bps for owner_id, bps, _ in raw_shares}

    pool_cents = int((pool / CENT).to_integral_value(rounding=ROUND_DOWN))
    floor_cents = int(sum(floored.values()) / CENT)
    leftover_cents = pool_cents - floor_cents

    ordering = sorted(
        (s["owner_id"] for s in splits),
        key=lambda owner_id: (-remainders[owner_id], -bps_by_owner[owner_id], owner_id),
    )
    for owner_id in ordering[:leftover_cents]:
        floored[owner_id] += CENT

    return [{"owner_id": s["owner_id"], "amount": floored[s["owner_id"]]} for s in splits]


def compute_royalty(gross: Decimal, splits: Iterable[Split]) -> RoyaltyBreakdown:
    """
    Full breakdown for one royalty obligation. `gross` is the creator
    payout pool in full — no fee is deducted, `net` equals `gross`, and
    `platform_fee` is always zero (kept in the return shape for schema
    stability, not because any fee applies). Raises AssertionError if
    the balanced-journal invariant (sum(payouts) == pool) fails — that
    must never happen for valid inputs and should hard-fail before any
    ledger write is attempted.
    """
    pool = gross.quantize(CENT)
    payouts = distribute_splits(pool, splits)

    total_payout = sum((p["amount"] for p in payouts), Decimal("0"))
    if total_payout != pool:
        raise AssertionError(
            f"balanced-journal invariant violated: payouts({total_payout}) != pool({pool})"
        )

    return {"gross": gross, "platform_fee": Decimal("0.00"), "net": gross, "payouts": payouts}
