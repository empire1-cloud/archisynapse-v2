from __future__ import annotations

import hashlib

import httpx
import pytest

from agent_commerce.errors import InvalidInvoice, PaymentFailed, PriceMismatch
from agent_commerce.l402 import AgentCommerceService, parse_l402_challenge
from agent_commerce.models import DecodedInvoice, PaymentSettlement


PREIMAGE = "11" * 32
PAYMENT_HASH = hashlib.sha256(bytes.fromhex(PREIMAGE)).hexdigest()


class FakeProvider:
    name = "fake-lnd"

    def __init__(self, *, amount=25, fee=1, bad_preimage=False):
        self.amount = amount
        self.fee = fee
        self.bad_preimage = bad_preimage
        self.pay_calls = 0
        self.lookup_result = None

    async def decode_invoice(self, payment_request: str):
        return DecodedInvoice(
            payment_request_sha256=hashlib.sha256(payment_request.encode()).hexdigest(),
            amount_sats=self.amount,
            payment_hash=PAYMENT_HASH,
            description="agent-query",
            raw={"source": "fake"},
        )

    async def pay_invoice(self, payment_request: str):
        self.pay_calls += 1
        preimage = "22" * 32 if self.bad_preimage else PREIMAGE
        return PaymentSettlement(
            provider=self.name,
            provider_payment_id=PAYMENT_HASH,
            payment_hash=PAYMENT_HASH,
            preimage=preimage,
            route_fee_sats=self.fee,
            raw_evidence={"fake": True},
        )

    async def lookup_payment(self, payment_hash: str):
        return self.lookup_result


def make_transport(*, delivery_status=200, body=b'{"result":"ok"}'):
    def handler(request: httpx.Request):
        if request.headers.get("Authorization"):
            return httpx.Response(delivery_status, content=body, headers={"content-type": "application/json"})
        return httpx.Response(
            402,
            json={"error": "Payment Required"},
            headers={"WWW-Authenticate": 'L402 macaroon="mac-123", invoice="lnfake123"'},
        )

    return httpx.MockTransport(handler)


def create_authorization(auth, expires_at, *, total=100, per_call=50, fee=3):
    return auth.create(
        tenant_id="tenant-a",
        orchestrator_id="orch-a",
        max_total_sats=total,
        max_per_call_sats=per_call,
        max_route_fee_sats=fee,
        max_calls=5,
        expires_at=expires_at,
        allowed_agent_npubs=["npub-agent"],
        allowed_specialties=["research"],
    )


@pytest.mark.asyncio
async def test_paid_call_creates_authoritative_signed_receipt(stack, expires_at):
    db, auth, signer, receipts, _ = stack
    authorization = create_authorization(auth, expires_at)
    provider = FakeProvider(amount=25, fee=1)
    async with httpx.AsyncClient(transport=make_transport()) as client:
        service = AgentCommerceService(
            authorizations=auth,
            receipts=receipts,
            payment_provider=provider,
            http_client=client,
        )
        receipt, body = await service.execute(
            authorization_id=authorization.id,
            idempotency_key="call-success-123",
            orchestration_id="orch-run-1",
            agent_npub="npub-agent",
            specialty="research",
            endpoint="https://agent.example/api/task",
            quoted_sats=25,
            query="research this",
        )

    assert body == b'{"result":"ok"}'
    assert receipt.invoice.amount_sats == 25
    assert receipt.total_debit_sats == 26
    assert receipt.delivery.status == "delivered"
    assert receipt.settlement["payment_hash"] == PAYMENT_HASH
    assert receipt.settlement["preimage_hash"] == hashlib.sha256(PREIMAGE.encode()).hexdigest()
    assert PREIMAGE not in str(receipt.to_dict())
    assert signer.verify(receipt.to_dict())
    current = auth.get(authorization.id)
    assert current.spent_sats == 26
    assert current.reserved_sats == 0
    assert current.call_count == 1
    assert db.verify_audit_chain()


@pytest.mark.asyncio
async def test_delivery_failure_still_commits_real_spend(stack, expires_at):
    _, auth, _, receipts, _ = stack
    authorization = create_authorization(auth, expires_at)
    provider = FakeProvider(amount=20, fee=2)
    async with httpx.AsyncClient(transport=make_transport(delivery_status=500, body=b"seller failed")) as client:
        service = AgentCommerceService(
            authorizations=auth,
            receipts=receipts,
            payment_provider=provider,
            http_client=client,
        )
        receipt, _ = await service.execute(
            authorization_id=authorization.id,
            idempotency_key="delivery-fail-123",
            orchestration_id="orch-run-2",
            agent_npub="npub-agent",
            specialty="research",
            endpoint="https://agent.example/api/task",
            quoted_sats=20,
            query="do work",
        )
    assert receipt.delivery.status == "delivery_failed"
    assert receipt.total_debit_sats == 22
    assert auth.get(authorization.id).spent_sats == 22


@pytest.mark.asyncio
async def test_bait_and_switch_is_blocked_before_reservation_or_payment(stack, expires_at):
    _, auth, _, receipts, _ = stack
    authorization = create_authorization(auth, expires_at)
    provider = FakeProvider(amount=40)
    async with httpx.AsyncClient(transport=make_transport()) as client:
        service = AgentCommerceService(
            authorizations=auth,
            receipts=receipts,
            payment_provider=provider,
            http_client=client,
        )
        with pytest.raises(PriceMismatch):
            await service.execute(
                authorization_id=authorization.id,
                idempotency_key="bait-switch-123",
                orchestration_id="orch-run-3",
                agent_npub="npub-agent",
                specialty="research",
                endpoint="https://agent.example/api/task",
                quoted_sats=25,
                query="do work",
            )
    assert provider.pay_calls == 0
    assert auth.get(authorization.id).reserved_sats == 0


@pytest.mark.asyncio
async def test_bad_preimage_never_becomes_settlement(stack, expires_at):
    _, auth, _, receipts, _ = stack
    authorization = create_authorization(auth, expires_at)
    provider = FakeProvider(amount=20, bad_preimage=True)
    async with httpx.AsyncClient(transport=make_transport()) as client:
        service = AgentCommerceService(
            authorizations=auth,
            receipts=receipts,
            payment_provider=provider,
            http_client=client,
        )
        with pytest.raises(PaymentFailed):
            await service.execute(
                authorization_id=authorization.id,
                idempotency_key="bad-preimage-123",
                orchestration_id="orch-run-4",
                agent_npub="npub-agent",
                specialty="research",
                endpoint="https://agent.example/api/task",
                quoted_sats=20,
                query="do work",
            )
    current = auth.get(authorization.id)
    # State remains payment_initiated for provider reconciliation; budget is not silently released.
    assert current.reserved_sats > 0
    assert current.spent_sats == 0


def test_l402_challenge_parser_is_strict():
    assert parse_l402_challenge('L402 invoice="inv", macaroon="mac"') == ("mac", "inv")
    with pytest.raises(InvalidInvoice):
        parse_l402_challenge('Basic realm="x"')
    with pytest.raises(InvalidInvoice):
        parse_l402_challenge('L402 macaroon="mac"')
