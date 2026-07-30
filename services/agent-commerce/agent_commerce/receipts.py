from __future__ import annotations

import hashlib
import hmac
import json
from dataclasses import asdict
from uuid import uuid4

from .errors import AuthorizationDenied, IdempotencyConflict, ReceiptIntegrityError
from .models import DecodedInvoice, DeliveryEvidence, PaymentReceipt, Reservation, iso_now
from .storage import Database


def canonical_json(value: dict) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


class ReceiptSigner:
    def __init__(self, signing_key: str):
        if len(signing_key.encode()) < 32:
            raise ValueError("receipt signing key must be at least 32 bytes")
        self._key = signing_key.encode()

    def sign_payload(self, payload: dict) -> tuple[str, str]:
        material = canonical_json(payload).encode()
        digest = hashlib.sha256(material).hexdigest()
        signature = hmac.new(self._key, digest.encode(), hashlib.sha256).hexdigest()
        return digest, signature

    def verify(self, receipt: dict) -> bool:
        supplied_digest = receipt.get("receipt_sha256")
        supplied_signature = receipt.get("signature")
        payload = {k: v for k, v in receipt.items() if k not in {"receipt_sha256", "signature"}}
        digest, signature = self.sign_payload(payload)
        return hmac.compare_digest(str(supplied_digest), digest) and hmac.compare_digest(
            str(supplied_signature), signature
        )


class ReceiptStore:
    def __init__(self, db: Database, signer: ReceiptSigner):
        self.db = db
        self.signer = signer

    def finalize_paid_call(
        self,
        *,
        reservation: Reservation,
        authorization: dict,
        endpoint: str,
        invoice: DecodedInvoice,
        settlement: dict,
        delivery: DeliveryEvidence,
    ) -> PaymentReceipt:
        if reservation.status != "paid":
            raise AuthorizationDenied("only paid reservations can be finalized")
        total_debit = invoice.amount_sats + int(settlement["route_fee_sats"])
        created_at = iso_now()
        receipt_id = f"rcpt_{uuid4().hex}"
        payload = {
            "id": receipt_id,
            "authorization_id": reservation.authorization_id,
            "authorization_version": authorization["version"],
            "reservation_id": reservation.id,
            "orchestration_id": reservation.orchestration_id,
            "idempotency_key": reservation.idempotency_key,
            "tenant_id": authorization["tenant_id"],
            "orchestrator_id": authorization["orchestrator_id"],
            "agent_npub": reservation.agent_npub,
            "specialty": reservation.specialty,
            "endpoint": endpoint,
            "quoted_sats": reservation.quoted_sats,
            "invoice": asdict(invoice),
            "settlement": settlement,
            "delivery": asdict(delivery),
            "total_debit_sats": total_debit,
            "created_at": created_at,
        }
        receipt_sha256, signature = self.signer.sign_payload(payload)
        full = {**payload, "receipt_sha256": receipt_sha256, "signature": signature}

        with self.db.transaction(immediate=True) as conn:
            existing = conn.execute("SELECT payload_json FROM receipts WHERE reservation_id=?", (reservation.id,)).fetchone()
            if existing:
                existing_payload = json.loads(existing["payload_json"])
                if not self.signer.verify(existing_payload):
                    raise ReceiptIntegrityError("stored receipt failed signature verification")
                return self._from_dict(existing_payload)

            row = conn.execute("SELECT * FROM reservations WHERE id=?", (reservation.id,)).fetchone()
            if not row or row["status"] != "paid":
                raise IdempotencyConflict("reservation state changed before receipt finalization")
            if total_debit > row["reserved_sats"]:
                raise AuthorizationDenied("final debit exceeds reservation")
            auth = conn.execute("SELECT * FROM authorizations WHERE id=?", (reservation.authorization_id,)).fetchone()
            if not auth:
                raise AuthorizationDenied("authorization disappeared")
            new_spent = auth["spent_sats"] + total_debit
            if new_spent > auth["max_total_sats"]:
                raise AuthorizationDenied("final debit would exceed authorization total")

            conn.execute(
                """
                INSERT INTO receipts
                (id, reservation_id, authorization_id, agent_npub, total_debit_sats,
                 delivery_status, payload_json, receipt_sha256, signature, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    receipt_id,
                    reservation.id,
                    reservation.authorization_id,
                    reservation.agent_npub,
                    total_debit,
                    delivery.status,
                    canonical_json(full),
                    receipt_sha256,
                    signature,
                    created_at,
                ),
            )
            conn.execute(
                """
                UPDATE authorizations
                SET reserved_sats=reserved_sats-?, spent_sats=spent_sats+?, call_count=call_count+1,
                    status=CASE WHEN call_count+1 >= max_calls OR spent_sats+? >= max_total_sats THEN 'consumed' ELSE status END,
                    updated_at=?
                WHERE id=?
                """,
                (row["reserved_sats"], total_debit, total_debit, created_at, reservation.authorization_id),
            )
            conn.execute(
                "UPDATE reservations SET status='finalized', receipt_id=?, updated_at=? WHERE id=?",
                (receipt_id, created_at, reservation.id),
            )
            self.db.append_audit_event(
                conn,
                event_id=f"evt_{uuid4().hex}",
                aggregate_type="receipt",
                aggregate_id=receipt_id,
                event_type="receipt.finalized",
                payload={
                    "reservation_id": reservation.id,
                    "authorization_id": reservation.authorization_id,
                    "receipt_sha256": receipt_sha256,
                    "total_debit_sats": total_debit,
                    "delivery_status": delivery.status,
                },
            )
        return self._from_dict(full)

    def get(self, receipt_id: str) -> PaymentReceipt | None:
        conn = self.db.connect()
        try:
            row = conn.execute("SELECT payload_json FROM receipts WHERE id=?", (receipt_id,)).fetchone()
        finally:
            conn.close()
        if not row:
            return None
        payload = json.loads(row["payload_json"])
        if not self.signer.verify(payload):
            raise ReceiptIntegrityError("receipt signature verification failed")
        return self._from_dict(payload)

    @staticmethod
    def _from_dict(payload: dict) -> PaymentReceipt:
        return PaymentReceipt(
            id=payload["id"],
            authorization_id=payload["authorization_id"],
            authorization_version=payload["authorization_version"],
            reservation_id=payload["reservation_id"],
            orchestration_id=payload["orchestration_id"],
            idempotency_key=payload["idempotency_key"],
            tenant_id=payload["tenant_id"],
            orchestrator_id=payload["orchestrator_id"],
            agent_npub=payload["agent_npub"],
            specialty=payload["specialty"],
            endpoint=payload["endpoint"],
            quoted_sats=payload["quoted_sats"],
            invoice=DecodedInvoice(**payload["invoice"]),
            settlement=payload["settlement"],
            delivery=DeliveryEvidence(**payload["delivery"]),
            total_debit_sats=payload["total_debit_sats"],
            created_at=payload["created_at"],
            receipt_sha256=payload["receipt_sha256"],
            signature=payload["signature"],
        )
