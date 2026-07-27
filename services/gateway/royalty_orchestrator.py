"""
Orchestration for the Lyrica royalty receipt loop —
spec/SPEC-royalty-loop-v1.md.

Canonical flow (architecture-corrected):
  Lyrica signed event -> gateway verify/decide -> real transaction
  service (owns ledger posting) -> persisted signed receipt -> Lyrica.

The gateway NEVER posts to the ledger and NEVER invents a
transaction_id or ledger_transaction_id — both come verbatim from the
transaction service's response. All gateway-side state (idempotency,
receipts, rejections) is Postgres-backed (royalty_state.py), not
process memory or a JSON file.
"""

import hashlib
import json
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Optional

from pydantic import ValidationError

import royalty_state as state
from royalty_decision import evaluate_decision
from royalty_events import RoyaltyObligationCreated
from royalty_keys import gateway_receipt_signer, verify_event_signature
from royalty_money_math import compute_royalty, format_ledger_amount  # pre-flight validation only — see below
from royalty_tenant_resolver import tenant_resolver
from royalty_transaction_client import (
    RoyaltyIdempotencyConflict,
    RoyaltyTransactionClient,
    TransactionServiceError,
)

REPLAY_WINDOW = timedelta(minutes=5)


class RoyaltyRejection(Exception):
    """Raised for any reject/error path; carries the exact HTTP status + error code."""

    def __init__(self, status_code: int, code: str, message: str, body: Optional[dict] = None):
        super().__init__(message)
        self.status_code = status_code
        self.code = code
        self.message = message
        self.body = body  # when set, the route returns this instead of {code, message}


@dataclass
class VerifiedRequest:
    event: RoyaltyObligationCreated
    tenant_id: str
    correlation_id: str
    key_id: str
    raw_body: bytes


def _request_hash(raw_body: bytes) -> str:
    return hashlib.sha256(raw_body).hexdigest()


async def verify_and_parse(raw_body: bytes, headers: dict) -> VerifiedRequest:
    """
    Signature verification happens over the exact raw body bytes, before
    any JSON parsing changes the representation — per spec §2.
    """
    key_id = headers.get("x-empire1-key-id")
    signature_header = headers.get("x-empire1-signature")
    correlation_id = headers.get("x-correlation-id")

    try:
        body = json.loads(raw_body)
    except (json.JSONDecodeError, UnicodeDecodeError):
        raise RoyaltyRejection(400, "invalid_schema", "body is not valid JSON")

    tenant_id = body.get("tenant_id")
    if not tenant_id or not isinstance(tenant_id, str):
        raise RoyaltyRejection(400, "invalid_schema", "tenant_id is required")

    # Tenant identity (SLA113-issued key, per spec §2 transport table). Not
    # one of the section-7 error codes (that table only covers the ed25519
    # event signature) — this is the separate "who is calling us" check.
    auth_header = headers.get("authorization", "")
    bearer_token = auth_header[len("Bearer "):] if auth_header.startswith("Bearer ") else None
    if not bearer_token or not await state.check_tenant_api_key(tenant_id, bearer_token):
        await state.record_rejection(correlation_id, key_id, "invalid_tenant_auth")
        raise RoyaltyRejection(401, "unauthorized", "missing or invalid tenant bearer token")

    if not key_id or not signature_header:
        await state.record_rejection(correlation_id, key_id, "missing_signature_headers")
        raise RoyaltyRejection(401, "invalid_signature", "missing X-Empire1-Key-Id / X-Empire1-Signature")

    public_key_b64 = await state.get_tenant_key(tenant_id, key_id)
    if public_key_b64 is None:
        if await state.key_registered_to_any_tenant(key_id):
            await state.record_rejection(correlation_id, key_id, "tenant_mismatch")
            raise RoyaltyRejection(403, "tenant_mismatch", f"key {key_id} is not registered to tenant {tenant_id}")
        await state.record_rejection(correlation_id, key_id, "unknown_key")
        raise RoyaltyRejection(403, "unknown_key", f"key {key_id} is not registered")

    if not verify_event_signature(raw_body, signature_header, public_key_b64):
        await state.record_rejection(correlation_id, key_id, "invalid_signature")
        raise RoyaltyRejection(401, "invalid_signature", "signature does not verify")

    try:
        event = RoyaltyObligationCreated.model_validate(body)
    except ValidationError as exc:
        raise RoyaltyRejection(400, "invalid_schema", str(exc))

    occurred_at = datetime.fromisoformat(event.occurred_at.replace("Z", "+00:00"))
    now = datetime.now(timezone.utc)
    if abs(now - occurred_at) > REPLAY_WINDOW:
        await state.record_rejection(correlation_id, key_id, "stale_event")
        raise RoyaltyRejection(422, "stale_event", "occurred_at is outside the 5 minute replay window")

    return VerifiedRequest(
        event=event,
        tenant_id=tenant_id,
        correlation_id=correlation_id or event.correlation_id,
        key_id=key_id,
        raw_body=raw_body,
    )


def _build_receipt(
    event: RoyaltyObligationCreated,
    obligation: dict,
    decision_policy: str,
    risk_score: float,
    status_reasons: list,
) -> dict:
    status_map = {
        "PENDING": "processing",
        "POSTED": "paid",
        "HELD": "held",
        "BLOCKED": "blocked",
        "REVERSED": "reversed",
    }
    receipt_id = f"rcp_{uuid.uuid4().hex[:20]}"
    issued_at = datetime.now(timezone.utc).isoformat()
    payouts = [
        {"owner_id": p["ownerId"], "amount": format_ledger_amount(Decimal(p["amount"])), "state": p["state"].lower()}
        for p in obligation.get("payouts", [])
    ]
    receipt_body = {
        "schema_version": "1.0",
        "receipt_id": receipt_id,
        "status": status_map.get(obligation["status"], "processing"),
        "status_reasons": status_reasons,
        "event_id": event.event_id,
        "correlation_id": event.correlation_id,
        "tenant_id": event.tenant_id,
        "transaction_id": obligation["id"],
        "ledger_transaction_id": obligation.get("ledgerTransactionId"),
        "amounts": {
            "currency": event.amount.currency,
            "gross": format_ledger_amount(Decimal(event.amount.value)),
            "platform_fee": "0.0000",
            "net": format_ledger_amount(Decimal(event.amount.value)),
        },
        "payouts": payouts,
        "decision": {
            "policy": decision_policy,
            "risk_score": risk_score,
            "checks": ["ownership_verified", "dna_match", "vics_valid"],
        },
        "issued_at": issued_at,
    }
    signature = gateway_receipt_signer.sign(json.dumps(receipt_body, sort_keys=True).encode("utf-8"))
    return {**receipt_body, "signature": signature}


async def process_obligation_created(
    verified: VerifiedRequest, transaction_client: RoyaltyTransactionClient
) -> tuple[dict, int]:
    """Returns (receipt_dict, http_status)."""
    event = verified.event
    tenant_id = verified.tenant_id
    request_hash = _request_hash(verified.raw_body)

    claim = await state.claim_idempotency(tenant_id, event.idempotency_key, request_hash)
    if claim["outcome"] == "conflict":
        raise RoyaltyRejection(409, "idempotency_conflict", "same idempotency_key, different payload")
    if claim["outcome"] == "processing":
        raise RoyaltyRejection(
            409,
            "processing",
            "the original request with this idempotency_key is still being processed; retry shortly",
        )
    if claim["outcome"] == "completed":
        receipt = await state.load_royalty_receipt(claim["receipt_id"])
        if receipt:
            status_code = 422 if receipt["decision"]["policy"] == "ownership_invalid" else 200
            return receipt, status_code

    # claim["outcome"] == "claimed" -- pre-flight sanity check only. The
    # transaction service recomputes and posts authoritatively; if this
    # raises, we fail before even calling it (cheap, no network hop, and
    # does not contradict "sole owner of posting" since nothing is written).
    try:
        splits = [{"owner_id": s.owner_id, "bps": s.bps} for s in event.splits]
        compute_royalty(Decimal(event.amount.value), splits)
    except (ValueError, AssertionError) as exc:
        await state.fail_idempotency(tenant_id, event.idempotency_key, str(exc)[:255])
        raise RoyaltyRejection(400, "invalid_schema", str(exc))

    try:
        decision = await evaluate_decision(
            tenant_id=tenant_id,
            track_id=event.track.track_id,
            dna_tag=event.track.dna_tag,
            soulprint_hash=event.track.soulprint_hash,
            vics_proof_id=event.track.vics_proof.proof_id,
            creator_id=event.creator.creator_id,
            idempotency_key=event.idempotency_key,
            trigger_actor_id=event.trigger.actor_id,
            trigger_kind=event.trigger.kind,
            trigger_source_ref=event.trigger.source_ref,
            amount=event.amount.value,
        )

        organization_id = tenant_resolver.resolve(tenant_id)
        splits_payload = [{"ownerId": s.owner_id, "bps": s.bps} for s in event.splits]

        try:
            obligation = await transaction_client.create_obligation(
                organization_id=organization_id,
                event_id=event.event_id,
                correlation_id=event.correlation_id,
                idempotency_key=event.idempotency_key,
                tenant_id=tenant_id,
                track_id=event.track.track_id,
                creator_id=event.creator.creator_id,
                trigger_kind=event.trigger.kind,
                amount=Decimal(event.amount.value),
                currency=event.amount.currency,
                splits=splits_payload,
                decision=decision.outcome,
                decision_policy=decision.policy,
                risk_score=decision.risk_score,
                status_reasons=decision.reasons,
                request_hash=request_hash,
            )
        except RoyaltyIdempotencyConflict as exc:
            await state.fail_idempotency(tenant_id, event.idempotency_key, "downstream_idempotency_conflict")
            raise RoyaltyRejection(409, "idempotency_conflict", str(exc))
        except TransactionServiceError as exc:
            await state.fail_idempotency(tenant_id, event.idempotency_key, str(exc)[:255])
            raise RoyaltyRejection(503, "retry_later", "transaction service unavailable", body={
                "code": "retry_later", "message": "transaction service unavailable", "retryable": True
            })

        receipt = _build_receipt(event, obligation, decision.policy, decision.risk_score, decision.reasons)
        await state.save_royalty_receipt(receipt)
        await state.complete_idempotency(tenant_id, event.idempotency_key, receipt["receipt_id"])

        http_status = 422 if decision.policy == "ownership_invalid" else 201
        return receipt, http_status
    except RoyaltyRejection:
        raise
    except Exception as exc:  # noqa: BLE001 -- unexpected failure must still fail the claim
        await state.fail_idempotency(tenant_id, event.idempotency_key, str(exc)[:255])
        raise


async def release_obligation(
    tenant_id: str, event_id: str, transaction_client: RoyaltyTransactionClient
) -> tuple[dict, int]:
    receipt_id = f"rcp_rel_{event_id}"
    existing_receipt = await state.load_royalty_receipt(receipt_id)
    if existing_receipt is not None:
        return existing_receipt, 200

    organization_id = tenant_resolver.resolve(tenant_id)
    idempotency_key = f"release-{event_id}"
    try:
        obligation = await transaction_client.release_obligation(organization_id, event_id, idempotency_key)
    except TransactionServiceError as exc:
        if exc.status_code == 404:
            raise RoyaltyRejection(404, "not_found", "royalty obligation not found")
        if exc.status_code == 409:
            raise RoyaltyRejection(409, "invalid_state", "obligation is not HELD")
        raise RoyaltyRejection(503, "retry_later", "transaction service unavailable", body={
            "code": "retry_later", "message": "transaction service unavailable", "retryable": True
        })

    receipt = _receipt_from_obligation(obligation, receipt_id)
    await state.save_royalty_receipt(receipt)
    return receipt, 200


async def reverse_obligation(
    tenant_id: str,
    reversed_event_id: str,
    reversal_event_id: str,
    reversal_idempotency_key: str,
    reason: str,
    transaction_client: RoyaltyTransactionClient,
) -> tuple[dict, int]:
    receipt_id = f"rcp_rev_{reversal_event_id}"
    existing_receipt = await state.load_royalty_receipt(receipt_id)
    if existing_receipt is not None:
        return existing_receipt, 200

    organization_id = tenant_resolver.resolve(tenant_id)
    try:
        obligation, status_code = await transaction_client.reverse_obligation(
            organization_id, reversed_event_id, reversal_event_id, reversal_idempotency_key, reason
        )
    except TransactionServiceError as exc:
        if exc.status_code == 404:
            raise RoyaltyRejection(404, "not_found", "royalty obligation not found")
        if exc.status_code == 409:
            raise RoyaltyRejection(409, "invalid_state", "obligation cannot be reversed in its current state")
        raise RoyaltyRejection(503, "retry_later", "transaction service unavailable", body={
            "code": "retry_later", "message": "transaction service unavailable", "retryable": True
        })

    receipt = _receipt_from_obligation(obligation, receipt_id)
    await state.save_royalty_receipt(receipt)
    return receipt, status_code


def _receipt_from_obligation(obligation: dict, receipt_id: str) -> dict:
    """Rebuild a receipt shape from a transaction-service obligation response
    for release/reversal, which return the obligation directly rather than
    a royalty.obligation.created event. receipt_id is deterministic per
    operation (release/reversal) + event_id so repeat calls reuse the same
    receipt_id instead of minting a new one on every replay."""
    status_map = {
        "PENDING": "processing",
        "POSTED": "paid",
        "HELD": "held",
        "BLOCKED": "blocked",
        "REVERSED": "reversed",
    }
    issued_at = datetime.now(timezone.utc).isoformat()
    payouts = [
        {"owner_id": p["ownerId"], "amount": format_ledger_amount(Decimal(p["amount"])), "state": p["state"].lower()}
        for p in obligation.get("payouts", [])
    ]
    receipt_body = {
        "schema_version": "1.0",
        "receipt_id": receipt_id,
        "status": status_map.get(obligation["status"], "processing"),
        "status_reasons": obligation.get("statusReasons", []),
        "event_id": obligation["eventId"],
        "correlation_id": obligation["correlationId"],
        "tenant_id": obligation["tenantId"],
        "transaction_id": obligation["id"],
        "ledger_transaction_id": obligation.get("ledgerTransactionId"),
        "amounts": {
            "currency": obligation["currency"],
            "gross": format_ledger_amount(Decimal(str(obligation["amount"]))),
            "platform_fee": "0.0000",
            "net": format_ledger_amount(Decimal(str(obligation["amount"]))),
        },
        "payouts": payouts,
        "decision": {
            "policy": obligation.get("decisionPolicy") or "allow",
            "risk_score": float(obligation.get("riskScore") or 0.0),
            "checks": ["ownership_verified", "dna_match", "vics_valid"],
        },
        "issued_at": issued_at,
    }
    signature = gateway_receipt_signer.sign(json.dumps(receipt_body, sort_keys=True).encode("utf-8"))
    return {**receipt_body, "signature": signature}
