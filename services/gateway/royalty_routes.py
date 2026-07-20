"""
FastAPI routes for the Lyrica royalty receipt loop.
Included into the main gateway app (see main.py: app.include_router(royalty_router)).
"""

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

import royalty_state as state
from royalty_keys import gateway_receipt_signer, tenant_key_registry
from royalty_ledger_client import LedgerClient
from royalty_orchestrator import RoyaltyRejection, process_obligation_created, verify_and_parse

royalty_router = APIRouter()
royalty_ledger_client = LedgerClient()


async def close_royalty_ledger_client() -> None:
    await royalty_ledger_client.close()


class RegisterTenantKeyRequest(BaseModel):
    key_id: str
    public_key_b64: str


class RegisterTenantApiKeyRequest(BaseModel):
    api_key: str


@royalty_router.post("/admin/tenants/{tenant_id}/keys")
async def register_tenant_key(tenant_id: str, request: RegisterTenantKeyRequest):
    tenant_key_registry.register(tenant_id, request.key_id, request.public_key_b64)
    return {"tenant_id": tenant_id, "key_id": request.key_id, "registered": True}


@royalty_router.post("/admin/tenants/{tenant_id}/api-key")
async def register_tenant_api_key(tenant_id: str, request: RegisterTenantApiKeyRequest):
    state.register_tenant_api_key(tenant_id, request.api_key)
    return {"tenant_id": tenant_id, "registered": True}


@royalty_router.get("/api/v1/keys/{key_id}")
async def get_gateway_public_key(key_id: str):
    if key_id != gateway_receipt_signer.key_id:
        raise HTTPException(status_code=404, detail="Unknown key_id")
    return {"key_id": key_id, "alg": "ed25519", "public_key_b64": gateway_receipt_signer.public_key_b64}


@royalty_router.post("/api/v1/events")
async def ingest_royalty_event(request: Request):
    raw_body = await request.body()
    headers = {k.lower(): v for k, v in request.headers.items()}

    try:
        verified = verify_and_parse(raw_body, headers)
        receipt, status_code = await process_obligation_created(verified, royalty_ledger_client)
    except RoyaltyRejection as rejection:
        raise HTTPException(
            status_code=rejection.status_code,
            detail={"code": rejection.code, "message": rejection.message},
        )

    return receipt if status_code == 200 else _created(receipt)


def _created(receipt: dict):
    from fastapi.responses import JSONResponse

    return JSONResponse(status_code=201, content=receipt)


@royalty_router.get("/api/v1/receipts/{receipt_id}")
async def get_royalty_receipt(receipt_id: str):
    receipt = state.load_royalty_receipt(receipt_id)
    if not receipt:
        raise HTTPException(status_code=404, detail="Receipt not found")
    return receipt


@royalty_router.get("/api/v1/rejections")
async def list_royalty_rejections():
    return {"rejections": state.list_rejections()}
