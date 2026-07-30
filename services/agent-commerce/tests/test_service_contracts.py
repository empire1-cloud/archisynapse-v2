from __future__ import annotations

import hashlib
import json
from datetime import datetime, timedelta, timezone

import pytest

from agent_commerce.contracts import SERVICE_TEMPLATES, ServiceContractStore
from agent_commerce.errors import ServiceContractViolation
from agent_commerce.fable import FableReceiptStore
from agent_commerce.receipts import ReceiptSigner
from agent_commerce.storage import Database


@pytest.fixture()
def stores(tmp_path):
    db = Database(tmp_path / "commerce.db")
    contracts = ServiceContractStore(db)
    signer = ReceiptSigner("x" * 32)
    fable = FableReceiptStore(db, signer)
    return db, contracts, signer, fable


def create_contract(contracts, **overrides):
    template = SERVICE_TEMPLATES["seo.search-intelligence.v1"]
    payload = {
        key: value
        for key, value in template.items()
        if key not in {"name", "description"}
    }
    payload.update(
        tenant_id="tenant-lyrica",
        provider_agent_npub="npub-seo",
        endpoint="https://agents.example.com/seo",
        settlement_policy="prepaid_l402",
        max_price_sats=2500,
        expires_at=(datetime.now(timezone.utc) + timedelta(hours=1)).isoformat(),
    )
    payload.update(overrides)
    return contracts.create(**payload)


def test_templates_keep_specialists_separate():
    seo = SERVICE_TEMPLATES["seo.search-intelligence.v1"]
    reliability = SERVICE_TEMPLATES["reliability.workflow-verification.v1"]
    assert seo["specialty"] == "seo_specialist"
    assert reliability["specialty"] == "reliability_agent"
    assert "health_verdict" in reliability["required_deliverables"]
    assert "page_gap_analysis" in seo["required_deliverables"]


def test_contract_enforces_tenant_provider_endpoint_and_price(stores):
    _, contracts, _, _ = stores
    contract = create_contract(contracts)
    validated = contracts.validate_paid_call(
        contract.id,
        tenant_id="tenant-lyrica",
        agent_npub="npub-seo",
        specialty="seo_specialist",
        endpoint="https://agents.example.com/seo",
        quoted_sats=2000,
    )
    assert validated.id == contract.id
    with pytest.raises(ServiceContractViolation):
        contracts.validate_paid_call(
            contract.id,
            tenant_id="tenant-other",
            agent_npub="npub-seo",
            specialty="seo_specialist",
            endpoint="https://agents.example.com/seo",
            quoted_sats=2000,
        )
    with pytest.raises(ServiceContractViolation):
        contracts.validate_paid_call(
            contract.id,
            tenant_id="tenant-lyrica",
            agent_npub="npub-seo",
            specialty="seo_specialist",
            endpoint="https://agents.example.com/seo",
            quoted_sats=3000,
        )


def test_delivery_waits_for_independent_validator(stores):
    _, contracts, _, _ = stores
    contract = create_contract(contracts)
    body = json.dumps(
        {
            "search_opportunities": [{"query": "creator-owned AI music"}],
            "intent_map": {"creator-owned AI music": "commercial"},
            "page_gap_analysis": ["royalty tracking page"],
            "prioritized_actions": ["publish royalty evidence page"],
            "evidence": {
                "source_urls": ["https://example.com/source"],
                "analysis_timestamp": "2026-07-30T12:00:00Z",
                "tenant_scope": "tenant-lyrica",
            },
        }
    ).encode()
    receipt = {"delivery": {"status": "delivered", "latency_ms": 1000}}
    evaluation = contracts.evaluate_delivery(
        contract,
        payment_receipt=receipt,
        body=body,
    )
    assert evaluation["status"] == "pending_independent_validation"
    assert evaluation["technical_delivery_passed"] is True


def test_missing_evidence_rejects_delivery(stores):
    _, contracts, _, _ = stores
    contract = create_contract(contracts)
    body = json.dumps(
        {
            "search_opportunities": [1],
            "intent_map": {"x": "y"},
            "page_gap_analysis": [1],
            "prioritized_actions": [1],
        }
    ).encode()
    evaluation = contracts.evaluate_delivery(
        contract,
        payment_receipt={"delivery": {"status": "delivered", "latency_ms": 100}},
        body=body,
    )
    assert evaluation["status"] == "rejected"
    assert evaluation["missing_evidence"]


def test_fable_receipts_are_signed_idempotent_and_audited(stores):
    db, contracts, signer, fable = stores
    contract = create_contract(contracts)
    first = fable.issue_contract_receipt(contract.to_dict())
    second = fable.issue_contract_receipt(contract.to_dict())
    assert first["id"] == second["id"]
    assert signer.verify(first)
    assert db.verify_audit_chain()

    authority = fable.issue_execution_authorization(
        contract=contract.to_dict(),
        authorization={
            "id": "auth-1",
            "tenant_id": contract.tenant_id,
            "orchestrator_id": "hic",
            "version": 1,
            "expires_at": "2026-07-31T00:00:00Z",
        },
        request={
            "orchestration_id": "exec-1",
            "agent_npub": contract.provider_agent_npub,
            "specialty": contract.specialty,
            "endpoint": contract.endpoint,
            "quoted_sats": 1000,
            "query": "audit SEO",
            "context": "public pages only",
        },
    )
    assert signer.verify(authority)
    assert authority["body"]["query_sha256"] == hashlib.sha256(b"audit SEO").hexdigest()
    assert "audit SEO" not in json.dumps(authority)


def test_provider_cannot_self_validate(stores):
    _, contracts, _, _ = stores
    contract = create_contract(contracts)
    with pytest.raises(ServiceContractViolation):
        contracts.evaluate_validator_outcome(
            contract,
            outcome={
                "validator_id": contract.provider_agent_npub,
                "success": True,
                "quality_score": 1.0,
                "latency_ms": 100,
                "evidence_sha256": "e" * 64,
            },
            delivery_verdict={"blockers": []},
        )
