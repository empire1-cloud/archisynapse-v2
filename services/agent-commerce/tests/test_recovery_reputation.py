from __future__ import annotations

import hashlib

import pytest

from agent_commerce.errors import IdempotencyConflict
from agent_commerce.models import DecodedInvoice, PaymentSettlement
from agent_commerce.recovery import reconcile_initiated_payment


PREIMAGE = "33" * 32
PAYMENT_HASH = hashlib.sha256(bytes.fromhex(PREIMAGE)).hexdigest()


class LookupProvider:
    name = "fake-lnd"

    async def decode_invoice(self, payment_request: str):
        raise NotImplementedError

    async def pay_invoice(self, payment_request: str):
        raise AssertionError("recovery must never pay again")

    async def lookup_payment(self, payment_hash: str):
        assert payment_hash == PAYMENT_HASH
        return PaymentSettlement(
            provider=self.name,
            provider_payment_id=PAYMENT_HASH,
            payment_hash=PAYMENT_HASH,
            preimage=PREIMAGE,
            route_fee_sats=1,
            raw_evidence={"lookup": "SUCCEEDED"},
        )


def setup_initiated(auth, expires_at):
    authorization = auth.create(
        tenant_id="tenant-a",
        orchestrator_id="orch-a",
        max_total_sats=100,
        max_per_call_sats=50,
        max_route_fee_sats=2,
        max_calls=3,
        expires_at=expires_at,
        allowed_agent_npubs=["npub-agent"],
        allowed_specialties=["research"],
    )
    reservation = auth.reserve(
        authorization_id=authorization.id,
        idempotency_key="recover-call-123",
        orchestration_id="orch-recover",
        agent_npub="npub-agent",
        specialty="research",
        quoted_sats=20,
    )
    invoice = DecodedInvoice(
        payment_request_sha256="a" * 64,
        amount_sats=20,
        payment_hash=PAYMENT_HASH,
        raw={"source": "test"},
    )
    reservation = auth.bind_invoice(reservation.id, invoice=invoice)
    reservation = auth.mark_payment_initiated(reservation.id)
    return authorization, reservation


@pytest.mark.asyncio
async def test_recovery_never_pays_twice_and_finalizes_unknown_delivery(stack, expires_at):
    _, auth, signer, receipts, _ = stack
    authorization, reservation = setup_initiated(auth, expires_at)
    receipt = await reconcile_initiated_payment(
        authorizations=auth,
        receipts=receipts,
        payment_provider=LookupProvider(),
        reservation_id=reservation.id,
    )
    assert receipt.delivery.status == "delivery_unknown"
    assert receipt.total_debit_sats == 21
    assert signer.verify(receipt.to_dict())
    current = auth.get(authorization.id)
    assert current.spent_sats == 21
    assert current.reserved_sats == 0


@pytest.mark.asyncio
async def test_reputation_requires_receipt_and_is_once_only(stack, expires_at):
    _, auth, _, receipts, reputation = stack
    _, reservation = setup_initiated(auth, expires_at)
    receipt = await reconcile_initiated_payment(
        authorizations=auth,
        receipts=receipts,
        payment_provider=LookupProvider(),
        reservation_id=reservation.id,
    )
    initial = reputation.get("npub-agent")
    assert initial["evidence_state"] == "unverified"
    first = reputation.record_verified_outcome(
        receipt_id=receipt.id,
        validator_id="validator-1",
        success=True,
        quality_score=0.9,
        latency_ms=1200,
        evidence_sha256="e" * 64,
    )
    assert first["verified_jobs"] == 1
    repeated = reputation.record_verified_outcome(
        receipt_id=receipt.id,
        validator_id="validator-1",
        success=True,
        quality_score=0.9,
        latency_ms=1200,
        evidence_sha256="e" * 64,
    )
    assert repeated["verified_jobs"] == 1
    with pytest.raises(IdempotencyConflict):
        reputation.record_verified_outcome(
            receipt_id=receipt.id,
            validator_id="validator-2",
            success=False,
            quality_score=0.1,
            latency_ms=9000,
            evidence_sha256="f" * 64,
        )
