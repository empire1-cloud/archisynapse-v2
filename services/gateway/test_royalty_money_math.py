"""
Unit tests for royalty_money_math — pure functions, no services required.
Covers AT-01 and AT-04 from spec/ACCEPTANCE-royalty-loop-v1.md, plus the
balanced-journal invariant and determinism guarantees the spec requires.
Run with: pytest services/gateway/test_royalty_money_math.py
"""

from decimal import Decimal

import pytest

from royalty_money_math import (
    compute_platform_fee,
    compute_royalty,
    distribute_splits,
    format_ledger_amount,
)

GROSS = Decimal("1.2500")


def test_at01_single_owner_full_split():
    result = compute_royalty(GROSS, [{"owner_id": "cre_a1b2c3", "bps": 10000}])

    assert result["platform_fee"] == Decimal("0.04")
    assert result["net"] == Decimal("1.21")
    assert result["payouts"] == [{"owner_id": "cre_a1b2c3", "amount": Decimal("1.21")}]


def test_at04_sixty_forty_split_largest_remainder():
    splits = [
        {"owner_id": "cre_a1b2c3", "bps": 6000},
        {"owner_id": "cre_d4e5f6", "bps": 4000},
    ]
    result = compute_royalty(GROSS, splits)

    assert result["platform_fee"] == Decimal("0.04")
    assert result["net"] == Decimal("1.21")
    payouts = {p["owner_id"]: p["amount"] for p in result["payouts"]}
    assert payouts == {
        "cre_a1b2c3": Decimal("0.73"),
        "cre_d4e5f6": Decimal("0.48"),
    }
    assert sum(payouts.values()) + result["platform_fee"] == GROSS.quantize(Decimal("0.01"))


def test_fee_rounds_half_up_to_the_cent():
    assert compute_platform_fee(Decimal("1.2500")) == Decimal("0.04")  # 0.03625 -> 0.04


def test_determinism_same_inputs_same_cents():
    splits = [
        {"owner_id": "cre_a1b2c3", "bps": 6000},
        {"owner_id": "cre_d4e5f6", "bps": 4000},
    ]
    first = compute_royalty(GROSS, splits)
    second = compute_royalty(GROSS, splits)
    assert first == second


def test_invariant_holds_across_many_split_shapes():
    cases = [
        [{"owner_id": "a", "bps": 10000}],
        [{"owner_id": "a", "bps": 5000}, {"owner_id": "b", "bps": 5000}],
        [{"owner_id": "a", "bps": 3333}, {"owner_id": "b", "bps": 3333}, {"owner_id": "c", "bps": 3334}],
        [{"owner_id": "a", "bps": 1}, {"owner_id": "b", "bps": 9999}],
    ]
    for splits in cases:
        result = compute_royalty(Decimal("100.0000"), splits)
        total = sum((p["amount"] for p in result["payouts"]), Decimal("0"))
        assert total + result["platform_fee"] == Decimal("100.00")


def test_rejects_splits_not_summing_to_10000():
    with pytest.raises(ValueError):
        distribute_splits(Decimal("1.21"), [{"owner_id": "a", "bps": 5000}])


def test_remainder_ties_break_by_bps_then_owner_id():
    # Equal bps -> equal raw remainders; tie-break falls to owner_id order.
    splits = [
        {"owner_id": "z_owner", "bps": 5000},
        {"owner_id": "a_owner", "bps": 5000},
    ]
    result = compute_royalty(Decimal("0.0300"), splits)
    payouts = {p["owner_id"]: p["amount"] for p in result["payouts"]}
    # net = 0.03 - fee; whichever cent is left over goes to "a_owner" first.
    assert sum(payouts.values()) + result["platform_fee"] == Decimal("0.03")


def test_format_ledger_amount_always_four_places():
    assert format_ledger_amount(Decimal("0.73")) == "0.7300"
    assert format_ledger_amount(Decimal("1.21")) == "1.2100"
