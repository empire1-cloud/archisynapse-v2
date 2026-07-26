import asyncio
import os
import unittest
import uuid

import asyncpg

from gateway_store import (
    GatewayStore,
    IdempotencyConflict,
    IdempotencyInProgress,
)


class GatewayPostgresConcurrencyTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        database_url = os.environ.get(
            "DATABASE_URL",
            "postgresql://postgres:postgres@127.0.0.1:5432/archisynapse",
        )
        self.pool = await asyncpg.create_pool(database_url, min_size=2, max_size=20)
        self.store = GatewayStore(self.pool, None)
        self.merchant_id = f"mer_test_{uuid.uuid4().hex[:12]}"
        async with self.pool.acquire() as connection:
            await connection.execute(
                """
                INSERT INTO gateway_merchants (merchant_id, name, plan, status)
                VALUES ($1, 'Concurrency Test', 'test', 'ACTIVE')
                """,
                self.merchant_id,
            )

    async def asyncTearDown(self):
        async with self.pool.acquire() as connection:
            await connection.execute(
                "DELETE FROM gateway_payment_idempotency WHERE merchant_id = $1",
                self.merchant_id,
            )
            await connection.execute(
                "DELETE FROM gateway_payment_receipts WHERE merchant_id = $1",
                self.merchant_id,
            )
            await connection.execute(
                "DELETE FROM gateway_merchants WHERE merchant_id = $1",
                self.merchant_id,
            )
        await self.pool.close()

    async def test_one_concurrent_request_claims_the_key(self):
        key = f"idem_{uuid.uuid4().hex}"
        request_hash = "a" * 64

        async def claim():
            try:
                result = await self.store.claim_idempotency(
                    merchant_id=self.merchant_id,
                    idempotency_key=key,
                    request_hash=request_hash,
                    reclaim_after_seconds=3600,
                )
                return result.state
            except IdempotencyInProgress:
                return "in_progress"

        results = await asyncio.gather(*(claim() for _ in range(20)))
        self.assertEqual(results.count("new"), 1)
        self.assertEqual(results.count("in_progress"), 19)

        receipt = {
            "event_id": f"evt_{uuid.uuid4().hex}",
            "correlation_id": f"corr_{uuid.uuid4().hex}",
            "status": "completed",
            "transaction_id": f"pay_{uuid.uuid4().hex}",
        }
        await self.store.save_receipt(
            merchant_id=self.merchant_id,
            idempotency_key=key,
            request_hash=request_hash,
            receipt=receipt,
        )
        await self.store.complete_idempotency(
            merchant_id=self.merchant_id,
            idempotency_key=key,
            event_id=receipt["event_id"],
        )

        replay = await self.store.claim_idempotency(
            merchant_id=self.merchant_id,
            idempotency_key=key,
            request_hash=request_hash,
        )
        self.assertEqual(replay.state, "replay")
        self.assertEqual(replay.event_id, receipt["event_id"])

        with self.assertRaises(IdempotencyConflict):
            await self.store.claim_idempotency(
                merchant_id=self.merchant_id,
                idempotency_key=key,
                request_hash="b" * 64,
            )


if __name__ == "__main__":
    unittest.main()
