"""Cross-tenant denial proof for migration 007 (PostgreSQL row-level security).

Two tenants are seeded as the owner connection. Every check then runs inside a
transaction that switches to the `archisynapse_tenant` role and sets
`archisynapse.tenant_id`, the same way a tenant-scoped service call would.

Proves, against a real PostgreSQL database:
  * a tenant reads only its own rows, across transaction, ledger, gateway and
    royalty tables;
  * with no tenant set, nothing is readable;
  * a tenant cannot write a row for another tenant, re-tag its own row to
    another tenant, update another tenant's rows, or attach its ledger entry
    to another tenant's account;
  * credential tables and deletes are denied outright;
  * the normal path still works: a tenant posts its own journal entry and the
    balance trigger updates its own account;
  * the owner connection the services use today is unaffected.

Does NOT prove the services already use the tenant role; that wiring is next.
"""

import os
import unittest
import uuid

import asyncpg


DATABASE_URL = os.environ.get(
    "DATABASE_URL",
    "postgresql://postgres:postgres@127.0.0.1:5432/archisynapse",
)


class TenantIsolationRlsTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.conn = await asyncpg.connect(DATABASE_URL)
        suffix = uuid.uuid4().hex[:10]
        self.tenant_a = f"mer_rls_a_{suffix}"
        self.tenant_b = f"mer_rls_b_{suffix}"
        self.ids = {}
        for tenant in (self.tenant_a, self.tenant_b):
            self.ids[tenant] = await self._seed(tenant)

    async def asyncTearDown(self):
        tenants = [self.tenant_a, self.tenant_b]
        statements = [
            "DELETE FROM royalty_payouts WHERE royalty_obligation_id IN "
            "(SELECT id FROM royalty_obligations WHERE organization_id = ANY($1))",
            "DELETE FROM royalty_obligations WHERE organization_id = ANY($1)",
            "DELETE FROM journal_entries WHERE organization_id = ANY($1)",
            "DELETE FROM accounts WHERE organization_id = ANY($1)",
            "DELETE FROM refunds WHERE organization_id = ANY($1)",
            "DELETE FROM payments WHERE organization_id = ANY($1)",
            "DELETE FROM gateway_payment_idempotency WHERE merchant_id = ANY($1)",
            "DELETE FROM gateway_payment_receipts WHERE merchant_id = ANY($1)",
            "DELETE FROM gateway_merchant_api_keys WHERE merchant_id = ANY($1)",
            "DELETE FROM gateway_merchants WHERE merchant_id = ANY($1)",
        ]
        for statement in statements:
            await self.conn.execute(statement, tenants)
        await self.conn.close()

    # ------------------------------------------------------------------ helpers

    async def _seed(self, tenant):
        """Insert one row per covered table for `tenant`, as the owner."""
        await self.conn.execute(
            "INSERT INTO gateway_merchants (merchant_id, name, plan, status) "
            "VALUES ($1, 'RLS proof', 'test', 'ACTIVE')",
            tenant,
        )
        await self.conn.execute(
            "INSERT INTO gateway_merchant_api_keys "
            "(key_id, merchant_id, key_prefix, api_key_hash, environment) "
            "VALUES ($1, $2, 'ak_test', 'hash-not-a-key', 'test')",
            uuid.uuid4().hex,
            tenant,
        )
        await self.conn.execute(
            "INSERT INTO gateway_payment_receipts "
            "(event_id, merchant_id, correlation_id, idempotency_key, request_hash, status, payload) "
            "VALUES ($1, $2, 'corr', $3, $4, 'SUCCEEDED', '{}'::jsonb)",
            f"evt_{uuid.uuid4().hex}",
            tenant,
            f"idem_{uuid.uuid4().hex}",
            "a" * 64,
        )
        payment_id = await self.conn.fetchval(
            "INSERT INTO payments "
            "(organization_id, amount, payment_method_type, payment_method_token, idempotency_key) "
            "VALUES ($1, 12.50, 'CARD', 'tok_test', $2) RETURNING id",
            tenant,
            f"pay_{uuid.uuid4().hex}",
        )
        account_id = await self.conn.fetchval(
            "INSERT INTO accounts (organization_id, code, name, type) "
            "VALUES ($1, '1000', 'Cash', 'ASSET') RETURNING id",
            tenant,
        )
        await self.conn.execute(
            "INSERT INTO journal_entries "
            "(transaction_id, organization_id, account_id, debit_credit, amount, description) "
            "VALUES ($1, $2, $3, 'DEBIT', 12.50, 'seed')",
            uuid.uuid4(),
            tenant,
            account_id,
        )
        obligation_id = await self.conn.fetchval(
            "INSERT INTO royalty_obligations "
            "(organization_id, event_id, correlation_id, idempotency_key, tenant_id, track_id, "
            " creator_id, trigger_kind, amount, request_hash) "
            "VALUES ($1::text, $2, 'corr', $3, $1::text, 'track', 'creator', 'play', 1.00, $4) "
            "RETURNING id",
            tenant,
            f"evt_{uuid.uuid4().hex}",
            f"roy_{uuid.uuid4().hex}",
            "b" * 64,
        )
        await self.conn.execute(
            "INSERT INTO royalty_payouts (royalty_obligation_id, owner_id, amount) "
            "VALUES ($1, 'owner', 1.00)",
            obligation_id,
        )
        return {"payment": payment_id, "account": account_id, "obligation": obligation_id}

    async def _as_tenant(self, tenant, query, *args, method="fetch"):
        """Run one statement as the tenant role, inside its own transaction."""
        async with self.conn.transaction():
            await self.conn.execute("SET LOCAL ROLE archisynapse_tenant")
            if tenant is not None:
                await self.conn.execute(
                    "SELECT set_config('archisynapse.tenant_id', $1, true)", tenant
                )
            return await getattr(self.conn, method)(query, *args)

    # -------------------------------------------------------------------- reads

    async def test_tenant_reads_only_its_own_rows(self):
        checks = [
            ("payments", "organization_id"),
            ("accounts", "organization_id"),
            ("journal_entries", "organization_id"),
            ("royalty_obligations", "organization_id"),
            ("gateway_merchants", "merchant_id"),
            ("gateway_payment_receipts", "merchant_id"),
        ]
        tenants = [self.tenant_a, self.tenant_b]
        for table, column in checks:
            with self.subTest(table=table):
                rows = await self._as_tenant(
                    self.tenant_a,
                    f"SELECT {column} AS owner FROM {table} WHERE {column} = ANY($1)",
                    tenants,
                )
                self.assertEqual({row["owner"] for row in rows}, {self.tenant_a})

    async def test_child_rows_follow_their_parent(self):
        visible = await self._as_tenant(
            self.tenant_a,
            "SELECT royalty_obligation_id FROM royalty_payouts "
            "WHERE royalty_obligation_id = ANY($1)",
            [self.ids[self.tenant_a]["obligation"], self.ids[self.tenant_b]["obligation"]],
        )
        self.assertEqual(
            [row["royalty_obligation_id"] for row in visible],
            [self.ids[self.tenant_a]["obligation"]],
        )

    async def test_no_tenant_set_reads_nothing(self):
        count = await self._as_tenant(
            None,
            "SELECT count(*) FROM payments WHERE organization_id = ANY($1)",
            [self.tenant_a, self.tenant_b],
            method="fetchval",
        )
        self.assertEqual(count, 0)

    async def test_direct_lookup_of_other_tenant_row_finds_nothing(self):
        row = await self._as_tenant(
            self.tenant_a,
            "SELECT id FROM payments WHERE id = $1",
            self.ids[self.tenant_b]["payment"],
            method="fetchrow",
        )
        self.assertIsNone(row)

    # ------------------------------------------------------------------- writes

    async def test_cannot_insert_row_for_another_tenant(self):
        with self.assertRaises(asyncpg.exceptions.InsufficientPrivilegeError):
            await self._as_tenant(
                self.tenant_a,
                "INSERT INTO accounts (organization_id, code, name, type) "
                "VALUES ($1, '9999', 'Planted', 'ASSET')",
                self.tenant_b,
                method="execute",
            )

    async def test_cannot_update_another_tenants_rows(self):
        status = await self._as_tenant(
            self.tenant_a,
            "UPDATE payments SET description = 'tampered' WHERE id = $1",
            self.ids[self.tenant_b]["payment"],
            method="execute",
        )
        self.assertEqual(status, "UPDATE 0")
        description = await self.conn.fetchval(
            "SELECT description FROM payments WHERE id = $1",
            self.ids[self.tenant_b]["payment"],
        )
        self.assertIsNone(description)

    async def test_cannot_retag_own_row_to_another_tenant(self):
        with self.assertRaises(asyncpg.exceptions.InsufficientPrivilegeError):
            await self._as_tenant(
                self.tenant_a,
                "UPDATE payments SET organization_id = $1 WHERE id = $2",
                self.tenant_b,
                self.ids[self.tenant_a]["payment"],
                method="execute",
            )

    async def test_cannot_post_entry_against_another_tenants_account(self):
        with self.assertRaises(asyncpg.exceptions.InsufficientPrivilegeError):
            await self._as_tenant(
                self.tenant_a,
                "INSERT INTO journal_entries "
                "(transaction_id, organization_id, account_id, debit_credit, amount, description) "
                "VALUES ($1, $2, $3, 'DEBIT', 5.00, 'cross-tenant attempt')",
                uuid.uuid4(),
                self.tenant_a,
                self.ids[self.tenant_b]["account"],
                method="execute",
            )
        balance_b = await self.conn.fetchval(
            "SELECT balance FROM accounts WHERE id = $1", self.ids[self.tenant_b]["account"]
        )
        self.assertEqual(str(balance_b), "12.5000")

    async def test_credential_tables_are_denied(self):
        with self.assertRaises(asyncpg.exceptions.InsufficientPrivilegeError):
            await self._as_tenant(
                self.tenant_a,
                "SELECT api_key_hash FROM gateway_merchant_api_keys",
            )

    async def test_deletes_are_denied(self):
        with self.assertRaises(asyncpg.exceptions.InsufficientPrivilegeError):
            await self._as_tenant(
                self.tenant_a,
                "DELETE FROM payments WHERE id = $1",
                self.ids[self.tenant_a]["payment"],
                method="execute",
            )

    # ------------------------------------------------------------ normal paths

    async def test_tenant_can_post_its_own_entry_and_balance_updates(self):
        account_a = self.ids[self.tenant_a]["account"]
        await self._as_tenant(
            self.tenant_a,
            "INSERT INTO journal_entries "
            "(transaction_id, organization_id, account_id, debit_credit, amount, description) "
            "VALUES ($1, $2, $3, 'DEBIT', 7.50, 'own entry')",
            uuid.uuid4(),
            self.tenant_a,
            account_a,
            method="execute",
        )
        balance_a = await self.conn.fetchval(
            "SELECT balance FROM accounts WHERE id = $1", account_a
        )
        self.assertEqual(str(balance_a), "20.0000")

    async def test_owner_connection_is_unchanged(self):
        count = await self.conn.fetchval(
            "SELECT count(*) FROM payments WHERE organization_id = ANY($1)",
            [self.tenant_a, self.tenant_b],
        )
        self.assertEqual(count, 2)


if __name__ == "__main__":
    unittest.main()
