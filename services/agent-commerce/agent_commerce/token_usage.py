from __future__ import annotations

import json
import math
from typing import Any
from uuid import uuid4

from .errors import AuthorizationDenied, IdempotencyConflict, ReceiptIntegrityError
from .models import iso_now
from .receipts import canonical_json
from .token_common import _row


class UsageMixin:
    def finalize_usage(
        self,
        *,
        reservation_id: str,
        actual_input_tokens: int,
        actual_output_tokens: int,
        actual_cached_input_tokens: int = 0,
        actual_reasoning_tokens: int = 0,
        provider_request_id: str,
        response_sha256: str,
        outcome_status: str,
        provider_reported_cost_microusd: int | None = None,
    ) -> dict[str, Any]:
        if len(response_sha256) != 64:
            raise ValueError("response_sha256 must be a 64-character digest")
        if not provider_request_id.strip():
            raise ValueError("provider_request_id is required")
        actual_values = (
            actual_input_tokens,
            actual_output_tokens,
            actual_cached_input_tokens,
            actual_reasoning_tokens,
        )
        if min(actual_values) < 0:
            raise ValueError("actual token counts cannot be negative")
        now = iso_now()
        with self.db.transaction(immediate=True) as conn:
            reservation = conn.execute("SELECT * FROM ai_spend_reservations WHERE id=?", (reservation_id,)).fetchone()
            if not reservation:
                raise AuthorizationDenied("AI spend reservation not found")
            if reservation["status"] == "finalized":
                return self._get_receipt_conn(conn, reservation["receipt_id"])
            if reservation["status"] != "reserved":
                raise IdempotencyConflict(f"reservation cannot be finalized from {reservation['status']}")
            policy = conn.execute("SELECT * FROM ai_spend_policies WHERE id=?", (reservation["policy_id"],)).fetchone()
            rate = conn.execute("SELECT * FROM ai_rate_cards WHERE id=?", (reservation["rate_card_id"],)).fetchone()
            if not policy or not rate:
                raise AuthorizationDenied("policy or rate evidence disappeared")
            actual_cost = self.calculate_cost(
                _row(rate), input_tokens=actual_input_tokens, output_tokens=actual_output_tokens,
                cached_input_tokens=actual_cached_input_tokens, reasoning_tokens=actual_reasoning_tokens,
            )
            estimated_cost = reservation["estimated_cost_microusd"]
            anomaly_multiplier = float(policy["anomaly_multiplier"])
            estimated_tokens = max(1, reservation["estimated_input_tokens"] + reservation["estimated_output_tokens"] + reservation["estimated_cached_input_tokens"] + reservation["estimated_reasoning_tokens"])
            actual_tokens = sum(actual_values)
            anomaly_reasons: list[str] = []
            if actual_cost > math.ceil(estimated_cost * anomaly_multiplier):
                anomaly_reasons.append("cost_over_estimate")
            if actual_tokens > math.ceil(estimated_tokens * anomaly_multiplier):
                anomaly_reasons.append("tokens_over_estimate")
            if actual_input_tokens > policy["max_input_tokens"]:
                anomaly_reasons.append("input_limit_exceeded")
            if actual_output_tokens > policy["max_output_tokens"]:
                anomaly_reasons.append("output_limit_exceeded")

            receipt_id = f"aircpt_{uuid4().hex}"
            payload = {
                "id": receipt_id,
                "policy_id": policy["id"],
                "policy_version": policy["version"],
                "reservation_id": reservation_id,
                "tenant_id": policy["tenant_id"],
                "scope_type": policy["scope_type"],
                "scope_id": policy["scope_id"],
                "requested_route": {"provider": reservation["requested_provider"], "model": reservation["requested_model"]},
                "selected_route": {"provider": reservation["selected_provider"], "model": reservation["selected_model"]},
                "fallback_used": reservation["requested_provider"] != reservation["selected_provider"] or reservation["requested_model"] != reservation["selected_model"],
                "rate_card": {"id": rate["id"], "source_reference": rate["source_reference"], "effective_at": rate["effective_at"]},
                "estimated_usage": {
                    "input_tokens": reservation["estimated_input_tokens"],
                    "output_tokens": reservation["estimated_output_tokens"],
                    "cached_input_tokens": reservation["estimated_cached_input_tokens"],
                    "reasoning_tokens": reservation["estimated_reasoning_tokens"],
                    "cost_microusd": estimated_cost,
                },
                "actual_usage": {
                    "input_tokens": actual_input_tokens,
                    "output_tokens": actual_output_tokens,
                    "cached_input_tokens": actual_cached_input_tokens,
                    "reasoning_tokens": actual_reasoning_tokens,
                    "cost_microusd": actual_cost,
                },
                "provider_reported_cost_microusd": provider_reported_cost_microusd,
                "provider_request_id": provider_request_id.strip(),
                "response_sha256": response_sha256,
                "outcome_status": outcome_status.strip(),
                "variance_microusd": actual_cost - estimated_cost,
                "anomaly_reasons": anomaly_reasons,
                "reconciliation_status": "pending",
                "created_at": now,
            }
            digest, signature = self.signer.sign_payload(payload)
            full = {**payload, "receipt_sha256": digest, "signature": signature}
            conn.execute(
                "INSERT INTO ai_usage_receipts (id, reservation_id, policy_id, payload_json, receipt_sha256, signature, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (receipt_id, reservation_id, policy["id"], canonical_json(full), digest, signature, now),
            )
            conn.execute(
                """
                UPDATE ai_spend_reservations
                SET actual_input_tokens=?, actual_output_tokens=?, actual_cached_input_tokens=?, actual_reasoning_tokens=?,
                    actual_cost_microusd=?, provider_reported_cost_microusd=?, provider_request_id=?, response_sha256=?,
                    outcome_status=?, status='finalized', receipt_id=?, updated_at=?
                WHERE id=?
                """,
                (actual_input_tokens, actual_output_tokens, actual_cached_input_tokens, actual_reasoning_tokens,
                 actual_cost, provider_reported_cost_microusd, provider_request_id.strip(), response_sha256,
                 outcome_status.strip(), receipt_id, now, reservation_id),
            )
            conn.execute(
                """
                UPDATE ai_spend_policies
                SET reserved_microusd=MAX(0, reserved_microusd-?), spent_microusd=spent_microusd+?,
                    call_count=call_count+1, updated_at=? WHERE id=?
                """,
                (reservation["reserved_microusd"], actual_cost, now, policy["id"]),
            )
            if anomaly_reasons or policy["spent_microusd"] + actual_cost > policy["budget_microusd"]:
                self._pause_policy(
                    conn, policy_id=policy["id"], reason="runaway_or_over_budget_usage",
                    reservation_id=reservation_id,
                    details={"reasons": anomaly_reasons or ["budget_exceeded_by_actual_usage"], "estimated_cost_microusd": estimated_cost, "actual_cost_microusd": actual_cost},
                )
            self.db.append_audit_event(
                conn, event_id=f"evt_{uuid4().hex}", aggregate_type="ai_usage_receipt",
                aggregate_id=receipt_id, event_type="ai_usage.finalized",
                payload={"policy_id": policy["id"], "reservation_id": reservation_id, "actual_cost_microusd": actual_cost, "receipt_sha256": digest, "anomaly_reasons": anomaly_reasons},
            )
            return full

    def get_usage_receipt(self, receipt_id: str) -> dict[str, Any]:
        conn = self.db.connect()
        try:
            return self._get_receipt_conn(conn, receipt_id)
        finally:
            conn.close()

    def _get_receipt_conn(self, conn: Any, receipt_id: str) -> dict[str, Any]:
        row = conn.execute("SELECT payload_json FROM ai_usage_receipts WHERE id=?", (receipt_id,)).fetchone()
        if not row:
            raise AuthorizationDenied("AI usage receipt not found")
        payload = json.loads(row["payload_json"])
        if not self.signer.verify(payload):
            raise ReceiptIntegrityError("AI usage receipt signature verification failed")
        return payload

    def summary(self, policy_id: str) -> dict[str, Any]:
        policy = self.get_policy(policy_id)
        conn = self.db.connect()
        try:
            by_route = [dict(row) for row in conn.execute(
                """SELECT selected_provider AS provider, selected_model AS model, COUNT(*) AS calls,
                           COALESCE(SUM(actual_cost_microusd),0) AS actual_cost_microusd
                    FROM ai_spend_reservations WHERE policy_id=? AND status='finalized'
                    GROUP BY selected_provider, selected_model ORDER BY actual_cost_microusd DESC""",
                (policy_id,),
            ).fetchall()]
            alerts = [dict(row) for row in conn.execute(
                "SELECT * FROM ai_spend_alerts WHERE policy_id=? ORDER BY created_at DESC LIMIT 20", (policy_id,),
            ).fetchall()]
        finally:
            conn.close()
        return {"policy": policy, "remaining_microusd": max(0, policy["budget_microusd"] - policy["spent_microusd"] - policy["reserved_microusd"]), "by_route": by_route, "alerts": alerts}

    def _pause_policy(self, conn: Any, *, policy_id: str, reason: str, reservation_id: str | None, details: dict[str, Any]) -> None:
        now = iso_now()
        conn.execute("UPDATE ai_spend_policies SET status='paused', stop_reason=?, version=version+1, updated_at=? WHERE id=?", (reason, now, policy_id))
        self._alert(conn, policy_id=policy_id, reservation_id=reservation_id, severity="critical", alert_type=reason, message="AI spend policy was paused automatically", details=details)

    def _alert(self, conn: Any, *, policy_id: str | None, reservation_id: str | None, severity: str, alert_type: str, message: str, details: dict[str, Any]) -> None:
        conn.execute(
            "INSERT INTO ai_spend_alerts (id, policy_id, reservation_id, severity, alert_type, message, details_json, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (f"aialert_{uuid4().hex}", policy_id, reservation_id, severity, alert_type, message, canonical_json(details), iso_now()),
        )

    @staticmethod
    def _reservation_dict(row: Any) -> dict[str, Any]:
        return _row(row)
