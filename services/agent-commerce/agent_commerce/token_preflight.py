from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from typing import Any
from uuid import uuid4

from .errors import (
    AuthorizationDenied,
    BudgetExceeded,
    IdempotencyConflict,
    ModelRouteDenied,
    PolicyPaused,
    RateCardNotFound,
)
from .models import iso_now
from .token_common import _fingerprint, _parse_time, _route_key, _row


class PreflightMixin:
    def preflight(
        self,
        *,
        policy_id: str,
        idempotency_key: str,
        provider: str,
        model: str,
        estimated_input_tokens: int,
        estimated_output_tokens: int,
        estimated_cached_input_tokens: int = 0,
        estimated_reasoning_tokens: int = 0,
    ) -> dict[str, Any]:
        estimate_payload = {
            "provider": provider.strip().lower(),
            "model": model.strip(),
            "estimated_input_tokens": estimated_input_tokens,
            "estimated_output_tokens": estimated_output_tokens,
            "estimated_cached_input_tokens": estimated_cached_input_tokens,
            "estimated_reasoning_tokens": estimated_reasoning_tokens,
        }
        request_fingerprint = _fingerprint(estimate_payload)
        now = iso_now()
        with self.db.transaction(immediate=True) as conn:
            existing = conn.execute(
                "SELECT * FROM ai_spend_reservations WHERE policy_id=? AND idempotency_key=?",
                (policy_id, idempotency_key),
            ).fetchone()
            if existing:
                if existing["request_fingerprint"] != request_fingerprint:
                    raise IdempotencyConflict("idempotency key was already used for a different AI request")
                return self._reservation_dict(existing)

            policy = conn.execute("SELECT * FROM ai_spend_policies WHERE id=?", (policy_id,)).fetchone()
            if not policy:
                raise AuthorizationDenied("AI spend policy not found")
            self._validate_policy_for_preflight(policy, now)
            if estimated_input_tokens > policy["max_input_tokens"]:
                raise AuthorizationDenied("estimated input tokens exceed policy maximum")
            if estimated_output_tokens > policy["max_output_tokens"]:
                raise AuthorizationDenied("estimated output tokens exceed policy maximum")
            if min(
                estimated_input_tokens,
                estimated_output_tokens,
                estimated_cached_input_tokens,
                estimated_reasoning_tokens,
            ) < 0:
                raise ValueError("estimated token counts cannot be negative")

            minute_ago = (datetime.now(timezone.utc) - timedelta(minutes=1)).isoformat().replace("+00:00", "Z")
            recent = conn.execute(
                "SELECT COUNT(*) AS n FROM ai_spend_reservations WHERE policy_id=? AND created_at>=?",
                (policy_id, minute_ago),
            ).fetchone()["n"]
            if recent >= policy["max_calls_per_minute"]:
                self._pause_policy(
                    conn,
                    policy_id=policy_id,
                    reason="runaway_call_rate",
                    reservation_id=None,
                    details={"recent_calls": recent, "limit": policy["max_calls_per_minute"]},
                )
                raise PolicyPaused("AI spend policy paused after runaway call-rate detection")

            allowed = json.loads(policy["allowed_routes_json"])
            fallback = json.loads(policy["fallback_routes_json"])
            requested = {"provider": provider.strip().lower(), "model": model.strip()}
            candidates = [requested] + [r for r in fallback if r != requested]
            allowed_keys = {_route_key(r["provider"], r["model"]) for r in allowed}
            available = policy["budget_microusd"] - policy["spent_microusd"] - policy["reserved_microusd"]
            selected: tuple[dict[str, str], dict[str, Any], int] | None = None
            saw_allowed = False
            saw_rate = False
            for route in candidates:
                if _route_key(route["provider"], route["model"]) not in allowed_keys:
                    continue
                saw_allowed = True
                try:
                    rate = self._get_rate_card_conn(conn, route["provider"], route["model"], now)
                except RateCardNotFound:
                    continue
                saw_rate = True
                cost = self.calculate_cost(
                    rate,
                    input_tokens=estimated_input_tokens,
                    output_tokens=estimated_output_tokens,
                    cached_input_tokens=estimated_cached_input_tokens,
                    reasoning_tokens=estimated_reasoning_tokens,
                )
                if cost <= policy["max_per_call_microusd"] and cost <= available:
                    selected = (route, rate, cost)
                    break
            if not saw_allowed:
                raise ModelRouteDenied("requested model and configured fallbacks are not allowed")
            if not saw_rate:
                raise RateCardNotFound("no active rate card exists for any permitted route")
            if selected is None:
                raise BudgetExceeded("no permitted model route fits the per-call and remaining budget")

            route, rate, cost = selected
            reservation_id = f"aires_{uuid4().hex}"
            conn.execute(
                """
                INSERT INTO ai_spend_reservations
                (id, policy_id, idempotency_key, request_fingerprint,
                 requested_provider, requested_model, selected_provider, selected_model,
                 rate_card_id, estimated_input_tokens, estimated_output_tokens,
                 estimated_cached_input_tokens, estimated_reasoning_tokens,
                 estimated_cost_microusd, reserved_microusd, status, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'reserved', ?, ?)
                """,
                (
                    reservation_id, policy_id, idempotency_key, request_fingerprint,
                    requested["provider"], requested["model"], route["provider"], route["model"],
                    rate["id"], estimated_input_tokens, estimated_output_tokens,
                    estimated_cached_input_tokens, estimated_reasoning_tokens, cost, cost, now, now,
                ),
            )
            conn.execute(
                "UPDATE ai_spend_policies SET reserved_microusd=reserved_microusd+?, updated_at=? WHERE id=?",
                (cost, now, policy_id),
            )
            self.db.append_audit_event(
                conn,
                event_id=f"evt_{uuid4().hex}",
                aggregate_type="ai_spend_reservation",
                aggregate_id=reservation_id,
                event_type="ai_spend.preflight_allowed",
                payload={
                    "policy_id": policy_id,
                    "requested_route": requested,
                    "selected_route": route,
                    "estimated_cost_microusd": cost,
                    "fallback_used": route != requested,
                },
            )
            row = conn.execute("SELECT * FROM ai_spend_reservations WHERE id=?", (reservation_id,)).fetchone()
            return self._reservation_dict(row)

    def _validate_policy_for_preflight(self, policy: Any, now: str) -> None:
        status = policy["status"]
        if status == "paused":
            raise PolicyPaused(policy["stop_reason"] or "AI spend policy is paused")
        if status != "active":
            raise AuthorizationDenied(f"AI spend policy is {status}")
        now_dt = _parse_time(now)
        if now_dt < _parse_time(policy["period_start"]) or now_dt >= _parse_time(policy["period_end"]):
            raise AuthorizationDenied("AI spend policy is outside its active period")
        if policy["spent_microusd"] >= policy["budget_microusd"]:
            raise BudgetExceeded("AI spend policy budget is exhausted")

    def _get_rate_card_conn(self, conn: Any, provider: str, model: str, at: str) -> dict[str, Any]:
        row = conn.execute(
            """
            SELECT * FROM ai_rate_cards
            WHERE provider=? AND model=? AND active=1 AND effective_at<=?
            ORDER BY effective_at DESC LIMIT 1
            """,
            (provider.strip().lower(), model.strip(), at),
        ).fetchone()
        if not row:
            raise RateCardNotFound(f"no active rate card for {provider}/{model}")
        return _row(row)
