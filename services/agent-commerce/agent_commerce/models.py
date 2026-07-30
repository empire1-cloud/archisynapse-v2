from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any, Literal


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def iso_now() -> str:
    return utc_now().isoformat().replace("+00:00", "Z")


AuthorizationStatus = Literal["active", "revoked", "consumed", "expired"]
ReservationStatus = Literal[
    "reserved",
    "invoice_bound",
    "payment_initiated",
    "paid",
    "finalized",
    "released",
    "failed",
]


@dataclass(frozen=True)
class Authorization:
    id: str
    tenant_id: str
    orchestrator_id: str
    max_total_sats: int
    max_per_call_sats: int
    max_route_fee_sats: int
    max_calls: int
    allowed_agent_npubs: tuple[str, ...] = ()
    allowed_specialties: tuple[str, ...] = ()
    expires_at: str = ""
    status: AuthorizationStatus = "active"
    reserved_sats: int = 0
    spent_sats: int = 0
    call_count: int = 0
    version: int = 1
    created_at: str = field(default_factory=iso_now)
    updated_at: str = field(default_factory=iso_now)


@dataclass(frozen=True)
class Reservation:
    id: str
    authorization_id: str
    idempotency_key: str
    orchestration_id: str
    agent_npub: str
    specialty: str
    quoted_sats: int
    reserved_sats: int
    status: ReservationStatus
    invoice_sats: int | None = None
    payment_hash: str | None = None
    provider_payment_id: str | None = None
    route_fee_sats: int | None = None
    preimage_hash: str | None = None
    receipt_id: str | None = None
    invoice_payload: dict[str, Any] | None = None
    provider: str | None = None
    settled_at: str | None = None
    settlement_evidence: dict[str, Any] | None = None
    created_at: str = field(default_factory=iso_now)
    updated_at: str = field(default_factory=iso_now)


@dataclass(frozen=True)
class DecodedInvoice:
    payment_request_sha256: str
    amount_sats: int
    payment_hash: str
    description: str | None = None
    expires_at: str | None = None
    raw: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class PaymentSettlement:
    provider: str
    provider_payment_id: str
    payment_hash: str
    preimage: str
    route_fee_sats: int
    state: Literal["settled"] = "settled"
    settled_at: str = field(default_factory=iso_now)
    raw_evidence: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class DeliveryEvidence:
    status: Literal["delivered", "delivery_failed", "delivery_unknown"]
    http_status: int | None
    content_type: str | None
    body_sha256: str | None
    body_bytes: int
    latency_ms: int
    delivered_at: str = field(default_factory=iso_now)
    error: str | None = None


@dataclass(frozen=True)
class PaymentReceipt:
    id: str
    authorization_id: str
    authorization_version: int
    reservation_id: str
    orchestration_id: str
    idempotency_key: str
    tenant_id: str
    orchestrator_id: str
    agent_npub: str
    specialty: str
    endpoint: str
    quoted_sats: int
    invoice: DecodedInvoice
    settlement: dict[str, Any]
    delivery: DeliveryEvidence
    total_debit_sats: int
    created_at: str
    receipt_sha256: str
    signature: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)
