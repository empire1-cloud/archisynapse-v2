"""
Archisynapse API Gateway - Revenue Assurance Loop v1.
"""

import logging
import os
from datetime import datetime, timezone
from typing import Optional, Dict, Any
from uuid import UUID, uuid4

import httpx
from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, Field

from canonical_event import PaymentRequest, UnifiedReceipt
from orchestrator import orchestrator
from royalty_db import close_pool as close_royalty_pool, init_pool as init_royalty_pool
from royalty_routes import close_royalty_transaction_client, royalty_router
from stripe_routes import router as stripe_router
from runtime_state import (
    load_merchant_credentials,
    save_merchant_credentials,
    load_all_receipts,
    save_receipt,
)

logger = logging.getLogger("archisynapse.gateway")

FRAUD_SERVICE_URL = os.getenv("FRAUD_SERVICE_URL", "http://127.0.0.1:8080")
TRANSACTION_SERVICE_URL = os.getenv("TRANSACTION_SERVICE_URL", "http://127.0.0.1:3000")
LEDGER_SERVICE_URL = os.getenv("LEDGER_SERVICE_URL", "http://127.0.0.1:3001")
ANALYTICS_SERVICE_URL = os.getenv("ANALYTICS_SERVICE_URL", "http://127.0.0.1:8081")

app = FastAPI(
    title="Archisynapse API Gateway",
    description="Revenue Assurance Loop v1 - Canonical Event Processing",
    version="1.0.0",
)
app.include_router(royalty_router)
app.include_router(stripe_router)

receipt_store: Dict[str, UnifiedReceipt] = {}
correlation_store: Dict[str, str] = {}
request_store: Dict[str, UnifiedReceipt] = {}
merchant_credentials_store: Dict[str, Dict[str, str]] = load_merchant_credentials()


class ProcessPaymentRequest(BaseModel):
    merchant_id: UUID
    customer_id: str
    amount: float
    fee_amount: float = 0.0
    currency: str = "USD"
    payment_method: Optional[dict] = None
    payment_method_type: str = "CARD"
    payment_method_token: str = "tok_test_card"
    payment_method_last4: str = "4242"
    payment_method_brand: str = "VISA"
    description: Optional[str] = None
    metadata: dict = Field(default_factory=dict)
    idempotency_key: Optional[str] = None
    ip_address: Optional[str] = None
    country: Optional[str] = None
    device_id: Optional[str] = None
    email: Optional[str] = None
    session_id: Optional[str] = None


class MerchantBootstrapRequest(BaseModel):
    merchant_id: Optional[UUID] = None
    name: str
    plan: str = "growth"


class HealthResponse(BaseModel):
    status: str
    service: str
    version: str
    services: dict
    timestamp: str


@app.on_event("startup")
async def startup():
    # Load persisted receipts from disk
    saved = load_all_receipts()
    for event_id, receipt_data in saved.items():
        try:
            receipt_store[event_id] = UnifiedReceipt(**receipt_data)
        except Exception:
            pass
    for event_id, receipt in receipt_store.items():
        if receipt.correlation_id:
            correlation_store[receipt.correlation_id] = event_id
        if receipt.idempotency_key:
            request_store[_idempotency_store_key(receipt.merchant_id, receipt.idempotency_key)] = receipt
    logger.info(f"Loaded {len(receipt_store)} persisted receipts")

    fraud_key = os.getenv("FRAUD_API_KEY")
    analytics_key = os.getenv("ANALYTICS_API_KEY")
    if fraud_key and analytics_key and "mer_demo" not in merchant_credentials_store:
        merchant_credentials_store["mer_demo"] = {
            "fraud_api_key": fraud_key,
            "analytics_api_key": analytics_key,
        }
        save_merchant_credentials(merchant_credentials_store)
        logger.info("Loaded mer_demo credentials from environment")

    await init_royalty_pool()
    logger.info("Archisynapse API Gateway starting")


@app.on_event("shutdown")
async def shutdown():
    await orchestrator.close()
    await close_royalty_transaction_client()
    await close_royalty_pool()
    logger.info("Archisynapse API Gateway stopped")


def _idempotency_store_key(merchant_id: str, idempotency_key: str) -> str:
    return f"{merchant_id}:{idempotency_key}"


def _merchant_id_value(raw: UUID | str) -> str:
    return str(raw)


async def ensure_merchant_credentials(merchant_id: str, name: str = "Gateway Merchant", plan: str = "growth") -> Dict[str, str]:
    cached = merchant_credentials_store.get(merchant_id)
    if cached:
        return cached

    async with httpx.AsyncClient(timeout=15.0) as client:
        fraud_resp = await client.post(
            f"{FRAUD_SERVICE_URL}/admin/merchants",
            json={"merchant_id": merchant_id, "name": name},
        )
        analytics_resp = await client.post(
            f"{ANALYTICS_SERVICE_URL}/admin/merchants",
            json={"merchant_id": merchant_id, "name": name, "plan": plan},
        )

    if fraud_resp.status_code != 200 or analytics_resp.status_code != 200:
        fraud_ok = fraud_resp.status_code == 200 or (fraud_resp.status_code == 400 and "already exists" in fraud_resp.text)
        analytics_ok = analytics_resp.status_code == 200 or (analytics_resp.status_code == 400 and "already exists" in analytics_resp.text)
        if not (fraud_ok and analytics_ok):
            raise HTTPException(
                status_code=502,
                detail={
                    "error": "merchant_bootstrap_failed",
                    "fraud_status": fraud_resp.status_code,
                    "fraud_body": fraud_resp.text[:200],
                    "analytics_status": analytics_resp.status_code,
                    "analytics_body": analytics_resp.text[:200],
                },
            )

    fraud_api_key = fraud_resp.json().get("api_key") if fraud_resp.status_code == 200 else None
    analytics_api_key = analytics_resp.json().get("api_key") if analytics_resp.status_code == 200 else None

    credentials = {
        "fraud_api_key": fraud_api_key,
        "analytics_api_key": analytics_api_key,
    }
    merchant_credentials_store[merchant_id] = credentials
    save_merchant_credentials(merchant_credentials_store)
    return credentials


async def refresh_receipt_state(receipt: UnifiedReceipt) -> UnifiedReceipt:
    if not receipt.transaction_id:
        return receipt

    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            response = await client.get(
                f"{TRANSACTION_SERVICE_URL}/payments/{receipt.transaction_id}",
                headers={"X-Organization-ID": receipt.merchant_id},
            )
        if response.status_code == 200:
            payment = response.json()
            status = str(payment.get("status", receipt.status)).lower()
            receipt.status = status
    except Exception:
        return receipt

    return receipt


def _resolve_payment_method(request: ProcessPaymentRequest) -> Dict[str, str]:
    if request.payment_method:
        return {
            "type": request.payment_method.get("type", request.payment_method_type),
            "token": request.payment_method.get("token", request.payment_method_token),
            "last4": request.payment_method.get("last4", request.payment_method_last4),
            "brand": request.payment_method.get("brand", request.payment_method_brand),
        }

    return {
        "type": request.payment_method_type,
        "token": request.payment_method_token,
        "last4": request.payment_method_last4,
        "brand": request.payment_method_brand,
    }


@app.get("/health", response_model=HealthResponse)
async def health_check():
    services: Dict[str, str] = {}
    async with httpx.AsyncClient(timeout=5.0) as client:
        for name, url in [
            ("fraud", FRAUD_SERVICE_URL),
            ("transaction", TRANSACTION_SERVICE_URL),
            ("ledger", LEDGER_SERVICE_URL),
            ("analytics", ANALYTICS_SERVICE_URL),
        ]:
            try:
                response = await client.get(f"{url}/health")
                services[name] = "healthy" if response.status_code == 200 else "degraded"
            except Exception:
                services[name] = "unavailable"

    return HealthResponse(
        status="healthy" if all(v == "healthy" for v in services.values()) else "degraded",
        service="archisynapse-gateway",
        version="1.0.0",
        services=services,
        timestamp=datetime.now(timezone.utc).isoformat(),
    )


@app.post("/admin/merchant/bootstrap")
async def bootstrap_merchant(request: MerchantBootstrapRequest):
    merchant_id = _merchant_id_value(request.merchant_id or uuid4())
    credentials = await ensure_merchant_credentials(merchant_id, request.name, request.plan)
    return {
        "merchant_id": merchant_id,
        "fraud_api_key": credentials.get("fraud_api_key"),
        "analytics_api_key": credentials.get("analytics_api_key"),
    }


@app.post("/admin/recovery/replay")
async def replay_recovery():
    results = await orchestrator.replay_pending_recoveries(merchant_credentials_store)
    return results


@app.post("/v1/revenue/process", response_model=UnifiedReceipt)
async def process_payment(request: ProcessPaymentRequest):
    merchant_id = _merchant_id_value(request.merchant_id)
    if request.idempotency_key:
        cache_key = _idempotency_store_key(merchant_id, request.idempotency_key)
        existing = request_store.get(cache_key)
        if existing:
            return existing

    payment_method = _resolve_payment_method(request)
    merchant_credentials = await ensure_merchant_credentials(merchant_id, f"Merchant {merchant_id}")

    payment_request = PaymentRequest(
        merchant_id=merchant_id,
        customer_id=request.customer_id,
        amount=request.amount,
        fee_amount=request.fee_amount,
        currency=request.currency,
        payment_method_type=payment_method["type"],
        payment_method_token=payment_method["token"],
        payment_method_last4=payment_method["last4"],
        payment_method_brand=payment_method["brand"],
        description=request.description,
        metadata=request.metadata,
        ip_address=request.ip_address,
        country=request.country,
        device_id=request.device_id,
        email=request.email,
        session_id=request.session_id,
        fraud_api_key=merchant_credentials.get("fraud_api_key"),
        analytics_api_key=merchant_credentials.get("analytics_api_key"),
    )

    receipt = await orchestrator.process_payment(
        payment_request,
        idempotency_key=request.idempotency_key,
    )

    receipt_store[receipt.event_id] = receipt
    correlation_store[receipt.correlation_id] = receipt.event_id
    if receipt.idempotency_key:
        request_store[_idempotency_store_key(receipt.merchant_id, receipt.idempotency_key)] = receipt
    save_receipt(receipt.model_dump())
    return receipt


@app.get("/v1/revenue/receipt/{event_id}", response_model=UnifiedReceipt)
async def get_receipt(event_id: str):
    if event_id not in receipt_store:
        raise HTTPException(status_code=404, detail="Receipt not found")
    receipt = await refresh_receipt_state(receipt_store[event_id])
    receipt_store[event_id] = receipt
    save_receipt(receipt.model_dump())
    return receipt


@app.get("/v1/revenue/status/{correlation_id}")
async def get_status(correlation_id: str):
    if correlation_id not in correlation_store:
        raise HTTPException(status_code=404, detail="Correlation ID not found")
    event_id = correlation_store[correlation_id]
    receipt = receipt_store[event_id]
    return {
        "correlation_id": correlation_id,
        "event_id": event_id,
        "status": receipt.status,
        "fraud_decision": receipt.fraud_decision,
        "transaction_id": receipt.transaction_id,
        "ledger_transaction_id": receipt.ledger_transaction_id,
        "analytics_recorded": receipt.analytics_recorded,
    }


@app.get("/v1/revenue/receipts")
async def list_receipts(limit: int = 20, status: Optional[str] = None):
    receipts = list(receipt_store.values())
    if status:
        receipts = [receipt for receipt in receipts if receipt.status == status]
    receipts.sort(key=lambda receipt: receipt.processed_at or "", reverse=True)
    return {"total": len(receipts), "receipts": receipts[:limit]}


@app.get("/v1/revenue/verify/{event_id}")
async def verify_receipt(event_id: str):
    if event_id not in receipt_store:
        raise HTTPException(status_code=404, detail="Receipt not found")
    receipt = await refresh_receipt_state(receipt_store[event_id])
    receipt_store[event_id] = receipt
    return {
        "event_id": event_id,
        "correlation_id": receipt.correlation_id,
        "status": receipt.status,
        "components": {
            "fraud_checked": receipt.fraud_decision is not None,
            "transaction_processed": receipt.transaction_id is not None,
            "ledger_posted": receipt.ledger_transaction_id is not None,
            "analytics_recorded": receipt.analytics_recorded,
        },
        "errors": {
            "fraud": receipt.fraud_error,
            "transaction": receipt.transaction_error,
            "ledger": receipt.ledger_error,
            "analytics": receipt.analytics_error,
        },
    }


class RefundRequest(BaseModel):
    amount: str
    reason: str = "customer_requested"
    idempotency_key: Optional[str] = None


@app.post("/v1/revenue/refund/{payment_id}")
async def refund_payment(payment_id: str, request: RefundRequest):
    """Refund a payment: calls transaction service, then reverses analytics revenue."""
    # Find the original receipt to get merchant_id and analytics key
    original_receipt = None
    for receipt in receipt_store.values():
        if receipt.transaction_id == payment_id:
            original_receipt = receipt
            break

    if not original_receipt:
        raise HTTPException(status_code=404, detail="Transaction ID not found in receipts")

    merchant_id = original_receipt.merchant_id
    analytics_api_key = merchant_credentials_store.get(merchant_id, {}).get("analytics_api_key")

    result = await orchestrator.process_refund(
        transaction_id=payment_id,
        merchant_id=merchant_id,
        amount=request.amount,
        reason=request.reason,
        idempotency_key=request.idempotency_key,
        analytics_api_key=analytics_api_key,
    )

    if not result["refund_succeeded"]:
        raise HTTPException(status_code=502, detail=result.get("error", "Refund failed"))

    return {
        "transaction_id": payment_id,
        "refund_succeeded": result["refund_succeeded"],
        "analytics_reversed": result.get("analytics_reversed", False),
        "refund": result.get("refund"),
    }


@app.post("/admin/recovery/replay")
async def replay_recovery():
    """Replay pending ledger/analytics recovery items."""
    result = await orchestrator.replay_pending_recoveries(merchant_credentials_store)
    return {"replayed": result}


@app.get("/dashboard", response_class=HTMLResponse)
async def dashboard():
    return """<!DOCTYPE html>
<html><head><title>Archisynapse Revenue Assurance Loop</title>
<style>
body{font-family:-apple-system,sans-serif;margin:40px;background:#f5f5f5}
.container{max-width:1200px;margin:0 auto}
h1{color:#1a1a2e}
.card{background:white;border-radius:8px;padding:20px;margin:10px 0;box-shadow:0 2px 4px rgba(0,0,0,.1)}
.status{padding:5px 10px;border-radius:4px;font-weight:bold}
.status.completed{background:#d4edda;color:#155724}
.status.failed,.status.blocked{background:#f8d7da;color:#721c24}
.status.review,.status.partial{background:#fff3cd;color:#856404}
table{width:100%;border-collapse:collapse}th,td{padding:8px;text-align:left;border-bottom:1px solid #ddd}
.flow{display:flex;align-items:center;gap:10px;margin:20px 0}
.flow-step{padding:10px 15px;background:#e9ecef;border-radius:4px}
.flow-arrow{font-size:20px}
</style></head><body>
<div class="container">
<h1>Archisynapse Revenue Assurance Loop v1</h1>
<div class="card"><h2>Revenue Event Flow</h2>
<div class="flow">
<div class="flow-step">API Gateway</div><div class="flow-arrow">&rarr;</div>
<div class="flow-step">Fraud</div><div class="flow-arrow">&rarr;</div>
<div class="flow-step">Transaction</div><div class="flow-arrow">&rarr;</div>
<div class="flow-step">Ledger</div><div class="flow-arrow">&rarr;</div>
<div class="flow-step">Analytics</div><div class="flow-arrow">&rarr;</div>
<div class="flow-step">Receipt</div>
</div></div>
</div></body></html>"""


@app.get("/status")
async def status():
    return {
        "gateway": {"version": "1.0.0", "port": 9000},
        "services": {
            "fraud": FRAUD_SERVICE_URL,
            "transaction": TRANSACTION_SERVICE_URL,
            "ledger": LEDGER_SERVICE_URL,
            "analytics": ANALYTICS_SERVICE_URL,
        },
        "known_merchants": sorted(merchant_credentials_store.keys()),
    }
