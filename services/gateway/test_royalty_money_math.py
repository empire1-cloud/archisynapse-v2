"""
Unit tests for royalty_money_math — pure functions, no services required.
Covers AT-01, AT-04, and AT-04b from spec/ACCEPTANCE-royalty-loop-v1.md,
plus the balanced-journal invariant and determinism guarantees the spec
requires. There is no platform fee in this loop — amount.value is the
creator payout pool in full (spec/SPEC-royalty-loop-v1.md §4).
Run with: pytest services/gateway/test_royalty_money_math.py
"""

from decimal import Decimal

import pytest

from royalty_money_math import compute_royalty, distribute_splits, format_ledger_amount

GROSS = Decimal("1.2500")


def test_at01_single_owner_gets_the_full_pool():
    result = compute_royalty(GROSS, [{"owner_id": "cre_a1b2c3", "bps": 10000}])

    assert result["platform_fee"] == Decimal("0.00")
    assert result["net"] == GROSS
    assert result["payouts"] == [{"owner_id": "cre_a1b2c3", "amount": Decimal("1.25")}]


def test_at04_sixty_forty_split_divides_evenly():
    splits = [
        {"owner_id": "cre_a1b2c3", "bps": 6000},
        {"owner_id": "cre_d4e5f6", "bps": 4000},
    ]
    result = compute_royalty(GROSS, splits)

    assert result["platform_fee"] == Decimal("0.00")
    assert result["net"] == GROSS
    payouts = {p["owner_id"]: p["amount"] for p in result["payouts"]}
    assert payouts == {
        "cre_a1b2c3": Decimal("0.75"),
        "cre_d4e5f6": Decimal("0.50"),
    }
    assert sum(payouts.values()) == GROSS.quantize(Decimal("0.01"))


def test_at04b_split_with_a_genuine_remainder():
    # 3333/3333/3334 of $1.25 does not divide evenly: raw shares are
    # 0.416625 / 0.416625 / 0.41675 -> floor 0.41/0.41/0.41 = 1.23,
    # 2 cents left over. C's remainder (.00675) is largest and wins
    # first; A/B tie on remainder and bps, so owner_id breaks the tie.
    splits = [
        {"owner_id": "a_owner", "bps": 3333},
        {"owner_id": "b_owner", "bps": 3333},
        {"owner_id": "c_owner", "bps": 3334},
    ]
    result = compute_royalty(GROSS, splits)
    payouts = {p["owner_id"]: p["amount"] for p in result["payouts"]}

    assert payouts["c_owner"] == Decimal("0.42")
    assert payouts["a_owner"] == Decimal("0.42")  # "a_owner" < "b_owner" lexicographically
    assert payouts["b_owner"] == Decimal("0.41")
    assert sum(payouts.values()) == GROSS


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
        assert total == Decimal("100.00")


def test_rejects_splits_not_summing_to_10000():
    with pytest.raises(ValueError):
        distribute_splits(Decimal("1.25"), [{"owner_id": "a", "bps": 5000}])


def test_format_ledger_amount_always_four_places():
    assert format_ledger_amount(Decimal("0.75")) == "0.7500"
    assert format_ledger_amount(Decimal("1.25")) == "1.2500"
