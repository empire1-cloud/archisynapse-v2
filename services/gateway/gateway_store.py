"""Durable merchant identity, idempotency, and receipt storage for Archisynapse.

Financial truth remains in the transaction and ledger services. This module owns
only gateway identity and operational state.
"""

from __future__ import annotations

import base64
import hashlib
import json
import secrets
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, Mapping

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerifyMismatchError
from cryptography.hazmat.primitives.ciphers.aead import AESGCM


_PASSWORD_HASHER = PasswordHasher()
_ALLOWED_ENVIRONMENTS = {"test", "live"}


class GatewayStoreError(RuntimeError):
    """Base gateway-store error."""


class AuthenticationError(GatewayStoreError):
    """Raised when a merchant API key is missing, malformed, or invalid."""


class IdempotencyConflict(GatewayStoreError):
    """Raised when one idempotency key is reused for a different request."""


class IdempotencyInProgress(GatewayStoreError):
    """Raised while another worker still owns the same request."""


class CredentialConfigurationError(GatewayStoreError):
    """Raised when encrypted internal credentials cannot be configured."""


@dataclass(frozen=True)
class MerchantPrincipal:
    merchant_id: str
    name: str
    plan: str
    key_id: str


@dataclass(frozen=True)
class ProvisionedMerchant:
    merchant_id: str
    api_key: str
    key_id: str
    name: str
    plan: str


@dataclass(frozen=True)
class IdempotencyClaim:
    state: str
    event_id: str | None = None


class CredentialCipher:
    """Encrypt internal service credentials with an environment-managed key."""

    def __init__(self, key: bytes, *, key_id: str = "gateway-v1") -> None:
        if len(key) != 32:
            raise CredentialConfigurationError("gateway master key must decode to 32 bytes")
        self._cipher = AESGCM(key)
        self.key_id = key_id

    @classmethod
    def from_base64(cls, value: str, *, key_id: str = "gateway-v1") -> "CredentialCipher":
        try:
            padded = value + "=" * (-len(value) % 4)
            key = base64.urlsafe_b64decode(padded.encode("ascii"))
        except Exception as exc:
            raise CredentialConfigurationError("gateway master key is not valid base64") from exc
        return cls(key, key_id=key_id)

    def encrypt(self, merchant_id: str, credentials: Mapping[str, str | None]) -> bytes:
        plaintext = json.dumps(
            dict(credentials), sort_keys=True, separators=(",", ":")
        ).encode("utf-8")
        nonce = secrets.token_bytes(12)
        ciphertext = self._cipher.encrypt(nonce, plaintext, merchant_id.encode("utf-8"))
        return nonce + ciphertext

    def decrypt(self, merchant_id: str, encrypted: bytes) -> dict[str, str | None]:
        if len(encrypted) < 13:
            raise CredentialConfigurationError("encrypted credentials are invalid")
        nonce, ciphertext = encrypted[:12], encrypted[12:]
        plaintext = self._cipher.decrypt(
            nonce, ciphertext, merchant_id.encode("utf-8")
        )
        value = json.loads(plaintext.decode("utf-8"))
        if not isinstance(value, dict):
            raise CredentialConfigurationError("decrypted credentials are invalid")
        return value


def generate_merchant_id() -> str:
    return f"mer_{secrets.token_hex(12)}"


def generate_merchant_api_key(environment: str = "test") -> tuple[str, str, str]:
    if environment not in _ALLOWED_ENVIRONMENTS:
        raise ValueError("environment must be test or live")
    key_id = secrets.token_hex(8)
    secret = secrets.token_urlsafe(32)
    api_key = f"arch_{environment}_{key_id}_{secret}"
    return key_id, api_key, f"arch_{environment}_{key_id}"


def parse_merchant_api_key(api_key: str) -> tuple[str, str]:
    parts = api_key.split("_", 3)
    if len(parts) != 4 or parts[0] != "arch" or parts[1] not in _ALLOWED_ENVIRONMENTS:
        raise AuthenticationError("invalid merchant API key")
    key_id = parts[2]
    secret = parts[3]
    if len(key_id) != 16 or not secret:
        raise AuthenticationError("invalid merchant API key")
    return parts[1], key_id


def hash_api_key(api_key: str) -> str:
    return _PASSWORD_HASHER.hash(api_key)


def verify_api_key(api_key_hash: str, api_key: str) -> bool:
    try:
        return _PASSWORD_HASHER.verify(api_key_hash, api_key)
    except (VerifyMismatchError, InvalidHashError):
        return False


def canonical_request_hash(value: Any) -> str:
    def default(item: Any) -> Any:
        if isinstance(item, Decimal):
            return format(item, "f")
        if isinstance(item, datetime):
            return item.astimezone(timezone.utc).isoformat()
        raise TypeError(f"unsupported request value: {type(item).__name__}")

    body = json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        default=default,
    )
    return hashlib.sha256(body.encode("utf-8")).hexdigest()


def _decode_json(value: Any) -> Any:
    if isinstance(value, str):
        return json.loads(value)
    return value


class GatewayStore:
    """PostgreSQL-backed operational store for the public gateway."""

    def __init__(self, pool: Any, cipher: CredentialCipher | None) -> None:
        self.pool = pool
        self.cipher = cipher

    async def provision_merchant(
        self,
        *,
        merchant_id: str,
        name: str,
        plan: str,
        service_credentials: Mapping[str, str | None],
        environment: str = "test",
    ) -> ProvisionedMerchant:
        if self.cipher is None:
            raise CredentialConfigurationError(
                "ARCHISYNAPSE_GATEWAY_MASTER_KEY is required to provision merchants"
            )
        if not name.strip():
            raise ValueError("merchant name is required")
        key_id, api_key, key_prefix = generate_merchant_api_key(environment)
        encrypted = self.cipher.encrypt(merchant_id, service_credentials)
        api_key_hash = hash_api_key(api_key)

        async with self.pool.acquire() as connection:
            async with connection.transaction():
                await connection.execute(
                    """
                    INSERT INTO gateway_merchants (
                        merchant_id, name, plan, encrypted_service_credentials,
                        credentials_key_id, status
                    ) VALUES ($1, $2, $3, $4, $5, 'ACTIVE')
                    """,
                    merchant_id,
                    name.strip(),
                    plan,
                    encrypted,
                    self.cipher.key_id,
                )
                await connection.execute(
                    """
                    INSERT INTO gateway_merchant_api_keys (
                        key_id, merchant_id, key_prefix, api_key_hash, environment, status
                    ) VALUES ($1, $2, $3, $4, $5, 'ACTIVE')
                    """,
                    key_id,
                    merchant_id,
                    key_prefix,
                    api_key_hash,
                    environment,
                )
                await self._audit(
                    connection,
                    merchant_id=merchant_id,
                    event_type="merchant.provisioned",
                    details={"plan": plan, "environment": environment, "key_id": key_id},
                )
        return ProvisionedMerchant(
            merchant_id=merchant_id,
            api_key=api_key,
            key_id=key_id,
            name=name.strip(),
            plan=plan,
        )

    async def authenticate(self, api_key: str) -> MerchantPrincipal:
        _, key_id = parse_merchant_api_key(api_key)
        async with self.pool.acquire() as connection:
            row = await connection.fetchrow(
                """
                SELECT k.key_id, k.api_key_hash, k.status AS key_status,
                       m.merchant_id, m.name, m.plan, m.status AS merchant_status
                  FROM gateway_merchant_api_keys k
                  JOIN gateway_merchants m ON m.merchant_id = k.merchant_id
                 WHERE k.key_id = $1
                """,
                key_id,
            )
            if (
                row is None
                or row["key_status"] != "ACTIVE"
                or row["merchant_status"] != "ACTIVE"
                or not verify_api_key(row["api_key_hash"], api_key)
            ):
                raise AuthenticationError("invalid merchant API key")
            await connection.execute(
                "UPDATE gateway_merchant_api_keys SET last_used_at = now() WHERE key_id = $1",
                key_id,
            )
            return MerchantPrincipal(
                merchant_id=row["merchant_id"],
                name=row["name"],
                plan=row["plan"],
                key_id=row["key_id"],
            )

    async def get_service_credentials(self, merchant_id: str) -> dict[str, str | None]:
        if self.cipher is None:
            raise CredentialConfigurationError("gateway master key is not configured")
        async with self.pool.acquire() as connection:
            row = await connection.fetchrow(
                """
                SELECT encrypted_service_credentials, credentials_key_id
                  FROM gateway_merchants
                 WHERE merchant_id = $1 AND status = 'ACTIVE'
                """,
                merchant_id,
            )
        if row is None or row["encrypted_service_credentials"] is None:
            raise AuthenticationError("merchant service credentials are unavailable")
        if row["credentials_key_id"] != self.cipher.key_id:
            raise CredentialConfigurationError("merchant credentials use an unknown key id")
        return self.cipher.decrypt(merchant_id, bytes(row["encrypted_service_credentials"]))

    async def claim_idempotency(
        self,
        *,
        merchant_id: str,
        idempotency_key: str,
        request_hash: str,
        reclaim_after_seconds: int = 300,
    ) -> IdempotencyClaim:
        if not idempotency_key.strip():
            raise ValueError("Idempotency-Key is required")
        async with self.pool.acquire() as connection:
            async with connection.transaction():
                inserted = await connection.fetchrow(
                    """
                    INSERT INTO gateway_payment_idempotency (
                        merchant_id, idempotency_key, request_hash, status
                    ) VALUES ($1, $2, $3, 'PROCESSING')
                    ON CONFLICT (merchant_id, idempotency_key) DO NOTHING
                    RETURNING merchant_id
                    """,
                    merchant_id,
                    idempotency_key,
                    request_hash,
                )
                if inserted is not None:
                    return IdempotencyClaim(state="new")

                row = await connection.fetchrow(
                    """
                    SELECT request_hash, status, event_id, claimed_at
                      FROM gateway_payment_idempotency
                     WHERE merchant_id = $1 AND idempotency_key = $2
                     FOR UPDATE
                    """,
                    merchant_id,
                    idempotency_key,
                )
                if row is None:
                    raise GatewayStoreError("idempotency row disappeared")
                if row["request_hash"] != request_hash:
                    raise IdempotencyConflict(
                        "Idempotency-Key was already used for a different request"
                    )
                if row["status"] == "COMPLETED":
                    return IdempotencyClaim(state="replay", event_id=row["event_id"])
                if row["status"] == "PROCESSING":
                    reclaimed = await connection.fetchrow(
                        """
                        UPDATE gateway_payment_idempotency
                           SET claimed_at = now(), failed_at = NULL, failure_reason = NULL
                         WHERE merchant_id = $1 AND idempotency_key = $2
                           AND claimed_at <= now() - ($3 * interval '1 second')
                        RETURNING merchant_id
                        """,
                        merchant_id,
                        idempotency_key,
                        reclaim_after_seconds,
                    )
                    if reclaimed is None:
                        raise IdempotencyInProgress(
                            "another request is already processing this Idempotency-Key"
                        )
                    return IdempotencyClaim(state="reclaimed")

                await connection.execute(
                    """
                    UPDATE gateway_payment_idempotency
                       SET status = 'PROCESSING', claimed_at = now(),
                           failed_at = NULL, failure_reason = NULL
                     WHERE merchant_id = $1 AND idempotency_key = $2
                    """,
                    merchant_id,
                    idempotency_key,
                )
                return IdempotencyClaim(state="retry")

    async def save_receipt(
        self,
        *,
        merchant_id: str,
        idempotency_key: str,
        request_hash: str,
        receipt: Mapping[str, Any],
    ) -> None:
        event_id = str(receipt["event_id"])
        correlation_id = str(receipt["correlation_id"])
        status = str(receipt["status"])
        payload = json.dumps(dict(receipt), sort_keys=True, default=str)
        async with self.pool.acquire() as connection:
            await connection.execute(
                """
                INSERT INTO gateway_payment_receipts (
                    event_id, merchant_id, correlation_id, idempotency_key,
                    request_hash, status, payload
                ) VALUES ($1, $2, $3, $4, $5, $6, $7::jsonb)
                ON CONFLICT (event_id) DO UPDATE
                   SET status = EXCLUDED.status,
                       payload = EXCLUDED.payload,
                       updated_at = now()
                """,
                event_id,
                merchant_id,
                correlation_id,
                idempotency_key,
                request_hash,
                status,
                payload,
            )

    async def complete_idempotency(
        self, *, merchant_id: str, idempotency_key: str, event_id: str
    ) -> None:
        async with self.pool.acquire() as connection:
            await connection.execute(
                """
                UPDATE gateway_payment_idempotency
                   SET status = 'COMPLETED', event_id = $3, completed_at = now(),
                       failure_reason = NULL, failed_at = NULL
                 WHERE merchant_id = $1 AND idempotency_key = $2
                """,
                merchant_id,
                idempotency_key,
                event_id,
            )

    async def fail_idempotency(
        self, *, merchant_id: str, idempotency_key: str, reason: str
    ) -> None:
        async with self.pool.acquire() as connection:
            await connection.execute(
                """
                UPDATE gateway_payment_idempotency
                   SET status = 'FAILED', failure_reason = $3, failed_at = now()
                 WHERE merchant_id = $1 AND idempotency_key = $2
                """,
                merchant_id,
                idempotency_key,
                reason[:500],
            )

    async def get_receipt(self, *, merchant_id: str, event_id: str) -> dict[str, Any] | None:
        async with self.pool.acquire() as connection:
            row = await connection.fetchrow(
                """
                SELECT payload FROM gateway_payment_receipts
                 WHERE merchant_id = $1 AND event_id = $2
                """,
                merchant_id,
                event_id,
            )
        return _decode_json(row["payload"]) if row else None

    async def get_receipt_by_transaction_id(
        self, *, merchant_id: str, transaction_id: str
    ) -> dict[str, Any] | None:
        async with self.pool.acquire() as connection:
            row = await connection.fetchrow(
                """
                SELECT payload FROM gateway_payment_receipts
                 WHERE merchant_id = $1
                   AND payload->>'transaction_id' = $2
                 ORDER BY created_at DESC
                 LIMIT 1
                """,
                merchant_id,
                transaction_id,
            )
        return _decode_json(row["payload"]) if row else None

    async def list_receipts(
        self, *, merchant_id: str, limit: int = 20, status: str | None = None
    ) -> list[dict[str, Any]]:
        limit = max(1, min(limit, 100))
        async with self.pool.acquire() as connection:
            if status:
                rows = await connection.fetch(
                    """
                    SELECT payload FROM gateway_payment_receipts
                     WHERE merchant_id = $1 AND status = $2
                     ORDER BY created_at DESC LIMIT $3
                    """,
                    merchant_id,
                    status,
                    limit,
                )
            else:
                rows = await connection.fetch(
                    """
                    SELECT payload FROM gateway_payment_receipts
                     WHERE merchant_id = $1
                     ORDER BY created_at DESC LIMIT $2
                    """,
                    merchant_id,
                    limit,
                )
        return [_decode_json(row["payload"]) for row in rows]

    async def receipt_for_idempotency(
        self, *, merchant_id: str, event_id: str | None
    ) -> dict[str, Any] | None:
        if event_id is None:
            return None
        return await self.get_receipt(merchant_id=merchant_id, event_id=event_id)

    async def _audit(
        self,
        connection: Any,
        *,
        merchant_id: str | None,
        event_type: str,
        details: Mapping[str, Any],
    ) -> None:
        await connection.execute(
            """
            INSERT INTO gateway_audit_events (merchant_id, event_type, details)
            VALUES ($1, $2, $3::jsonb)
            """,
            merchant_id,
            event_type,
            json.dumps(dict(details), sort_keys=True, default=str),
        )
