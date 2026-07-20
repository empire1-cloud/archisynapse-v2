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
from datetime import datetime, timezone
from typing import Optional

import asyncpg
import httpx

from royalty_keys import sign_with_private_key

MAX_BACKOFF_SECONDS = 30
BASE_BACKOFF_SECONDS = 1


def _backoff_seconds(attempts: int) -> float:
    capped = min(BASE_BACKOFF_SECONDS * (2 ** attempts), MAX_BACKOFF_SECONDS)
    return capped * (0.5 + random.random() * 0.5)  # jitter: 50%-100% of the capped value


class LyricaOutboxSimulator:
    def __init__(self, pool: asyncpg.Pool, gateway_url: str, tenant_id: str, tenant_api_key: str):
        self.pool = pool
        self.gateway_url = gateway_url
        self.tenant_id = tenant_id
        self.tenant_api_key = tenant_api_key

    async def enqueue(self, event: dict, private_key_b64: str, key_id: str) -> None:
        """Persist BEFORE any attempt to send -- this call must complete
        before the caller considers the obligation 'recorded'."""
        await self.pool.execute(
            """
            INSERT INTO lyrica_outbox (event_id, idempotency_key, correlation_id, payload, private_key_b64, key_id)
            VALUES ($1, $2, $3, $4, $5, $6)
            ON CONFLICT (event_id) DO NOTHING
            """,
            event["event_id"],
            event["idempotency_key"],
            event["correlation_id"],
            json.dumps(event),
            private_key_b64,
            key_id,
        )

    async def _attempt_one(self, client: httpx.AsyncClient, row: asyncpg.Record) -> None:
        event = json.loads(row["payload"])
        body_bytes = json.dumps(event).encode("utf-8")
        signature = sign_with_private_key(row["private_key_b64"], body_bytes)

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
                SET state = 'receipted', receipt = $2, attempts = attempts + 1
                WHERE event_id = $1
                """,
                row["event_id"],
                response.text,
            )
            return

        if response.status_code == 503:
            await self._mark_retry(row["event_id"], row["attempts"], f"503 retry_later: {response.text}")
            return

        # Any other status (400/401/403/409/422) is NOT retryable --
        # the same idempotency_key would just fail the same way again.
        await self.pool.execute(
            """
            UPDATE lyrica_outbox
            SET state = 'rejected', last_error = $2, attempts = attempts + 1
            WHERE event_id = $1
            """,
            row["event_id"],
            f"{response.status_code}: {response.text}",
        )

    async def _mark_retry(self, event_id: str, attempts: int, error: str) -> None:
        delay = _backoff_seconds(attempts)
        await self.pool.execute(
            """
            UPDATE lyrica_outbox
            SET state = 'sent', attempts = attempts + 1, last_error = $2,
                next_attempt_at = now() + ($3 || ' seconds')::interval
            WHERE event_id = $1
            """,
            event_id,
            error[:1000],
            str(delay),
        )

    async def run_once(self) -> int:
        """Attempt delivery for every due (pending/sent) row. Returns count attempted."""
        rows = await self.pool.fetch(
            """
            SELECT * FROM lyrica_outbox
            WHERE state IN ('pending', 'sent') AND next_attempt_at <= now()
            ORDER BY created_at
            """
        )
        async with httpx.AsyncClient() as client:
            for row in rows:
                await self._attempt_one(client, row)
        return len(rows)

    async def run_until_all_settled(self, timeout_seconds: float = 60.0) -> None:
        deadline = time.monotonic() + timeout_seconds
        while time.monotonic() < deadline:
            pending = await self.pool.fetchval(
                "SELECT count(*) FROM lyrica_outbox WHERE state IN ('pending', 'sent')"
            )
            if pending == 0:
                return
            await self.run_once()
            import asyncio

            await asyncio.sleep(0.2)
        raise TimeoutError("outbox did not settle within timeout")
