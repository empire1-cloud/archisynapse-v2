"""
HTTP client for the transaction service's royalty endpoints. The
gateway NEVER calls the ledger directly for this domain — it calls the
transaction service, which is the sole owner of ledger posting (see
services/transaction/royalty-service-core.ts). Every transaction_id
and ledger_transaction_id the gateway ever sees comes from a response
here; the gateway never invents one.
"""

import os
from decimal import Decimal
from typing import Optional

import httpx

TRANSACTION_SERVICE_URL = os.getenv("TRANSACTION_SERVICE_URL", "http://127.0.0.1:3000")


class TransactionServiceError(Exception):
    def __init__(self, status_code: int, body: str):
        super().__init__(f"transaction service error {status_code}: {body}")
        self.status_code = status_code
        self.body = body


class RoyaltyIdempotencyConflict(Exception):
    pass


class RoyaltyTransactionClient:
    def __init__(self, base_url: str = TRANSACTION_SERVICE_URL, timeout: float = 20.0):
        self.base_url = base_url
        self.client = httpx.AsyncClient(timeout=timeout)

    async def close(self) -> None:
        await self.client.aclose()

    async def create_obligation(
        self,
        organization_id: str,
        event_id: str,
        correlation_id: str,
        idempotency_key: str,
        tenant_id: str,
        track_id: str,
        creator_id: str,
        trigger_kind: str,
        amount: Decimal,
        currency: str,
        splits: list,
        decision: str,
        decision_policy: str,
        risk_score: float,
        status_reasons: list,
        request_hash: str,
    ) -> dict:
        response = await self.client.post(
            f"{self.base_url}/royalties",
            json={
                "eventId": event_id,
                "correlationId": correlation_id,
                "idempotencyKey": idempotency_key,
                "tenantId": tenant_id,
                "trackId": track_id,
                "creatorId": creator_id,
                "triggerKind": trigger_kind,
                "amount": str(amount),
                "currency": currency,
                "splits": splits,
                "decision": decision,
                "decisionPolicy": decision_policy,
                "riskScore": risk_score,
                "statusReasons": status_reasons,
                "requestHash": request_hash,
            },
            headers={"X-Organization-ID": organization_id},
        )
        if response.status_code == 409:
            raise RoyaltyIdempotencyConflict(response.text)
        if response.status_code not in (200, 201):
            raise TransactionServiceError(response.status_code, response.text)
        return response.json()

    async def get_obligation(self, organization_id: str, event_id: str) -> Optional[dict]:
        response = await self.client.get(
            f"{self.base_url}/royalties/{event_id}",
            headers={"X-Organization-ID": organization_id},
        )
        if response.status_code == 404:
            return None
        if response.status_code != 200:
            raise TransactionServiceError(response.status_code, response.text)
        return response.json()

    async def release_obligation(self, organization_id: str, event_id: str, idempotency_key: str) -> dict:
        response = await self.client.post(
            f"{self.base_url}/royalties/{event_id}/release",
            headers={"X-Organization-ID": organization_id, "Idempotency-Key": idempotency_key},
        )
        if response.status_code not in (200, 201):
            raise TransactionServiceError(response.status_code, response.text)
        return response.json()

    async def reverse_obligation(
        self,
        organization_id: str,
        event_id: str,
        reversal_event_id: str,
        reversal_idempotency_key: str,
        reason: str,
    ) -> tuple[dict, int]:
        """Returns (obligation, http_status) -- 201 fresh reversal, 200 idempotent replay."""
        response = await self.client.post(
            f"{self.base_url}/royalties/{event_id}/reverse",
            json={
                "reversalEventId": reversal_event_id,
                "reversalIdempotencyKey": reversal_idempotency_key,
                "reason": reason,
            },
            headers={"X-Organization-ID": organization_id},
        )
        if response.status_code not in (200, 201):
            raise TransactionServiceError(response.status_code, response.text)
        return response.json(), response.status_code
