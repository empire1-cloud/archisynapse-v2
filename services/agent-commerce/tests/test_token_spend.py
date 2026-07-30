from __future__ import annotations

import threading
from datetime import datetime, timedelta, timezone

import pytest

from agent_commerce.errors import BudgetExceeded, IdempotencyConflict, ModelRouteDenied, PolicyPaused
from agent_commerce.receipts import ReceiptSigner
from agent_commerce.storage import Database
from agent_commerce.token_spend import TokenSpendStore


@pytest.fixture
def stack(tmp_path):
    db = Database(tmp_path / "token-spend.db")
    signer = ReceiptSigner("t" * 64)
    store = TokenSpendStore(db, signer)
    store.put_rate_card(provider="provider-a", model="expensive", input_microusd_per_million=10_000_000, output_microusd_per_million=20_000_000, cached_input_microusd_per_million=2_000_000, reasoning_microusd_per_million=30_000_000, source_reference="provider-a-price-sheet-v1")
    store.put_rate_card(provider="provider-b", model="cheap", input_microusd_per_million=1_000_000, output_microusd_per_million=2_000_000, cached_input_microusd_per_million=200_000, reasoning_microusd_per_million=3_000_000, source_reference="provider-b-price-sheet-v1")
    return store, signer


def make_policy(store, **overrides):
    now = datetime.now(timezone.utc)
    params = dict(
        tenant_id="tenant-a", scope_type="project", scope_id=f"project-{now.timestamp()}",
        budget_microusd=100_000, max_per_call_microusd=50_000,
        max_input_tokens=10_000, max_output_tokens=10_000, max_calls_per_minute=100,
        anomaly_multiplier=3.0,
        allowed_routes=[{"provider": "provider-a", "model": "expensive"}, {"provider": "provider-b", "model": "cheap"}],
        fallback_routes=[{"provider": "provider-b", "model": "cheap"}],
        period_start=now.isoformat().replace("+00:00", "Z"),
        period_end=(now + timedelta(days=30)).isoformat().replace("+00:00", "Z"),
    )
    params.update(overrides)
    return store.create_policy(**params)


def reserve(store, policy_id, *, key="call-00000001", provider="provider-b", model="cheap", input_tokens=1000, output_tokens=1000):
    return store.preflight(policy_id=policy_id, idempotency_key=key, provider=provider, model=model, estimated_input_tokens=input_tokens, estimated_output_tokens=output_tokens)


def finalize(store, reservation_id, *, request_id="req-1", input_tokens=1000, output_tokens=1000):
    return store.finalize_usage(reservation_id=reservation_id, actual_input_tokens=input_tokens, actual_output_tokens=output_tokens, provider_request_id=request_id, response_sha256="a" * 64, outcome_status="success")


def test_rate_card_cost_math(stack):
    store, _ = stack
    rate = store.get_rate_card(provider="provider-b", model="cheap")
    assert store.calculate_cost(rate, input_tokens=1000, output_tokens=1000, cached_input_tokens=1000, reasoning_tokens=1000) == 6_200


def test_preflight_reserves_before_model_call(stack):
    store, _ = stack
    policy = make_policy(store)
    reservation = reserve(store, policy["id"])
    assert reservation["status"] == "reserved"
    assert reservation["estimated_cost_microusd"] == 3_000
    current = store.get_policy(policy["id"])
    assert current["reserved_microusd"] == 3_000 and current["spent_microusd"] == 0


def test_fallback_route_selected_when_requested_model_too_expensive(stack):
    store, _ = stack
    policy = make_policy(store, max_per_call_microusd=5_000)
    reservation = reserve(store, policy["id"], provider="provider-a", model="expensive")
    assert reservation["selected_provider"] == "provider-b" and reservation["selected_model"] == "cheap"


def test_denies_route_without_permitted_fallback(stack):
    store, _ = stack
    policy = make_policy(store, allowed_routes=[{"provider": "provider-b", "model": "cheap"}], fallback_routes=[])
    with pytest.raises(ModelRouteDenied):
        reserve(store, policy["id"], provider="provider-a", model="expensive")


def test_per_call_budget_blocks_before_consumption(stack):
    store, _ = stack
    policy = make_policy(store, max_per_call_microusd=2_000, fallback_routes=[])
    with pytest.raises(BudgetExceeded):
        reserve(store, policy["id"])
    assert store.get_policy(policy["id"])["reserved_microusd"] == 0


def test_atomic_concurrent_reservations_prevent_overspend(stack):
    store, _ = stack
    policy = make_policy(store, budget_microusd=100_000, max_per_call_microusd=40_000, allowed_routes=[{"provider": "provider-a", "model": "expensive"}], fallback_routes=[])
    successes, failures, lock = [], [], threading.Lock()
    def worker(i):
        try:
            result = reserve(store, policy["id"], key=f"concurrent-{i:08d}", provider="provider-a", model="expensive")
            with lock: successes.append(result)
        except BudgetExceeded as exc:
            with lock: failures.append(exc)
    threads = [threading.Thread(target=worker, args=(i,)) for i in range(10)]
    [thread.start() for thread in threads]
    [thread.join() for thread in threads]
    assert len(successes) == 3 and len(failures) == 7
    current = store.get_policy(policy["id"])
    assert current["reserved_microusd"] == 90_000 and current["spent_microusd"] == 0


def test_idempotent_preflight_replay_and_conflict(stack):
    store, _ = stack
    policy = make_policy(store)
    first = reserve(store, policy["id"], key="idempotent-1234")
    second = reserve(store, policy["id"], key="idempotent-1234")
    assert first["id"] == second["id"]
    with pytest.raises(IdempotencyConflict):
        reserve(store, policy["id"], key="idempotent-1234", provider="provider-a", model="expensive")


def test_finalize_records_actual_usage_and_signed_receipt(stack):
    store, signer = stack
    policy = make_policy(store, anomaly_multiplier=10.0)
    receipt = finalize(store, reserve(store, policy["id"])["id"], input_tokens=1200, output_tokens=1300)
    assert receipt["actual_usage"]["cost_microusd"] == 3_800
    assert receipt["variance_microusd"] == 800 and signer.verify(receipt)
    current = store.get_policy(policy["id"])
    assert current["reserved_microusd"] == 0 and current["spent_microusd"] == 3_800


def test_runaway_actual_usage_pauses_policy_but_keeps_spend(stack):
    store, _ = stack
    policy = make_policy(store, anomaly_multiplier=2.0)
    receipt = finalize(store, reserve(store, policy["id"], input_tokens=100, output_tokens=100)["id"], input_tokens=100, output_tokens=1000)
    assert "cost_over_estimate" in receipt["anomaly_reasons"]
    current = store.get_policy(policy["id"])
    assert current["status"] == "paused" and current["spent_microusd"] == receipt["actual_usage"]["cost_microusd"]
    with pytest.raises(PolicyPaused): reserve(store, policy["id"], key="after-runaway-01")


def test_manual_emergency_stop_blocks_new_calls(stack):
    store, _ = stack
    policy = make_policy(store)
    assert store.emergency_stop(policy["id"], reason="founder stop")["status"] == "paused"
    with pytest.raises(PolicyPaused): reserve(store, policy["id"])


def test_provider_reconciliation_confirms_matching_cost(stack):
    store, _ = stack
    policy = make_policy(store)
    receipt = finalize(store, reserve(store, policy["id"])["id"], request_id="provider-match")
    event = store.reconcile_provider_event(provider="provider-b", provider_event_id="event-match", provider_request_id="provider-match", cost_microusd=receipt["actual_usage"]["cost_microusd"])
    assert event["status"] == "confirmed" and store.get_policy(policy["id"])["status"] == "active"


def test_provider_reconciliation_mismatch_pauses_policy(stack):
    store, _ = stack
    policy = make_policy(store)
    finalize(store, reserve(store, policy["id"])["id"], request_id="provider-mismatch")
    event = store.reconcile_provider_event(provider="provider-b", provider_event_id="event-mismatch", provider_request_id="provider-mismatch", cost_microusd=50_000, tolerance_microusd=10)
    assert event["status"] == "disputed"
    assert store.get_policy(policy["id"])["stop_reason"] == "provider_reconciliation_mismatch"


def test_orphaned_provider_charge_is_recorded_and_alerted(stack):
    store, _ = stack
    event = store.reconcile_provider_event(provider="provider-a", provider_event_id="orphan-event", provider_request_id="unknown-request", cost_microusd=12_345)
    assert event["status"] == "orphaned" and event["reservation_id"] is None


def test_provider_event_replay_is_idempotent_but_conflicts_on_change(stack):
    store, _ = stack
    policy = make_policy(store)
    receipt = finalize(store, reserve(store, policy["id"])["id"], request_id="provider-replay")
    kwargs = dict(provider="provider-b", provider_event_id="event-replay", provider_request_id="provider-replay", cost_microusd=receipt["actual_usage"]["cost_microusd"])
    assert store.reconcile_provider_event(**kwargs)["id"] == store.reconcile_provider_event(**kwargs)["id"]
    with pytest.raises(IdempotencyConflict):
        store.reconcile_provider_event(**{**kwargs, "cost_microusd": 99_999})
