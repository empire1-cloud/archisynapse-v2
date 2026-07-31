"""Archisynapse merchant access lifecycle operator.

Provides audited PostgreSQL-backed API-key rotation, revocation, merchant
suspension, and safe resumption. New API keys are returned exactly once.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Literal

import asyncpg

REPO_ROOT = Path(__file__).resolve().parents[2]
GATEWAY_DIR = REPO_ROOT / "services" / "gateway"
if str(GATEWAY_DIR) not in sys.path:
    sys.path.insert(0, str(GATEWAY_DIR))

from gateway_store import generate_merchant_api_key, hash_api_key  # noqa: E402

Environment = Literal["test", "live"]


class MerchantAdminError(RuntimeError):
    """Raised when a lifecycle operation cannot be completed safely."""


@dataclass(frozen=True)
class LifecycleResult:
    merchant_id: str
    merchant_status: str
    action: str
    key_id: str | None = None
    api_key: str | None = None
    revoked_keys: int = 0


def _database_url() -> str:
    value = os.getenv("DATABASE_URL", "").strip()
    if not value:
        raise MerchantAdminError("DATABASE_URL is required")
    return value


async def _merchant_for_update(connection: Any, merchant_id: str) -> Any:
    row = await connection.fetchrow(
        """
        SELECT merchant_id, name, plan, status
          FROM gateway_merchants
         WHERE merchant_id = $1
         FOR UPDATE
        """,
        merchant_id,
    )
    if row is None:
        raise MerchantAdminError("merchant not found")
    return row


async def _audit(
    connection: Any,
    *,
    merchant_id: str,
    event_type: str,
    details: dict[str, Any],
) -> None:
    await connection.execute(
        """
        INSERT INTO gateway_audit_events (merchant_id, event_type, details)
        VALUES ($1, $2, $3::jsonb)
        """,
        merchant_id,
        event_type,
        json.dumps(details, sort_keys=True),
    )


async def _revoke_active_keys(connection: Any, merchant_id: str) -> int:
    result = await connection.execute(
        """
        UPDATE gateway_merchant_api_keys
           SET status = 'REVOKED', revoked_at = COALESCE(revoked_at, now())
         WHERE merchant_id = $1 AND status = 'ACTIVE'
        """,
        merchant_id,
    )
    try:
        return int(str(result).rsplit(" ", 1)[-1])
    except (TypeError, ValueError):
        return 0


async def _issue_key(
    connection: Any,
    *,
    merchant_id: str,
    environment: Environment,
) -> tuple[str, str]:
    key_id, api_key, key_prefix = generate_merchant_api_key(environment)
    await connection.execute(
        """
        INSERT INTO gateway_merchant_api_keys (
            key_id, merchant_id, key_prefix, api_key_hash, environment, status
        ) VALUES ($1, $2, $3, $4, $5, 'ACTIVE')
        """,
        key_id,
        merchant_id,
        key_prefix,
        hash_api_key(api_key),
        environment,
    )
    return key_id, api_key


async def rotate_key(
    pool: Any,
    *,
    merchant_id: str,
    environment: Environment = "test",
) -> LifecycleResult:
    async with pool.acquire() as connection:
        async with connection.transaction():
            merchant = await _merchant_for_update(connection, merchant_id)
            if merchant["status"] != "ACTIVE":
                raise MerchantAdminError("merchant must be ACTIVE before key rotation")
            revoked = await _revoke_active_keys(connection, merchant_id)
            key_id, api_key = await _issue_key(
                connection, merchant_id=merchant_id, environment=environment
            )
            await _audit(
                connection,
                merchant_id=merchant_id,
                event_type="merchant.api_key.rotated",
                details={
                    "key_id": key_id,
                    "environment": environment,
                    "revoked_keys": revoked,
                },
            )
    return LifecycleResult(
        merchant_id=merchant_id,
        merchant_status="ACTIVE",
        action="ROTATED",
        key_id=key_id,
        api_key=api_key,
        revoked_keys=revoked,
    )


async def revoke_key(
    pool: Any,
    *,
    merchant_id: str,
    key_id: str,
) -> LifecycleResult:
    async with pool.acquire() as connection:
        async with connection.transaction():
            merchant = await _merchant_for_update(connection, merchant_id)
            row = await connection.fetchrow(
                """
                UPDATE gateway_merchant_api_keys
                   SET status = 'REVOKED', revoked_at = COALESCE(revoked_at, now())
                 WHERE merchant_id = $1 AND key_id = $2 AND status = 'ACTIVE'
                RETURNING key_id
                """,
                merchant_id,
                key_id,
            )
            if row is None:
                raise MerchantAdminError("active API key not found for merchant")
            await _audit(
                connection,
                merchant_id=merchant_id,
                event_type="merchant.api_key.revoked",
                details={"key_id": key_id},
            )
    return LifecycleResult(
        merchant_id=merchant_id,
        merchant_status=merchant["status"],
        action="REVOKED",
        key_id=key_id,
        revoked_keys=1,
    )


async def suspend_merchant(pool: Any, *, merchant_id: str) -> LifecycleResult:
    async with pool.acquire() as connection:
        async with connection.transaction():
            merchant = await _merchant_for_update(connection, merchant_id)
            if merchant["status"] == "CLOSED":
                raise MerchantAdminError("closed merchant cannot be suspended")
            revoked = await _revoke_active_keys(connection, merchant_id)
            await connection.execute(
                """
                UPDATE gateway_merchants
                   SET status = 'SUSPENDED', updated_at = now()
                 WHERE merchant_id = $1
                """,
                merchant_id,
            )
            await _audit(
                connection,
                merchant_id=merchant_id,
                event_type="merchant.suspended",
                details={"revoked_keys": revoked},
            )
    return LifecycleResult(
        merchant_id=merchant_id,
        merchant_status="SUSPENDED",
        action="SUSPENDED",
        revoked_keys=revoked,
    )


async def resume_merchant(
    pool: Any,
    *,
    merchant_id: str,
    environment: Environment = "test",
) -> LifecycleResult:
    async with pool.acquire() as connection:
        async with connection.transaction():
            merchant = await _merchant_for_update(connection, merchant_id)
            if merchant["status"] != "SUSPENDED":
                raise MerchantAdminError("only a SUSPENDED merchant can be resumed")
            revoked = await _revoke_active_keys(connection, merchant_id)
            key_id, api_key = await _issue_key(
                connection, merchant_id=merchant_id, environment=environment
            )
            await connection.execute(
                """
                UPDATE gateway_merchants
                   SET status = 'ACTIVE', updated_at = now()
                 WHERE merchant_id = $1
                """,
                merchant_id,
            )
            await _audit(
                connection,
                merchant_id=merchant_id,
                event_type="merchant.resumed",
                details={
                    "key_id": key_id,
                    "environment": environment,
                    "revoked_keys": revoked,
                },
            )
    return LifecycleResult(
        merchant_id=merchant_id,
        merchant_status="ACTIVE",
        action="RESUMED",
        key_id=key_id,
        api_key=api_key,
        revoked_keys=revoked,
    )


async def list_access(pool: Any, *, merchant_id: str) -> dict[str, Any]:
    async with pool.acquire() as connection:
        merchant = await connection.fetchrow(
            """
            SELECT merchant_id, name, plan, status, created_at, updated_at
              FROM gateway_merchants
             WHERE merchant_id = $1
            """,
            merchant_id,
        )
        if merchant is None:
            raise MerchantAdminError("merchant not found")
        keys = await connection.fetch(
            """
            SELECT key_id, key_prefix, environment, status, created_at,
                   last_used_at, revoked_at
              FROM gateway_merchant_api_keys
             WHERE merchant_id = $1
             ORDER BY created_at DESC
            """,
            merchant_id,
        )
    return {"merchant": dict(merchant), "keys": [dict(row) for row in keys]}


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Archisynapse merchant access lifecycle")
    commands = parser.add_subparsers(dest="command", required=True)
    for name in ("rotate-key", "resume"):
        command = commands.add_parser(name)
        command.add_argument("merchant_id")
        command.add_argument("--environment", choices=("test", "live"), default="test")
    for name in ("suspend", "show"):
        command = commands.add_parser(name)
        command.add_argument("merchant_id")
    revoke = commands.add_parser("revoke-key")
    revoke.add_argument("merchant_id")
    revoke.add_argument("key_id")
    return parser


async def _run(args: argparse.Namespace) -> dict[str, Any]:
    pool = await asyncpg.create_pool(_database_url(), min_size=1, max_size=2)
    try:
        if args.command == "rotate-key":
            result = await rotate_key(
                pool, merchant_id=args.merchant_id, environment=args.environment
            )
            return asdict(result)
        if args.command == "revoke-key":
            return asdict(
                await revoke_key(pool, merchant_id=args.merchant_id, key_id=args.key_id)
            )
        if args.command == "suspend":
            return asdict(await suspend_merchant(pool, merchant_id=args.merchant_id))
        if args.command == "resume":
            result = await resume_merchant(
                pool, merchant_id=args.merchant_id, environment=args.environment
            )
            return asdict(result)
        return await list_access(pool, merchant_id=args.merchant_id)
    finally:
        await pool.close()


def main() -> int:
    try:
        output = asyncio.run(_run(_parser().parse_args()))
    except MerchantAdminError as exc:
        print(json.dumps({"status": "REFUSED", "error": str(exc)}), file=sys.stderr)
        return 2
    print(json.dumps(output, default=str, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
