"""Production-facing Archisynapse gateway.

Merchant identity, idempotency, and receipts are kept in PostgreSQL. Payment
receipts can be signed with Ed25519. The gateway does not claim production
settlement or direct card-network connectivity.
"""
from __future__ import annotations

import os
import secrets
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, Annotated, Literal

import httpx
from fastapi import Depends, FastAPI, Header, HTTPException, Query, status
from fastapi.encoders import jsonable_encoder
from pydantic import BaseModel, Field, field_validator, model_validator

from canonical_event import PaymentRequest as CanonicalPaymentRequest, UnifiedReceipt
from gateway_store import (
    AuthenticationError,
    CredentialCipher,
    CredentialConfigurationError,
    GatewayStore,
    IdempotencyConflict,
    IdempotencyInProgress,
    MerchantPrincipal,
    canonical_request_hash,
    generate_merchant_id,
)
from orchestrator import orchestrator
from receipt_proof import (
    ReceiptProofConfigurationError,
    ReceiptSigner,
    build_receipt_signer_from_env,
    verify_receipt,
)
from royalty_db import close_pool, get_pool, init_pool
from royalty_routes import close_royalty_transaction_client, royalty_router

FRAUD_SERVICE_URL = os.getenv("FRAUD_SERVICE_URL", "http://127.0.0.1:8000")
TRANSACTION_SERVICE_URL = os.getenv(
    "TRANSACTION_SERVICE_URL", "http://127.0.0.1:3000"
)
LEDGER_SERVICE_URL = os.getenv("LEDGER_SERVICE_URL", "http://127.0.0.1:3001")
ANALYTICS_SERVICE_URL = os.getenv(
    "ANALYTICS_SERVICE_URL", "http://127.0.0.1:8081"
)

app = FastAPI(
    title="Archisynapse",
    description="Payment orchestration, ledger evidence, and signed receipts.",
    version="0.3.0",
)
app.include_router(royalty_router)

_store: GatewayStore | None = None
_receipt_signer: ReceiptSigner | None = None


class MerchantCreateRequest(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    plan: str = Field(default="growth", min_length=1, max_length=50)
    environment: Literal["test", "live"] = "test"


class MerchantCreateResponse(BaseModel):
    merchant_id: str
    name: str
    plan: str
    api_key: str
    api_key_revealed_once: bool = True


class PaymentCreateRequest(BaseModel):
    customer_id: str = Field(min_length=1, max_length=255)
    amount: Decimal = Field(gt=0, max_digits=19, decimal_places=4)
    fee_amount: Decimal = Field(
        default=Decimal("0"), ge=0, max_digits=19, decimal_places=4
    )
    currency: str = Field(default="USD", min_length=3, max_length=3)
    payment_method_type: Literal["CARD", "BANK_TRANSFER", "WALLET"] = "CARD"
    payment_method_token: str = Field(min_length=1, max_length=255)
    payment_method_last4: str | None = Field(default=None, max_length=4)
    payment_method_brand: str | None = Field(default=None, max_length=50)
    description: str | None = Field(default=None, max_length=500)
    metadata: dict[str, Any] = Field(default_factory=dict)
    ip_address: str | None = Field(default=None, max_length=45)
    country: str | None = Field(default=None, max_length=2)
    device_id: str | None = Field(default=None, max_length=255)
    email: str | None = Field(default=None, max_length=320)
    session_id: str | None = Field(default=None, max_length=255)

    @field_validator("currency")
    @classmethod
    def normalize_currency(cls, value: str) -> str:
        return value.upper()

    @field_validator("country")
    @classmethod
    def normalize_country(cls, value: str | None) -> str | None:
        return value.upper() if value else value

    @model_validator(mode="after")
    def fee_cannot_exceed_amount(self) -> "PaymentCreateRequest":
        if self.fee_amount > self.amount:
            raise ValueError("fee_amount cannot exceed amount")
        return self


class RefundRequest(BaseModel):
    amount: Decimal = Field(gt=0, max_digits=19, decimal_places=4)
    reason: str = Field(default="customer_requested", min_length=1, max_length=500)


class HealthResponse(BaseModel):
    status: str
    service: str
    database: str
    merchant_key_encryption: str
    receipt_signing: str
    dependencies: dict[str, str]
    timestamp: str


def _build_cipher() -> CredentialCipher | None:
    raw = os.getenv("ARCHISYNAPSE_GATEWAY_MASTER_KEY", "").strip()
    if not raw:
        return None
    key_id = os.getenv("ARCHISYNAPSE_GATEWAY_KEY_ID", "gateway-v1")
    return CredentialCipher.from_base64(raw, key_id=key_id)


@app.on_event("startup")
async def startup() -> None:
    global _store, _receipt_signer
    pool = await init_pool()
    _store = GatewayStore(pool, _build_cipher())
    try:
        _receipt_signer = build_receipt_signer_from_env()
    except ReceiptProofConfigurationError as exc:
        raise RuntimeError(f"receipt signing configuration is invalid: {exc}") from exc


@app.on_event("shutdown")
async def shutdown() -> None:
    await orchestrator.close()
    await close_royalty_transaction_client()
    await close_pool()


def get_store() -> GatewayStore:
    if _store is None:
        raise HTTPException(status_code=503, detail="gateway database is not ready")
    return _store


def require_admin_token(
    token: Annotated[
        str | None, Header(alias="X-Archisynapse-Admin-Token")
    ] = None,
) -> None:
    expected = os.getenv("ARCHISYNAPSE_ADMIN_TOKEN", "")
    if not expected:
        raise HTTPException(status_code=503, detail="admin access is not configured")
    if token is None or not secrets.compare_digest(token, expected):
        raise HTTPException(status_code=403, detail="admin access denied")


async def require_merchant(
    authorization: Annotated[str | None, Header()] = None,
    x_api_key: Annotated[str | None, Header(alias="X-Api-Key")] = None,
    store: GatewayStore = Depends(get_store),
) -> MerchantPrincipal:
    api_key = x_api_key
    if authorization:
        scheme, _, value = authorization.partition(" ")
        if scheme.lower() == "bearer" and value:
            api_key = value
    if not api_key:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="merchant API key is required",
        )
    try:
        return await store.authenticate(api_key)
    except AuthenticationError as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail=str(exc)
        ) from exc


async def _create_internal_service_credentials(
    *, merchant_id: str, name: str, plan: str
) -> dict[str, str | None]:
    async with httpx.AsyncClient(timeout=15.0) as client:
        fraud_response = await client.post(
            f"{FRAUD_SERVICE_URL}/admin/merchants",
            json={"merchant_id": merchant_id, "name": name},
        )
        analytics_response = await client.post(
            f"{ANALYTICS_SERVICE_URL}/admin/merchants",
            json={"merchant_id": merchant_id, "name": name, "plan": plan},
        )
    if fraud_response.status_code != 200:
        raise HTTPException(
            status_code=502,
            detail={"error": "fraud merchant setup failed", "status": fraud_response.status_code},
        )
    if analytics_response.status_code != 200:
        raise HTTPException(
            status_code=502,
            detail={
                "error": "analytics merchant setup failed",
                "status": analytics_response.status_code,
            },
        )
    fraud_key = fraud_response.json().get("api_key")
    analytics_key = analytics_response.json().get("api_key")
    if not fraud_key or not analytics_key:
        raise HTTPException(
            status_code=502, detail="internal services did not return merchant credentials"
        )
    return {"fraud_api_key": fraud_key, "analytics_api_key": analytics_key}


async def _dependency_health(name: str, url: str) -> tuple[str, str]:
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            response = await client.get(f"{url}/health")
        return name, "healthy" if response.status_code == 200 else "degraded"
    except Exception:
        return name, "unavailable"


@app.get("/health", response_model=HealthResponse)
async def health() -> HealthResponse:
    database = "unavailable"
    try:
        async with get_pool().acquire() as connection:
            await connection.fetchval("SELECT 1")
        database = "healthy"
    except Exception:
        database = "unavailable"

    dependencies = dict(
        [
            await _dependency_health("fraud", FRAUD_SERVICE_URL),
            await _dependency_health("transaction", TRANSACTION_SERVICE_URL),
            await _dependency_health("ledger", LEDGER_SERVICE_URL),
            await _dependency_health("analytics", ANALYTICS_SERVICE_URL),
        ]
    )
    encryption = "configured" if _store and _store.cipher else "not_configured"
    signing = "configured" if _receipt_signer else "not_configured"
    overall = (
        "healthy"
        if database == "healthy" and all(value == "healthy" for value in dependencies.values())
        else "degraded"
    )
    return HealthResponse(
        status=overall,
        service="archisynapse-gateway",
        database=database,
        merchant_key_encryption=encryption,
        receipt_signing=signing,
        dependencies=dependencies,
        timestamp=datetime.now(timezone.utc).isoformat(),
    )


@app.get("/status")
async def system_status() -> dict[str, Any]:
    processor = os.getenv("ARCHISYNAPSE_PROCESSOR", "disabled").strip().lower()
    return {
        "service": "archisynapse-gateway",
        "version": "0.3.0",
        "processor": {
            "adapter": processor,
            "test_mode": processor == "stripe_test",
            "live_money": False,
        },
        "receipt_signing": {
            "configured": _receipt_signer is not None,
            "algorithm": "Ed25519" if _receipt_signer else None,
            "key_id": _receipt_signer.key_id if _receipt_signer else None,
        },
        "production_ready": False,
        "claims": {
            "settlement_speed": "not measured",
            "fee_savings": "not measured",
            "throughput": "not measured",
            "certifications": [],
        },
    }


@app.post(
    "/admin/merchants",
    response_model=MerchantCreateResponse,
    status_code=201,
    dependencies=[Depends(require_admin_token)],
)
async def create_merchant(
    request: MerchantCreateRequest,
    store: GatewayStore = Depends(get_store),
) -> MerchantCreateResponse:
    merchant_id = generate_merchant_id()
    try:
        credentials = await _create_internal_service_credentials(
            merchant_id=merchant_id, name=request.name, plan=request.plan
        )
        provisioned = await store.provision_merchant(
            merchant_id=merchant_id,
            name=request.name,
            plan=request.plan,
            service_credentials=credentials,
            environment=request.environment,
        )
    except CredentialConfigurationError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    return MerchantCreateResponse(
        merchant_id=provisioned.merchant_id,
        name=provisioned.name,
        plan=provisioned.plan,
        api_key=provisioned.api_key,
    )


@app.get("/v1/merchant/me")
async def merchant_me(
    merchant: MerchantPrincipal = Depends(require_merchant),
) -> dict[str, str]:
    return {
        "merchant_id": merchant.merchant_id,
        "name": merchant.name,
        "plan": merchant.plan,
        "key_id": merchant.key_id,
    }


@app.get("/v1/proof/key")
async def receipt_proof_key() -> dict[str, Any]:
    if _receipt_signer is None:
        raise HTTPException(status_code=404, detail="receipt signing is not configured")
    return {
        "algorithm": _receipt_signer.algorithm,
        "key_id": _receipt_signer.key_id,
        "public_key_b64": _receipt_signer.public_key_b64(),
    }


@app.post("/v1/payments", response_model=UnifiedReceipt, status_code=201)
@app.post("/v1/revenue/process", response_model=UnifiedReceipt, status_code=201)
async def process_payment(
    request: PaymentCreateRequest,
    idempotency_key: Annotated[str, Header(alias="Idempotency-Key", min_length=1)],
    merchant: MerchantPrincipal = Depends(require_merchant),
    store: GatewayStore = Depends(get_store),
) -> UnifiedReceipt:
    request_body = jsonable_encoder(request)
    request_hash = canonical_request_hash(
        {"merchant_id": merchant.merchant_id, "payment": request_body}
    )
    try:
        claim = await store.claim_idempotency(
            merchant_id=merchant.merchant_id,
            idempotency_key=idempotency_key,
            request_hash=request_hash,
        )
    except (IdempotencyConflict, IdempotencyInProgress) as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc

    if claim.state == "replay":
        existing = await store.receipt_for_idempotency(
            merchant_id=merchant.merchant_id, event_id=claim.event_id
        )
        if existing is None:
            raise HTTPException(
                status_code=500, detail="completed request is missing its receipt"
            )
        return UnifiedReceipt(**existing)

    try:
        credentials = await store.get_service_credentials(merchant.merchant_id)
        payment_request = CanonicalPaymentRequest(
            merchant_id=merchant.merchant_id,
            customer_id=request.customer_id,
            amount=request.amount,
            fee_amount=request.fee_amount,
            currency=request.currency,
            payment_method_type=request.payment_method_type,
            payment_method_token=request.payment_method_token,
            payment_method_last4=request.payment_method_last4 or "",
            payment_method_brand=request.payment_method_brand or "",
            description=request.description,
            metadata=request.metadata,
            ip_address=request.ip_address,
            country=request.country,
            device_id=request.device_id,
            email=request.email,
            session_id=request.session_id,
            fraud_api_key=credentials.get("fraud_api_key"),
            analytics_api_key=credentials.get("analytics_api_key"),
        )
        receipt = await orchestrator.process_payment(
            payment_request, idempotency_key=idempotency_key
        )
        payload = receipt.model_dump(mode="json")
        stored_payload = _receipt_signer.attach(payload) if _receipt_signer else payload
        await store.save_receipt(
            merchant_id=merchant.merchant_id,
            idempotency_key=idempotency_key,
            request_hash=request_hash,
            receipt=stored_payload,
        )
        await store.complete_idempotency(
            merchant_id=merchant.merchant_id,
            idempotency_key=idempotency_key,
            event_id=receipt.event_id,
        )
        return receipt
    except HTTPException:
        raise
    except Exception as exc:
        await store.fail_idempotency(
            merchant_id=merchant.merchant_id,
            idempotency_key=idempotency_key,
            reason=f"{type(exc).__name__}: {exc}",
        )
        raise HTTPException(status_code=502, detail="payment processing failed") from exc


@app.get("/v1/receipts")
async def list_receipts(
    limit: int = Query(default=20, ge=1, le=100),
    receipt_status: str | None = Query(default=None, alias="status"),
    merchant: MerchantPrincipal = Depends(require_merchant),
    store: GatewayStore = Depends(get_store),
) -> dict[str, Any]:
    receipts = await store.list_receipts(
        merchant_id=merchant.merchant_id, limit=limit, status=receipt_status
    )
    return {"total": len(receipts), "receipts": receipts}


@app.get("/v1/receipts/{event_id}", response_model=UnifiedReceipt)
async def get_receipt(
    event_id: str,
    merchant: MerchantPrincipal = Depends(require_merchant),
    store: GatewayStore = Depends(get_store),
) -> UnifiedReceipt:
    receipt = await store.get_receipt(
        merchant_id=merchant.merchant_id, event_id=event_id
    )
    if receipt is None:
        raise HTTPException(status_code=404, detail="receipt not found")
    return UnifiedReceipt(**receipt)


@app.get("/v1/receipts/{event_id}/evidence")
async def get_receipt_evidence(
    event_id: str,
    merchant: MerchantPrincipal = Depends(require_merchant),
    store: GatewayStore = Depends(get_store),
) -> dict[str, Any]:
    receipt = await store.get_receipt(
        merchant_id=merchant.merchant_id, event_id=event_id
    )
    if receipt is None:
        raise HTTPException(status_code=404, detail="receipt not found")
    valid, message = verify_receipt(receipt)
    return {"receipt": receipt, "signature_valid": valid, "verification": message}


@app.get("/v1/receipts/{event_id}/verify")
async def verify_stored_receipt(
    event_id: str,
    merchant: MerchantPrincipal = Depends(require_merchant),
    store: GatewayStore = Depends(get_store),
) -> dict[str, Any]:
    receipt = await store.get_receipt(
        merchant_id=merchant.merchant_id, event_id=event_id
    )
    if receipt is None:
        raise HTTPException(status_code=404, detail="receipt not found")
    valid, message = verify_receipt(receipt)
    proof = receipt.get("_proof") if isinstance(receipt, dict) else None
    return {
        "event_id": event_id,
        "valid": valid,
        "message": message,
        "proof": proof,
    }


@app.post("/v1/payments/{payment_id}/refund")
async def refund_payment(
    payment_id: str,
    request: RefundRequest,
    idempotency_key: Annotated[str, Header(alias="Idempotency-Key", min_length=1)],
    merchant: MerchantPrincipal = Depends(require_merchant),
    store: GatewayStore = Depends(get_store),
) -> dict[str, Any]:
    original = await store.get_receipt_by_transaction_id(
        merchant_id=merchant.merchant_id, transaction_id=payment_id
    )
    if original is None:
        raise HTTPException(status_code=404, detail="payment receipt not found")
    credentials = await store.get_service_credentials(merchant.merchant_id)
    result = await orchestrator.process_refund(
        transaction_id=payment_id,
        merchant_id=merchant.merchant_id,
        amount=format(request.amount, "f"),
        reason=request.reason,
        idempotency_key=idempotency_key,
        analytics_api_key=credentials.get("analytics_api_key"),
    )
    if not result.get("refund_succeeded"):
        raise HTTPException(status_code=502, detail="refund processing failed")
    return result
