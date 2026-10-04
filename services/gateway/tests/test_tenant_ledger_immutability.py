"""Least-privilege UPDATE for the tenant role (migration 008).

Proves, against a real PostgreSQL database, for a session running as
`archisynapse_tenant` with its own tenant id set:
  * it cannot UPDATE any append-only table, including its own journal entries
    and audit records;
  * it can update exactly the columns the services update, and no others
    (for example, never an amount);
  * it cannot change its own account balance directly;
  * posting a journal entry still updates the balance through the ledger trigger;
  * tables the services do update (payments) still accept tenant updates;
  * owner connections are unchanged.
"""

import os
import unittest
import uuid

import asyncpg


DATABASE_URL = os.environ.get(
    "DATABASE_URL",
    "postgresql://postgres:postgres@127.0.0.1:5432/archisynapse",
)

APPEND_ONLY = {
    "journal_entries": "description",
    "audit_logs": "entity_id",
    "refunds": "reason",
    "unposted_payments": "error_message",
    "royalty_payouts": "state",
    "royalty_reversals": "reversal_event_id",
    "gateway_audit_events": "event_type",
}


# Columns each service UPDATE / ON CONFLICT DO UPDATE statement sets.
SERVICE_UPDATES = {
    "payments": ["status", "processor_transaction_id", "failure_reason", "ledger_transaction_id"],
    "processor_refund_attempts": [
        "status", "processor_refund_id", "failure_reason", "processor_succeeded_at",
        "ledger_transaction_id", "ledger_succeeded_at", "updated_at",
    ],
    "transactions": ["status", "updated_at"],
    "idempotency_store": ["response", "expires_at"],
    "royalty_obligations": ["status", "ledger_transaction_id"],
    "gateway_payment_idempotency": [
        "status", "claimed_at", "failed_at", "failure_reason", "event_id", "completed_at",
    ],
    "gateway_payment_receipts": ["status", "payload", "updated_at"],
    "royalty_idempotency": [
        "status", "claimed_at", "failure_reason", "receipt_id", "completed_at", "failed_at",
    ],
    "royalty_receipts": ["status", "payload", "updated_at"],
}

# Columns that must never change after a row is written.
FROZEN_COLUMNS = {
    "payments": ["amount", "currency", "organization_id", "idempotency_key", "fee_amount"],
    "processor_refund_attempts": ["amount", "payment_id", "processor_payment_id"],
    "transactions": ["amount", "type", "reference_id", "idempotency_key"],
    "idempotency_store": ["request_hash", "organization_id"],
    "royalty_obligations": ["amount", "splits", "creator_id", "request_hash"],
    "gateway_payment_idempotency": ["request_hash"],
    "gateway_payment_receipts": ["request_hash", "idempotency_key", "correlation_id"],
    "royalty_idempotency": ["request_hash"],
    "royalty_receipts": ["event_id", "correlation_id"],
    "accounts": ["code", "type", "name", "organization_id"],
}


class TenantLedgerImmutabilityTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.conn = await asyncpg.connect(DATABASE_URL)
        self.tenant = f"mer_imm_{uuid.uuid4().hex[:10]}"
        await self.conn.execute(
            "INSERT INTO gateway_merchants (merchant_id, name, plan, status) "
            "VALUES ($1, 'Immutability proof', 'test', 'ACTIVE')",
            self.tenant,
        )
        self.payment_id = await self.conn.fetchval(
            "INSERT INTO payments "
            "(organization_id, amount, payment_method_type, payment_method_token, idempotency_key) "
            "VALUES ($1, 12.50, 'CARD', 'tok_test', $2) RETURNING id",
            self.tenant,
            f"pay_{uuid.uuid4().hex}",
        )
        self.account_id = await self.conn.fetchval(
            "INSERT INTO accounts (organization_id, code, name, type) "
            "VALUES ($1, '1000', 'Cash', 'ASSET') RETURNING id",
            self.tenant,
        )
        self.entry_id = await self.conn.fetchval(
            "INSERT INTO journal_entries "
            "(transaction_id, organization_id, account_id, debit_credit, amount, description) "
            "VALUES ($1, $2, $3, 'DEBIT', 12.50, 'seed') RETURNING id",
            uuid.uuid4(),
            self.tenant,
            self.account_id,
        )

    async def asyncTearDown(self):
        for statement in (
            "DELETE FROM journal_entries WHERE organization_id = $1",
            "DELETE FROM accounts WHERE organization_id = $1",
            "DELETE FROM payments WHERE organization_id = $1",
            "DELETE FROM gateway_merchants WHERE merchant_id = $1",
        ):
            await self.conn.execute(statement, self.tenant)
        await self.conn.close()

    async def _as_tenant(self, query, *args, method="execute"):
        async with self.conn.transaction():
            await self.conn.execute("SET LOCAL ROLE archisynapse_tenant")
            await self.conn.execute(
                "SELECT set_config('archisynapse.tenant_id', $1, true)", self.tenant
            )
            return await getattr(self.conn, method)(query, *args)

    async def _balance(self):
        return str(
            await self.conn.fetchval("SELECT balance FROM accounts WHERE id = $1", self.account_id)
        )

    async def test_own_journal_entry_cannot_be_rewritten(self):
        with self.assertRaises(asyncpg.exceptions.InsufficientPrivilegeError):
            await self._as_tenant(
                "UPDATE journal_entries SET amount = 9999, debit_credit = 'CREDIT' WHERE id = $1",
                self.entry_id,
            )
        row = await self.conn.fetchrow(
            "SELECT amount, debit_credit FROM journal_entries WHERE id = $1", self.entry_id
        )
        self.assertEqual((str(row["amount"]), row["debit_credit"]), ("12.5000", "DEBIT"))

    async def test_no_append_only_table_accepts_updates(self):
        for table, column in APPEND_ONLY.items():
            with self.subTest(table=table):
                with self.assertRaises(asyncpg.exceptions.InsufficientPrivilegeError):
                    await self._as_tenant(f"UPDATE {table} SET {column} = {column} WHERE false")

    async def test_service_update_columns_are_allowed(self):
        for table, columns in SERVICE_UPDATES.items():
            assignments = ", ".join(f"{c} = {c}" for c in columns)
            with self.subTest(table=table):
                status = await self._as_tenant(f"UPDATE {table} SET {assignments} WHERE false")
                self.assertEqual(status, "UPDATE 0")

    async def test_frozen_columns_are_denied(self):
        for table, columns in FROZEN_COLUMNS.items():
            for column in columns:
                with self.subTest(table=table, column=column):
                    with self.assertRaises(asyncpg.exceptions.InsufficientPrivilegeError):
                        await self._as_tenant(f"UPDATE {table} SET {column} = {column} WHERE false")

    async def test_own_payment_amount_cannot_change(self):
        with self.assertRaises(asyncpg.exceptions.InsufficientPrivilegeError):
            await self._as_tenant("UPDATE payments SET amount = 0.01 WHERE id = $1", self.payment_id)

    async def test_account_balance_cannot_be_set_directly(self):
        with self.assertRaises(asyncpg.exceptions.InsufficientPrivilegeError):
            await self._as_tenant(
                "UPDATE accounts SET balance = 1000000 WHERE id = $1", self.account_id
            )
        self.assertEqual(await self._balance(), "12.5000")

    async def test_posting_an_entry_still_updates_the_balance(self):
        await self._as_tenant(
            "INSERT INTO journal_entries "
            "(transaction_id, organization_id, account_id, debit_credit, amount, description) "
            "VALUES ($1, $2, $3, 'DEBIT', 7.50, 'own entry')",
            uuid.uuid4(),
            self.tenant,
            self.account_id,
        )
        self.assertEqual(await self._balance(), "20.0000")

    async def test_payments_still_accept_tenant_status_updates(self):
        status = await self._as_tenant(
            "UPDATE payments SET status = 'SUCCEEDED' WHERE id = $1", self.payment_id
        )
        self.assertEqual(status, "UPDATE 1")

    async def test_owner_connection_is_unchanged(self):
        status = await self.conn.execute(
            "UPDATE accounts SET name = 'Cash (owner edit)' WHERE id = $1", self.account_id
        )
        self.assertEqual(status, "UPDATE 1")


if __name__ == "__main__":
    unittest.main()
