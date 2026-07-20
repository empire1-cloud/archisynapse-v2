"""
True concurrency proof: N genuinely overlapping requests (asyncio.gather,
not sequential) with the SAME idempotency_key against the real
gateway/transaction/ledger stack. Verifies exactly one final financial
effect and that every losing request matches the documented
PROCESSING/replay contract -- never a duplicate obligation, never an
unhandled status.

Run: python3 test_royalty_concurrency.py
"""

import asyncio
import json
import os
import subprocess
import sys
import time
import uuid
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

import asyncpg
import httpx

sys.path.insert(0, os.path.dirname(__file__))
from royalty_keys import generate_tenant_keypair, sign_with_private_key  # noqa: E402

GATEWAY_DIR = Path(__file__).resolve().parent
REPO_ROOT = GATEWAY_DIR.parent.parent
LEDGER_DIR = REPO_ROOT / "services" / "ledger"
TRANSACTION_DIR = REPO_ROOT / "services" / "transaction"
FRAUD_DIR = REPO_ROOT / "services" / "fraud"
TSX = str(REPO_ROOT / "node_modules" / ".bin" / "tsx")

LEDGER_PORT = 3321
TRANSACTION_PORT = 3320
FRAUD_PORT = 8382
GATEWAY_PORT = 9320
DATABASE_URL = "postgresql://postgres:postgres@127.0.0.1:5432/archisynapse"
ADMIN_TOKEN = "concurrency-admin-token"
RUN_ID = uuid.uuid4().hex[:8]
TENANT_ID = f"lyrica-conc-{RUN_ID}"
N_CONCURRENT = 10


def start_services():
    env = {**os.environ, "DB_HOST": "127.0.0.1", "DB_PORT": "5432", "DB_NAME": "archisynapse",
           "DB_USER": "postgres", "DB_PASSWORD": "postgres", "LEDGER_SERVICE_URL": f"http://127.0.0.1:{LEDGER_PORT}"}
    ledger = subprocess.Popen([TSX, "ledger-service-index.ts"], cwd=str(LEDGER_DIR),
                              env={**env, "PORT": str(LEDGER_PORT)},
                              stdout=open("/tmp/conc-ledger.log", "w"), stderr=subprocess.STDOUT)
    transaction = subprocess.Popen([TSX, "transaction-service-index.ts"], cwd=str(TRANSACTION_DIR),
                                    env={**env, "PORT": str(TRANSACTION_PORT)},
                                    stdout=open("/tmp/conc-transaction.log", "w"), stderr=subprocess.STDOUT)
    fraud_env = {**os.environ, "ARCHISYNAPSE_DATABASE_URL": "postgresql+psycopg2://postgres:postgres@127.0.0.1:5432/archisynapse", "ARCHISYNAPSE_PEPPER": "conc-pepper"}
    fraud_python = str(FRAUD_DIR / ".venv" / "bin" / "python3")
    fraud = subprocess.Popen(
        [fraud_python, "-c", f"import uvicorn; uvicorn.run('archisynapse_fraud_mvp:app', host='127.0.0.1', port={FRAUD_PORT}, log_level='warning')"],
        cwd=str(FRAUD_DIR), env=fraud_env, stdout=open("/tmp/conc-fraud.log", "w"), stderr=subprocess.STDOUT,
    )
    gateway_env = {
        **os.environ, "DATABASE_URL": DATABASE_URL, "TRANSACTION_SERVICE_URL": f"http://127.0.0.1:{TRANSACTION_PORT}",
        "FRAUD_SERVICE_URL": f"http://127.0.0.1:{FRAUD_PORT}", "ROYALTY_LOOP_ENABLED": "true",
        "ROYALTY_ADMIN_TOKEN": ADMIN_TOKEN, "ROYALTY_TEST_FIXTURES_ENABLED": "true",
    }
    gateway = subprocess.Popen(
        [sys.executable, "-c", f"import uvicorn; uvicorn.run('main:app', host='127.0.0.1', port={GATEWAY_PORT}, log_level='warning')"],
        cwd=str(GATEWAY_DIR), env=gateway_env, stdout=open("/tmp/conc-gateway.log", "w"), stderr=subprocess.STDOUT,
    )
    return {"ledger": ledger, "transaction": transaction, "fraud": fraud, "gateway": gateway}


def stop(procs):
    for p in procs.values():
        p.terminate()
    for p in procs.values():
        try:
            p.wait(timeout=5)
        except subprocess.TimeoutExpired:
            p.kill()


async def wait_healthy(client, url, timeout=20.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            r = await client.get(url, timeout=2.0)
            if r.status_code == 200:
                return
        except httpx.RequestError:
            pass
        await asyncio.sleep(0.3)
    raise RuntimeError(f"{url} not healthy in time")


async def fire_one(client, event, priv, key_id):
    body = json.dumps(event).encode("utf-8")
    sig = sign_with_private_key(priv, body)
    return await client.post(
        f"http://127.0.0.1:{GATEWAY_PORT}/api/v1/events",
        content=body,
        headers={
            "Content-Type": "application/json", "Authorization": "Bearer conc-tenant-token",
            "X-Empire1-Signature": sig, "X-Empire1-Key-Id": key_id,
            "X-Correlation-Id": event["correlation_id"], "Idempotency-Key": event["idempotency_key"],
        },
    )


async def main():
    procs = start_services()
    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            await wait_healthy(client, f"http://127.0.0.1:{LEDGER_PORT}/health")
            await wait_healthy(client, f"http://127.0.0.1:{TRANSACTION_PORT}/health")
            await wait_healthy(client, f"http://127.0.0.1:{FRAUD_PORT}/health")
            await wait_healthy(client, f"http://127.0.0.1:{GATEWAY_PORT}/health")

            admin_headers = {"Authorization": f"Bearer {ADMIN_TOKEN}"}
            priv, pub = generate_tenant_keypair()
            await client.post(f"http://127.0.0.1:{GATEWAY_PORT}/admin/tenants/{TENANT_ID}/api-key",
                               json={"api_key": "conc-tenant-token"}, headers=admin_headers)
            await client.post(f"http://127.0.0.1:{GATEWAY_PORT}/admin/tenants/{TENANT_ID}/keys",
                               json={"key_id": "lyr-k1", "public_key_b64": pub}, headers=admin_headers)

            event = {
                "schema_version": "1.0", "event_id": f"evt_conc_{RUN_ID}", "event_type": "royalty.obligation.created",
                "occurred_at": datetime.now(timezone.utc).isoformat(), "correlation_id": f"corr_conc_{RUN_ID}",
                "idempotency_key": f"idem_conc_{RUN_ID}", "tenant_id": TENANT_ID,
                "track": {"track_id": "trk_1", "dna_tag": "dna1", "soulprint_hash": "sp1",
                          "vics_proof": {"proof_id": "vics_ok", "issued_at": datetime.now(timezone.utc).isoformat(), "chain_ref": "ref"}},
                "creator": {"creator_id": "cre_1", "identity_ref": "ref"},
                "splits": [{"owner_id": "cre_a1b2c3", "bps": 10000}],
                "trigger": {"kind": "remix", "source_ref": "ref", "actor_id": "usr_1"},
                "amount": {"currency": "USD", "value": "1.2500"},
            }

            # Genuinely overlapping: all N requests fired concurrently via
            # asyncio.gather, not a for-loop of sequential awaits.
            responses = await asyncio.gather(*[fire_one(client, event, priv, "lyr-k1") for _ in range(N_CONCURRENT)])

            status_counts = Counter(r.status_code for r in responses)
            print(f"Status code distribution across {N_CONCURRENT} concurrent requests: {dict(status_counts)}")

            bodies = []
            for r in responses:
                try:
                    bodies.append(r.json())
                except Exception:
                    bodies.append({"_raw": r.text, "_status": r.status_code})

            winners = [b for r, b in zip(responses, bodies) if r.status_code in (200, 201)]
            losers = [(r.status_code, b) for r, b in zip(responses, bodies) if r.status_code not in (200, 201)]

            assert winners, "no request succeeded at all -- concurrency should still let exactly one through"
            receipt_ids = {w["receipt_id"] for w in winners}
            assert len(receipt_ids) == 1, f"expected all successful responses to share one receipt_id, got {receipt_ids}"

            for status, body in losers:
                detail = body.get("detail", body)
                code = detail.get("code") if isinstance(detail, dict) else None
                assert status == 409 and code in ("processing", "idempotency_conflict"), (
                    f"unexpected loser response: status={status} body={body} "
                    "-- every non-winning concurrent request must be a documented 409 (processing or idempotency_conflict), never anything else"
                )

            pool = await asyncpg.create_pool(DATABASE_URL, min_size=1, max_size=5)
            obligations = await pool.fetch("SELECT * FROM royalty_obligations WHERE event_id=$1", event["event_id"])
            assert len(obligations) == 1, f"expected exactly 1 obligation row under concurrency, got {len(obligations)}"
            obligation = obligations[0]

            journal = await pool.fetch("SELECT debit_credit, amount FROM journal_entries WHERE transaction_id=$1", obligation["ledger_transaction_id"])
            debits = sum(float(row["amount"]) for row in journal if row["debit_credit"] == "DEBIT")
            credits = sum(float(row["amount"]) for row in journal if row["debit_credit"] == "CREDIT")
            assert abs(debits - credits) < 0.0001, f"unbalanced journal under concurrency: {debits} vs {credits}"

            receipts = await pool.fetch("SELECT receipt_id FROM royalty_receipts WHERE event_id=$1", event["event_id"])
            assert len(receipts) == 1, f"expected exactly 1 persisted receipt, got {len(receipts)}"

            # Retry AFTER the fact -- must replay the same receipt, no new effect.
            replay = await fire_one(client, event, priv, "lyr-k1")
            assert replay.status_code == 200 and replay.json()["receipt_id"] == list(receipt_ids)[0]
            obligations_after_replay = await pool.fetch("SELECT * FROM royalty_obligations WHERE event_id=$1", event["event_id"])
            assert len(obligations_after_replay) == 1, "post-hoc replay must not create a second obligation"

            await pool.close()

        print(f"\nAT-concurrency PASS: {N_CONCURRENT} genuinely concurrent requests -> "
              f"exactly 1 obligation, 1 balanced journal ({debits}=={credits}), 1 receipt. "
              f"Losers: {Counter(s for s, _ in losers)}. Post-hoc replay confirmed idempotent.")
    finally:
        stop(procs)


if __name__ == "__main__":
    asyncio.run(main())
