from __future__ import annotations

import math
from typing import Any
from uuid import uuid4

from .errors import IdempotencyConflict, UsageReconciliationError
from .models import iso_now
from .token_common import _fingerprint, _row


class ReconcileMixin:
    def reconcile_provider_event(
        self,
        *,
        provider: str,
        provider_event_id: str,
        provider_request_id: str | None,
        cost_microusd: int,
        input_tokens: int | None = None,
        output_tokens: int | None = None,
        cached_input_tokens: int | None = None,
        reasoning_tokens: int | None = None,
        tolerance_microusd: int = 1_000,
    ) -> dict[str, Any]:
        if cost_microusd < 0 or tolerance_microusd < 0:
            raise ValueError("cost and tolerance cannot be negative")
        event_payload = {
            "provider": provider.strip().lower(),
            "provider_event_id": provider_event_id.strip(),
            "provider_request_id": provider_request_id.strip() if provider_request_id else None,
            "cost_microusd": cost_microusd,
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "cached_input_tokens": cached_input_tokens,
            "reasoning_tokens": reasoning_tokens,
        }
        payload_hash = _fingerprint(event_payload)
        now = iso_now()
        with self.db.transaction(immediate=True) as conn:
            existing = conn.execute(
                "SELECT * FROM ai_provider_usage_events WHERE provider=? AND provider_event_id=?",
                (event_payload["provider"], event_payload["provider_event_id"]),
            ).fetchone()
            if existing:
                if existing["payload_sha256"] != payload_hash:
                    raise IdempotencyConflict("provider event ID was replayed with different usage data")
                return _row(existing)

            reservation = None
            if event_payload["provider_request_id"]:
                reservation = conn.execute(
                    "SELECT * FROM ai_spend_reservations WHERE selected_provider=? AND provider_request_id=?",
                    (event_payload["provider"], event_payload["provider_request_id"]),
                ).fetchone()
            event_id = f"aipevt_{uuid4().hex}"
            if not reservation:
                conn.execute(
                    """
                    INSERT INTO ai_provider_usage_events
                    (id, provider, provider_event_id, provider_request_id, cost_microusd,
                     input_tokens, output_tokens, cached_input_tokens, reasoning_tokens,
                     reservation_id, status, payload_sha256, received_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, 'orphaned', ?, ?)
                    """,
                    (event_id, event_payload["provider"], event_payload["provider_event_id"], event_payload["provider_request_id"],
                     cost_microusd, input_tokens, output_tokens, cached_input_tokens, reasoning_tokens, payload_hash, now),
                )
                self._alert(
                    conn, policy_id=None, reservation_id=None, severity="critical",
                    alert_type="orphaned_provider_charge",
                    message="Provider reported AI spend with no matching internal reservation",
                    details=event_payload,
                )
                return _row(conn.execute("SELECT * FROM ai_provider_usage_events WHERE id=?", (event_id,)).fetchone())

            expected = reservation["actual_cost_microusd"]
            if expected is None:
                raise UsageReconciliationError("provider event arrived before internal usage was finalized")
            effective_tolerance = max(tolerance_microusd, math.ceil(expected * 0.01))
            difference = abs(cost_microusd - expected)
            status = "confirmed" if difference <= effective_tolerance else "disputed"
            conn.execute(
                """
                INSERT INTO ai_provider_usage_events
                (id, provider, provider_event_id, provider_request_id, cost_microusd,
                 input_tokens, output_tokens, cached_input_tokens, reasoning_tokens,
                 reservation_id, status, payload_sha256, received_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (event_id, event_payload["provider"], event_payload["provider_event_id"], event_payload["provider_request_id"],
                 cost_microusd, input_tokens, output_tokens, cached_input_tokens, reasoning_tokens,
                 reservation["id"], status, payload_hash, now),
            )
            conn.execute("UPDATE ai_spend_reservations SET reconciliation_status=?, updated_at=? WHERE id=?", (status, now, reservation["id"]))
            if status == "disputed":
                self._pause_policy(
                    conn, policy_id=reservation["policy_id"], reason="provider_reconciliation_mismatch",
                    reservation_id=reservation["id"],
                    details={"internal_cost_microusd": expected, "provider_cost_microusd": cost_microusd,
                             "difference_microusd": difference, "tolerance_microusd": effective_tolerance},
                )
            self.db.append_audit_event(
                conn, event_id=f"evt_{uuid4().hex}", aggregate_type="ai_provider_usage_event",
                aggregate_id=event_id, event_type=f"ai_usage.reconciliation_{status}",
                payload={"reservation_id": reservation["id"], "internal_cost_microusd": expected,
                         "provider_cost_microusd": cost_microusd, "difference_microusd": difference},
            )
            return _row(conn.execute("SELECT * FROM ai_provider_usage_events WHERE id=?", (event_id,)).fetchone())
