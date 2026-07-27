"""
FastAPI routes for the Lyrica royalty receipt loop.
Included into the main gateway app (see main.py: app.include_router(royalty_router)).
"""

import os

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel

import royalty_state as state
from royalty_admin_auth import require_admin
from royalty_authz import (
    AuthorizationDenied,
    require_obligation_tenant,
    resolve_policy_principal,
)
from royalty_keys import gateway_receipt_signer
from royalty_orchestrator import (
    RoyaltyRejection,
    process_obligation_created,
    release_obligation,
    reverse_obligation,
    verify_and_parse,
)
from royalty_transaction_client import RoyaltyTransactionClient

royalty_router = APIRouter()
royalty_transaction_client = RoyaltyTransactionClient()


async def close_royalty_transaction_client() -> None:
    await royalty_transaction_client.close()


def _feature_enabled() -> bool:
    return os.getenv("ROYALTY_LOOP_ENABLED", "false").lower() == "true"


def _feature_disabled_response() -> JSONResponse:
    return JSONResponse(
        status_code=503,
        content={
            "code": "retry_later",
            "message": "Royalty loop is currently disabled",
            "retryable": True,
        },
    )


class RegisterTenantKeyRequest(BaseModel):
    key_id: str
    public_key_b64: str


class RegisterTenantApiKeyRequest(BaseModel):
    api_key: str


class ReverseRequest(BaseModel):
    reversal_event_id: str
    reversal_idempotency_key: str
    reason: str


def _bearer_token(request: Request) -> str | None:
    authorization = request.headers.get("authorization", "")
    if not authorization.startswith("Bearer "):
        return None
    return authorization[len("Bearer "):].strip() or None


async def _authorize_obligation_action(request: Request, event_id: str) -> tuple[str, dict]:
    """Resolve policy identity, then bind it to the persisted obligation.

    X-Tenant-Id is accepted only as an optional consistency assertion; it
    never grants access. The bearer principal chooses the scoped lookup,
    and the persisted transaction-service obligation is checked again
    before any release or reversal is attempted.
    """
    try:
        principal = await resolve_policy_principal(_bearer_token(request))
    except AuthorizationDenied as exc:
        raise HTTPException(
            status_code=403,
            detail={"code": "forbidden", "reason": exc.reason},
        ) from exc

    claimed_tenant = request.headers.get("x-tenant-id")
    if claimed_tenant and claimed_tenant != principal.tenant_id:
        raise HTTPException(
            status_code=403,
            detail={"code": "forbidden", "reason": "wrong_tenant"},
        )

    organization_id = principal.tenant_id
    try:
        obligation = await royalty_transaction_client.get_obligation(organization_id, event_id)
    except Exception as exc:
        raise HTTPException(
            status_code=503,
            detail={"code": "retry_later", "message": "transaction service unavailable"},
        ) from exc
    if obligation is None:
        raise HTTPException(
            status_code=404,
            detail={"code": "not_found", "message": "royalty obligation not found"},
        )

    try:
        require_obligation_tenant(principal, obligation["tenantId"])
    except AuthorizationDenied as exc:
        raise HTTPException(
            status_code=403,
            detail={"code": "forbidden", "reason": exc.reason},
        ) from exc
    return principal.tenant_id, obligation


@royalty_router.post("/admin/tenants/{tenant_id}/keys", dependencies=[Depends(require_admin)])
async def register_tenant_key(tenant_id: str, request: RegisterTenantKeyRequest):
    await state.register_tenant_key(tenant_id, request.key_id, request.public_key_b64)
    return {"tenant_id": tenant_id, "key_id": request.key_id, "registered": True}


@royalty_router.post("/admin/tenants/{tenant_id}/api-key", dependencies=[Depends(require_admin)])
async def register_tenant_api_key(tenant_id: str, request: RegisterTenantApiKeyRequest):
    await state.register_tenant_api_key(tenant_id, request.api_key)
    return {"tenant_id": tenant_id, "registered": True}


@royalty_router.get("/api/v1/keys/{key_id}")
async def get_gateway_public_key(key_id: str):
    if key_id != gateway_receipt_signer.key_id:
        raise HTTPException(status_code=404, detail="Unknown key_id")
    return {"key_id": key_id, "alg": "ed25519", "public_key_b64": gateway_receipt_signer.public_key_b64}


@royalty_router.post("/api/v1/events")
async def ingest_royalty_event(request: Request):
    if not _feature_enabled():
        return _feature_disabled_response()

    raw_body = await request.body()
    headers = {k.lower(): v for k, v in request.headers.items()}

    try:
        verified = await verify_and_parse(raw_body, headers)
        receipt, status_code = await process_obligation_created(verified, royalty_transaction_client)
    except RoyaltyRejection as rejection:
        if rejection.body is not None:
            return JSONResponse(status_code=rejection.status_code, content=rejection.body)
        raise HTTPException(
            status_code=rejection.status_code,
            detail={"code": rejection.code, "message": rejection.message},
        )

    if status_code in (200, 422):
        return JSONResponse(status_code=status_code, content=receipt)
    return JSONResponse(status_code=201, content=receipt)


@royalty_router.post("/api/v1/events/{event_id}/release")
async def release_royalty_event(event_id: str, request: Request):
    """
    Canonical path per spec/SPEC-royalty-loop-v1.md §5. tenant_id is
    resolved from the obligation's own record via the transaction
    service (release is looked up by event_id alone; the caller does
    not need to know the organization_id).
    """
    if not _feature_enabled():
        return _feature_disabled_response()

    tenant_id, _obligation = await _authorize_obligation_action(request, event_id)

    try:
        receipt, status_code = await release_obligation(tenant_id, event_id, royalty_transaction_client)
    except RoyaltyRejection as rejection:
        if rejection.body is not None:
            return JSONResponse(status_code=rejection.status_code, content=rejection.body)
        raise HTTPException(
            status_code=rejection.status_code,
            detail={"code": rejection.code, "message": rejection.message},
        )

    return JSONResponse(status_code=status_code, content=receipt)


@royalty_router.post("/api/v1/events/{event_id}/reverse")
async def reverse_royalty_event(event_id: str, request: Request, body: ReverseRequest):
    if not _feature_enabled():
        return _feature_disabled_response()

    tenant_id, _obligation = await _authorize_obligation_action(request, event_id)

    try:
        receipt, status_code = await reverse_obligation(
            tenant_id,
            event_id,
            body.reversal_event_id,
            body.reversal_idempotency_key,
            body.reason,
            royalty_transaction_client,
        )
    except RoyaltyRejection as rejection:
        if rejection.body is not None:
            return JSONResponse(status_code=rejection.status_code, content=rejection.body)
        raise HTTPException(
            status_code=rejection.status_code,
            detail={"code": rejection.code, "message": rejection.message},
        )

    return JSONResponse(status_code=status_code, content=receipt)


@royalty_router.get("/api/v1/receipts/{receipt_id}")
async def get_royalty_receipt(receipt_id: str):
    receipt = await state.load_royalty_receipt(receipt_id)
    if not receipt:
        raise HTTPException(status_code=404, detail="Receipt not found")
    return receipt


@royalty_router.get("/api/v1/rejections", dependencies=[Depends(require_admin)])
async def list_royalty_rejections():
    return {"rejections": await state.list_rejections()}
