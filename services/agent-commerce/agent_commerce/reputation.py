from __future__ import annotations

from uuid import uuid4

from .errors import IdempotencyConflict, ReceiptIntegrityError
from .models import iso_now
from .receipts import ReceiptStore
from .storage import Database


class ReputationStore:
    def __init__(self, db: Database, receipts: ReceiptStore):
        self.db = db
        self.receipts = receipts

    @staticmethod
    def _score(*, verified: int, successful: int, quality_sum: float) -> float:
        reliability = (successful + 2) / (verified + 4)
        quality = (quality_sum + 1.0) / (verified + 2)
        return round(100 * ((0.70 * reliability) + (0.30 * quality)), 4)

    def record_verified_outcome(
        self,
        *,
        receipt_id: str,
        validator_id: str,
        success: bool,
        quality_score: float,
        latency_ms: int,
        evidence_sha256: str,
    ) -> dict:
        if not 0 <= quality_score <= 1:
            raise ValueError("quality_score must be between 0 and 1")
        if latency_ms < 0:
            raise ValueError("latency_ms cannot be negative")
        receipt = self.receipts.get(receipt_id)
        if not receipt:
            raise ReceiptIntegrityError("cannot score missing receipt")
        now = iso_now()
        with self.db.transaction(immediate=True) as conn:
            existing = conn.execute(
                "SELECT * FROM reputation_outcomes WHERE receipt_id=?", (receipt_id,)
            ).fetchone()
            if existing:
                stable = (
                    existing["validator_id"],
                    bool(existing["success"]),
                    existing["quality_score"],
                    existing["latency_ms"],
                    existing["evidence_sha256"],
                )
                incoming = (validator_id, success, quality_score, latency_ms, evidence_sha256)
                if stable != incoming:
                    raise IdempotencyConflict("receipt already has a different verified outcome")
                return self.get(receipt.agent_npub)

            conn.execute(
                """
                INSERT INTO reputation_outcomes
                (receipt_id, agent_npub, validator_id, success, quality_score, latency_ms, evidence_sha256, verified_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    receipt_id,
                    receipt.agent_npub,
                    validator_id,
                    int(success),
                    quality_score,
                    latency_ms,
                    evidence_sha256,
                    now,
                ),
            )
            current = conn.execute(
                "SELECT * FROM agent_reputation WHERE agent_npub=?", (receipt.agent_npub,)
            ).fetchone()
            verified = (current["verified_jobs"] if current else 0) + 1
            successful = (current["successful_jobs"] if current else 0) + int(success)
            failed = (current["failed_jobs"] if current else 0) + int(not success)
            total_paid = (current["total_paid_sats"] if current else 0) + receipt.total_debit_sats
            latency_sum = (current["latency_sum_ms"] if current else 0) + latency_ms
            quality_sum = (current["quality_sum"] if current else 0.0) + quality_score
            score = self._score(verified=verified, successful=successful, quality_sum=quality_sum)
            conn.execute(
                """
                INSERT INTO agent_reputation
                (agent_npub, verified_jobs, successful_jobs, failed_jobs, total_paid_sats,
                 latency_sum_ms, quality_sum, score, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(agent_npub) DO UPDATE SET
                    verified_jobs=excluded.verified_jobs,
                    successful_jobs=excluded.successful_jobs,
                    failed_jobs=excluded.failed_jobs,
                    total_paid_sats=excluded.total_paid_sats,
                    latency_sum_ms=excluded.latency_sum_ms,
                    quality_sum=excluded.quality_sum,
                    score=excluded.score,
                    updated_at=excluded.updated_at
                """,
                (
                    receipt.agent_npub,
                    verified,
                    successful,
                    failed,
                    total_paid,
                    latency_sum,
                    quality_sum,
                    score,
                    now,
                ),
            )
            self.db.append_audit_event(
                conn,
                event_id=f"evt_{uuid4().hex}",
                aggregate_type="agent_reputation",
                aggregate_id=receipt.agent_npub,
                event_type="reputation.outcome_verified",
                payload={
                    "receipt_id": receipt_id,
                    "validator_id": validator_id,
                    "success": success,
                    "quality_score": quality_score,
                    "latency_ms": latency_ms,
                    "evidence_sha256": evidence_sha256,
                    "score": score,
                },
            )
        return self.get(receipt.agent_npub)

    def get(self, agent_npub: str) -> dict:
        conn = self.db.connect()
        try:
            row = conn.execute("SELECT * FROM agent_reputation WHERE agent_npub=?", (agent_npub,)).fetchone()
        finally:
            conn.close()
        if not row:
            return {
                "agent_npub": agent_npub,
                "verified_jobs": 0,
                "successful_jobs": 0,
                "failed_jobs": 0,
                "total_paid_sats": 0,
                "average_latency_ms": None,
                "average_quality": None,
                "score": 50.0,
                "evidence_state": "unverified",
            }
        verified = row["verified_jobs"]
        return {
            "agent_npub": row["agent_npub"],
            "verified_jobs": verified,
            "successful_jobs": row["successful_jobs"],
            "failed_jobs": row["failed_jobs"],
            "total_paid_sats": row["total_paid_sats"],
            "average_latency_ms": round(row["latency_sum_ms"] / verified, 2) if verified else None,
            "average_quality": round(row["quality_sum"] / verified, 4) if verified else None,
            "score": row["score"],
            "evidence_state": "verified",
            "updated_at": row["updated_at"],
        }
