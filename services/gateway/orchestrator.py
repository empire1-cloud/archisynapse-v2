"""
Revenue Assurance Loop Orchestrator for Archisynapse.

Canonical event flow:
  API Gateway -> Fraud Decision -> Transaction Service -> Ledger Posting -> Revenue Intelligence -> Unified Receipt

The Transaction Service is the SOLE owner of ledger posting.
The Gateway never posts directly — it queries by referenceId if needed.
"""

import uuid
import os
import logging
from typing import Optional, Dict, Any

import httpx

from canonical_event import (
    CanonicalEvent,
    PaymentRequest,
    UnifiedReceipt,
    minor_to_dollars,
)
from runtime_state import (
    ANALYTICS_RECOVERY_FILE,
    LEDGER_RECOVERY_FILE,
    get_recovery_queue,
    push_recovery_item,
    remove_recovery_item,
)

logger = logging.getLogger("archisynapse.orchestrator")

FRAUD_SERVICE_URL = os.getenv("FRAUD_SERVICE_URL", "http://127.0.0.1:8080")
TRANSACTION_SERVICE_URL = os.getenv("TRANSACTION_SERVICE_URL", "http://127.0.0.1:3000")
LEDGER_SERVICE_URL = os.getenv("LEDGER_SERVICE_URL", "http://127.0.0.1:3001")
ANALYTICS_SERVICE_URL = os.getenv("ANALYTICS_SERVICE_URL", "http://127.0.0.1:8081")


class RevenueAssuranceOrchestrator:
    def __init__(self):
        self.client = httpx.AsyncClient(timeout=30.0)
        self.fraud_base_url = FRAUD_SERVICE_URL
        self.transaction_base_url = TRANSACTION_SERVICE_URL
        self.ledger_base_url = LEDGER_SERVICE_URL
        self.analytics_base_url = ANALYTICS_SERVICE_URL

    async def close(self):
        await self.client.aclose()

    async def process_payment(
        self,
        request: PaymentRequest,
        idempotency_key: Optional[str] = None,
    ) -> UnifiedReceipt:
        if not idempotency_key:
            idempotency_key = f"idem_{uuid.uuid4().hex[:16]}"

        event = request.to_canonical_event(idempotency_key)
        event.correlation_id = event.generate_correlation_id()
        event.status = "processing"

        try:
            await self._check_fraud(event, request.fraud_api_key)

            if event.fraud_decision == "approve":
                await self._process_transaction(event, request)

                if event.transaction_id:
                    if event.ledger_transaction_id:
                        await self._record_analytics(event, request.analytics_api_key)
                        if event.analytics_recorded:
                            event.status = "completed"
                        else:
                            event.status = "partial"
                            event.error = event.analytics_error or "Analytics recording failed"
                    else:
                        # Transaction succeeded but no ledger ID returned.
                        # Query ledger by referenceId. If absent, enqueue reconciliation.
                        await self._reconcile_missing_ledger(event, request.merchant_id)
                        if event.ledger_transaction_id:
                            await self._record_analytics(event, request.analytics_api_key)
                            if event.analytics_recorded:
                                event.status = "completed"
                            else:
                                event.status = "partial"
                                event.error = event.analytics_error or "Analytics recording failed"
                        else:
                            event.status = "partial"
                            event.error = event.ledger_error or "Ledger posting pending reconciliation"
                else:
                    event.status = "failed"
                    event.error = event.transaction_error or "Transaction processing failed"
            elif event.fraud_decision == "block":
                event.status = "blocked"
                event.error = "Payment blocked by fraud detection"
            else:
                event.status = "review"
                event.error = "Payment requires manual review"
        except Exception as exc:
            event.status = "failed"
            event.error = f"Unexpected error: {exc}"
            logger.exception("Unexpected orchestration error")

        return UnifiedReceipt.from_event(event)

    async def process_refund(
        self,
        transaction_id: str,
        merchant_id: str,
        amount: str,
        reason: str,
        idempotency_key: Optional[str] = None,
        analytics_api_key: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Process a refund: call transaction service, then reverse analytics revenue."""
        result: Dict[str, Any] = {
            "transaction_id": transaction_id,
            "refund_succeeded": False,
            "analytics_reversed": False,
            "error": None,
        }

        # 1. Call transaction service refund endpoint
        try:
            headers = {
                "Content-Type": "application/json",
                "X-Organization-ID": merchant_id,
            }
            if idempotency_key:
                headers["Idempotency-Key"] = idempotency_key

            response = await self.client.post(
                f"{self.transaction_base_url}/payments/{transaction_id}/refund",
                json={"amount": amount, "reason": reason},
                headers=headers,
            )
            if response.status_code in (200, 201):
                result["refund_succeeded"] = True
                result["refund"] = response.json()
            else:
                result["error"] = f"Transaction refund failed: HTTP {response.status_code}"
                return result
        except httpx.RequestError as exc:
            result["error"] = f"Transaction service unavailable: {exc}"
            return result

        # 2. Reverse analytics revenue (post negative amount with status "refunded")
        if analytics_api_key:
            try:
                refund_amount = -float(amount)
                await self._record_analytics_refund(
                    merchant_id=merchant_id,
                    transaction_id=transaction_id,
                    amount=refund_amount,
                    reason=reason,
                    analytics_api_key=analytics_api_key,
                )
                result["analytics_reversed"] = True
            except Exception as exc:
                logger.warning(f"Analytics reversal failed (non-fatal): {exc}")
                result["analytics_reversed"] = False

        return result

    async def _record_analytics_refund(
        self,
        merchant_id: str,
        transaction_id: str,
        amount: float,
        reason: str,
        analytics_api_key: str,
    ) -> None:
        """Post a negative-amount transaction to analytics to reverse recognized revenue."""
        headers = {"Content-Type": "application/json"}
        if analytics_api_key:
            headers["X-Api-Key"] = analytics_api_key

        payload = {
            "customer_id": f"refund_{transaction_id}",
            "amount": amount,
            "fee_amount": 0,
            "currency": "USD",
            "status": "refunded",
            "payment_method": "REFUND",
            "correlation_id": f"refund_{transaction_id}",
            "event_id": f"refund_{transaction_id}",
            "external_transaction_id": transaction_id,
        }

        response = await self.client.post(
            f"{self.analytics_base_url}/transactions",
            json=payload,
            headers=headers,
        )
        if response.status_code != 200:
            raise Exception(f"Analytics refund reversal failed: HTTP {response.status_code}")

    async def replay_pending_recoveries(self, merchant_credentials: Dict[str, Dict[str, str]]) -> Dict[str, int]:
        results = {
            "ledger_pending": 0,
            "ledger_replayed": 0,
            "analytics_pending": 0,
            "analytics_replayed": 0,
        }

        ledger_queue = get_recovery_queue(LEDGER_RECOVERY_FILE)
        results["ledger_pending"] = len(ledger_queue)
        for item in ledger_queue:
            event = CanonicalEvent.model_validate(item["event"])
            await self._reconcile_missing_ledger(event, event.merchant_id)
            if event.ledger_transaction_id:
                creds = merchant_credentials.get(event.merchant_id, {})
                await self._record_analytics(event, creds.get("analytics_api_key"))
                remove_recovery_item(LEDGER_RECOVERY_FILE, event.correlation_id)
                results["ledger_replayed"] += 1

        analytics_queue = get_recovery_queue(ANALYTICS_RECOVERY_FILE)
        results["analytics_pending"] = len(analytics_queue)
        for item in analytics_queue:
            event = CanonicalEvent.model_validate(item["event"])
            creds = merchant_credentials.get(event.merchant_id, {})
            await self._record_analytics(event, creds.get("analytics_api_key"))
            if event.analytics_recorded:
                remove_recovery_item(ANALYTICS_RECOVERY_FILE, event.correlation_id)
                results["analytics_replayed"] += 1

        return results

    async def _check_fraud(self, event: CanonicalEvent, fraud_api_key: Optional[str]) -> None:
        headers = {"Content-Type": "application/json", "Idempotency-Key": event.idempotency_key}
        if fraud_api_key:
            headers["X-Api-Key"] = fraud_api_key

        fraud_request = {
            "event_type": "payment",
            "user_id": event.customer_id,
            "device_id": event.device_id,
            "email": event.email,
            "session_id": event.session_id,
            "ip_address": event.ip_address,
            "country": event.country,
            "amount": event.amount_dollars,
            "currency": event.currency,
            "payment_status": "pending",
        }

        try:
            response = await self.client.post(
                f"{self.fraud_base_url}/risk/checkout",
                json=fraud_request,
                headers=headers,
            )
            if response.status_code == 200:
                payload = response.json()
                event.fraud_decision = payload.get("decision", "block")
                event.fraud_score = payload.get("risk_score", 100)
                event.fraud_reasons = payload.get("reasons", [])
            else:
                event.fraud_decision = "block"
                event.fraud_score = 100
                event.fraud_error = f"HTTP {response.status_code}: {response.text[:200]}"
        except httpx.RequestError as exc:
            event.fraud_decision = "block"
            event.fraud_score = 100
            event.fraud_error = f"Service unavailable: {exc}"

    async def _process_transaction(self, event: CanonicalEvent, request: PaymentRequest) -> None:
        amount_str = f"{event.amount_dollars:.2f}"
        fee_str = f"{event.fee_dollars:.2f}"
        transaction_request = {
            "customerId": event.customer_id,
            "amount": amount_str,
            "feeAmount": fee_str,
            "currency": event.currency,
            "paymentMethod": {
                "type": event.payment_method_type,
                "token": request.payment_method_token,
                "last4": event.payment_method_last4,
                "brand": event.payment_method_brand,
            },
            "metadata": {
                **request.metadata,
                "correlation_id": event.correlation_id,
                "event_id": event.event_id,
                "fraud_decision": event.fraud_decision,
                "fraud_score": event.fraud_score,
                "processor_fee_amount": fee_str,
            },
        }
        if request.description:
            transaction_request["description"] = request.description

        try:
            response = await self.client.post(
                f"{self.transaction_base_url}/payments",
                json=transaction_request,
                headers={
                    "Content-Type": "application/json",
                    "X-Organization-ID": event.merchant_id,
                    "Idempotency-Key": event.idempotency_key,
                },
            )
            if response.status_code in (200, 201):
                payload = response.json()
                event.transaction_id = payload.get("id")
                event.ledger_transaction_id = payload.get("ledgerTransactionId")
            else:
                event.transaction_error = f"HTTP {response.status_code}: {response.text[:200]}"
        except httpx.RequestError as exc:
            event.transaction_error = f"Service unavailable: {exc}"

    async def _reconcile_missing_ledger(self, event: CanonicalEvent, merchant_id: str) -> None:
        """
        Transaction succeeded but returned no ledger_transaction_id.
        Query the ledger by referenceId. If absent, enqueue durable reconciliation.
        The transaction service will retry using the same payment-derived idempotency key.
        """
        try:
            # Query ledger transactions by referenceId
            response = await self.client.get(
                f"{self.ledger_base_url}/transactions",
                params={"referenceId": event.transaction_id},
                headers={"X-Organization-ID": merchant_id},
            )
            if response.status_code == 200:
                payload = response.json()
                # Check if any transaction matches our referenceId
                if isinstance(payload, list):
                    for txn in payload:
                        if txn.get("referenceId") == event.transaction_id:
                            event.ledger_transaction_id = txn.get("id")
                            event.ledger_entries = txn.get("entries", [])
                            remove_recovery_item(LEDGER_RECOVERY_FILE, event.correlation_id)
                            return
                elif isinstance(payload, dict) and payload.get("referenceId") == event.transaction_id:
                    event.ledger_transaction_id = payload.get("id")
                    event.ledger_entries = payload.get("entries", [])
                    remove_recovery_item(LEDGER_RECOVERY_FILE, event.correlation_id)
                    return

            # Not found — enqueue for reconciliation
            event.ledger_error = "Ledger transaction not found by referenceId — enqueued for reconciliation"
            self._queue_ledger_recovery(event)
        except httpx.RequestError as exc:
            event.ledger_error = f"Ledger query unavailable: {exc}"
            self._queue_ledger_recovery(event)

    async def _record_analytics(self, event: CanonicalEvent, analytics_api_key: Optional[str]) -> None:
        headers = {"Content-Type": "application/json"}
        if analytics_api_key:
            headers["X-Api-Key"] = analytics_api_key

        payload = {
            "customer_id": event.customer_id,
            "amount": event.amount_dollars,
            "fee_amount": event.fee_dollars,
            "currency": event.currency,
            "status": "completed",
            "payment_method": event.payment_method_type,
            "country": event.country,
            "correlation_id": event.correlation_id,
            "event_id": event.event_id,
            "external_transaction_id": event.transaction_id,
        }

        try:
            response = await self.client.post(
                f"{self.analytics_base_url}/transactions",
                json=payload,
                headers=headers,
            )
            if response.status_code == 200:
                body = response.json()
                event.analytics_recorded = True
                event.analytics_transaction_id = str(body.get("transaction_id"))
                remove_recovery_item(ANALYTICS_RECOVERY_FILE, event.correlation_id)
            else:
                event.analytics_error = f"HTTP {response.status_code}: {response.text[:200]}"
                self._queue_analytics_recovery(event)
        except httpx.RequestError as exc:
            event.analytics_error = f"Service unavailable: {exc}"
            self._queue_analytics_recovery(event)

    def _queue_ledger_recovery(self, event: CanonicalEvent) -> None:
        push_recovery_item(
            LEDGER_RECOVERY_FILE,
            {
                "correlation_id": event.correlation_id,
                "event": event.model_dump(),
            },
        )

    def _queue_analytics_recovery(self, event: CanonicalEvent) -> None:
        push_recovery_item(
            ANALYTICS_RECOVERY_FILE,
            {
                "correlation_id": event.correlation_id,
                "event": event.model_dump(),
            },
        )


orchestrator = RevenueAssuranceOrchestrator()
