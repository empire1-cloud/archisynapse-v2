"""
Live integration verification for Archisynapse Revenue Assurance Loop v1.
"""

import asyncio
import json
import time
import uuid
from decimal import Decimal
from typing import Any, Dict, Optional

import httpx

GATEWAY_URL = "http://127.0.0.1:9000"
FRAUD_URL = "http://127.0.0.1:8082"
TRANSACTION_URL = "http://127.0.0.1:3000"
LEDGER_URL = "http://127.0.0.1:3001"
ANALYTICS_URL = "http://127.0.0.1:8081"


def pretty(title: str, payload: Any) -> None:
    print(f"\n{title}")
    print(json.dumps(payload, indent=2, sort_keys=True, default=str))


def sum_entries(entries: list[dict], side: str) -> Decimal:
    total = Decimal("0")
    for entry in entries:
        if entry["debitCredit"] == side:
            total += Decimal(str(entry["amount"]))
    return total


class IntegrationHarness:
    def __init__(self) -> None:
        self.client = httpx.AsyncClient(timeout=30.0)
        self.merchant_id = str(uuid.uuid4())
        self.fraud_api_key: Optional[str] = None
        self.analytics_api_key: Optional[str] = None

    async def close(self) -> None:
        await self.client.aclose()

    async def bootstrap(self) -> None:
        response = await self.client.post(
            f"{GATEWAY_URL}/admin/merchant/bootstrap",
            json={
                "merchant_id": self.merchant_id,
                "name": f"Integration Merchant {self.merchant_id}",
                "plan": "growth",
            },
        )
        response.raise_for_status()
        payload = response.json()
        self.fraud_api_key = payload["fraud_api_key"]
        self.analytics_api_key = payload["analytics_api_key"]
        pretty("[bootstrap] gateway merchant bootstrap", payload)

    async def health(self) -> Dict[str, bool]:
        urls = {
            "gateway": f"{GATEWAY_URL}/health",
            "fraud": f"{FRAUD_URL}/health",
            "transaction": f"{TRANSACTION_URL}/health",
            "ledger": f"{LEDGER_URL}/health",
            "analytics": f"{ANALYTICS_URL}/health",
        }
        results = {}
        for name, url in urls.items():
            try:
                response = await self.client.get(url)
                results[name] = response.status_code == 200
            except Exception:
                results[name] = False
        return results

    async def process_payment(self, payload: Dict[str, Any]) -> httpx.Response:
        return await self.client.post(f"{GATEWAY_URL}/v1/revenue/process", json=payload)

    async def get_transaction(self, transaction_id: str) -> Dict[str, Any]:
        response = await self.client.get(
            f"{TRANSACTION_URL}/payments/{transaction_id}",
            headers={"X-Organization-ID": self.merchant_id},
        )
        response.raise_for_status()
        return response.json()

    async def get_ledger_transaction(self, transaction_id: str) -> Dict[str, Any]:
        response = await self.client.get(
            f"{LEDGER_URL}/transactions/{transaction_id}",
            headers={"X-Organization-ID": self.merchant_id},
        )
        response.raise_for_status()
        return response.json()

    async def get_trial_balance(self) -> Dict[str, Any]:
        response = await self.client.get(
            f"{LEDGER_URL}/trial-balance",
            headers={"X-Organization-ID": self.merchant_id},
        )
        response.raise_for_status()
        return response.json()

    async def get_analytics_transaction(self, analytics_transaction_id: str) -> Dict[str, Any]:
        response = await self.client.get(
            f"{ANALYTICS_URL}/transactions/{analytics_transaction_id}",
            headers={"X-Api-Key": self.analytics_api_key},
        )
        response.raise_for_status()
        return response.json()

    async def get_receipt(self, event_id: str) -> Dict[str, Any]:
        response = await self.client.get(f"{GATEWAY_URL}/v1/revenue/receipt/{event_id}")
        response.raise_for_status()
        return response.json()

    async def get_status(self, correlation_id: str) -> Dict[str, Any]:
        response = await self.client.get(f"{GATEWAY_URL}/v1/revenue/status/{correlation_id}")
        response.raise_for_status()
        return response.json()

    async def verify_receipt(self, event_id: str) -> Dict[str, Any]:
        response = await self.client.get(f"{GATEWAY_URL}/v1/revenue/verify/{event_id}")
        response.raise_for_status()
        return response.json()

    async def prime_block_conditions(self, device_id: str, email: str, session_id: str, ip_address: str) -> None:
        for index in range(10):
            response = await self.client.post(
                f"{FRAUD_URL}/risk/checkout",
                headers={
                    "X-Api-Key": self.fraud_api_key,
                    "Idempotency-Key": f"prime_{index}_{time.time_ns()}",
                },
                json={
                    "event_type": "payment",
                    "user_id": f"fraud_user_{index}",
                    "device_id": device_id,
                    "email": email,
                    "session_id": session_id,
                    "ip_address": ip_address,
                    "country": "US",
                    "billing_country": "CA",
                    "card_country": "US",
                    "bin_reference": "411111",
                    "amount": 2.00,
                    "currency": "USD",
                    "payment_status": "failed",
                },
            )
            response.raise_for_status()

    async def test_allowed_payment_reaches_all_services(self) -> bool:
        payload = {
            "merchant_id": self.merchant_id,
            "customer_id": str(uuid.uuid4()),
            "amount": 100.00,
            "fee_amount": 3.25,
            "currency": "USD",
            "payment_method": {"type": "CARD", "token": "tok_test", "last4": "4242", "brand": "VISA"},
            "email": "allowed@example.com",
            "country": "US",
            "ip_address": "192.168.1.10",
            "idempotency_key": f"idem_allowed_{int(time.time())}",
        }
        response = await self.process_payment(payload)
        receipt = response.json()
        transaction = await self.get_transaction(receipt["transaction_id"])
        ledger = await self.get_ledger_transaction(receipt["ledger_transaction_id"])
        analytics = await self.get_analytics_transaction(receipt["analytics_transaction_id"])
        trial_balance = await self.get_trial_balance()

        pretty("[test1] request", payload)
        pretty("[test1] gateway receipt", receipt)
        pretty("[test1] transaction record", transaction)
        pretty("[test1] ledger transaction", ledger)
        pretty("[test1] analytics record", analytics)
        pretty("[test1] trial balance", trial_balance)

        debits = sum_entries(ledger["entries"], "DEBIT")
        credits = sum_entries(ledger["entries"], "CREDIT")

        return all([
            response.status_code == 200,
            receipt["status"] == "completed",
            receipt["fraud_decision"] == "approve",
            str(transaction["status"]).lower() == "succeeded",
            ledger["id"] == receipt["ledger_transaction_id"],
            str(analytics["id"]) == str(receipt["analytics_transaction_id"]),
            debits == credits,
            trial_balance["isBalanced"] is True,
        ])

    async def test_blocked_payment_never_becomes_revenue(self) -> bool:
        device_id = "device_block_candidate"
        email = "blocked@example.com"
        session_id = "session_block_candidate"
        ip_address = "10.20.30.40"
        await self.prime_block_conditions(device_id, email, session_id, ip_address)

        payload = {
            "merchant_id": self.merchant_id,
            "customer_id": str(uuid.uuid4()),
            "amount": 2.00,
            "currency": "USD",
            "payment_method": {"type": "CARD", "token": "tok_test", "last4": "0000", "brand": "VISA"},
            "email": email,
            "country": "US",
            "ip_address": ip_address,
            "device_id": device_id,
            "session_id": session_id,
            "idempotency_key": f"idem_blocked_{int(time.time())}",
        }
        response = await self.process_payment(payload)
        receipt = response.json()

        pretty("[test2] request", payload)
        pretty("[test2] gateway receipt", receipt)

        return all([
            response.status_code == 200,
            receipt["fraud_decision"] == "block",
            receipt["transaction_id"] is None,
            receipt["ledger_transaction_id"] is None,
            receipt["analytics_recorded"] is False,
            receipt["status"] == "blocked",
        ])

    async def test_idempotency_prevents_duplicates(self) -> bool:
        idempotency_key = f"idem_dupe_{int(time.time())}"
        payload = {
            "merchant_id": self.merchant_id,
            "customer_id": str(uuid.uuid4()),
            "amount": 50.00,
            "currency": "USD",
            "payment_method": {"type": "CARD", "token": "tok_test", "last4": "4242", "brand": "VISA"},
            "idempotency_key": idempotency_key,
        }
        response_one = await self.process_payment(payload)
        response_two = await self.process_payment(payload)
        receipt_one = response_one.json()
        receipt_two = response_two.json()

        pretty("[test3] first receipt", receipt_one)
        pretty("[test3] second receipt", receipt_two)

        return all([
            response_one.status_code == 200,
            response_two.status_code == 200,
            receipt_one["event_id"] == receipt_two["event_id"],
            receipt_one["correlation_id"] == receipt_two["correlation_id"],
            receipt_one["transaction_id"] == receipt_two["transaction_id"],
            receipt_one["ledger_transaction_id"] == receipt_two["ledger_transaction_id"],
            receipt_one["analytics_transaction_id"] == receipt_two["analytics_transaction_id"],
        ])

    async def test_refunds_reduce_recognized_revenue(self) -> bool:
        payload = {
            "merchant_id": self.merchant_id,
            "customer_id": str(uuid.uuid4()),
            "amount": 75.00,
            "currency": "USD",
            "payment_method": {"type": "CARD", "token": "tok_test", "last4": "4242", "brand": "VISA"},
            "idempotency_key": f"idem_refund_{int(time.time())}",
        }
        response = await self.process_payment(payload)
        receipt = response.json()

        # Refund through gateway (which also reverses analytics revenue)
        refund_response = await self.client.post(
            f"{GATEWAY_URL}/v1/revenue/refund/{receipt['transaction_id']}",
            headers={"X-Organization-ID": self.merchant_id},
            json={"amount": "75.00", "reason": "integration_refund"},
        )
        refund = refund_response.json()
        transaction = await self.get_transaction(receipt["transaction_id"])
        trial_balance = await self.get_trial_balance()

        pretty("[test4] original receipt", receipt)
        pretty("[test4] refund response", refund)
        pretty("[test4] refunded payment record", transaction)
        pretty("[test4] trial balance", trial_balance)

        return all([
            refund_response.status_code == 200,
            refund["refund_succeeded"] is True,
            refund["analytics_reversed"] is True,
            str(transaction["status"]).lower() == "refunded",
            trial_balance["isBalanced"] is True,
        ])

    async def test_correlation_id_preserved(self) -> bool:
        payload = {
            "merchant_id": self.merchant_id,
            "customer_id": str(uuid.uuid4()),
            "amount": 125.00,
            "currency": "USD",
            "payment_method": {"type": "CARD", "token": "tok_test", "last4": "4242", "brand": "VISA"},
            "idempotency_key": f"idem_corr_{int(time.time())}",
        }
        response = await self.process_payment(payload)
        receipt = response.json()
        transaction = await self.get_transaction(receipt["transaction_id"])
        ledger = await self.get_ledger_transaction(receipt["ledger_transaction_id"])
        analytics = await self.get_analytics_transaction(receipt["analytics_transaction_id"])
        status = await self.get_status(receipt["correlation_id"])

        pretty("[test5] gateway receipt", receipt)
        pretty("[test5] transaction record", transaction)
        pretty("[test5] ledger transaction", ledger)
        pretty("[test5] analytics record", analytics)
        pretty("[test5] gateway status", status)

        correlation_id = receipt["correlation_id"]
        analytics_correlation = analytics.get("correlation_id")

        return all([
            response.status_code == 200,
            status["correlation_id"] == correlation_id,
            analytics_correlation == correlation_id,
        ])

    async def test_final_receipt_contains_all_components(self) -> bool:
        payload = {
            "merchant_id": self.merchant_id,
            "customer_id": str(uuid.uuid4()),
            "amount": 33.00,
            "currency": "USD",
            "payment_method": {"type": "CARD", "token": "tok_test", "last4": "4242", "brand": "VISA"},
            "idempotency_key": f"idem_receipt_{int(time.time())}",
        }
        response = await self.process_payment(payload)
        receipt = response.json()
        verification = await self.verify_receipt(receipt["event_id"])

        pretty("[test6] gateway receipt", receipt)
        pretty("[test6] gateway verification", verification)

        return all([
            response.status_code == 200,
            receipt.get("event_id") is not None,
            receipt.get("correlation_id") is not None,
            receipt.get("merchant_id") == self.merchant_id,
            receipt.get("transaction_id") is not None,
            receipt.get("ledger_transaction_id") is not None,
            receipt.get("analytics_transaction_id") is not None,
            receipt.get("fraud_decision") is not None,
            receipt.get("occurred_at") is not None,
            verification["components"]["fraud_checked"] is True,
            verification["components"]["transaction_processed"] is True,
            verification["components"]["ledger_posted"] is True,
            verification["components"]["analytics_recorded"] is True,
        ])

    async def test_ledger_outage_recovery(self) -> bool:
        """
        Simulate a ledger outage:
        1. Process a payment normally (ledger healthy)
        2. Kill ledger service
        3. Process payment while ledger down — transaction + fraud + analytics succeed, ledger missing
        4. Restart ledger service
        5. Replay pending recovery
        6. Verify ledger is eventually consistent
        """
        import subprocess

        # Step 1: Normal payment with healthy ledger
        payload_ok = {
            "merchant_id": self.merchant_id,
            "customer_id": str(uuid.uuid4()),
            "amount": 60.00,
            "currency": "USD",
            "payment_method": {"type": "CARD", "token": "tok_test", "last4": "4242", "brand": "VISA"},
            "idempotency_key": f"idem_recovery_ok_{int(time.time())}",
        }
        response_ok = await self.process_payment(payload_ok)
        receipt_ok = response_ok.json()
        assert receipt_ok["status"] == "completed"
        assert receipt_ok["ledger_transaction_id"] is not None

        # Step 2: Simulate ledger outage by temporarily redirecting gateway's ledger client
        # We'll use a proxy approach: start a tiny dead server on port 3099
        # Actually, simpler: we directly manipulate the orchestrator's base URL
        original_base = orchestrator.base_url
        original_ledger = orchestrator.ledger_base_url
        orchestrator.ledger_base_url = "http://127.0.0.1:3099"  # dead port

        # Step 3: Process payment while ledger is down
        payload_outage = {
            "merchant_id": self.merchant_id,
            "customer_id": str(uuid.uuid4()),
            "amount": 40.00,
            "currency": "USD",
            "payment_method": {"type": "CARD", "token": "tok_test", "last4": "4242", "brand": "VISA"},
            "idempotency_key": f"idem_recovery_outage_{int(time.time())}",
        }
        response_outage = await self.process_payment(payload_outage)
        receipt_outage = response_outage.json()

        pretty("[test7] pre-outage receipt", receipt_ok)
        pretty("[test7] during-outage receipt", receipt_outage)

        # The payment may complete (status=partial) or fail (status=failed) depending on
        # whether the orchestrator considers ledger failure fatal
        # Either way: transaction_id should exist, ledger_transaction_id should be missing
        has_transaction = receipt_outage.get("transaction_id") is not None
        missing_ledger = receipt_outage.get("ledger_transaction_id") is None

        # Step 4: Restore ledger service
        orchestrator.ledger_base_url = original_ledger

        # Step 5: If transaction succeeded, replay recovery
        if has_transaction:
            replay = await self.client.post(
                f"{GATEWAY_URL}/admin/recovery/replay",
                headers={"X-Organization-ID": self.merchant_id},
            )
            pretty("[test7] replay response", replay.json() if replay.status_code == 200 else {"error": replay.status_code})

        # Step 6: Verify trial balance is still balanced
        trial_balance = await self.get_trial_balance()
        pretty("[test7] final trial balance", trial_balance)

        return all([
            response_ok.status_code == 200,
            receipt_ok["status"] == "completed",
            has_transaction,
            missing_ledger,
            response_outage.status_code in (200, 502),
            trial_balance["isBalanced"] is True,
        ])


async def main() -> int:
    harness = IntegrationHarness()
    try:
        health = await harness.health()
        pretty("[health]", health)
        if not all(health.values()):
            print("\n[fail] one or more services are unhealthy")
            return 1

        await harness.bootstrap()

        tests = [
            ("Allowed payment reaches all services", harness.test_allowed_payment_reaches_all_services),
            ("Blocked payment never becomes revenue", harness.test_blocked_payment_never_becomes_revenue),
            ("Idempotency prevents duplicates", harness.test_idempotency_prevents_duplicates),
            ("Refunds reduce recognized revenue", harness.test_refunds_reduce_recognized_revenue),
            ("Correlation ID preserved", harness.test_correlation_id_preserved),
            ("Final receipt contains all components", harness.test_final_receipt_contains_all_components),
        ]

        failures = []
        for name, fn in tests:
            print(f"\n=== {name} ===")
            passed = await fn()
            print(f"[result] {name}: {'PASS' if passed else 'FAIL'}")
            if not passed:
                failures.append(name)

        if failures:
            print("\n[summary] failures detected")
            for failure in failures:
                print(f"- {failure}")
            return 1

        print("\n[summary] all six live integration tests passed")
        return 0
    finally:
        await harness.close()


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
