from __future__ import annotations

import hashlib
import json
import sqlite3
import threading
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

from .models import iso_now


SCHEMA = """
PRAGMA journal_mode=WAL;
PRAGMA foreign_keys=ON;
PRAGMA busy_timeout=5000;

CREATE TABLE IF NOT EXISTS authorizations (
    id TEXT PRIMARY KEY,
    tenant_id TEXT NOT NULL,
    orchestrator_id TEXT NOT NULL,
    max_total_sats INTEGER NOT NULL CHECK(max_total_sats > 0),
    max_per_call_sats INTEGER NOT NULL CHECK(max_per_call_sats > 0),
    max_route_fee_sats INTEGER NOT NULL DEFAULT 0 CHECK(max_route_fee_sats >= 0),
    max_calls INTEGER NOT NULL CHECK(max_calls > 0),
    allowed_agent_npubs_json TEXT NOT NULL DEFAULT '[]',
    allowed_specialties_json TEXT NOT NULL DEFAULT '[]',
    expires_at TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'active',
    reserved_sats INTEGER NOT NULL DEFAULT 0,
    spent_sats INTEGER NOT NULL DEFAULT 0,
    call_count INTEGER NOT NULL DEFAULT 0,
    version INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS reservations (
    id TEXT PRIMARY KEY,
    authorization_id TEXT NOT NULL REFERENCES authorizations(id),
    idempotency_key TEXT NOT NULL,
    orchestration_id TEXT NOT NULL,
    agent_npub TEXT NOT NULL,
    specialty TEXT NOT NULL,
    quoted_sats INTEGER NOT NULL CHECK(quoted_sats > 0),
    reserved_sats INTEGER NOT NULL CHECK(reserved_sats > 0),
    invoice_sats INTEGER,
    payment_hash TEXT,
    invoice_json TEXT,
    provider TEXT,
    provider_payment_id TEXT,
    route_fee_sats INTEGER,
    preimage_hash TEXT,
    settled_at TEXT,
    settlement_evidence_json TEXT,
    status TEXT NOT NULL,
    receipt_id TEXT,
    failure_code TEXT,
    failure_message TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(authorization_id, idempotency_key)
);
CREATE INDEX IF NOT EXISTS idx_reservations_status ON reservations(status);
CREATE INDEX IF NOT EXISTS idx_reservations_payment_hash ON reservations(payment_hash);

CREATE TABLE IF NOT EXISTS receipts (
    id TEXT PRIMARY KEY,
    reservation_id TEXT NOT NULL UNIQUE REFERENCES reservations(id),
    authorization_id TEXT NOT NULL REFERENCES authorizations(id),
    agent_npub TEXT NOT NULL,
    total_debit_sats INTEGER NOT NULL,
    delivery_status TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    receipt_sha256 TEXT NOT NULL UNIQUE,
    signature TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS reputation_outcomes (
    receipt_id TEXT PRIMARY KEY REFERENCES receipts(id),
    agent_npub TEXT NOT NULL,
    validator_id TEXT NOT NULL,
    success INTEGER NOT NULL CHECK(success IN (0,1)),
    quality_score REAL NOT NULL CHECK(quality_score >= 0 AND quality_score <= 1),
    latency_ms INTEGER NOT NULL CHECK(latency_ms >= 0),
    evidence_sha256 TEXT NOT NULL,
    verified_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS agent_reputation (
    agent_npub TEXT PRIMARY KEY,
    verified_jobs INTEGER NOT NULL DEFAULT 0,
    successful_jobs INTEGER NOT NULL DEFAULT 0,
    failed_jobs INTEGER NOT NULL DEFAULT 0,
    total_paid_sats INTEGER NOT NULL DEFAULT 0,
    latency_sum_ms INTEGER NOT NULL DEFAULT 0,
    quality_sum REAL NOT NULL DEFAULT 0,
    score REAL NOT NULL DEFAULT 50,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS service_contracts (
    id TEXT PRIMARY KEY,
    service_id TEXT NOT NULL,
    service_version TEXT NOT NULL,
    tenant_id TEXT NOT NULL,
    provider_agent_npub TEXT NOT NULL,
    specialty TEXT NOT NULL,
    endpoint TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'active',
    settlement_policy TEXT NOT NULL,
    max_price_sats INTEGER NOT NULL CHECK(max_price_sats >= 0),
    response_deadline_ms INTEGER NOT NULL CHECK(response_deadline_ms > 0),
    delivery_deadline_ms INTEGER NOT NULL CHECK(delivery_deadline_ms > 0),
    availability_target_bps INTEGER NOT NULL CHECK(availability_target_bps BETWEEN 0 AND 10000),
    min_quality_score REAL NOT NULL CHECK(min_quality_score >= 0 AND min_quality_score <= 1),
    max_response_bytes INTEGER NOT NULL CHECK(max_response_bytes > 0),
    max_retries INTEGER NOT NULL DEFAULT 0 CHECK(max_retries >= 0),
    validator_required INTEGER NOT NULL DEFAULT 1 CHECK(validator_required IN (0,1)),
    provider_self_verify_allowed INTEGER NOT NULL DEFAULT 0 CHECK(provider_self_verify_allowed IN (0,1)),
    refund_on_failed_delivery INTEGER NOT NULL DEFAULT 1 CHECK(refund_on_failed_delivery IN (0,1)),
    required_deliverables_json TEXT NOT NULL DEFAULT '[]',
    required_evidence_json TEXT NOT NULL DEFAULT '[]',
    expires_at TEXT,
    version INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(tenant_id, service_id, service_version, provider_agent_npub)
);
CREATE INDEX IF NOT EXISTS idx_service_contracts_tenant_status
ON service_contracts(tenant_id, status);

CREATE TABLE IF NOT EXISTS fable_receipts (
    id TEXT PRIMARY KEY,
    receipt_type TEXT NOT NULL,
    contract_id TEXT REFERENCES service_contracts(id),
    authorization_id TEXT,
    execution_id TEXT,
    payment_receipt_id TEXT,
    payload_json TEXT NOT NULL,
    receipt_sha256 TEXT NOT NULL UNIQUE,
    signature TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_fable_receipts_contract
ON fable_receipts(contract_id, receipt_type);

CREATE TABLE IF NOT EXISTS audit_events (
    sequence INTEGER PRIMARY KEY AUTOINCREMENT,
    event_id TEXT NOT NULL UNIQUE,
    aggregate_type TEXT NOT NULL,
    aggregate_id TEXT NOT NULL,
    event_type TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    previous_hash TEXT,
    event_hash TEXT NOT NULL UNIQUE,
    created_at TEXT NOT NULL
);
"""


class Database:
    def __init__(self, path: str | Path):
        self.path = str(path)
        self._init_lock = threading.Lock()
        self._initialized = False
        self.initialize()

    def connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path, timeout=5, isolation_level=None, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("PRAGMA busy_timeout=5000")
        return conn

    def initialize(self) -> None:
        with self._init_lock:
            if self._initialized:
                return
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
            conn = self.connect()
            try:
                conn.executescript(SCHEMA)
            finally:
                conn.close()
            self._initialized = True

    @contextmanager
    def transaction(self, *, immediate: bool = False) -> Iterator[sqlite3.Connection]:
        conn = self.connect()
        try:
            conn.execute("BEGIN IMMEDIATE" if immediate else "BEGIN")
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def append_audit_event(
        self,
        conn: sqlite3.Connection,
        *,
        event_id: str,
        aggregate_type: str,
        aggregate_id: str,
        event_type: str,
        payload: dict[str, Any],
    ) -> str:
        payload_json = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
        row = conn.execute("SELECT event_hash FROM audit_events ORDER BY sequence DESC LIMIT 1").fetchone()
        previous_hash = row["event_hash"] if row else None
        created_at = iso_now()
        material = "|".join(
            [previous_hash or "", event_id, aggregate_type, aggregate_id, event_type, payload_json, created_at]
        )
        event_hash = hashlib.sha256(material.encode()).hexdigest()
        conn.execute(
            """
            INSERT INTO audit_events
            (event_id, aggregate_type, aggregate_id, event_type, payload_json, previous_hash, event_hash, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (event_id, aggregate_type, aggregate_id, event_type, payload_json, previous_hash, event_hash, created_at),
        )
        return event_hash

    def verify_audit_chain(self) -> bool:
        conn = self.connect()
        try:
            rows = conn.execute("SELECT * FROM audit_events ORDER BY sequence ASC").fetchall()
        finally:
            conn.close()
        previous_hash = None
        for row in rows:
            material = "|".join(
                [
                    previous_hash or "",
                    row["event_id"],
                    row["aggregate_type"],
                    row["aggregate_id"],
                    row["event_type"],
                    row["payload_json"],
                    row["created_at"],
                ]
            )
            expected = hashlib.sha256(material.encode()).hexdigest()
            if row["previous_hash"] != previous_hash or row["event_hash"] != expected:
                return False
            previous_hash = expected
        return True
