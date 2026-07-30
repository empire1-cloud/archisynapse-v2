from __future__ import annotations

import hashlib
import json
from typing import Any
from uuid import uuid4

from .errors import IdempotencyConflict, ReceiptIntegrityError
from .models import iso_now
from .receipts import ReceiptSigner, canonical_json
from .storage import Database


def _sha256_text(value: str | None) -> str | None:
    if value is None:
        return None
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


class FableReceiptStore:
    """Signed FABLE-5 receipts for contracts, execution authority, and SLA verdicts."""

    def __init__(self, db: Database, signer: ReceiptSigner):
        self.db = db
        self.signer = signer

    def issue(
        self,
        *,
        receipt_type: str,
        contract_id: str | None,
        authorization_id: str | None,
        execution_id: str,
        payment_receipt_id: str | None,
        body: dict[str, Any],
    ) -> dict[str, Any]:
        conn = self.db.connect()
        try:
            existing = conn.execute(
                """
                SELECT payload_json FROM fable_receipts
                WHERE receipt_type=? AND contract_id IS ? AND execution_id=?
                ORDER BY created_at ASC LIMIT 1
                """,
                (receipt_type, contract_id, execution_id),
            ).fetchone()
        finally:
            conn.close()
        if existing:
            payload = json.loads(existing["payload_json"])
            if not self.signer.verify(payload):
                raise ReceiptIntegrityError("stored FABLE receipt failed signature verification")
            stable = (
                payload.get("authorization_id"),
                payload.get("payment_receipt_id"),
                canonical_json(payload.get("body") or {}),
            )
            incoming = (
                authorization_id,
                payment_receipt_id,
                canonical_json(body),
            )
            if stable != incoming:
                raise IdempotencyConflict(
                    "FABLE execution id already exists with different receipt content"
                )
            return payload

        receipt_id = f"fable_{uuid4().hex}"
        created_at = iso_now()
        payload = {
            "id": receipt_id,
            "receipt_type": receipt_type,
            "contract_id": contract_id,
            "authorization_id": authorization_id,
            "execution_id": execution_id,
            "payment_receipt_id": payment_receipt_id,
            "body": body,
            "created_at": created_at,
        }
        digest, signature = self.signer.sign_payload(payload)
        full = {**payload, "receipt_sha256": digest, "signature": signature}
        with self.db.transaction(immediate=True) as conn:
            conn.execute(
                """
                INSERT INTO fable_receipts
                (id, receipt_type, contract_id, authorization_id, execution_id,
                 payment_receipt_id, payload_json, receipt_sha256, signature, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    receipt_id,
                    receipt_type,
                    contract_id,
                    authorization_id,
                    execution_id,
                    payment_receipt_id,
                    canonical_json(full),
                    digest,
                    signature,
                    created_at,
                ),
            )
            self.db.append_audit_event(
                conn,
                event_id=f"evt_{uuid4().hex}",
                aggregate_type="fable_receipt",
                aggregate_id=receipt_id,
                event_type=f"fable.{receipt_type}",
                payload={
                    "contract_id": contract_id,
                    "authorization_id": authorization_id,
                    "execution_id": execution_id,
                    "payment_receipt_id": payment_receipt_id,
                    "receipt_sha256": digest,
                },
            )
        return full

    def issue_contract_receipt(self, contract: dict[str, Any]) -> dict[str, Any]:
        return self.issue(
            receipt_type="service_contract",
            contract_id=contract["id"],
            authorization_id=None,
            execution_id=f"contract:{contract['id']}:v{contract['version']}",
            payment_receipt_id=None,
            body={"contract": contract},
        )

    def issue_execution_authorization(
        self,
        *,
        contract: dict[str, Any],
        authorization: dict[str, Any],
        request: dict[str, Any],
    ) -> dict[str, Any]:
        execution_id = request["orchestration_id"]
        return self.issue(
            receipt_type="execution_authorization",
            contract_id=contract["id"],
            authorization_id=authorization["id"],
            execution_id=execution_id,
            payment_receipt_id=None,
            body={
                "tenant_id": authorization["tenant_id"],
                "orchestrator_id": authorization["orchestrator_id"],
                "agent_npub": request["agent_npub"],
                "specialty": request["specialty"],
                "endpoint": request["endpoint"],
                "quoted_sats": request["quoted_sats"],
                "authorization_version": authorization["version"],
                "authorization_expires_at": authorization["expires_at"],
                "contract_version": contract["version"],
                "settlement_policy": contract["settlement_policy"],
                "query_sha256": _sha256_text(request.get("query")),
                "context_sha256": _sha256_text(request.get("context")),
            },
        )

    def issue_delivery_verdict(
        self,
        *,
        contract: dict[str, Any],
        authorization_id: str,
        execution_id: str,
        payment_receipt: dict[str, Any],
        evaluation: dict[str, Any],
    ) -> dict[str, Any]:
        return self.issue(
            receipt_type="delivery_verdict",
            contract_id=contract["id"],
            authorization_id=authorization_id,
            execution_id=execution_id,
            payment_receipt_id=payment_receipt["id"],
            body={
                "payment_receipt_sha256": payment_receipt["receipt_sha256"],
                "payment_total_debit_sats": payment_receipt["total_debit_sats"],
                "delivery": payment_receipt["delivery"],
                "sla_evaluation": evaluation,
            },
        )

    def issue_final_sla_verdict(
        self,
        *,
        contract: dict[str, Any],
        authorization_id: str,
        execution_id: str,
        payment_receipt_id: str,
        result: dict[str, Any],
    ) -> dict[str, Any]:
        return self.issue(
            receipt_type="final_sla_verdict",
            contract_id=contract["id"],
            authorization_id=authorization_id,
            execution_id=execution_id,
            payment_receipt_id=payment_receipt_id,
            body=result,
        )

    def get(self, receipt_id: str) -> dict[str, Any] | None:
        conn = self.db.connect()
        try:
            row = conn.execute(
                "SELECT payload_json FROM fable_receipts WHERE id=?", (receipt_id,)
            ).fetchone()
        finally:
            conn.close()
        if not row:
            return None
        payload = json.loads(row["payload_json"])
        if not self.signer.verify(payload):
            raise ReceiptIntegrityError("FABLE receipt signature verification failed")
        return payload

    def find_delivery_verdict(
        self, *, contract_id: str, payment_receipt_id: str
    ) -> dict[str, Any] | None:
        conn = self.db.connect()
        try:
            row = conn.execute(
                """
                SELECT payload_json FROM fable_receipts
                WHERE receipt_type='delivery_verdict' AND contract_id=? AND payment_receipt_id=?
                ORDER BY created_at DESC LIMIT 1
                """,
                (contract_id, payment_receipt_id),
            ).fetchone()
        finally:
            conn.close()
        if not row:
            return None
        payload = json.loads(row["payload_json"])
        if not self.signer.verify(payload):
            raise ReceiptIntegrityError("FABLE delivery receipt failed signature verification")
        return payload
