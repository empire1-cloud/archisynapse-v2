from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable
from uuid import uuid4

from .errors import AuthorizationDenied
from .models import iso_now
from .receipts import canonical_json
from .token_common import _parse_time, _route_key, _row


class PolicyMixin:
    def create_policy(
        self,
        *,
        tenant_id: str,
        scope_type: str,
        scope_id: str,
        budget_microusd: int,
        max_per_call_microusd: int,
        max_input_tokens: int,
        max_output_tokens: int,
        allowed_routes: Iterable[dict[str, str]],
        fallback_routes: Iterable[dict[str, str]] = (),
        period_start: str | None = None,
        period_end: str | None = None,
        max_calls_per_minute: int = 60,
        anomaly_multiplier: float = 3.0,
    ) -> dict[str, Any]:
        now = datetime.now(timezone.utc)
        start = period_start or now.isoformat().replace("+00:00", "Z")
        end = period_end or (now + timedelta(days=30)).isoformat().replace("+00:00", "Z")
        if _parse_time(end) <= _parse_time(start):
            raise ValueError("period_end must be after period_start")
        if min(budget_microusd, max_per_call_microusd, max_input_tokens, max_output_tokens, max_calls_per_minute) <= 0:
            raise ValueError("policy limits must be positive")
        if anomaly_multiplier < 1:
            raise ValueError("anomaly_multiplier must be at least 1")
        allowed = self._normalize_routes(allowed_routes)
        fallbacks = self._normalize_routes(fallback_routes)
        if not allowed:
            raise ValueError("at least one allowed route is required")
        if any(route not in allowed for route in fallbacks):
            raise ValueError("fallback routes must also be allowed")
        policy_id = f"aipol_{uuid4().hex}"
        created = iso_now()
        with self.db.transaction(immediate=True) as conn:
            conn.execute(
                """
                INSERT INTO ai_spend_policies
                (id, tenant_id, scope_type, scope_id, period_start, period_end,
                 budget_microusd, max_per_call_microusd, max_input_tokens,
                 max_output_tokens, max_calls_per_minute, anomaly_multiplier,
                 allowed_routes_json, fallback_routes_json, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    policy_id, tenant_id.strip(), scope_type.strip(), scope_id.strip(),
                    start, end, budget_microusd, max_per_call_microusd,
                    max_input_tokens, max_output_tokens, max_calls_per_minute,
                    anomaly_multiplier, canonical_json(allowed), canonical_json(fallbacks),
                    created, created,
                ),
            )
            self.db.append_audit_event(
                conn,
                event_id=f"evt_{uuid4().hex}",
                aggregate_type="ai_spend_policy",
                aggregate_id=policy_id,
                event_type="ai_spend_policy.created",
                payload={
                    "tenant_id": tenant_id.strip(),
                    "scope_type": scope_type.strip(),
                    "scope_id": scope_id.strip(),
                    "budget_microusd": budget_microusd,
                    "allowed_routes": allowed,
                },
            )
        return self.get_policy(policy_id)

    @staticmethod
    def _normalize_routes(routes: Iterable[dict[str, str]]) -> list[dict[str, str]]:
        result: list[dict[str, str]] = []
        seen: set[str] = set()
        for route in routes:
            provider = str(route.get("provider", "")).strip().lower()
            model = str(route.get("model", "")).strip()
            if not provider or not model:
                raise ValueError("each route requires provider and model")
            key = _route_key(provider, model)
            if key not in seen:
                result.append({"provider": provider, "model": model})
                seen.add(key)
        return result

    def get_policy(self, policy_id: str) -> dict[str, Any]:
        conn = self.db.connect()
        try:
            row = conn.execute("SELECT * FROM ai_spend_policies WHERE id=?", (policy_id,)).fetchone()
        finally:
            conn.close()
        if not row:
            raise AuthorizationDenied("AI spend policy not found")
        result = _row(row)
        result["allowed_routes"] = json.loads(result.pop("allowed_routes_json"))
        result["fallback_routes"] = json.loads(result.pop("fallback_routes_json"))
        return result

    def emergency_stop(self, policy_id: str, *, reason: str) -> dict[str, Any]:
        if not reason.strip():
            raise ValueError("reason is required")
        now = iso_now()
        with self.db.transaction(immediate=True) as conn:
            row = conn.execute("SELECT id FROM ai_spend_policies WHERE id=?", (policy_id,)).fetchone()
            if not row:
                raise AuthorizationDenied("AI spend policy not found")
            conn.execute(
                "UPDATE ai_spend_policies SET status='paused', stop_reason=?, version=version+1, updated_at=? WHERE id=?",
                (reason.strip(), now, policy_id),
            )
            self._alert(
                conn, policy_id=policy_id, reservation_id=None, severity="critical",
                alert_type="emergency_stop", message="AI spend policy was stopped",
                details={"reason": reason.strip()},
            )
            self.db.append_audit_event(
                conn,
                event_id=f"evt_{uuid4().hex}",
                aggregate_type="ai_spend_policy",
                aggregate_id=policy_id,
                event_type="ai_spend_policy.paused",
                payload={"reason": reason.strip()},
            )
        return self.get_policy(policy_id)
