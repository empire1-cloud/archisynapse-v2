from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from fastapi import Depends, FastAPI, Header, HTTPException
from pydantic import BaseModel, Field

from .authorization import AuthorizationStore
from .errors import AgentCommerceError
from .identity import AgentProfileSigner
from .l402 import AgentCommerceService, LndRestPaymentProvider
from .receipts import ReceiptSigner, ReceiptStore
from .recovery import reconcile_initiated_payment
from .reputation import ReputationStore
from .storage import Database
from .token_spend import TokenSpendStore


class AuthorizationCreate(BaseModel):
    tenant_id: str = Field(min_length=1, max_length=128)
    orchestrator_id: str = Field(min_length=1, max_length=128)
    max_total_sats: int = Field(gt=0)
    max_per_call_sats: int = Field(gt=0)
    max_route_fee_sats: int = Field(default=0, ge=0)
    max_calls: int = Field(gt=0)
    expires_at: str
    allowed_agent_npubs: list[str] = []
    allowed_specialties: list[str] = []


class RevokeRequest(BaseModel):
    reason: str = Field(min_length=1, max_length=500)


class AgentCallRequest(BaseModel):
    authorization_id: str
    idempotency_key: str = Field(min_length=8, max_length=128)
    orchestration_id: str = Field(min_length=1, max_length=128)
    agent_npub: str = Field(min_length=1, max_length=256)
    specialty: str = Field(min_length=1, max_length=128)
    endpoint: str
    quoted_sats: int = Field(gt=0)
    query: str = Field(min_length=1, max_length=2000)
    context: str | None = Field(default=None, max_length=4000)


class OutcomeRequest(BaseModel):
    validator_id: str = Field(min_length=1, max_length=128)
    success: bool
    quality_score: float = Field(ge=0, le=1)
    latency_ms: int = Field(ge=0)
    evidence_sha256: str = Field(min_length=64, max_length=64)


class ProfileRequest(BaseModel):
    npub: str
    name: str
    specialty: str
    price_sats: int = Field(ge=0)
    endpoint: str
    capabilities: list[str] = []
    ttl_seconds: int = Field(default=300, ge=30, le=3600)


class ModelRoute(BaseModel):
    provider: str = Field(min_length=1, max_length=64)
    model: str = Field(min_length=1, max_length=256)


class RateCardCreate(BaseModel):
    provider: str = Field(min_length=1, max_length=64)
    model: str = Field(min_length=1, max_length=256)
    input_microusd_per_million: int = Field(ge=0)
    output_microusd_per_million: int = Field(ge=0)
    cached_input_microusd_per_million: int = Field(default=0, ge=0)
    reasoning_microusd_per_million: int = Field(default=0, ge=0)
    source_reference: str = Field(min_length=1, max_length=500)
    effective_at: str | None = None


class TokenPolicyCreate(BaseModel):
    tenant_id: str = Field(min_length=1, max_length=128)
    scope_type: str = Field(min_length=1, max_length=64)
    scope_id: str = Field(min_length=1, max_length=256)
    budget_microusd: int = Field(gt=0)
    max_per_call_microusd: int = Field(gt=0)
    max_input_tokens: int = Field(gt=0)
    max_output_tokens: int = Field(gt=0)
    allowed_routes: list[ModelRoute] = Field(min_length=1)
    fallback_routes: list[ModelRoute] = Field(default_factory=list)
    period_start: str | None = None
    period_end: str | None = None
    max_calls_per_minute: int = Field(default=60, gt=0)
    anomaly_multiplier: float = Field(default=3.0, ge=1.0)


class TokenPreflightRequest(BaseModel):
    policy_id: str
    idempotency_key: str = Field(min_length=8, max_length=128)
    provider: str = Field(min_length=1, max_length=64)
    model: str = Field(min_length=1, max_length=256)
    estimated_input_tokens: int = Field(ge=0)
    estimated_output_tokens: int = Field(ge=0)
    estimated_cached_input_tokens: int = Field(default=0, ge=0)
    estimated_reasoning_tokens: int = Field(default=0, ge=0)


class TokenFinalizeRequest(BaseModel):
    actual_input_tokens: int = Field(ge=0)
    actual_output_tokens: int = Field(ge=0)
    actual_cached_input_tokens: int = Field(default=0, ge=0)
    actual_reasoning_tokens: int = Field(default=0, ge=0)
    provider_request_id: str = Field(min_length=1, max_length=256)
    response_sha256: str = Field(min_length=64, max_length=64)
    outcome_status: str = Field(min_length=1, max_length=64)
    provider_reported_cost_microusd: int | None = Field(default=None, ge=0)


class ProviderUsageEventRequest(BaseModel):
    provider: str = Field(min_length=1, max_length=64)
    provider_event_id: str = Field(min_length=1, max_length=256)
    provider_request_id: str | None = Field(default=None, max_length=256)
    cost_microusd: int = Field(ge=0)
    input_tokens: int | None = Field(default=None, ge=0)
    output_tokens: int | None = Field(default=None, ge=0)
    cached_input_tokens: int | None = Field(default=None, ge=0)
    reasoning_tokens: int | None = Field(default=None, ge=0)
    tolerance_microusd: int = Field(default=1000, ge=0)


class EmergencyStopRequest(BaseModel):
    reason: str = Field(min_length=1, max_length=500)


def _required(name: str) -> str:
    value = os.getenv(name, "").strip()
    if not value:
        raise RuntimeError(f"{name} is required")
    return value


DB_PATH = os.getenv("AGENT_COMMERCE_DB_PATH", str(Path(__file__).resolve().parents[1] / "agent-commerce.db"))
DB = Database(DB_PATH)
AUTHORIZATIONS = AuthorizationStore(DB)
RECEIPT_SIGNER = ReceiptSigner(_required("AGENT_COMMERCE_RECEIPT_SIGNING_KEY"))
RECEIPTS = ReceiptStore(DB, RECEIPT_SIGNER)
REPUTATION = ReputationStore(DB, RECEIPTS)
PROFILE_SIGNER = AgentProfileSigner(_required("AGENT_PROFILE_SIGNING_KEY"))
AI_SPEND_SIGNING_KEY = os.getenv("AI_SPEND_RECEIPT_SIGNING_KEY", "").strip() or _required("AGENT_COMMERCE_RECEIPT_SIGNING_KEY")
AI_SPEND = TokenSpendStore(DB, ReceiptSigner(AI_SPEND_SIGNING_KEY))
PAYMENTS = LndRestPaymentProvider(
    base_url=_required("BUYER_LND_REST"),
    macaroon_hex=_required("BUYER_LND_MACAROON"),
    verify_tls=os.getenv("BUYER_LND_VERIFY_TLS", "true").lower() == "true",
)
COMMERCE = AgentCommerceService(authorizations=AUTHORIZATIONS, receipts=RECEIPTS, payment_provider=PAYMENTS)

app = FastAPI(title="Archisynapse Agent Commerce Rail", version="1.1.0")


def require_internal_token(authorization: str | None = Header(default=None)) -> None:
    expected = _required("AGENT_COMMERCE_INTERNAL_TOKEN")
    if authorization != f"Bearer {expected}":
        raise HTTPException(status_code=401, detail="invalid internal token")


@app.exception_handler(AgentCommerceError)
async def commerce_error_handler(_, exc: AgentCommerceError):
    from fastapi.responses import JSONResponse
    return JSONResponse(status_code=422, content={"error": exc.code, "message": str(exc), "details": exc.details})


@app.exception_handler(ValueError)
async def value_error_handler(_, exc: ValueError):
    from fastapi.responses import JSONResponse
    return JSONResponse(status_code=422, content={"error": "invalid_request", "message": str(exc)})


@app.get("/health")
def health() -> dict[str, Any]:
    return {"status": "ok", "service": "agent-commerce", "version": "1.1.0", "agent_payments": True, "ai_token_spend_controls": True, "audit_chain_valid": DB.verify_audit_chain()}


@app.post("/v1/authorizations", dependencies=[Depends(require_internal_token)])
def create_authorization(request: AuthorizationCreate):
    return AUTHORIZATIONS.create(**request.model_dump())


@app.get("/v1/authorizations/{authorization_id}", dependencies=[Depends(require_internal_token)])
def get_authorization(authorization_id: str):
    result = AUTHORIZATIONS.get(authorization_id)
    if not result:
        raise HTTPException(status_code=404, detail="authorization not found")
    return result


@app.post("/v1/authorizations/{authorization_id}/revoke", dependencies=[Depends(require_internal_token)])
def revoke_authorization(authorization_id: str, request: RevokeRequest):
    return AUTHORIZATIONS.revoke(authorization_id, reason=request.reason)


@app.post("/v1/agent-calls", dependencies=[Depends(require_internal_token)])
async def execute_agent_call(request: AgentCallRequest):
    receipt, body = await COMMERCE.execute(**request.model_dump())
    return {"receipt": receipt.to_dict(), "result_utf8": body.decode("utf-8", errors="replace")}


@app.post("/v1/reservations/{reservation_id}/reconcile", dependencies=[Depends(require_internal_token)])
async def reconcile_reservation(reservation_id: str):
    receipt = await reconcile_initiated_payment(authorizations=AUTHORIZATIONS, receipts=RECEIPTS, payment_provider=PAYMENTS, reservation_id=reservation_id)
    if not receipt:
        reservation = AUTHORIZATIONS.get_reservation(reservation_id)
        if not reservation:
            raise HTTPException(status_code=404, detail="reservation not found")
        return {"status": reservation.status, "reconciled": False, "message": "provider has not independently proven settlement; no second payment was attempted"}
    return {"status": "finalized", "reconciled": True, "receipt": receipt.to_dict()}


@app.get("/v1/receipts/{receipt_id}", dependencies=[Depends(require_internal_token)])
def get_receipt(receipt_id: str):
    receipt = RECEIPTS.get(receipt_id)
    if not receipt:
        raise HTTPException(status_code=404, detail="receipt not found")
    return receipt.to_dict()


@app.post("/v1/receipts/{receipt_id}/outcomes", dependencies=[Depends(require_internal_token)])
def record_outcome(receipt_id: str, request: OutcomeRequest):
    return REPUTATION.record_verified_outcome(receipt_id=receipt_id, **request.model_dump())


@app.get("/v1/agents/{agent_npub}/reputation", dependencies=[Depends(require_internal_token)])
def get_reputation(agent_npub: str):
    return REPUTATION.get(agent_npub)


@app.post("/v1/identity/profile", dependencies=[Depends(require_internal_token)])
def sign_profile(request: ProfileRequest):
    return PROFILE_SIGNER.build_profile(**request.model_dump(), production=os.getenv("NODE_ENV", "development") == "production")


@app.post("/v1/ai-spend/rate-cards", dependencies=[Depends(require_internal_token)])
def create_ai_rate_card(request: RateCardCreate):
    return AI_SPEND.put_rate_card(**request.model_dump())


@app.post("/v1/ai-spend/policies", dependencies=[Depends(require_internal_token)])
def create_ai_spend_policy(request: TokenPolicyCreate):
    payload = request.model_dump()
    payload["allowed_routes"] = [route.model_dump() for route in request.allowed_routes]
    payload["fallback_routes"] = [route.model_dump() for route in request.fallback_routes]
    return AI_SPEND.create_policy(**payload)


@app.get("/v1/ai-spend/policies/{policy_id}", dependencies=[Depends(require_internal_token)])
def get_ai_spend_policy(policy_id: str):
    return AI_SPEND.get_policy(policy_id)


@app.post("/v1/ai-spend/policies/{policy_id}/stop", dependencies=[Depends(require_internal_token)])
def stop_ai_spend_policy(policy_id: str, request: EmergencyStopRequest):
    return AI_SPEND.emergency_stop(policy_id, reason=request.reason)


@app.get("/v1/ai-spend/policies/{policy_id}/summary", dependencies=[Depends(require_internal_token)])
def get_ai_spend_summary(policy_id: str):
    return AI_SPEND.summary(policy_id)


@app.post("/v1/ai-spend/preflight", dependencies=[Depends(require_internal_token)])
def ai_spend_preflight(request: TokenPreflightRequest):
    return AI_SPEND.preflight(**request.model_dump())


@app.post("/v1/ai-spend/reservations/{reservation_id}/finalize", dependencies=[Depends(require_internal_token)])
def finalize_ai_usage(reservation_id: str, request: TokenFinalizeRequest):
    return AI_SPEND.finalize_usage(reservation_id=reservation_id, **request.model_dump())


@app.get("/v1/ai-spend/receipts/{receipt_id}", dependencies=[Depends(require_internal_token)])
def get_ai_usage_receipt(receipt_id: str):
    return AI_SPEND.get_usage_receipt(receipt_id)


@app.post("/v1/ai-spend/reconcile", dependencies=[Depends(require_internal_token)])
def reconcile_ai_provider_usage(request: ProviderUsageEventRequest):
    return AI_SPEND.reconcile_provider_event(**request.model_dump())
