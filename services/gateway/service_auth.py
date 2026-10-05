"""Outbound service tokens for gateway calls to internal services.

The ledger and transaction services only trust X-Organization-ID from a
caller that proves which internal service it is (service-auth.ts in each
service). The gateway proves it with `Authorization: Bearer <token>`.

Environment (each token may be given directly or as a file path; the file is
re-read on every call so a rotated token takes effect without a restart):

    ARCHISYNAPSE_GATEWAY_TO_TRANSACTION_TOKEN / ..._TOKEN_FILE   gateway -> transaction
    ARCHISYNAPSE_GATEWAY_TO_LEDGER_TOKEN      / ..._TOKEN_FILE   gateway -> ledger (read-only)

Each hop has its own variable name, so a shared environment can never hand
the gateway the transaction service's ledger write token by accident.

With no token configured no header is sent, and an enforcing service rejects
the call. That is deliberate: fail closed, never fall back to trust.
"""

from __future__ import annotations

import os
from typing import Mapping, Optional

TRANSACTION_PREFIX = "ARCHISYNAPSE_GATEWAY_TO_TRANSACTION"
LEDGER_PREFIX = "ARCHISYNAPSE_GATEWAY_TO_LEDGER"


def outbound_service_token(prefix: str, env: Optional[Mapping[str, str]] = None) -> Optional[str]:
    env = os.environ if env is None else env
    direct = (env.get(f"{prefix}_TOKEN") or "").strip()
    if direct:
        return direct
    path = (env.get(f"{prefix}_TOKEN_FILE") or "").strip()
    if path:
        with open(path, encoding="utf-8") as handle:
            value = handle.read().strip()
        return value or None
    return None


def _headers(prefix: str, organization_id: str, env: Optional[Mapping[str, str]]) -> dict[str, str]:
    headers = {"X-Organization-ID": organization_id}
    token = outbound_service_token(prefix, env)
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return headers


def transaction_service_headers(
    organization_id: str, env: Optional[Mapping[str, str]] = None
) -> dict[str, str]:
    """Headers for a gateway call to the transaction service."""
    return _headers(TRANSACTION_PREFIX, organization_id, env)


def ledger_service_headers(
    organization_id: str, env: Optional[Mapping[str, str]] = None
) -> dict[str, str]:
    """Headers for a gateway read from the ledger service."""
    return _headers(LEDGER_PREFIX, organization_id, env)
