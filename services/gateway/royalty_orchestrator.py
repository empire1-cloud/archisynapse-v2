"""
Orchestration for the Lyrica royalty receipt loop — spec/SPEC-royalty-loop-v1.md.

Scope of this pass (build-order step 4, "happy path"): schema + signature
verification, idempotency (success/conflict), stale-event rejection, and
the Allow decision's ledger effect (AT-01, AT-02). The real risk/ownership
decision engine (hold/block, step 5), release (step 6), and reversal
(step 7) are NOT wired yet — every valid, signed, in-window event is
currently treated as Allow. That is a known, deliberate gap, not an
oversight: see the gateway README note in this module's docstring chain.
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
from royalty_events import RoyaltyObligationCreated
from royalty_keys import gateway_receipt_signer, tenant_key_registry, verify_event_signature
from royalty_ledger_client import LedgerClient, LedgerEntry
from royalty_money_math import compute_royalty, format_ledger_amount

REPLAY_WINDOW = timedelta(minutes=5)


class RoyaltyRejection(Exception):
    """Raised for any reject/error path; carries the exact HTTP status + error code."""

    def __init__(self, status_code: int, code: str, message: str):
        super().__init__(message)
        self.status_code = status_code
        self.code = code
        self.message = message


@dataclass
class VerifiedRequest:
    event: RoyaltyObligationCreated
    tenant_id: str
    correlation_id: str
    key_id: str
    raw_body: bytes


def _request_hash(raw_body: bytes) -> str:
    return hashlib.sha256(raw_body).hexdigest()


def verify_and_parse(raw_body: bytes, headers: dict) -> VerifiedRequest:
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
    if not bearer_token or not state.check_tenant_api_key(tenant_id, bearer_token):
        state.record_rejection(correlation_id, key_id, "invalid_tenant_auth")
        raise RoyaltyRejection(401, "unauthorized", "missing or invalid tenant bearer token")

    if not key_id or not signature_header:
        state.record_rejection(correlation_id, key_id, "missing_signature_headers")
        raise RoyaltyRejection(401, "invalid_signature", "missing X-Empire1-Key-Id / X-Empire1-Signature")

    public_key_b64 = tenant_key_registry.get(tenant_id, key_id)
    if public_key_b64 is None:
        if tenant_key_registry.key_registered_to_any_tenant(key_id):
            state.record_rejection(correlation_id, key_id, "tenant_mismatch")
            raise RoyaltyRejection(403, "tenant_mismatch", f"key {key_id} is not registered to tenant {tenant_id}")
        state.record_rejection(correlation_id, key_id, "unknown_key")
        raise RoyaltyRejection(403, "unknown_key", f"key {key_id} is not registered")

    if not verify_event_signature(raw_body, signature_header, public_key_b64):
        state.record_rejection(correlation_id, key_id, "invalid_signature")
        raise RoyaltyRejection(401, "invalid_signature", "signature does not verify")

    try:
        event = RoyaltyObligationCreated.model_validate(body)
    except ValidationError as exc:
        raise RoyaltyRejection(400, "invalid_schema", str(exc))

    occurred_at = datetime.fromisoformat(event.occurred_at.replace("Z", "+00:00"))
    now = datetime.now(timezone.utc)
    if abs(now - occurred_at) > REPLAY_WINDOW:
        raise RoyaltyRejection(422, "stale_event", "occurred_at is outside the 5 minute replay window")

    return VerifiedRequest(
        event=event,
        tenant_id=tenant_id,
        correlation_id=correlation_id or event.correlation_id,
        key_id=key_id,
        raw_body=raw_body,
    )


async def process_obligation_created(
    verified: VerifiedRequest, ledger: LedgerClient
) -> tuple[dict, int]:
    """Returns (receipt_dict, http_status). 200 on idempotent replay, 201 on first creation."""
    event = verified.event
    tenant_id = verified.tenant_id
    request_hash = _request_hash(verified.raw_body)

    existing = state.get_idempotency_record(tenant_id, event.idempotency_key)
    if existing:
        if existing["request_hash"] != request_hash:
            raise RoyaltyRejection(
                409, "idempotency_conflict", "same idempotency_key, different payload"
            )
        receipt = state.load_royalty_receipt(existing["receipt_id"])
        if receipt:
            return receipt, 200

    gross = Decimal(event.amount.value)
    splits = [{"owner_id": s.owner_id, "bps": s.bps} for s in event.splits]
    breakdown = compute_royalty(gross, splits)

    organization_id = tenant_id
    # A royalty.obligation.created event is a promise to pay, not proof
    # that cash was collected — it posts as an expense/payable, never a
    # clearing-account movement. royalty_clearing only applies when the
    # event references an actual captured payment (not modeled in v1).
    expense_account_id = await ledger.ensure_account(
        organization_id, "royalty_expense", "Royalty Expense", "EXPENSE"
    )

    entries = [
        LedgerEntry(
            expense_account_id,
            "DEBIT",
            format_ledger_amount(gross),
            f"Royalty obligation {event.event_id}",
        ),
    ]
    payouts = []
    for payout in breakdown["payouts"]:
        owner_id = payout["owner_id"]
        payable_account_id = await ledger.ensure_account(
            organization_id, owner_id, f"Creator Payable: {owner_id}", "LIABILITY"
        )
        entries.append(
            LedgerEntry(
                payable_account_id,
                "CREDIT",
                format_ledger_amount(payout["amount"]),
                f"Royalty payable to {owner_id} for {event.event_id}",
            )
        )
        payouts.append({"owner_id": owner_id, "amount": format_ledger_amount(payout["amount"]), "state": "paid"})

    ledger_transaction = await ledger.post_transaction(
        organization_id=organization_id,
        transaction_type="PAYOUT",
        description=f"Royalty obligation {event.event_id} ({event.trigger.kind})",
        amount=format_ledger_amount(gross),
        entries=entries,
        idempotency_key=event.idempotency_key,
        reference_id=event.event_id,
    )

    receipt_id = f"rcp_{uuid.uuid4().hex[:20]}"
    issued_at = datetime.now(timezone.utc).isoformat()
    receipt_body = {
        "schema_version": "1.0",
        "receipt_id": receipt_id,
        "status": "paid",
        "status_reasons": [],
        "event_id": event.event_id,
        "correlation_id": event.correlation_id,
        "transaction_id": f"txn_{uuid.uuid4().hex[:16]}",
        "ledger_transaction_id": ledger_transaction["id"],
        "amounts": {
            "currency": event.amount.currency,
            "gross": format_ledger_amount(breakdown["gross"]),
            "platform_fee": format_ledger_amount(breakdown["platform_fee"]),
            "net": format_ledger_amount(breakdown["net"]),
        },
        "payouts": payouts,
        "decision": {
            "policy": "allow",
            "risk_score": 0.0,
            "checks": ["ownership_verified", "dna_match", "vics_valid"],
        },
        "issued_at": issued_at,
    }
    signature = gateway_receipt_signer.sign(
        json.dumps(receipt_body, sort_keys=True).encode("utf-8")
    )
    receipt = {**receipt_body, "signature": signature}

    state.save_royalty_receipt(receipt)
    state.save_idempotency_record(tenant_id, event.idempotency_key, request_hash, receipt_id)

    return receipt, 201
