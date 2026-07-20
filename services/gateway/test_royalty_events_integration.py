"""
Live integration check for POST /api/v1/events against a running gateway
+ mock ledger — no in-process mocking of the HTTP boundary. Exercises
AT-01 (happy path), AT-02 (idempotent retry), and AT-03 (idempotency
conflict) from spec/ACCEPTANCE-royalty-loop-v1.md.

Run: python3 test_royalty_events_integration.py
(spawns its own gateway + mock_ledger subprocesses on throwaway ports)
"""

import asyncio
import json
import os
import subprocess
import sys
import time
from pathlib import Path

import httpx

sys.path.insert(0, os.path.dirname(__file__))
from royalty_keys import generate_tenant_keypair, sign_with_private_key  # noqa: E402

HERE = Path(__file__).resolve().parent
LEDGER_DIR = HERE.parent / "ledger"
GATEWAY_PORT = 9199
LEDGER_PORT = 3199
GATEWAY_URL = f"http://127.0.0.1:{GATEWAY_PORT}"
LEDGER_URL = f"http://127.0.0.1:{LEDGER_PORT}"


def make_event(idempotency_key: str, amount_value: str = "1.2500", splits=None):
    return {
        "schema_version": "1.0",
        "event_id": f"evt_{idempotency_key}",
        "event_type": "royalty.obligation.created",
        "occurred_at": _now_iso(),
        "correlation_id": f"corr_{idempotency_key}",
        "idempotency_key": idempotency_key,
        "tenant_id": "lyrica",
        "track": {
            "track_id": "trk_9f3a2b1c",
            "dna_tag": "dna_v2_7c1e",
            "soulprint_hash": "sp_sha256_4b09",
            "vics_proof": {
                "proof_id": "vics_01H",
                "issued_at": _now_iso(),
                "chain_ref": "vics://empire1/lyrica/trk_9f3a2b1c",
            },
        },
        "creator": {"creator_id": "cre_a1b2c3", "identity_ref": "sla113://identity/cre_a1b2c3"},
        "splits": splits or [{"owner_id": "cre_a1b2c3", "bps": 10000}],
        "trigger": {"kind": "remix", "source_ref": "lyrica://remix/rmx_7788", "actor_id": "usr_listener_42"},
        "amount": {"currency": "USD", "value": amount_value},
    }


def _now_iso():
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).isoformat()


async def post_event(client: httpx.AsyncClient, event: dict, private_key_b64: str, key_id: str):
    body_bytes = json.dumps(event).encode("utf-8")
    signature = sign_with_private_key(private_key_b64, body_bytes)
    headers = {
        "Content-Type": "application/json",
        "Authorization": "Bearer lyrica-tenant-token",
        "X-Empire1-Signature": signature,
        "X-Empire1-Key-Id": key_id,
        "X-Correlation-Id": event["correlation_id"],
        "Idempotency-Key": event["idempotency_key"],
    }
    return await client.post(f"{GATEWAY_URL}/api/v1/events", content=body_bytes, headers=headers)


def _reset_royalty_runtime_state():
    """Scratch state only (idempotency/keys/receipts) — safe to wipe between runs."""
    import shutil

    runtime_dir = HERE / ".runtime"
    for name in ("royalty_idempotency.json", "royalty_tenant_keys.json", "royalty_tenant_api_keys.json", "royalty_rejections.json"):
        path = runtime_dir / name
        if path.exists():
            path.unlink()
    receipts_dir = runtime_dir / "royalty_receipts"
    if receipts_dir.exists():
        shutil.rmtree(receipts_dir)


async def main():
    env = {**os.environ, "LEDGER_SERVICE_URL": LEDGER_URL}

    _reset_royalty_runtime_state()

    ledger_db = Path("/tmp/ledger.db")
    if ledger_db.exists():
        ledger_db.unlink()

    ledger_proc = subprocess.Popen(
        [sys.executable, "-c", f"import uvicorn; uvicorn.run('mock_ledger:app', host='127.0.0.1', port={LEDGER_PORT}, log_level='warning')"],
        cwd=str(LEDGER_DIR),
        env=env,
    )
    gateway_proc = subprocess.Popen(
        [sys.executable, "-c", f"import uvicorn; uvicorn.run('main:app', host='127.0.0.1', port={GATEWAY_PORT}, log_level='warning')"],
        cwd=str(HERE),
        env=env,
    )

    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            await _wait_healthy(client, f"{LEDGER_URL}/health")
            await _wait_healthy(client, f"{GATEWAY_URL}/health")

            private_key_b64, public_key_b64 = generate_tenant_keypair()
            await client.post(
                f"{GATEWAY_URL}/admin/tenants/lyrica/api-key", json={"api_key": "lyrica-tenant-token"}
            )
            await client.post(
                f"{GATEWAY_URL}/admin/tenants/lyrica/keys",
                json={"key_id": "lyr-k1", "public_key_b64": public_key_b64},
            )

            # AT-01: happy path, single owner
            event = make_event("test-at01-001")
            resp = await post_event(client, event, private_key_b64, "lyr-k1")
            assert resp.status_code == 201, f"AT-01 expected 201, got {resp.status_code}: {resp.text}"
            receipt = resp.json()
            assert receipt["status"] in ("processing", "paid"), receipt
            assert receipt["amounts"]["gross"] == "1.2500", receipt
            assert receipt["amounts"]["platform_fee"] == "0.0000", receipt
            assert receipt["amounts"]["net"] == "1.2500", receipt
            assert receipt["payouts"] == [{"owner_id": "cre_a1b2c3", "amount": "1.2500", "state": "paid"}], receipt
            first_receipt_id = receipt["receipt_id"]
            first_ledger_txn_id = receipt["ledger_transaction_id"]
            print("AT-01 PASS —", receipt["amounts"], receipt["payouts"])

            # AT-02: idempotent retry, same key + same body, 3x
            for i in range(3):
                retry_event = make_event("test-at01-001")
                retry_event["occurred_at"] = event["occurred_at"]  # must match exactly for hash equality
                resp = await post_event(client, event, private_key_b64, "lyr-k1")
                assert resp.status_code == 200, f"AT-02 retry {i} expected 200, got {resp.status_code}: {resp.text}"
                retry_receipt = resp.json()
                assert retry_receipt["receipt_id"] == first_receipt_id, "AT-02: receipt_id changed on retry"
                assert retry_receipt["ledger_transaction_id"] == first_ledger_txn_id, "AT-02: ledger txn changed on retry"
            print("AT-02 PASS — 3/3 retries returned identical receipt_id + ledger_transaction_id")

            # AT-03: same idempotency_key, different amount -> conflict
            conflicting_event = make_event("test-at01-001", amount_value="2.0000")
            resp = await post_event(client, conflicting_event, private_key_b64, "lyr-k1")
            assert resp.status_code == 409, f"AT-03 expected 409, got {resp.status_code}: {resp.text}"
            assert resp.json()["detail"]["code"] == "idempotency_conflict", resp.json()
            print("AT-03 PASS — conflicting payload rejected with idempotency_conflict")

            # AT-04: 60/40 split with deterministic rounding
            split_event = make_event(
                "test-at04-001",
                splits=[{"owner_id": "cre_a1b2c3", "bps": 6000}, {"owner_id": "cre_d4e5f6", "bps": 4000}],
            )
            resp = await post_event(client, split_event, private_key_b64, "lyr-k1")
            assert resp.status_code == 201, f"AT-04 expected 201, got {resp.status_code}: {resp.text}"
            split_receipt = resp.json()
            payouts_by_owner = {p["owner_id"]: p["amount"] for p in split_receipt["payouts"]}
            assert payouts_by_owner == {"cre_a1b2c3": "0.7500", "cre_d4e5f6": "0.5000"}, split_receipt
            print("AT-04 PASS —", payouts_by_owner)

            # Tampered signature -> 401 (AT-05 smoke check)
            tampered_event = make_event("test-at05-001")
            body_bytes = json.dumps(tampered_event).encode("utf-8")
            bad_signature = sign_with_private_key(private_key_b64, b"not the real body")
            resp = await client.post(
                f"{GATEWAY_URL}/api/v1/events",
                content=body_bytes,
                headers={
                    "Content-Type": "application/json",
                    "Authorization": "Bearer lyrica-tenant-token",
                    "X-Empire1-Signature": bad_signature,
                    "X-Empire1-Key-Id": "lyr-k1",
                },
            )
            assert resp.status_code == 401, f"AT-05 expected 401, got {resp.status_code}: {resp.text}"
            print("AT-05 PASS — tampered signature rejected")

        print("\nALL ROYALTY INTEGRATION CHECKS PASSED")
    finally:
        gateway_proc.terminate()
        ledger_proc.terminate()
        gateway_proc.wait(timeout=5)
        ledger_proc.wait(timeout=5)


async def _wait_healthy(client: httpx.AsyncClient, url: str, timeout: float = 15.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            resp = await client.get(url, timeout=2.0)
            if resp.status_code == 200:
                return
        except httpx.RequestError:
            pass
        await asyncio.sleep(0.3)
    raise RuntimeError(f"{url} did not become healthy in time")


if __name__ == "__main__":
    asyncio.run(main())
