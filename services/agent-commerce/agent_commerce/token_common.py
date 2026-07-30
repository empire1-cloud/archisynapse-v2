from __future__ import annotations

import hashlib
import json
import math
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable
from uuid import uuid4

from .errors import (
    AuthorizationDenied,
    BudgetExceeded,
    IdempotencyConflict,
    ModelRouteDenied,
    PolicyPaused,
    RateCardNotFound,
    ReceiptIntegrityError,
    UsageReconciliationError,
)
from .models import iso_now
from .receipts import ReceiptSigner, canonical_json
from .storage import Database

TOKEN_SCHEMA = """
CREATE TABLE IF NOT EXISTS ai_rate_cards (
    id TEXT PRIMARY KEY,
    provider TEXT NOT NULL,
    model TEXT NOT NULL,
    input_microusd_per_million INTEGER NOT NULL CHECK(input_microusd_per_million >= 0),
    output_microusd_per_million INTEGER NOT NULL CHECK(output_microusd_per_million >= 0),
    cached_input_microusd_per_million INTEGER NOT NULL DEFAULT 0 CHECK(cached_input_microusd_per_million >= 0),
    reasoning_microusd_per_million INTEGER NOT NULL DEFAULT 0 CHECK(reasoning_microusd_per_million >= 0),
    source_reference TEXT NOT NULL,
    effective_at TEXT NOT NULL,
    active INTEGER NOT NULL DEFAULT 1 CHECK(active IN (0,1)),
    created_at TEXT NOT NULL,
    UNIQUE(provider, model, effective_at)
);
CREATE INDEX IF NOT EXISTS idx_ai_rate_cards_lookup
    ON ai_rate_cards(provider, model, active, effective_at DESC);

CREATE TABLE IF NOT EXISTS ai_spend_policies (
    id TEXT PRIMARY KEY,
    tenant_id TEXT NOT NULL,
    scope_type TEXT NOT NULL,
    scope_id TEXT NOT NULL,
    period_start TEXT NOT NULL,
    period_end TEXT NOT NULL,
    budget_microusd INTEGER NOT NULL CHECK(budget_microusd > 0),
    max_per_call_microusd INTEGER NOT NULL CHECK(max_per_call_microusd > 0),
    max_input_tokens INTEGER NOT NULL CHECK(max_input_tokens > 0),
    max_output_tokens INTEGER NOT NULL CHECK(max_output_tokens > 0),
    max_calls_per_minute INTEGER NOT NULL DEFAULT 60 CHECK(max_calls_per_minute > 0),
    anomaly_multiplier REAL NOT NULL DEFAULT 3.0 CHECK(anomaly_multiplier >= 1.0),
    allowed_routes_json TEXT NOT NULL,
    fallback_routes_json TEXT NOT NULL DEFAULT '[]',
    reserved_microusd INTEGER NOT NULL DEFAULT 0,
    spent_microusd INTEGER NOT NULL DEFAULT 0,
    call_count INTEGER NOT NULL DEFAULT 0,
    status TEXT NOT NULL DEFAULT 'active',
    stop_reason TEXT,
    version INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(tenant_id, scope_type, scope_id, period_start)
);
CREATE INDEX IF NOT EXISTS idx_ai_policy_scope
    ON ai_spend_policies(tenant_id, scope_type, scope_id, status);

CREATE TABLE IF NOT EXISTS ai_spend_reservations (
    id TEXT PRIMARY KEY,
    policy_id TEXT NOT NULL REFERENCES ai_spend_policies(id),
    idempotency_key TEXT NOT NULL,
    request_fingerprint TEXT NOT NULL,
    requested_provider TEXT NOT NULL,
    requested_model TEXT NOT NULL,
    selected_provider TEXT NOT NULL,
    selected_model TEXT NOT NULL,
    rate_card_id TEXT NOT NULL REFERENCES ai_rate_cards(id),
    estimated_input_tokens INTEGER NOT NULL,
    estimated_output_tokens INTEGER NOT NULL,
    estimated_cached_input_tokens INTEGER NOT NULL,
    estimated_reasoning_tokens INTEGER NOT NULL,
    estimated_cost_microusd INTEGER NOT NULL,
    reserved_microusd INTEGER NOT NULL,
    actual_input_tokens INTEGER,
    actual_output_tokens INTEGER,
    actual_cached_input_tokens INTEGER,
    actual_reasoning_tokens INTEGER,
    actual_cost_microusd INTEGER,
    provider_reported_cost_microusd INTEGER,
    provider_request_id TEXT,
    response_sha256 TEXT,
    outcome_status TEXT,
    reconciliation_status TEXT NOT NULL DEFAULT 'pending',
    status TEXT NOT NULL DEFAULT 'reserved',
    receipt_id TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(policy_id, idempotency_key)
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_ai_provider_request_unique
    ON ai_spend_reservations(selected_provider, provider_request_id)
    WHERE provider_request_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_ai_reservation_status
    ON ai_spend_reservations(status, created_at);

CREATE TABLE IF NOT EXISTS ai_usage_receipts (
    id TEXT PRIMARY KEY,
    reservation_id TEXT NOT NULL UNIQUE REFERENCES ai_spend_reservations(id),
    policy_id TEXT NOT NULL REFERENCES ai_spend_policies(id),
    payload_json TEXT NOT NULL,
    receipt_sha256 TEXT NOT NULL UNIQUE,
    signature TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS ai_provider_usage_events (
    id TEXT PRIMARY KEY,
    provider TEXT NOT NULL,
    provider_event_id TEXT NOT NULL,
    provider_request_id TEXT,
    cost_microusd INTEGER NOT NULL CHECK(cost_microusd >= 0),
    input_tokens INTEGER,
    output_tokens INTEGER,
    cached_input_tokens INTEGER,
    reasoning_tokens INTEGER,
    reservation_id TEXT REFERENCES ai_spend_reservations(id),
    status TEXT NOT NULL,
    payload_sha256 TEXT NOT NULL,
    received_at TEXT NOT NULL,
    UNIQUE(provider, provider_event_id)
);

CREATE TABLE IF NOT EXISTS ai_spend_alerts (
    id TEXT PRIMARY KEY,
    policy_id TEXT,
    reservation_id TEXT,
    severity TEXT NOT NULL,
    alert_type TEXT NOT NULL,
    message TEXT NOT NULL,
    details_json TEXT NOT NULL,
    created_at TEXT NOT NULL
);
"""


def _route_key(provider: str, model: str) -> str:
    return f"{provider.strip().lower()}:{model.strip()}"


def _parse_time(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _fingerprint(payload: dict[str, Any]) -> str:
    return hashlib.sha256(canonical_json(payload).encode()).hexdigest()


def _row(row: Any) -> dict[str, Any]:
    return dict(row) if row is not None else {}
