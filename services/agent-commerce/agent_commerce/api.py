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
PAYMENTS = LndRestPaymentProvider(
    base_url=_required("BUYER_LND_REST"),
    macaroon_hex=_required("BUYER_LND_MACAROON"),
    verify_tls=os.getenv("BUYER_LND_VERIFY_TLS", "true").lower() == "true",
)
COMMERCE = AgentCommerceService(
    authorizations=AUTHORIZATIONS,
    receipts=RECEIPTS,
    payment_provider=PAYMENTS,
)

app = FastAPI(title="Archisynapse Agent Commerce Rail", version="1.0.0")


def require_internal_token(authorization: str | None = Header(default=None)) -> None:
    expected = _required("AGENT_COMMERCE_INTERNAL_TOKEN")
    if authorization != f"Bearer {expected}":
        raise HTTPException(status_code=401, detail="invalid internal token")


@app.exception_handler(AgentCommerceError)
async def commerce_error_handler(_, exc: AgentCommerceError):
    from fastapi.responses import JSONResponse

    return JSONResponse(status_code=422, content={"error": exc.code, "message": str(exc), "details": exc.details})


@app.get("/health")
def health() -> dict[str, Any]:
    return {"status": "ok", "service": "agent-commerce", "audit_chain_valid": DB.verify_audit_chain()}


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
    receipt = await reconcile_initiated_payment(
        authorizations=AUTHORIZATIONS,
        receipts=RECEIPTS,
        payment_provider=PAYMENTS,
        reservation_id=reservation_id,
    )
    if not receipt:
        reservation = AUTHORIZATIONS.get_reservation(reservation_id)
        if not reservation:
            raise HTTPException(status_code=404, detail="reservation not found")
        return {
            "status": reservation.status,
            "reconciled": False,
            "message": "provider has not independently proven settlement; no second payment was attempted",
        }
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
    return PROFILE_SIGNER.build_profile(
        **request.model_dump(),
        production=os.getenv("NODE_ENV", "development") == "production",
    )
