from __future__ import annotations

import importlib.util
import sys
import unittest
from pathlib import Path

MODULE_PATH = Path(__file__).parents[1] / "app.py"
spec = importlib.util.spec_from_file_location("merchant_admin_app", MODULE_PATH)
assert spec and spec.loader
app = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = app
spec.loader.exec_module(app)


class AsyncContext:
    def __init__(self, value=None):
        self.value = value

    async def __aenter__(self):
        return self.value

    async def __aexit__(self, exc_type, exc, tb):
        return False


class FakeConnection:
    def __init__(self, status="ACTIVE"):
        self.merchant = {
            "merchant_id": "mer_test",
            "name": "Test Merchant",
            "plan": "growth",
            "status": status,
            "created_at": "now",
            "updated_at": "now",
        }
        self.keys = {
            "oldkey0000000001": {
                "key_id": "oldkey0000000001",
                "merchant_id": "mer_test",
                "key_prefix": "arch_test_oldkey0000000001",
                "api_key_hash": "hashed-old",
                "environment": "test",
                "status": "ACTIVE",
                "created_at": "now",
                "last_used_at": None,
                "revoked_at": None,
            }
        }
        self.audit = []

    def transaction(self):
        return AsyncContext()

    async def fetchrow(self, query, *args):
        sql = " ".join(query.split()).lower()
        if sql.startswith("select merchant_id, name, plan, status"):
            return dict(self.merchant) if args[0] == "mer_test" else None
        if sql.startswith("update gateway_merchant_api_keys") and "returning key_id" in sql:
            merchant_id, key_id = args
            key = self.keys.get(key_id)
            if not key or key["merchant_id"] != merchant_id or key["status"] != "ACTIVE":
                return None
            key["status"] = "REVOKED"
            key["revoked_at"] = "now"
            return {"key_id": key_id}
        return None

    async def fetch(self, query, *args):
        return [dict(row) for row in self.keys.values() if row["merchant_id"] == args[0]]

    async def execute(self, query, *args):
        sql = " ".join(query.split()).lower()
        if sql.startswith("update gateway_merchant_api_keys"):
            merchant_id = args[0]
            count = 0
            for key in self.keys.values():
                if key["merchant_id"] == merchant_id and key["status"] == "ACTIVE":
                    key["status"] = "REVOKED"
                    key["revoked_at"] = "now"
                    count += 1
            return f"UPDATE {count}"
        if sql.startswith("insert into gateway_merchant_api_keys"):
            key_id, merchant_id, prefix, api_key_hash, environment = args
            self.keys[key_id] = {
                "key_id": key_id,
                "merchant_id": merchant_id,
                "key_prefix": prefix,
                "api_key_hash": api_key_hash,
                "environment": environment,
                "status": "ACTIVE",
                "created_at": "now",
                "last_used_at": None,
                "revoked_at": None,
            }
            return "INSERT 0 1"
        if sql.startswith("update gateway_merchants"):
            self.merchant["status"] = "SUSPENDED" if "'suspended'" in sql else "ACTIVE"
            return "UPDATE 1"
        if sql.startswith("insert into gateway_audit_events"):
            self.audit.append({"merchant_id": args[0], "event_type": args[1], "details": args[2]})
            return "INSERT 0 1"
        raise AssertionError(f"unexpected SQL: {sql}")


class FakePool:
    def __init__(self, connection):
        self.connection = connection

    def acquire(self):
        return AsyncContext(self.connection)


class MerchantLifecycleTests(unittest.IsolatedAsyncioTestCase):
    async def test_rotate_revokes_old_key_and_returns_new_key_once(self):
        connection = FakeConnection()
        result = await app.rotate_key(FakePool(connection), merchant_id="mer_test")
        self.assertEqual(result.action, "ROTATED")
        self.assertEqual(result.revoked_keys, 1)
        self.assertTrue(result.api_key.startswith("arch_test_"))
        self.assertEqual(connection.keys["oldkey0000000001"]["status"], "REVOKED")
        self.assertNotEqual(connection.keys[result.key_id]["api_key_hash"], result.api_key)
        self.assertEqual(connection.audit[-1]["event_type"], "merchant.api_key.rotated")

    async def test_suspend_revokes_all_access(self):
        connection = FakeConnection()
        result = await app.suspend_merchant(FakePool(connection), merchant_id="mer_test")
        self.assertEqual(result.merchant_status, "SUSPENDED")
        self.assertEqual(connection.merchant["status"], "SUSPENDED")
        self.assertTrue(all(key["status"] == "REVOKED" for key in connection.keys.values()))
        self.assertEqual(connection.audit[-1]["event_type"], "merchant.suspended")

    async def test_resume_requires_suspension_and_issues_fresh_key(self):
        active = FakeConnection(status="ACTIVE")
        with self.assertRaises(app.MerchantAdminError):
            await app.resume_merchant(FakePool(active), merchant_id="mer_test")

        suspended = FakeConnection(status="SUSPENDED")
        suspended.keys["oldkey0000000001"]["status"] = "REVOKED"
        result = await app.resume_merchant(FakePool(suspended), merchant_id="mer_test")
        self.assertEqual(result.action, "RESUMED")
        self.assertEqual(suspended.merchant["status"], "ACTIVE")
        self.assertTrue(result.api_key.startswith("arch_test_"))
        self.assertEqual(suspended.keys[result.key_id]["status"], "ACTIVE")

    async def test_revoke_key_is_merchant_scoped(self):
        connection = FakeConnection()
        result = await app.revoke_key(
            FakePool(connection), merchant_id="mer_test", key_id="oldkey0000000001"
        )
        self.assertEqual(result.action, "REVOKED")
        self.assertEqual(connection.keys["oldkey0000000001"]["status"], "REVOKED")
        with self.assertRaises(app.MerchantAdminError):
            await app.revoke_key(
                FakePool(connection), merchant_id="mer_test", key_id="oldkey0000000001"
            )


if __name__ == "__main__":
    unittest.main()
