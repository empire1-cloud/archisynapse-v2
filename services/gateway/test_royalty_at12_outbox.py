"""
AT-12: outage durability, driven by the real LyricaOutboxSimulator
(royalty_outbox_simulator.py) against the real gateway/transaction/ledger
stack. Demonstrates:
  outage -> persisted pending event -> retry with the same identity ->
  one accepted transaction -> one balanced ledger posting -> one receipt
  -> delivered state -- and that this survives the SIMULATOR restarting
  (state lives in Postgres, not process memory).

Run: python3 test_royalty_at12_outbox.py
"""

import asyncio
import json
import os
import subprocess
import sys
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

import asyncpg
import httpx

sys.path.insert(0, os.path.dirname(__file__))
from royalty_keys import generate_tenant_keypair  # noqa: E402
from royalty_outbox_simulator import LyricaOutboxSimulator  # noqa: E402

GATEWAY_DIR = Path(__file__).resolve().parent
REPO_ROOT = GATEWAY_DIR.parent.parent
LEDGER_DIR = REPO_ROOT / "services" / "ledger"
TRANSACTION_DIR = REPO_ROOT / "services" / "transaction"
FRAUD_DIR = REPO_ROOT / "services" / "fraud"
TSX = str(REPO_ROOT / "node_modules" / ".bin" / "tsx")

LEDGER_PORT = 3311
TRANSACTION_PORT = 3310
FRAUD_PORT = 8381
GATEWAY_PORT = 9310
DATABASE_URL = "postgresql://postgres:postgres@127.0.0.1:5432/archisynapse"
ADMIN_TOKEN = "at12-admin-token"
RUN_ID = uuid.uuid4().hex[:8]
TENANT_ID = f"lyrica-at12-{RUN_ID}"


def start_dependency_services():
    env = {
        **os.environ, "DB_HOST": "127.0.0.1", "DB_PORT": "5432", "DB_NAME": "archisynapse",
        "DB_USER": "postgres", "DB_PASSWORD": "postgres", "LEDGER_SERVICE_URL": f"http://127.0.0.1:{LEDGER_PORT}",
    }
    ledger = subprocess.Popen([TSX, "ledger-service-index.ts"], cwd=str(LEDGER_DIR),
                              env={**env, "PORT": str(LEDGER_PORT)},
                              stdout=open("/tmp/at12-ledger.log", "w"), stderr=subprocess.STDOUT)
    transaction = subprocess.Popen([TSX, "transaction-service-index.ts"], cwd=str(TRANSACTION_DIR),
                                    env={**env, "PORT": str(TRANSACTION_PORT)},
                                    stdout=open("/tmp/at12-transaction.log", "w"), stderr=subprocess.STDOUT)
    fraud_env = {**os.environ, "ARCHISYNAPSE_DATABASE_URL": "postgresql+psycopg2://postgres:postgres@127.0.0.1:5432/archisynapse", "ARCHISYNAPSE_PEPPER": "at12-pepper"}
    fraud_python = str(FRAUD_DIR / ".venv" / "bin" / "python3")
    fraud = subprocess.Popen(
        [fraud_python, "-c", f"import uvicorn; uvicorn.run('archisynapse_fraud_mvp:app', host='127.0.0.1', port={FRAUD_PORT}, log_level='warning')"],
        cwd=str(FRAUD_DIR), env=fraud_env, stdout=open("/tmp/at12-fraud.log", "w"), stderr=subprocess.STDOUT,
    )
    return {"ledger": ledger, "transaction": transaction, "fraud": fraud}


def start_gateway():
    gateway_env = {
        **os.environ,
        "DATABASE_URL": DATABASE_URL,
        "TRANSACTION_SERVICE_URL": f"http://127.0.0.1:{TRANSACTION_PORT}",
        "FRAUD_SERVICE_URL": f"http://127.0.0.1:{FRAUD_PORT}",
        "ROYALTY_LOOP_ENABLED": "true",
        "ROYALTY_ADMIN_TOKEN": ADMIN_TOKEN,
        "ROYALTY_TEST_FIXTURES_ENABLED": "true",
    }
    return subprocess.Popen(
        [sys.executable, "-c", f"import uvicorn; uvicorn.run('main:app', host='127.0.0.1', port={GATEWAY_PORT}, log_level='warning')"],
        cwd=str(GATEWAY_DIR), env=gateway_env, stdout=open("/tmp/at12-gateway.log", "w"), stderr=subprocess.STDOUT,
    )


def stop(procs):
    for p in procs:
        p.terminate()
    for p in procs:
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


async def main():
    deps = start_dependency_services()
    gateway_proc = None
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            await wait_healthy(client, f"http://127.0.0.1:{LEDGER_PORT}/health")
            await wait_healthy(client, f"http://127.0.0.1:{TRANSACTION_PORT}/health")
            await wait_healthy(client, f"http://127.0.0.1:{FRAUD_PORT}/health")

        pool = await asyncpg.create_pool(DATABASE_URL, min_size=1, max_size=5)

        # --- OUTAGE: gateway is NOT running yet ---
        priv, pub = generate_tenant_keypair()
        event = {
            "schema_version": "1.0", "event_id": f"evt_at12_{RUN_ID}", "event_type": "royalty.obligation.created",
            "occurred_at": datetime.now(timezone.utc).isoformat(), "correlation_id": f"corr_at12_{RUN_ID}",
            "idempotency_key": f"idem_at12_{RUN_ID}", "tenant_id": TENANT_ID,
            "track": {"track_id": "trk_1", "dna_tag": "dna1", "soulprint_hash": "sp1",
                      "vics_proof": {"proof_id": "vics_ok", "issued_at": datetime.now(timezone.utc).isoformat(), "chain_ref": "ref"}},
            "creator": {"creator_id": "cre_1", "identity_ref": "ref"},
            "splits": [{"owner_id": "cre_a1b2c3", "bps": 10000}],
            "trigger": {"kind": "remix", "source_ref": "ref", "actor_id": "usr_1"},
            "amount": {"currency": "USD", "value": "1.2500"},
        }

        simulator = LyricaOutboxSimulator(pool, f"http://127.0.0.1:{GATEWAY_PORT}", TENANT_ID, "at12-tenant-token")
        await simulator.enqueue(event, priv, "lyr-k1")
        print("Enqueued event while gateway is DOWN (outage simulated).")

        attempted = await simulator.run_once()
        row = await pool.fetchrow("SELECT * FROM lyrica_outbox WHERE event_id=$1", event["event_id"])
        print(f"After first attempt during outage: state={row['state']} attempts={row['attempts']} (attempted={attempted})")
        assert row["state"] == "sent" and row["attempts"] == 1, "expected a retryable connection-error state"

        # --- RECOVERY: start the gateway now ---
        gateway_proc = start_gateway()
        async with httpx.AsyncClient(timeout=10.0) as client:
            await wait_healthy(client, f"http://127.0.0.1:{GATEWAY_PORT}/health")
            await client.post(f"http://127.0.0.1:{GATEWAY_PORT}/admin/tenants/{TENANT_ID}/api-key",
                               json={"api_key": "at12-tenant-token"}, headers={"Authorization": f"Bearer {ADMIN_TOKEN}"})
            await client.post(f"http://127.0.0.1:{GATEWAY_PORT}/admin/tenants/{TENANT_ID}/keys",
                               json={"key_id": "lyr-k1", "public_key_b64": pub}, headers={"Authorization": f"Bearer {ADMIN_TOKEN}"})
        print("Gateway recovered.")

        # --- Simulator "restart": fresh instance, same Postgres-backed state ---
        del simulator
        simulator2 = LyricaOutboxSimulator(pool, f"http://127.0.0.1:{GATEWAY_PORT}", TENANT_ID, "at12-tenant-token")
        await simulator2.run_until_all_settled(timeout_seconds=30.0)

        final_row = await pool.fetchrow("SELECT * FROM lyrica_outbox WHERE event_id=$1", event["event_id"])
        print(f"Final outbox state: {final_row['state']}, attempts={final_row['attempts']}")
        assert final_row["state"] == "receipted", f"expected receipted, got {final_row['state']}"

        obligations = await pool.fetch("SELECT * FROM royalty_obligations WHERE event_id=$1", event["event_id"])
        assert len(obligations) == 1, f"expected exactly 1 obligation row, got {len(obligations)}"
        obligation = obligations[0]
        assert obligation["status"] == "POSTED"

        journal = await pool.fetch("SELECT debit_credit, amount FROM journal_entries WHERE transaction_id=$1", obligation["ledger_transaction_id"])
        debits = sum(float(r["amount"]) for r in journal if r["debit_credit"] == "DEBIT")
        credits = sum(float(r["amount"]) for r in journal if r["debit_credit"] == "CREDIT")
        assert abs(debits - credits) < 0.0001 and abs(debits - 1.25) < 0.0001, f"unbalanced: {debits} vs {credits}"

        receipt_payload = json.loads(final_row["receipt"])
        assert receipt_payload["status"] in ("processing", "paid")

        print(f"\nAT-12 PASS: exactly 1 obligation row, 1 balanced ledger transaction ({debits}=={credits}), "
              f"1 receipt, outbox state=receipted, no duplicates, {final_row['attempts']} total attempts.")
        sys.exit(0)
    finally:
        if gateway_proc:
            stop([gateway_proc])
        stop(list(deps.values()))


if __name__ == "__main__":
    asyncio.run(main())
