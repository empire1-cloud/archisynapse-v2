"""The gateway store runs merchant-scoped queries under the tenant role.

Proves, against a real PostgreSQL database with migrations 007+ applied:
  * merchant-scoped store methods run as `archisynapse_tenant` with the
    merchant id set, so even a query with no WHERE clause sees one merchant;
  * the full payment idempotency and receipt flow works under the role
    (claim, save, complete, replay, conflict, fail, list, lookups, credentials);
  * one merchant's store calls cannot read another merchant's receipt;
  * the role and setting end with the transaction: the pooled connection is
    back to the owner afterwards;
  * provisioning and API-key authentication still run as the owner;
  * ARCHISYNAPSE_TENANT_ISOLATION=off restores the previous behaviour.
"""

import base64
import os
import unittest
import uuid

import asyncpg

from gateway_store import (
    CredentialCipher,
    GatewayStore,
    IdempotencyConflict,
    TENANT_ROLE,
    generate_merchant_id,
    tenant_isolation_from_env,
)


DATABASE_URL = os.environ.get(
    "DATABASE_URL",
    "postgresql://postgres:postgres@127.0.0.1:5432/archisynapse",
)


def receipt(event_id: str, transaction_id: str) -> dict:
    return {
        "event_id": event_id,
        "correlation_id": f"corr_{event_id}",
        "status": "completed",
        "transaction_id": transaction_id,
        "amount": "12.50",
    }


class GatewayTenantRoleTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        # max_size=1 makes every call reuse the same pooled connection, so the
        # test can see whether a role or setting leaked from one call to the next.
        self.pool = await asyncpg.create_pool(DATABASE_URL, min_size=1, max_size=1)
        cipher = CredentialCipher(base64.urlsafe_b64decode(base64.urlsafe_b64encode(os.urandom(32))))
        self.store = GatewayStore(self.pool, cipher, tenant_isolation=True)
        self.merchants = []
        for name in ("Tenant A", "Tenant B"):
            provisioned = await self.store.provision_merchant(
                merchant_id=generate_merchant_id(),
                name=name,
                plan="test",
                service_credentials={"fraud_api_key": f"fraud-{name}", "analytics_api_key": None},
            )
            self.merchants.append(provisioned)
        self.a = self.merchants[0].merchant_id
        self.b = self.merchants[1].merchant_id

    async def asyncTearDown(self):
        ids = [self.a, self.b]
        async with self.pool.acquire() as connection:
            for statement in (
                "DELETE FROM gateway_payment_idempotency WHERE merchant_id = ANY($1)",
                "DELETE FROM gateway_payment_receipts WHERE merchant_id = ANY($1)",
                "DELETE FROM gateway_audit_events WHERE merchant_id = ANY($1)",
                "DELETE FROM gateway_merchant_api_keys WHERE merchant_id = ANY($1)",
                "DELETE FROM gateway_merchants WHERE merchant_id = ANY($1)",
            ):
                await connection.execute(statement, ids)
        await self.pool.close()

    async def _store_receipt(self, merchant_id: str) -> tuple[str, str]:
        key = f"idem_{uuid.uuid4().hex}"
        event_id = f"evt_{uuid.uuid4().hex}"
        transaction_id = str(uuid.uuid4())
        claim = await self.store.claim_idempotency(
            merchant_id=merchant_id, idempotency_key=key, request_hash="a" * 64
        )
        self.assertEqual(claim.state, "new")
        await self.store.save_receipt(
            merchant_id=merchant_id,
            idempotency_key=key,
            request_hash="a" * 64,
            receipt=receipt(event_id, transaction_id),
        )
        await self.store.complete_idempotency(
            merchant_id=merchant_id, idempotency_key=key, event_id=event_id
        )
        return key, event_id

    async def test_scoped_connection_runs_as_tenant_role(self):
        async with self.store.tenant_connection(self.a) as connection:
            role = await connection.fetchval("SELECT current_user")
            tenant = await connection.fetchval("SELECT current_setting('archisynapse.tenant_id', true)")
        self.assertEqual((role, tenant), (TENANT_ROLE, self.a))

    async def test_query_without_where_clause_sees_one_merchant(self):
        await self._store_receipt(self.a)
        await self._store_receipt(self.b)
        async with self.store.tenant_connection(self.a) as connection:
            owners = await connection.fetch(
                "SELECT DISTINCT merchant_id FROM gateway_payment_receipts WHERE merchant_id = ANY($1)",
                [self.a, self.b],
            )
            merchants = await connection.fetch(
                "SELECT merchant_id FROM gateway_merchants WHERE merchant_id = ANY($1)",
                [self.a, self.b],
            )
        self.assertEqual([r["merchant_id"] for r in owners], [self.a])
        self.assertEqual([r["merchant_id"] for r in merchants], [self.a])

    async def test_full_flow_works_under_the_role(self):
        key, event_id = await self._store_receipt(self.a)
        replay = await self.store.claim_idempotency(
            merchant_id=self.a, idempotency_key=key, request_hash="a" * 64
        )
        self.assertEqual((replay.state, replay.event_id), ("replay", event_id))
        with self.assertRaises(IdempotencyConflict):
            await self.store.claim_idempotency(
                merchant_id=self.a, idempotency_key=key, request_hash="b" * 64
            )
        stored = await self.store.get_receipt(merchant_id=self.a, event_id=event_id)
        self.assertEqual(stored["event_id"], event_id)
        by_txn = await self.store.get_receipt_by_transaction_id(
            merchant_id=self.a, transaction_id=stored["transaction_id"]
        )
        self.assertEqual(by_txn["event_id"], event_id)
        listed = await self.store.list_receipts(merchant_id=self.a)
        self.assertEqual([r["event_id"] for r in listed], [event_id])
        self.assertEqual(
            (await self.store.get_service_credentials(self.a))["fraud_api_key"], "fraud-Tenant A"
        )

        failed_key = f"idem_{uuid.uuid4().hex}"
        await self.store.claim_idempotency(
            merchant_id=self.a, idempotency_key=failed_key, request_hash="c" * 64
        )
        await self.store.fail_idempotency(
            merchant_id=self.a, idempotency_key=failed_key, reason="processor declined"
        )
        retry = await self.store.claim_idempotency(
            merchant_id=self.a, idempotency_key=failed_key, request_hash="c" * 64
        )
        self.assertEqual(retry.state, "retry")

    async def test_merchant_cannot_read_another_merchants_receipt(self):
        _, event_b = await self._store_receipt(self.b)
        self.assertIsNone(await self.store.get_receipt(merchant_id=self.a, event_id=event_b))
        self.assertEqual(await self.store.list_receipts(merchant_id=self.a), [])

    async def test_role_and_setting_end_with_the_transaction(self):
        await self._store_receipt(self.a)
        async with self.pool.acquire() as connection:
            role = await connection.fetchval("SELECT current_user")
            tenant = await connection.fetchval("SELECT current_setting('archisynapse.tenant_id', true)")
        self.assertEqual(role, "postgres")
        self.assertIn(tenant, (None, ""))

    async def test_authentication_still_runs_as_owner(self):
        principal = await self.store.authenticate(self.merchants[0].api_key)
        self.assertEqual(principal.merchant_id, self.a)

    async def test_isolation_off_uses_the_owner_connection(self):
        store = GatewayStore(self.pool, None, tenant_isolation=False)
        async with store.tenant_connection(self.a) as connection:
            self.assertEqual(await connection.fetchval("SELECT current_user"), "postgres")

    async def test_environment_setting(self):
        self.assertTrue(tenant_isolation_from_env({}))
        self.assertTrue(tenant_isolation_from_env({"ARCHISYNAPSE_TENANT_ISOLATION": "enforce"}))
        self.assertFalse(tenant_isolation_from_env({"ARCHISYNAPSE_TENANT_ISOLATION": "off"}))
        with self.assertRaises(ValueError):
            tenant_isolation_from_env({"ARCHISYNAPSE_TENANT_ISOLATION": "maybe"})


if __name__ == "__main__":
    unittest.main()
