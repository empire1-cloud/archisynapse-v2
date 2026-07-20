"""
Durable, Postgres-backed state for the royalty receipt loop: receipts,
idempotency claims, rejections, tenant signing keys, tenant API-key
hashes. Replaces the earlier .runtime/*.json files -- financial-adjacent
state must not depend on process memory or a JSON file on one machine.

This module owns the gateway's OWN operational state. It never posts to
the ledger and never computes money math -- that's the transaction
service's job (see royalty_transaction_client.py).
"""

from datetime import datetime, timezone
from typing import Optional

from argon2 import PasswordHasher
from argon2.exceptions import VerifyMismatchError

from royalty_db import get_pool

PROCESSING_ABANDONED_AFTER_SECONDS = 30

_hasher = PasswordHasher()


# ---------------------------------------------------------------------------
# Idempotency: PROCESSING -> COMPLETED | FAILED, with abandoned-claim recovery.
# ---------------------------------------------------------------------------


async def claim_idempotency(tenant_id: str, idempotency_key: str, request_hash: str) -> dict:
    """
    Atomically claims (tenant_id, idempotency_key) or reports the state of
    an existing claim. Returns one of:
      {"outcome": "claimed"}                      -- caller should process
      {"outcome": "completed", "receipt_id": str}  -- caller returns 200 + stored receipt
      {"outcome": "conflict"}                      -- same key, different payload -> 409
      {"outcome": "processing"}                    -- original request still in flight
    """
    pool = get_pool()
    async with pool.acquire() as conn:
        async with conn.transaction():
            inserted = await conn.fetchrow(
                """
                INSERT INTO royalty_idempotency (tenant_id, idempotency_key, request_hash, status)
                VALUES ($1, $2, $3, 'PROCESSING')
                ON CONFLICT (tenant_id, idempotency_key) DO NOTHING
                RETURNING tenant_id
                """,
                tenant_id,
                idempotency_key,
                request_hash,
            )
            if inserted is not None:
                return {"outcome": "claimed"}

            existing = await conn.fetchrow(
                "SELECT * FROM royalty_idempotency WHERE tenant_id=$1 AND idempotency_key=$2 FOR UPDATE",
                tenant_id,
                idempotency_key,
            )
            if existing["request_hash"] != request_hash:
                return {"outcome": "conflict"}

            if existing["status"] == "COMPLETED":
                return {"outcome": "completed", "receipt_id": existing["receipt_id"]}

            if existing["status"] == "FAILED":
                # Original attempt errored before completing -- safe to
                # retry under the same key since it never committed.
                await conn.execute(
                    """
                    UPDATE royalty_idempotency
                    SET status='PROCESSING', claimed_at=now(), failure_reason=NULL
                    WHERE tenant_id=$1 AND idempotency_key=$2
                    """,
                    tenant_id,
                    idempotency_key,
                )
                return {"outcome": "claimed"}

            # status == PROCESSING: either genuinely in flight, or the
            # process that claimed it died before marking COMPLETED/FAILED.
            age_seconds = (datetime.now(timezone.utc) - existing["claimed_at"]).total_seconds()
            if age_seconds > PROCESSING_ABANDONED_AFTER_SECONDS:
                await conn.execute(
                    "UPDATE royalty_idempotency SET claimed_at=now() WHERE tenant_id=$1 AND idempotency_key=$2",
                    tenant_id,
                    idempotency_key,
                )
                return {"outcome": "claimed"}

            return {"outcome": "processing"}


async def complete_idempotency(tenant_id: str, idempotency_key: str, receipt_id: str) -> None:
    pool = get_pool()
    await pool.execute(
        """
        UPDATE royalty_idempotency
        SET status='COMPLETED', receipt_id=$3, completed_at=now()
        WHERE tenant_id=$1 AND idempotency_key=$2
        """,
        tenant_id,
        idempotency_key,
        receipt_id,
    )


async def fail_idempotency(tenant_id: str, idempotency_key: str, reason: str) -> None:
    pool = get_pool()
    await pool.execute(
        """
        UPDATE royalty_idempotency
        SET status='FAILED', failure_reason=$3, failed_at=now()
        WHERE tenant_id=$1 AND idempotency_key=$2
        """,
        tenant_id,
        idempotency_key,
        reason[:255],
    )


# ---------------------------------------------------------------------------
# Receipts
# ---------------------------------------------------------------------------


async def save_royalty_receipt(receipt: dict) -> None:
    import json

    pool = get_pool()
    await pool.execute(
        """
        INSERT INTO royalty_receipts (receipt_id, event_id, correlation_id, tenant_id, status, payload)
        VALUES ($1, $2, $3, $4, $5, $6)
        ON CONFLICT (receipt_id) DO UPDATE
        SET status=$5, payload=$6, updated_at=now()
        """,
        receipt["receipt_id"],
        receipt["event_id"],
        receipt["correlation_id"],
        receipt["tenant_id"],
        receipt["status"],
        json.dumps(receipt),
    )


async def load_royalty_receipt(receipt_id: str) -> Optional[dict]:
    pool = get_pool()
    row = await pool.fetchrow(
        "SELECT payload FROM royalty_receipts WHERE receipt_id=$1", receipt_id
    )
    if row is None:
        return None
    import json

    return json.loads(row["payload"])


# ---------------------------------------------------------------------------
# Rejections (signature/auth failures -- no financial objects created)
# ---------------------------------------------------------------------------


async def record_rejection(correlation_id: Optional[str], key_id: Optional[str], reason: str) -> None:
    pool = get_pool()
    await pool.execute(
        "INSERT INTO royalty_rejections (correlation_id, key_id, reason) VALUES ($1, $2, $3)",
        correlation_id,
        key_id,
        reason,
    )


async def list_rejections() -> list[dict]:
    pool = get_pool()
    rows = await pool.fetch(
        "SELECT correlation_id, key_id, reason, occurred_at FROM royalty_rejections ORDER BY occurred_at DESC LIMIT 500"
    )
    return [dict(r) for r in rows]


# ---------------------------------------------------------------------------
# Tenant ed25519 public keys
# ---------------------------------------------------------------------------


async def register_tenant_key(tenant_id: str, key_id: str, public_key_b64: str) -> None:
    pool = get_pool()
    await pool.execute(
        """
        INSERT INTO royalty_tenant_keys (tenant_id, key_id, public_key_b64)
        VALUES ($1, $2, $3)
        ON CONFLICT (tenant_id, key_id) DO UPDATE SET public_key_b64=$3, revoked_at=NULL
        """,
        tenant_id,
        key_id,
        public_key_b64,
    )


async def get_tenant_key(tenant_id: str, key_id: str) -> Optional[str]:
    pool = get_pool()
    row = await pool.fetchrow(
        "SELECT public_key_b64 FROM royalty_tenant_keys WHERE tenant_id=$1 AND key_id=$2 AND revoked_at IS NULL",
        tenant_id,
        key_id,
    )
    return row["public_key_b64"] if row else None


async def key_registered_to_any_tenant(key_id: str) -> bool:
    pool = get_pool()
    row = await pool.fetchrow(
        "SELECT 1 FROM royalty_tenant_keys WHERE key_id=$1 AND revoked_at IS NULL LIMIT 1", key_id
    )
    return row is not None


# ---------------------------------------------------------------------------
# Tenant API keys -- hashed (argon2id), never plaintext.
# ---------------------------------------------------------------------------


async def register_tenant_api_key(tenant_id: str, api_key: str) -> None:
    pool = get_pool()
    key_hash = _hasher.hash(api_key)
    await pool.execute(
        """
        INSERT INTO royalty_tenant_api_keys (tenant_id, api_key_hash)
        VALUES ($1, $2)
        ON CONFLICT (tenant_id) DO UPDATE SET api_key_hash=$2, rotated_at=now()
        """,
        tenant_id,
        key_hash,
    )


async def check_tenant_api_key(tenant_id: str, api_key: str) -> bool:
    pool = get_pool()
    row = await pool.fetchrow(
        "SELECT api_key_hash FROM royalty_tenant_api_keys WHERE tenant_id=$1", tenant_id
    )
    if row is None:
        return False
    try:
        _hasher.verify(row["api_key_hash"], api_key)
        return True
    except VerifyMismatchError:
        return False
