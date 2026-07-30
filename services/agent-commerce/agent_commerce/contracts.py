from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from typing import Any
from uuid import uuid4

from .errors import ServiceContractNotFound, ServiceContractViolation
from .models import iso_now
from .storage import Database


SETTLEMENT_POLICIES = {
    "prepaid_l402",
    "after_verified_delivery",
    "no_spend",
}


SERVICE_TEMPLATES: dict[str, dict[str, Any]] = {
    "seo.search-intelligence.v1": {
        "service_id": "seo.search-intelligence",
        "service_version": "1.0.0",
        "name": "Empire Search Intelligence Agent",
        "specialty": "seo_specialist",
        "description": "Finds evidence-backed search demand and maps it to one protected product universe.",
        "required_deliverables": [
            "search_opportunities",
            "intent_map",
            "page_gap_analysis",
            "prioritized_actions",
        ],
        "required_evidence": [
            "evidence.source_urls",
            "evidence.analysis_timestamp",
            "evidence.tenant_scope",
        ],
        "response_deadline_ms": 30_000,
        "delivery_deadline_ms": 900_000,
        "availability_target_bps": 9950,
        "min_quality_score": 0.80,
        "max_response_bytes": 2_000_000,
        "validator_required": True,
        "provider_self_verify_allowed": False,
        "refund_on_failed_delivery": True,
        "max_retries": 1,
    },
    "reliability.workflow-verification.v1": {
        "service_id": "reliability.workflow-verification",
        "service_version": "1.0.0",
        "name": "Empire Reliability Agent",
        "specialty": "reliability_agent",
        "description": "Verifies deployment health, end-to-end contracts, evidence integrity, and fail-safe behavior.",
        "required_deliverables": [
            "health_verdict",
            "failed_contracts",
            "business_impact",
            "recommended_action",
            "irreversible_action_status",
        ],
        "required_evidence": [
            "evidence.probe_results",
            "evidence.checked_at",
            "evidence.deployed_version",
        ],
        "response_deadline_ms": 15_000,
        "delivery_deadline_ms": 600_000,
        "availability_target_bps": 9990,
        "min_quality_score": 0.90,
        "max_response_bytes": 2_000_000,
        "validator_required": True,
        "provider_self_verify_allowed": False,
        "refund_on_failed_delivery": True,
        "max_retries": 1,
    },
    "social.performance-analysis.v1": {
        "service_id": "social.performance-analysis",
        "service_version": "1.0.0",
        "name": "Social Media Analysis Agent",
        "specialty": "social_analysis",
        "description": "Explains content performance and conversion evidence without inventing attribution.",
        "required_deliverables": [
            "performance_summary",
            "winning_patterns",
            "failed_patterns",
            "conversion_findings",
            "next_actions",
        ],
        "required_evidence": [
            "evidence.data_window",
            "evidence.source_accounts",
            "evidence.metric_definitions",
        ],
        "response_deadline_ms": 30_000,
        "delivery_deadline_ms": 900_000,
        "availability_target_bps": 9950,
        "min_quality_score": 0.80,
        "max_response_bytes": 2_000_000,
        "validator_required": True,
        "provider_self_verify_allowed": False,
        "refund_on_failed_delivery": True,
        "max_retries": 1,
    },
}


def _parse_time(value: str | None) -> datetime | None:
    if not value:
        return None
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def _has_path(value: Any, path: str) -> bool:
    current = value
    for part in path.split("."):
        if not isinstance(current, dict) or part not in current:
            return False
        current = current[part]
    if current is None:
        return False
    if isinstance(current, (str, list, dict, tuple, set)):
        return len(current) > 0
    return True


@dataclass(frozen=True)
class ServiceContract:
    id: str
    service_id: str
    service_version: str
    tenant_id: str
    provider_agent_npub: str
    specialty: str
    endpoint: str
    status: str
    settlement_policy: str
    max_price_sats: int
    response_deadline_ms: int
    delivery_deadline_ms: int
    availability_target_bps: int
    min_quality_score: float
    max_response_bytes: int
    max_retries: int
    validator_required: bool
    provider_self_verify_allowed: bool
    refund_on_failed_delivery: bool
    required_deliverables: tuple[str, ...]
    required_evidence: tuple[str, ...]
    expires_at: str | None
    version: int
    created_at: str
    updated_at: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class ServiceContractStore:
    def __init__(self, db: Database):
        self.db = db

    @staticmethod
    def templates() -> list[dict[str, Any]]:
        return [{"template_id": key, **value} for key, value in SERVICE_TEMPLATES.items()]

    @staticmethod
    def template(template_id: str) -> dict[str, Any]:
        result = SERVICE_TEMPLATES.get(template_id)
        if not result:
            raise ServiceContractNotFound("service template not found")
        return {"template_id": template_id, **result}

    @staticmethod
    def _from_row(row) -> ServiceContract:
        return ServiceContract(
            id=row["id"],
            service_id=row["service_id"],
            service_version=row["service_version"],
            tenant_id=row["tenant_id"],
            provider_agent_npub=row["provider_agent_npub"],
            specialty=row["specialty"],
            endpoint=row["endpoint"],
            status=row["status"],
            settlement_policy=row["settlement_policy"],
            max_price_sats=row["max_price_sats"],
            response_deadline_ms=row["response_deadline_ms"],
            delivery_deadline_ms=row["delivery_deadline_ms"],
            availability_target_bps=row["availability_target_bps"],
            min_quality_score=row["min_quality_score"],
            max_response_bytes=row["max_response_bytes"],
            max_retries=row["max_retries"],
            validator_required=bool(row["validator_required"]),
            provider_self_verify_allowed=bool(row["provider_self_verify_allowed"]),
            refund_on_failed_delivery=bool(row["refund_on_failed_delivery"]),
            required_deliverables=tuple(json.loads(row["required_deliverables_json"])),
            required_evidence=tuple(json.loads(row["required_evidence_json"])),
            expires_at=row["expires_at"],
            version=row["version"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )

    def create(
        self,
        *,
        service_id: str,
        service_version: str,
        tenant_id: str,
        provider_agent_npub: str,
        specialty: str,
        endpoint: str,
        settlement_policy: str,
        max_price_sats: int,
        response_deadline_ms: int,
        delivery_deadline_ms: int,
        availability_target_bps: int,
        min_quality_score: float,
        max_response_bytes: int,
        required_deliverables: list[str],
        required_evidence: list[str],
        max_retries: int = 0,
        validator_required: bool = True,
        provider_self_verify_allowed: bool = False,
        refund_on_failed_delivery: bool = True,
        expires_at: str | None = None,
        contract_id: str | None = None,
    ) -> ServiceContract:
        values = {
            "service_id": service_id.strip(),
            "service_version": service_version.strip(),
            "tenant_id": tenant_id.strip(),
            "provider_agent_npub": provider_agent_npub.strip(),
            "specialty": specialty.strip(),
            "endpoint": endpoint.strip(),
        }
        if not all(values.values()):
            raise ServiceContractViolation("service, tenant, provider, specialty, and endpoint are required")
        if settlement_policy not in SETTLEMENT_POLICIES:
            raise ServiceContractViolation("unsupported settlement policy")
        if max_price_sats < 0:
            raise ServiceContractViolation("max_price_sats cannot be negative")
        if settlement_policy == "prepaid_l402" and max_price_sats <= 0:
            raise ServiceContractViolation("prepaid L402 contracts require a positive price ceiling")
        if response_deadline_ms <= 0 or delivery_deadline_ms <= 0:
            raise ServiceContractViolation("SLA deadlines must be positive")
        if response_deadline_ms > delivery_deadline_ms:
            raise ServiceContractViolation("response deadline cannot exceed delivery deadline")
        if not 0 <= availability_target_bps <= 10_000:
            raise ServiceContractViolation("availability target must be 0-10000 basis points")
        if not 0 <= min_quality_score <= 1:
            raise ServiceContractViolation("minimum quality score must be 0-1")
        if max_response_bytes <= 0:
            raise ServiceContractViolation("max_response_bytes must be positive")
        if max_retries < 0:
            raise ServiceContractViolation("max_retries cannot be negative")
        deliverables = sorted(set(item.strip() for item in required_deliverables if item.strip()))
        evidence = sorted(set(item.strip() for item in required_evidence if item.strip()))
        if not deliverables or not evidence:
            raise ServiceContractViolation("deliverables and evidence requirements cannot be empty")
        if validator_required and provider_self_verify_allowed:
            raise ServiceContractViolation(
                "provider self-verification cannot satisfy an independent-validator contract"
            )
        expiry = _parse_time(expires_at)
        if expiry is not None and expiry <= datetime.now(timezone.utc):
            raise ServiceContractViolation("contract must expire in the future")

        now = iso_now()
        item_id = contract_id or f"svc_{uuid4().hex}"
        with self.db.transaction(immediate=True) as conn:
            conn.execute(
                """
                INSERT INTO service_contracts
                (id, service_id, service_version, tenant_id, provider_agent_npub, specialty,
                 endpoint, status, settlement_policy, max_price_sats, response_deadline_ms,
                 delivery_deadline_ms, availability_target_bps, min_quality_score,
                 max_response_bytes, max_retries, validator_required,
                 provider_self_verify_allowed, refund_on_failed_delivery,
                 required_deliverables_json, required_evidence_json, expires_at,
                 created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, 'active', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    item_id,
                    values["service_id"],
                    values["service_version"],
                    values["tenant_id"],
                    values["provider_agent_npub"],
                    values["specialty"],
                    values["endpoint"],
                    settlement_policy,
                    max_price_sats,
                    response_deadline_ms,
                    delivery_deadline_ms,
                    availability_target_bps,
                    min_quality_score,
                    max_response_bytes,
                    max_retries,
                    int(validator_required),
                    int(provider_self_verify_allowed),
                    int(refund_on_failed_delivery),
                    json.dumps(deliverables),
                    json.dumps(evidence),
                    expires_at,
                    now,
                    now,
                ),
            )
            self.db.append_audit_event(
                conn,
                event_id=f"evt_{uuid4().hex}",
                aggregate_type="service_contract",
                aggregate_id=item_id,
                event_type="service_contract.created",
                payload={
                    "service_id": values["service_id"],
                    "service_version": values["service_version"],
                    "tenant_id": values["tenant_id"],
                    "provider_agent_npub": values["provider_agent_npub"],
                    "specialty": values["specialty"],
                    "settlement_policy": settlement_policy,
                    "max_price_sats": max_price_sats,
                    "response_deadline_ms": response_deadline_ms,
                    "delivery_deadline_ms": delivery_deadline_ms,
                    "validator_required": validator_required,
                    "expires_at": expires_at,
                },
            )
            row = conn.execute("SELECT * FROM service_contracts WHERE id=?", (item_id,)).fetchone()
        return self._from_row(row)

    def get(self, contract_id: str) -> ServiceContract | None:
        conn = self.db.connect()
        try:
            row = conn.execute("SELECT * FROM service_contracts WHERE id=?", (contract_id,)).fetchone()
        finally:
            conn.close()
        return self._from_row(row) if row else None

    def list_for_tenant(self, tenant_id: str, *, status: str | None = None) -> list[ServiceContract]:
        conn = self.db.connect()
        try:
            if status:
                rows = conn.execute(
                    "SELECT * FROM service_contracts WHERE tenant_id=? AND status=? ORDER BY created_at DESC",
                    (tenant_id, status),
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT * FROM service_contracts WHERE tenant_id=? ORDER BY created_at DESC",
                    (tenant_id,),
                ).fetchall()
        finally:
            conn.close()
        return [self._from_row(row) for row in rows]

    def validate_paid_call(
        self,
        contract_id: str,
        *,
        tenant_id: str,
        agent_npub: str,
        specialty: str,
        endpoint: str,
        quoted_sats: int,
    ) -> ServiceContract:
        contract = self.get(contract_id)
        if not contract:
            raise ServiceContractNotFound("service contract not found")
        if contract.status != "active":
            raise ServiceContractViolation(f"service contract is {contract.status}")
        expiry = _parse_time(contract.expires_at)
        if expiry is not None and expiry <= datetime.now(timezone.utc):
            raise ServiceContractViolation("service contract has expired")
        if contract.tenant_id != tenant_id:
            raise ServiceContractViolation("tenant is outside the service contract")
        if contract.provider_agent_npub != agent_npub:
            raise ServiceContractViolation("provider identity does not match the service contract")
        if contract.specialty != specialty:
            raise ServiceContractViolation("specialty does not match the service contract")
        if contract.endpoint != endpoint:
            raise ServiceContractViolation("endpoint does not match the service contract")
        if contract.settlement_policy != "prepaid_l402":
            raise ServiceContractViolation(
                "this endpoint executes only prepaid_l402 contracts; no-spend and post-verification "
                "contracts require their own execution adapter"
            )
        if quoted_sats > contract.max_price_sats:
            raise ServiceContractViolation("quoted price exceeds the service contract")
        return contract

    def evaluate_delivery(
        self,
        contract: ServiceContract,
        *,
        payment_receipt: dict[str, Any],
        body: bytes,
    ) -> dict[str, Any]:
        delivery = payment_receipt.get("delivery") or {}
        blockers: list[str] = []
        if delivery.get("status") != "delivered":
            blockers.append("delivery was not successful")
        latency_ms = int(delivery.get("latency_ms") or 0)
        if latency_ms > contract.delivery_deadline_ms:
            blockers.append("delivery exceeded the SLA deadline")
        if len(body) > contract.max_response_bytes:
            blockers.append("delivery exceeded the contract response-size ceiling")

        parsed: Any = None
        try:
            parsed = json.loads(body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            blockers.append("delivery is not valid UTF-8 JSON")

        missing_deliverables: list[str] = []
        missing_evidence: list[str] = []
        if isinstance(parsed, dict):
            missing_deliverables = [
                item for item in contract.required_deliverables if not _has_path(parsed, item)
            ]
            missing_evidence = [
                item for item in contract.required_evidence if not _has_path(parsed, item)
            ]
            if missing_deliverables:
                blockers.append("required deliverables are missing")
            if missing_evidence:
                blockers.append("required evidence is missing")

        if blockers:
            status = "rejected"
        elif contract.validator_required:
            status = "pending_independent_validation"
        else:
            status = "accepted"

        return {
            "contract_id": contract.id,
            "service_id": contract.service_id,
            "service_version": contract.service_version,
            "status": status,
            "technical_delivery_passed": not blockers,
            "validator_required": contract.validator_required,
            "provider_self_verify_allowed": contract.provider_self_verify_allowed,
            "delivery_latency_ms": latency_ms,
            "delivery_deadline_ms": contract.delivery_deadline_ms,
            "missing_deliverables": missing_deliverables,
            "missing_evidence": missing_evidence,
            "blockers": blockers,
            "refund_on_failed_delivery": contract.refund_on_failed_delivery,
            "settlement_policy": contract.settlement_policy,
            "settlement_note": (
                "payment is already settled; rejection opens the refund/dispute path"
                if contract.settlement_policy == "prepaid_l402" and blockers
                else "settlement remains governed by the selected contract policy"
            ),
        }

    def evaluate_validator_outcome(
        self,
        contract: ServiceContract,
        *,
        outcome: dict[str, Any],
        delivery_verdict: dict[str, Any] | None,
    ) -> dict[str, Any]:
        blockers = list((delivery_verdict or {}).get("blockers") or [])
        quality_score = float(outcome["quality_score"])
        if not bool(outcome["success"]):
            blockers.append("independent validator marked the delivery unsuccessful")
        if quality_score < contract.min_quality_score:
            blockers.append("quality score is below the contract minimum")
        validator_id = str(outcome["validator_id"])
        if not contract.provider_self_verify_allowed and validator_id == contract.provider_agent_npub:
            blockers.append("provider cannot independently validate its own delivery")
        return {
            "contract_id": contract.id,
            "service_id": contract.service_id,
            "status": "accepted" if not blockers else "rejected",
            "validator_id": validator_id,
            "quality_score": quality_score,
            "minimum_quality_score": contract.min_quality_score,
            "evidence_sha256": outcome["evidence_sha256"],
            "blockers": blockers,
            "refund_recommended": bool(blockers and contract.refund_on_failed_delivery),
        }
