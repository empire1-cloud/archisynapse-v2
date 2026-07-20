"""
Admin authorization for royalty tenant/key administration endpoints.

Fails closed: if ROYALTY_ADMIN_TOKEN is not set in the environment,
every admin endpoint is disabled (503), not silently open. When it is
set, callers must present it as `Authorization: Bearer <token>`.

This is a founder/admin bearer token, not a per-tenant credential --
distinct from the tenant API keys registered *through* these endpoints.
"""

import hmac
import os

from fastapi import HTTPException, Request

ADMIN_TOKEN_ENV = "ROYALTY_ADMIN_TOKEN"


def require_admin(request: Request) -> None:
    admin_token = os.getenv(ADMIN_TOKEN_ENV)
    if not admin_token:
        raise HTTPException(
            status_code=503,
            detail={
                "code": "admin_disabled",
                "message": f"Royalty admin endpoints are disabled: {ADMIN_TOKEN_ENV} is not set",
            },
        )

    auth_header = request.headers.get("authorization", "")
    presented = auth_header[len("Bearer "):] if auth_header.startswith("Bearer ") else ""
    if not presented or not hmac.compare_digest(presented, admin_token):
        raise HTTPException(status_code=403, detail={"code": "forbidden", "message": "invalid admin token"})
