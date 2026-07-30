from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor

import pytest

from agent_commerce.errors import AuthorizationDenied, AuthorizationRevoked, BudgetExceeded, IdempotencyConflict


def create_auth(auth, expires_at, **overrides):
    payload = dict(
        tenant_id="tenant-a",
        orchestrator_id="orch-a",
        max_total_sats=100,
        max_per_call_sats=60,
        max_route_fee_sats=0,
        max_calls=2,
        expires_at=expires_at,
        allowed_agent_npubs=["npub-agent"],
        allowed_specialties=["research"],
    )
    payload.update(overrides)
    return auth.create(**payload)


def test_concurrent_reservation_cannot_overspend(stack, expires_at):
    db, auth, *_ = stack
    authorization = create_auth(auth, expires_at)

    def reserve(key):
        return auth.reserve(
            authorization_id=authorization.id,
            idempotency_key=key,
            orchestration_id=f"orch-{key}",
            agent_npub="npub-agent",
            specialty="research",
            quoted_sats=50,
        )

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(reserve, f"key-{n}-123456") for n in range(2)]
    results, errors = [], []
    for future in futures:
        try:
            results.append(future.result())
        except Exception as exc:
            errors.append(exc)

    assert len(results) == 1
    assert len(errors) == 1
    assert isinstance(errors[0], BudgetExceeded)
    current = auth.get(authorization.id)
    assert current.reserved_sats == 56  # ceil(50 * 1.10) under float arithmetic
    assert current.spent_sats == 0
    assert db.verify_audit_chain()


def test_scope_and_revocation_are_fail_closed(stack, expires_at):
    _, auth, *_ = stack
    authorization = create_auth(auth, expires_at)
    with pytest.raises(AuthorizationDenied):
        auth.reserve(
            authorization_id=authorization.id,
            idempotency_key="scope-test-123",
            orchestration_id="orch-1",
            agent_npub="npub-other",
            specialty="research",
            quoted_sats=10,
        )
    auth.revoke(authorization.id, reason="founder disabled autonomous spend")
    with pytest.raises(AuthorizationRevoked):
        auth.reserve(
            authorization_id=authorization.id,
            idempotency_key="revoked-test-123",
            orchestration_id="orch-2",
            agent_npub="npub-agent",
            specialty="research",
            quoted_sats=10,
        )


def test_idempotency_key_cannot_be_rebound(stack, expires_at):
    _, auth, *_ = stack
    authorization = create_auth(auth, expires_at)
    first = auth.reserve(
        authorization_id=authorization.id,
        idempotency_key="same-key-123456",
        orchestration_id="orch-1",
        agent_npub="npub-agent",
        specialty="research",
        quoted_sats=10,
    )
    repeated = auth.reserve(
        authorization_id=authorization.id,
        idempotency_key="same-key-123456",
        orchestration_id="orch-1",
        agent_npub="npub-agent",
        specialty="research",
        quoted_sats=10,
    )
    assert repeated.id == first.id
    with pytest.raises(IdempotencyConflict):
        auth.reserve(
            authorization_id=authorization.id,
            idempotency_key="same-key-123456",
            orchestration_id="orch-1",
            agent_npub="npub-agent",
            specialty="research",
            quoted_sats=11,
        )
