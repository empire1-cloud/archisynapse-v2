"""
Thin HTTP client for the ledger service's double-entry journal API
(services/ledger/ledger-service-api.ts), used by the royalty orchestrator.

The real ledger service does NOT dedupe accounts by (organization_id,
code) server-side — createAccount always inserts and the DB enforces
a UNIQUE(organization_id, code) constraint. `ensure_account` therefore
does a find-before-create at this layer.
"""

import os
from typing import Optional

import httpx

LEDGER_SERVICE_URL = os.getenv("LEDGER_SERVICE_URL", "http://127.0.0.1:3001")


class LedgerEntry:
    __slots__ = ("account_id", "debit_credit", "amount", "description")

    def __init__(self, account_id: str, debit_credit: str, amount: str, description: str):
        self.account_id = account_id
        self.debit_credit = debit_credit
        self.amount = amount
        self.description = description

    def to_json(self) -> dict:
        return {
            "accountId": self.account_id,
            "debitCredit": self.debit_credit,
            "amount": self.amount,
            "description": self.description,
        }


class LedgerClient:
    def __init__(self, base_url: str = LEDGER_SERVICE_URL, timeout: float = 15.0):
        self.base_url = base_url
        self.client = httpx.AsyncClient(timeout=timeout)

    async def close(self) -> None:
        await self.client.aclose()

    async def list_accounts(self, organization_id: str) -> list[dict]:
        response = await self.client.get(
            f"{self.base_url}/accounts",
            headers={"X-Organization-ID": organization_id},
        )
        response.raise_for_status()
        return response.json()

    async def create_account(
        self, organization_id: str, code: str, name: str, account_type: str, currency: str = "USD"
    ) -> dict:
        response = await self.client.post(
            f"{self.base_url}/accounts",
            json={"code": code, "name": name, "type": account_type, "currency": currency},
            headers={"X-Organization-ID": organization_id},
        )
        response.raise_for_status()
        return response.json()

    async def ensure_account(
        self, organization_id: str, code: str, name: str, account_type: str, currency: str = "USD"
    ) -> str:
        """Find-or-create by (organization_id, code); returns accountId."""
        existing = await self.list_accounts(organization_id)
        for account in existing:
            if account.get("code") == code:
                return account["id"]
        created = await self.create_account(organization_id, code, name, account_type, currency)
        return created["id"]

    async def post_transaction(
        self,
        organization_id: str,
        transaction_type: str,
        description: str,
        amount: str,
        entries: list[LedgerEntry],
        idempotency_key: Optional[str] = None,
        reference_id: Optional[str] = None,
        currency: str = "USD",
    ) -> dict:
        payload = {
            "type": transaction_type,
            "description": description,
            "amount": amount,
            "currency": currency,
            "entries": [e.to_json() for e in entries],
        }
        if idempotency_key:
            payload["idempotencyKey"] = idempotency_key
        if reference_id:
            payload["referenceId"] = reference_id

        response = await self.client.post(
            f"{self.base_url}/transactions",
            json=payload,
            headers={"X-Organization-ID": organization_id},
        )
        response.raise_for_status()
        return response.json()
