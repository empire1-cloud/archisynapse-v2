from __future__ import annotations

import json
from datetime import datetime, timezone
from math import ceil
from uuid import uuid4

from .errors import (
    AuthorizationDenied,
    AuthorizationExpired,
    AuthorizationRevoked,
    BudgetExceeded,
    IdempotencyConflict,
)
from .models import Authorization, DecodedInvoice, Reservation, iso_now
from .storage import Database


OPEN_RESERVATION_STATES = ("reserved", "invoice_bound", "payment_initiated", "paid")


def _parse_time(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


class AuthorizationStore:
    def __init__(self, db: Database):
        self.db = db

    @staticmethod
    def _authorization(row) -> Authorization:
        return Authorization(
            id=row["id"],
            tenant_id=row["tenant_id"],
            orchestrator_id=row["orchestrator_id"],
            max_total_sats=row["max_total_sats"],
            max_per_call_sats=row["max_per_call_sats"],
            max_route_fee_sats=row["max_route_fee_sats"],
            max_calls=row["max_calls"],
            allowed_agent_npubs=tuple(json.loads(row["allowed_agent_npubs_json"])),
            allowed_specialties=tuple(json.loads(row["allowed_specialties_json"])),
            expires_at=row["expires_at"],
            status=row["status"],
            reserved_sats=row["reserved_sats"],
            spent_sats=row["spent_sats"],
            call_count=row["call_count"],
            version=row["version"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )

    @staticmethod
    def _reservation(row) -> Reservation:
        return Reservation(
            id=row["id"],
            authorization_id=row["authorization_id"],
            idempotency_key=row["idempotency_key"],
            orchestration_id=row["orchestration_id"],
            agent_npub=row["agent_npub"],
            specialty=row["specialty"],
            quoted_sats=row["quoted_sats"],
            reserved_sats=row["reserved_sats"],
            invoice_sats=row["invoice_sats"],
            payment_hash=row["payment_hash"],
            provider_payment_id=row["provider_payment_id"],
            route_fee_sats=row["route_fee_sats"],
            preimage_hash=row["preimage_hash"],
            status=row["status"],
            receipt_id=row["receipt_id"],
            invoice_payload=json.loads(row["invoice_json"]) if row["invoice_json"] else None,
            provider=row["provider"],
            settled_at=row["settled_at"],
            settlement_evidence=json.loads(row["settlement_evidence_json"]) if row["settlement_evidence_json"] else None,
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )

    def create(
        self,
        *,
        tenant_id: str,
        orchestrator_id: str,
        max_total_sats: int,
        max_per_call_sats: int,
        max_calls: int,
        expires_at: str,
        max_route_fee_sats: int = 0,
        allowed_agent_npubs: list[str] | None = None,
        allowed_specialties: list[str] | None = None,
        authorization_id: str | None = None,
    ) -> Authorization:
        if min(max_total_sats, max_per_call_sats, max_calls) <= 0:
            raise ValueError("budgets and max_calls must be positive")
        if max_per_call_sats > max_total_sats:
            raise ValueError("max_per_call_sats cannot exceed max_total_sats")
        if max_route_fee_sats < 0:
            raise ValueError("max_route_fee_sats cannot be negative")
        if _parse_time(expires_at) <= datetime.now(timezone.utc):
            raise ValueError("authorization must expire in the future")
        now = iso_now()
        auth_id = authorization_id or f"auth_{uuid4().hex}"
        agents = sorted(set(allowed_agent_npubs or []))
        specialties = sorted(set(allowed_specialties or []))
        with self.db.transaction(immediate=True) as conn:
            conn.execute(
                """
                INSERT INTO authorizations
                (id, tenant_id, orchestrator_id, max_total_sats, max_per_call_sats,
                 max_route_fee_sats, max_calls, allowed_agent_npubs_json,
                 allowed_specialties_json, expires_at, status, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'active', ?, ?)
                """,
                (
                    auth_id,
                    tenant_id,
                    orchestrator_id,
                    max_total_sats,
                    max_per_call_sats,
                    max_route_fee_sats,
                    max_calls,
                    json.dumps(agents),
                    json.dumps(specialties),
                    expires_at,
                    now,
                    now,
                ),
            )
            self.db.append_audit_event(
                conn,
                event_id=f"evt_{uuid4().hex}",
                aggregate_type="authorization",
                aggregate_id=auth_id,
                event_type="authorization.created",
                payload={
                    "tenant_id": tenant_id,
                    "orchestrator_id": orchestrator_id,
                    "max_total_sats": max_total_sats,
                    "max_per_call_sats": max_per_call_sats,
                    "max_route_fee_sats": max_route_fee_sats,
                    "max_calls": max_calls,
                    "allowed_agent_npubs": agents,
                    "allowed_specialties": specialties,
                    "expires_at": expires_at,
                },
            )
            row = conn.execute("SELECT * FROM authorizations WHERE id = ?", (auth_id,)).fetchone()
        return self._authorization(row)

    def get(self, authorization_id: str) -> Authorization | None:
        conn = self.db.connect()
        try:
            row = conn.execute("SELECT * FROM authorizations WHERE id = ?", (authorization_id,)).fetchone()
        finally:
            conn.close()
        return self._authorization(row) if row else None

    def get_reservation(self, reservation_id: str) -> Reservation | None:
        conn = self.db.connect()
        try:
            row = conn.execute("SELECT * FROM reservations WHERE id = ?", (reservation_id,)).fetchone()
        finally:
            conn.close()
        return self._reservation(row) if row else None

    def revoke(self, authorization_id: str, *, reason: str) -> Authorization:
        now = iso_now()
        with self.db.transaction(immediate=True) as conn:
            row = conn.execute("SELECT * FROM authorizations WHERE id = ?", (authorization_id,)).fetchone()
            if not row:
                raise AuthorizationDenied("authorization not found")
            if row["status"] == "revoked":
                return self._authorization(row)
            conn.execute(
                "UPDATE authorizations SET status='revoked', version=version+1, updated_at=? WHERE id=?",
                (now, authorization_id),
            )
            self.db.append_audit_event(
                conn,
                event_id=f"evt_{uuid4().hex}",
                aggregate_type="authorization",
                aggregate_id=authorization_id,
                event_type="authorization.revoked",
                payload={"reason": reason},
            )
            row = conn.execute("SELECT * FROM authorizations WHERE id = ?", (authorization_id,)).fetchone()
        return self._authorization(row)

    def reserve(
        self,
        *,
        authorization_id: str,
        idempotency_key: str,
        orchestration_id: str,
        agent_npub: str,
        specialty: str,
        quoted_sats: int,
        advertised_tolerance: float = 0.10,
    ) -> Reservation:
        if quoted_sats <= 0:
            raise AuthorizationDenied("quoted_sats must be positive")
        now = iso_now()
        with self.db.transaction(immediate=True) as conn:
            existing = conn.execute(
                "SELECT * FROM reservations WHERE authorization_id=? AND idempotency_key=?",
                (authorization_id, idempotency_key),
            ).fetchone()
            if existing:
                stable = (existing["agent_npub"], existing["specialty"], existing["quoted_sats"], existing["orchestration_id"])
                incoming = (agent_npub, specialty, quoted_sats, orchestration_id)
                if stable != incoming:
                    raise IdempotencyConflict("idempotency key already used with different call details")
                return self._reservation(existing)

            row = conn.execute("SELECT * FROM authorizations WHERE id = ?", (authorization_id,)).fetchone()
            if not row:
                raise AuthorizationDenied("authorization not found")
            if row["status"] == "revoked":
                raise AuthorizationRevoked("authorization has been revoked")
            if row["status"] != "active":
                raise AuthorizationDenied(f"authorization is {row['status']}")
            if _parse_time(row["expires_at"]) <= datetime.now(timezone.utc):
                conn.execute(
                    "UPDATE authorizations SET status='expired', version=version+1, updated_at=? WHERE id=?",
                    (now, authorization_id),
                )
                raise AuthorizationExpired("authorization has expired")
            allowed_agents = set(json.loads(row["allowed_agent_npubs_json"]))
            allowed_specialties = set(json.loads(row["allowed_specialties_json"]))
            if allowed_agents and agent_npub not in allowed_agents:
                raise AuthorizationDenied("agent is outside authorization scope")
            if allowed_specialties and specialty not in allowed_specialties:
                raise AuthorizationDenied("specialty is outside authorization scope")
            if quoted_sats > row["max_per_call_sats"]:
                raise BudgetExceeded("quoted price exceeds per-call ceiling")

            placeholders = ",".join("?" for _ in OPEN_RESERVATION_STATES)
            open_count = conn.execute(
                f"SELECT COUNT(*) AS n FROM reservations WHERE authorization_id=? AND status IN ({placeholders})",
                (authorization_id, *OPEN_RESERVATION_STATES),
            ).fetchone()["n"]
            if row["call_count"] + open_count >= row["max_calls"]:
                raise BudgetExceeded("authorization call limit reached")

            invoice_ceiling = ceil(quoted_sats * (1 + advertised_tolerance))
            reserved_sats = invoice_ceiling + row["max_route_fee_sats"]
            if reserved_sats > row["max_per_call_sats"] + row["max_route_fee_sats"]:
                raise BudgetExceeded("reserved amount exceeds per-call ceiling")
            available = row["max_total_sats"] - row["spent_sats"] - row["reserved_sats"]
            if reserved_sats > available:
                raise BudgetExceeded("authorization total budget would be exceeded")

            reservation_id = f"rsv_{uuid4().hex}"
            conn.execute(
                """
                INSERT INTO reservations
                (id, authorization_id, idempotency_key, orchestration_id, agent_npub,
                 specialty, quoted_sats, reserved_sats, status, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'reserved', ?, ?)
                """,
                (
                    reservation_id,
                    authorization_id,
                    idempotency_key,
                    orchestration_id,
                    agent_npub,
                    specialty,
                    quoted_sats,
                    reserved_sats,
                    now,
                    now,
                ),
            )
            conn.execute(
                "UPDATE authorizations SET reserved_sats=reserved_sats+?, updated_at=? WHERE id=?",
                (reserved_sats, now, authorization_id),
            )
            self.db.append_audit_event(
                conn,
                event_id=f"evt_{uuid4().hex}",
                aggregate_type="reservation",
                aggregate_id=reservation_id,
                event_type="budget.reserved",
                payload={
                    "authorization_id": authorization_id,
                    "quoted_sats": quoted_sats,
                    "reserved_sats": reserved_sats,
                    "agent_npub": agent_npub,
                    "specialty": specialty,
                    "idempotency_key": idempotency_key,
                },
            )
            result = conn.execute("SELECT * FROM reservations WHERE id=?", (reservation_id,)).fetchone()
        return self._reservation(result)

    def bind_invoice(self, reservation_id: str, *, invoice: DecodedInvoice) -> Reservation:
        invoice_sats = invoice.amount_sats
        payment_hash = invoice.payment_hash
        if invoice_sats <= 0 or not payment_hash:
            raise AuthorizationDenied("invoice must have a positive amount and payment hash")
        now = iso_now()
        with self.db.transaction(immediate=True) as conn:
            row = conn.execute("SELECT * FROM reservations WHERE id=?", (reservation_id,)).fetchone()
            if not row:
                raise AuthorizationDenied("reservation not found")
            if row["status"] not in ("reserved", "invoice_bound"):
                raise AuthorizationDenied(f"cannot bind invoice while reservation is {row['status']}")
            if invoice_sats > row["reserved_sats"]:
                raise BudgetExceeded("invoice exceeds reserved budget")
            if row["payment_hash"] and row["payment_hash"] != payment_hash:
                raise IdempotencyConflict("reservation already bound to a different invoice")
            conn.execute(
                """
                UPDATE reservations SET invoice_sats=?, payment_hash=?, invoice_json=?, status='invoice_bound', updated_at=?
                WHERE id=?
                """,
                (invoice_sats, payment_hash, json.dumps(invoice.__dict__, sort_keys=True), now, reservation_id),
            )
            self.db.append_audit_event(
                conn,
                event_id=f"evt_{uuid4().hex}",
                aggregate_type="reservation",
                aggregate_id=reservation_id,
                event_type="invoice.bound",
                payload={"invoice_sats": invoice_sats, "payment_hash": payment_hash},
            )
            result = conn.execute("SELECT * FROM reservations WHERE id=?", (reservation_id,)).fetchone()
        return self._reservation(result)

    def mark_payment_initiated(self, reservation_id: str) -> Reservation:
        now = iso_now()
        with self.db.transaction(immediate=True) as conn:
            row = conn.execute("SELECT * FROM reservations WHERE id=?", (reservation_id,)).fetchone()
            if not row or row["status"] != "invoice_bound":
                raise AuthorizationDenied("payment can only start from invoice_bound")
            conn.execute(
                "UPDATE reservations SET status='payment_initiated', updated_at=? WHERE id=?",
                (now, reservation_id),
            )
            self.db.append_audit_event(
                conn,
                event_id=f"evt_{uuid4().hex}",
                aggregate_type="reservation",
                aggregate_id=reservation_id,
                event_type="payment.initiated",
                payload={"payment_hash": row["payment_hash"]},
            )
            result = conn.execute("SELECT * FROM reservations WHERE id=?", (reservation_id,)).fetchone()
        return self._reservation(result)

    def mark_paid(
        self,
        reservation_id: str,
        *,
        provider: str,
        provider_payment_id: str,
        route_fee_sats: int,
        preimage_hash: str,
        settled_at: str,
        settlement_evidence: dict,
    ) -> Reservation:
        if route_fee_sats < 0:
            raise ValueError("route fee cannot be negative")
        now = iso_now()
        with self.db.transaction(immediate=True) as conn:
            row = conn.execute("SELECT * FROM reservations WHERE id=?", (reservation_id,)).fetchone()
            if not row:
                raise AuthorizationDenied("reservation not found")
            if row["status"] == "paid":
                if (
                    row["provider"] != provider
                    or row["provider_payment_id"] != provider_payment_id
                    or row["preimage_hash"] != preimage_hash
                    or row["route_fee_sats"] != route_fee_sats
                ):
                    raise IdempotencyConflict("settlement evidence conflicts with prior payment")
                return self._reservation(row)
            if row["status"] != "payment_initiated":
                raise AuthorizationDenied("reservation is not awaiting payment settlement")
            total_debit = row["invoice_sats"] + route_fee_sats
            if total_debit > row["reserved_sats"]:
                raise BudgetExceeded("settled amount plus fees exceeds reserved budget")
            conn.execute(
                """
                UPDATE reservations SET provider=?, provider_payment_id=?, route_fee_sats=?, preimage_hash=?,
                    settled_at=?, settlement_evidence_json=?, status='paid', updated_at=? WHERE id=?
                """,
                (
                    provider,
                    provider_payment_id,
                    route_fee_sats,
                    preimage_hash,
                    settled_at,
                    json.dumps(settlement_evidence, sort_keys=True),
                    now,
                    reservation_id,
                ),
            )
            self.db.append_audit_event(
                conn,
                event_id=f"evt_{uuid4().hex}",
                aggregate_type="reservation",
                aggregate_id=reservation_id,
                event_type="payment.settled",
                payload={
                    "provider": provider,
                    "provider_payment_id": provider_payment_id,
                    "route_fee_sats": route_fee_sats,
                    "preimage_hash": preimage_hash,
                    "total_debit_sats": total_debit,
                },
            )
            result = conn.execute("SELECT * FROM reservations WHERE id=?", (reservation_id,)).fetchone()
        return self._reservation(result)

    def release(self, reservation_id: str, *, failure_code: str, failure_message: str) -> Reservation:
        now = iso_now()
        with self.db.transaction(immediate=True) as conn:
            row = conn.execute("SELECT * FROM reservations WHERE id=?", (reservation_id,)).fetchone()
            if not row:
                raise AuthorizationDenied("reservation not found")
            if row["status"] in ("released", "finalized"):
                return self._reservation(row)
            if row["status"] == "paid":
                raise AuthorizationDenied("paid reservations cannot be released; finalize a receipt instead")
            conn.execute(
                "UPDATE authorizations SET reserved_sats=reserved_sats-?, updated_at=? WHERE id=?",
                (row["reserved_sats"], now, row["authorization_id"]),
            )
            conn.execute(
                """
                UPDATE reservations SET status='released', failure_code=?, failure_message=?, updated_at=? WHERE id=?
                """,
                (failure_code, failure_message[:500], now, reservation_id),
            )
            self.db.append_audit_event(
                conn,
                event_id=f"evt_{uuid4().hex}",
                aggregate_type="reservation",
                aggregate_id=reservation_id,
                event_type="budget.released",
                payload={"failure_code": failure_code, "failure_message": failure_message[:500]},
            )
            result = conn.execute("SELECT * FROM reservations WHERE id=?", (reservation_id,)).fetchone()
        return self._reservation(result)
