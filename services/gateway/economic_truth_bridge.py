"""Durable Archisynapse -> SLA113 Economic Truth outbox."""
from __future__ import annotations

import asyncio
import json
import os
from typing import Any
from uuid import uuid4

import httpx

from royalty_db import get_pool


class EconomicTruthConfigurationError(RuntimeError):
    pass


def enforcement_enabled() -> bool:
    return os.getenv("ECONOMIC_TRUTH_ENFORCEMENT", "false").lower() == "true"


def require_action_authority(metadata: dict[str, Any]) -> str:
    action_id = str(metadata.get("economic_truth_action_id") or "").strip()
    authorization_receipt_id = str(metadata.get("economic_truth_authorization_receipt_id") or "").strip()
    if enforcement_enabled() and (not action_id or not authorization_receipt_id):
        raise EconomicTruthConfigurationError(
            "Economic execution refused: economic_truth_action_id and authorization receipt are required"
        )
    return action_id


async def enqueue_event(
    *,
    source: str,
    external_id: str,
    action_id: str,
    stage: str,
    payload: dict[str, Any],
) -> dict[str, Any]:
    if not action_id:
        return {"queued": False, "reason": "no_authorized_action"}
    pool = get_pool()
    row = await pool.fetchrow(
        """
        INSERT INTO economic_truth_outbox
          (id, source, external_id, action_id, stage, payload)
        VALUES ($1,$2,$3,$4,$5,$6::jsonb)
        ON CONFLICT (source, external_id, stage)
        DO UPDATE SET updated_at=now()
        RETURNING *
        """,
        uuid4(), source, external_id, action_id, stage, json.dumps(payload, default=str),
    )
    return dict(row)


def _payload(value: Any) -> dict[str, Any]:
    if isinstance(value, str):
        return json.loads(value)
    return dict(value or {})


async def deliver_event(row: dict[str, Any], transport=None) -> dict[str, Any]:
    api_url = os.getenv("ECONOMIC_TRUTH_API_URL", "").rstrip("/")
    api_key = os.getenv("ECONOMIC_TRUTH_INGEST_KEY", "")
    if not api_url or not api_key:
        raise EconomicTruthConfigurationError("Economic Truth endpoint and ingest key are required")
    path = {
        "outcome": f"/api/economic-truth/actions/{row['action_id']}/outcome",
        "verify": f"/api/economic-truth/actions/{row['action_id']}/verify",
        "reverse": f"/api/economic-truth/actions/{row['action_id']}/reverse",
    }[row["stage"]]
    pool = get_pool()
    payload = _payload(row["payload"])
    await pool.execute(
        "UPDATE economic_truth_outbox SET state='sending', attempts=attempts+1, updated_at=now() WHERE id=$1",
        row["id"],
    )
    try:
        if transport is None:
            async with httpx.AsyncClient(timeout=float(os.getenv("ECONOMIC_TRUTH_TIMEOUT_SECONDS", "8"))) as client:
                response = await client.post(
                    f"{api_url}{path}",
                    headers={"x-economic-truth-key": api_key},
                    json=payload,
                )
        else:
            response = await transport(f"{api_url}{path}", payload)
        if response.status_code >= 400:
            raise RuntimeError(f"Economic Truth HTTP {response.status_code}: {response.text[:300]}")
        body = response.json()
        for surface_id in payload.get("coverage_surfaces", []):
            heartbeat_path = f"{api_url}/api/economic-truth/coverage/heartbeat"
            heartbeat_body = {"surface_id": surface_id, "healthy": True, "detail": {"action_id": row["action_id"], "external_id": row["external_id"]}}
            if transport is None:
                heartbeat_response = await client.post(heartbeat_path, headers={"x-economic-truth-key": api_key}, json=heartbeat_body)
            else:
                heartbeat_response = await transport(heartbeat_path, heartbeat_body)
            if heartbeat_response.status_code >= 400:
                raise RuntimeError(f"Economic Truth heartbeat HTTP {heartbeat_response.status_code}")
        await pool.execute(
            """UPDATE economic_truth_outbox
               SET state='delivered', response=$2::jsonb, last_error=NULL, updated_at=now()
               WHERE id=$1""",
            row["id"], json.dumps(body, default=str),
        )
        return {"delivered": True, "response": body}
    except Exception as exc:
        await pool.execute(
            """UPDATE economic_truth_outbox
               SET state='pending', last_error=$2,
                   next_attempt_at=now() + make_interval(secs => LEAST(3600, GREATEST(30, attempts*30))),
                   updated_at=now()
               WHERE id=$1""",
            row["id"], str(exc)[:500],
        )
        return {"delivered": False, "error": str(exc)}


async def queue_and_deliver(**kwargs) -> dict[str, Any]:
    row = await enqueue_event(**kwargs)
    if not row.get("id"):
        return row
    return await deliver_event(row)


async def flush_outbox(limit: int = 100) -> dict[str, Any]:
    pool = get_pool()
    rows = await pool.fetch(
        """SELECT * FROM economic_truth_outbox
           WHERE state IN ('pending','sending') AND next_attempt_at <= now()
           ORDER BY created_at LIMIT $1 FOR UPDATE SKIP LOCKED""",
        limit,
    )
    results = [await deliver_event(dict(row)) for row in rows]
    return {"processed": len(results), "delivered": sum(1 for r in results if r.get("delivered")), "results": results}
