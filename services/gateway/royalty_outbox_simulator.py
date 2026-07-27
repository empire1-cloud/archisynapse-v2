"""
Reference implementation of Lyrica's transactional outbox
(spec/SPEC-royalty-loop-v1.md §8) — for AT-12. This is what Lyrica
itself must build; it lives here only as a runnable reference against
the real gateway, backed by Postgres so its own state survives a
restart (not a demonstration that quietly relies on process memory).

Contract:
  - persist the event BEFORE attempting delivery
  - retry with the SAME event_id / idempotency_key / correlation_id
  - 503 retry_later and connection errors are retryable
  - capped exponential backoff with jitter
  - mark delivered only after the receipt is received AND persisted
  - never lose or duplicate an event
"""

import json
import random
import time
import uuid
from datetime import datetime, timezone
from typing import Optional

import asyncpg
import httpx

from royalty_keys import sign_with_private_key
from royalty_signing_keys import (
    FailClosedSigningKeyProvider,
    SigningKeyProvider,
    SigningKeyUnavailable,
)

MAX_BACKOFF_SECONDS = 30
BASE_BACKOFF_SECONDS = 1
LEASE_SECONDS = 30


def _backoff_seconds(attempts: int) -> float:
    capped = min(BASE_BACKOFF_SECONDS * (2 ** attempts), MAX_BACKOFF_SECONDS)
    return capped * (0.5 + random.random() * 0.5)  # jitter: 50%-100% of the capped value


class LyricaOutboxSimulator:
    def __init__(
        self,
        pool: asyncpg.Pool,
        gateway_url: str,
        tenant_id: str,
        tenant_api_key: str,
        signing_key_provider: Optional[SigningKeyProvider] = None,
        worker_id: Optional[str] = None,
    ):
        self.pool = pool
        self.gateway_url = gateway_url
        self.tenant_id = tenant_id
        self.tenant_api_key = tenant_api_key
        self.signing_key_provider = signing_key_provider or FailClosedSigningKeyProvider()
        self.worker_id = worker_id or f"outbox-{uuid.uuid4().hex}"

    async def enqueue(self, event: dict, signing_key_ref: str, key_id: str) -> None:
        """Persist BEFORE any attempt to send -- this call must complete
        before the caller considers the obligation 'recorded'."""
        if not signing_key_ref:
            raise ValueError("signing_key_ref is required; raw private keys must not be persisted")
        await self.pool.execute(
            """
            INSERT INTO lyrica_outbox
              (event_id, idempotency_key, correlation_id, payload, signing_key_ref, key_id)
            VALUES ($1, $2, $3, $4, $5, $6)
            ON CONFLICT (event_id) DO NOTHING
            """,
            event["event_id"],
            event["idempotency_key"],
            event["correlation_id"],
            json.dumps(event),
            signing_key_ref,
            key_id,
        )

    async def _attempt_one(self, client: httpx.AsyncClient, row: asyncpg.Record) -> None:
        event = json.loads(row["payload"])
        body_bytes = json.dumps(event).encode("utf-8")
        try:
            private_key_b64 = await self.signing_key_provider.resolve_private_key(
                row["signing_key_ref"]
            )
        except SigningKeyUnavailable as exc:
            await self._mark_retry(
                row["event_id"], row["attempts"], f"signing key unavailable: {exc}"
            )
            return
        signature = sign_with_private_key(private_key_b64, body_bytes)

        try:
            response = await client.post(
                f"{self.gateway_url}/api/v1/events",
                content=body_bytes,
                headers={
                    "Content-Type": "application/json",
                    "Authorization": f"Bearer {self.tenant_api_key}",
                    "X-Empire1-Signature": signature,
                    "X-Empire1-Key-Id": row["key_id"],
                    "X-Correlation-Id": event["correlation_id"],
                    "Idempotency-Key": event["idempotency_key"],
                },
                timeout=10.0,
            )
        except httpx.RequestError as exc:
            await self._mark_retry(row["event_id"], row["attempts"], f"connection error: {exc}")
            return

        if response.status_code in (200, 201):
            await self.pool.execute(
                """
                UPDATE lyrica_outbox
                SET state = 'receipted', receipt = $2::jsonb, attempts = attempts + 1,
                    lease_owner = NULL, lease_expires_at = NULL, last_error = NULL
                WHERE event_id = $1 AND lease_owner = $3
                """,
                row["event_id"],
                response.text,
                self.worker_id,
            )
            return

        if response.status_code == 503:
            await self._mark_retry(row["event_id"], row["attempts"], f"503 retry_later: {response.text}")
            return

        if response.status_code == 409:
            try:
                body = response.json()
            except ValueError:
                body = {}
            detail = body.get("detail", body) if isinstance(body, dict) else {}
            code = detail.get("code") if isinstance(detail, dict) else None
            if code == "processing":
                await self._mark_retry(
                    row["event_id"], row["attempts"], "409 processing: retry original claim"
                )
                return

        # Validation/auth failures and idempotency_conflict are terminal for
        # this immutable outbox row. A 409 processing response was handled
        # above and remains retryable.
        await self.pool.execute(
            """
            UPDATE lyrica_outbox
            SET state = 'rejected', last_error = $2, attempts = attempts + 1,
                lease_owner = NULL, lease_expires_at = NULL
            WHERE event_id = $1 AND lease_owner = $3
            """,
            row["event_id"],
            f"{response.status_code}: {response.text}",
            self.worker_id,
        )

    async def _mark_retry(self, event_id: str, attempts: int, error: str) -> None:
        delay = _backoff_seconds(attempts)
        await self.pool.execute(
            """
            UPDATE lyrica_outbox
            SET state = 'sent', attempts = attempts + 1, last_error = $2,
                next_attempt_at = now() + ($3 || ' seconds')::interval,
                lease_owner = NULL, lease_expires_at = NULL
            WHERE event_id = $1 AND lease_owner = $4
            """,
            event_id,
            error[:1000],
            str(delay),
            self.worker_id,
        )

    async def run_once(self) -> int:
        """Lease and attempt due rows. Concurrent workers cannot share a row."""
        rows = await self.pool.fetch(
            """
            WITH due AS (
              SELECT event_id
              FROM lyrica_outbox
              WHERE next_attempt_at <= now()
                AND (
                  state IN ('pending', 'sent')
                  OR (
                    state = 'processing'
                    AND lease_expires_at IS NOT NULL
                    AND lease_expires_at <= now()
                  )
                )
              ORDER BY created_at
              FOR UPDATE SKIP LOCKED
              LIMIT 100
            )
            UPDATE lyrica_outbox AS outbox
            SET state = 'processing',
                lease_owner = $1,
                lease_expires_at = now() + ($2 || ' seconds')::interval
            FROM due
            WHERE outbox.event_id = due.event_id
            RETURNING outbox.*
            """,
            self.worker_id,
            str(LEASE_SECONDS),
        )
        async with httpx.AsyncClient() as client:
            for row in rows:
                await self._attempt_one(client, row)
        return len(rows)

    async def run_until_all_settled(self, timeout_seconds: float = 60.0) -> None:
        deadline = time.monotonic() + timeout_seconds
        while time.monotonic() < deadline:
            pending = await self.pool.fetchval(
                "SELECT count(*) FROM lyrica_outbox WHERE state IN ('pending', 'sent', 'processing')"
            )
            if pending == 0:
                return
            await self.run_once()
            import asyncio

            await asyncio.sleep(0.2)
        raise TimeoutError("outbox did not settle within timeout")
