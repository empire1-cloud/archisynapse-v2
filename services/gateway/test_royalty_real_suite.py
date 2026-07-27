"""
REAL BOUNDARY SUITE for the royalty receipt loop — Docker Postgres,
real ledger-service, real transaction-service, real fraud-service, real
gateway. No mock/SQLite ledger anywhere in this file. Requires:
  - `docker compose up -d postgres redis` already running
  - migrations 000-006 applied to that Postgres
  - node_modules present at repo root (for tsx)

Run: python3 test_royalty_real_suite.py
"""

import asyncio
import hashlib
import json
import os
import subprocess
import sys
import time
import uuid
from datetime import datetime, timedelta, timezone
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

LEDGER_PORT = 3301
TRANSACTION_PORT = 3300
FRAUD_PORT = 8380
GATEWAY_PORT = 9300

DATABASE_URL = "postgresql://postgres:postgres@127.0.0.1:5432/archisynapse"
ADMIN_TOKEN = "real-suite-admin-token"
RUN_ID = uuid.uuid4().hex[:8]
TENANT_ID = f"lyrica-{RUN_ID}"

RESULTS: list[dict] = []


def record(test_id: str, purpose: str, passed: bool, evidence: str = ""):
    RESULTS.append({"id": test_id, "purpose": purpose, "passed": passed, "evidence": evidence})
    print(f"{'PASS' if passed else 'FAIL'} {test_id}: {purpose}" + (f" -- {evidence}" if evidence else ""))
    return passed


def now_iso(offset: timedelta = timedelta()) -> str:
    return (datetime.now(timezone.utc) + offset).isoformat()


def make_event(idempotency_key: str, event_id: str = None, amount_value: str = "1.2500",
                splits=None, occurred_at: str = None, actor_id: str = "usr_listener_42",
                vics_proof_id: str = "vics_01H_valid", trigger_kind: str = "remix",
                trigger_source_ref: str = "lyrica://remix/rmx_7788") -> dict:
    return {
        "schema_version": "1.0",
        "event_id": event_id or f"evt_{idempotency_key}",
        "event_type": "royalty.obligation.created",
        "occurred_at": occurred_at or now_iso(),
        "correlation_id": f"corr_{idempotency_key}",
        "idempotency_key": idempotency_key,
        "tenant_id": TENANT_ID,
        "track": {
            "track_id": "trk_9f3a2b1c",
            "dna_tag": "dna_v2_7c1e",
            "soulprint_hash": "sp_sha256_4b09",
            "vics_proof": {
                "proof_id": vics_proof_id,
                "issued_at": now_iso(),
                "chain_ref": "vics://empire1/lyrica/trk_9f3a2b1c",
            },
        },
        "creator": {"creator_id": "cre_a1b2c3", "identity_ref": "sla113://identity/cre_a1b2c3"},
        "splits": splits or [{"owner_id": "cre_a1b2c3", "bps": 10000}],
        "trigger": {"kind": trigger_kind, "source_ref": trigger_source_ref, "actor_id": actor_id},
        "amount": {"currency": "USD", "value": amount_value},
    }


async def post_event(client: httpx.AsyncClient, event: dict, private_key_b64: str, key_id: str, gateway_url: str):
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
    return await client.post(f"{gateway_url}/api/v1/events", content=body_bytes, headers=headers)


async def wait_healthy(client: httpx.AsyncClient, url: str, timeout: float = 20.0):
    deadline = time.monotonic() + timeout
    last_err = None
    while time.monotonic() < deadline:
        try:
            resp = await client.get(url, timeout=2.0)
            if resp.status_code == 200:
                return
        except httpx.RequestError as exc:
            last_err = exc
        await asyncio.sleep(0.3)
    raise RuntimeError(f"{url} did not become healthy in time: {last_err}")


def start_services(royalty_loop_enabled: str = "true") -> dict:
    env = {
        **os.environ,
        "DB_HOST": "127.0.0.1",
        "DB_PORT": "5432",
        "DB_NAME": "archisynapse",
        "DB_USER": "postgres",
        "DB_PASSWORD": "postgres",
        "LEDGER_SERVICE_URL": f"http://127.0.0.1:{LEDGER_PORT}",
    }
    ledger = subprocess.Popen(
        [TSX, "ledger-service-index.ts"],
        cwd=str(LEDGER_DIR),
        env={**env, "PORT": str(LEDGER_PORT)},
        stdout=open("/tmp/real-suite-ledger.log", "w"),
        stderr=subprocess.STDOUT,
    )
    transaction = subprocess.Popen(
        [TSX, "transaction-service-index.ts"],
        cwd=str(TRANSACTION_DIR),
        env={**env, "PORT": str(TRANSACTION_PORT)},
        stdout=open("/tmp/real-suite-transaction.log", "w"),
        stderr=subprocess.STDOUT,
    )
    fraud_env = {
        **os.environ,
        "ARCHISYNAPSE_DATABASE_URL": "postgresql+psycopg2://postgres:postgres@127.0.0.1:5432/archisynapse",
        "ARCHISYNAPSE_PEPPER": "real-suite-test-pepper",
    }
    fraud_python = str(FRAUD_DIR / ".venv" / "bin" / "python3")
    fraud = subprocess.Popen(
        [fraud_python, "-c", f"import uvicorn; uvicorn.run('archisynapse_fraud_mvp:app', host='127.0.0.1', port={FRAUD_PORT}, log_level='warning')"],
        cwd=str(FRAUD_DIR),
        env=fraud_env,
        stdout=open("/tmp/real-suite-fraud.log", "w"),
        stderr=subprocess.STDOUT,
    )
    gateway_env = {
        **os.environ,
        "DATABASE_URL": DATABASE_URL,
        "TRANSACTION_SERVICE_URL": f"http://127.0.0.1:{TRANSACTION_PORT}",
        "FRAUD_SERVICE_URL": f"http://127.0.0.1:{FRAUD_PORT}",
        "ROYALTY_LOOP_ENABLED": royalty_loop_enabled,
        "ROYALTY_ADMIN_TOKEN": ADMIN_TOKEN,
        "ROYALTY_TEST_FIXTURES_ENABLED": "true",
        "ROYALTY_TEST_AUTHZ_PRINCIPALS": json.dumps({
            "policy-admin-token": {"tenant_id": TENANT_ID, "role": "policy_admin"},
            "wrong-role-token": {"tenant_id": TENANT_ID, "role": "viewer"},
            "wrong-tenant-token": {"tenant_id": f"other-{TENANT_ID}", "role": "policy_admin"},
        }),
    }
    gateway = subprocess.Popen(
        [sys.executable, "-c", f"import uvicorn; uvicorn.run('main:app', host='127.0.0.1', port={GATEWAY_PORT}, log_level='warning')"],
        cwd=str(GATEWAY_DIR),
        env=gateway_env,
        stdout=open(f"/tmp/real-suite-gateway-{royalty_loop_enabled}.log", "w"),
        stderr=subprocess.STDOUT,
    )
    return {"ledger": ledger, "transaction": transaction, "fraud": fraud, "gateway": gateway}


def stop_services(procs: dict):
    for proc in procs.values():
        proc.terminate()
    for proc in procs.values():
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()


async def run_suite() -> bool:
    gateway_url = f"http://127.0.0.1:{GATEWAY_PORT}"
    procs = start_services()
    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            await wait_healthy(client, f"http://127.0.0.1:{LEDGER_PORT}/health")
            await wait_healthy(client, f"http://127.0.0.1:{TRANSACTION_PORT}/health")
            await wait_healthy(client, f"http://127.0.0.1:{FRAUD_PORT}/health")
            await wait_healthy(client, f"{gateway_url}/health")

            admin_headers = {"Authorization": f"Bearer {ADMIN_TOKEN}"}
            private_key_b64, public_key_b64 = generate_tenant_keypair()
            await client.post(f"{gateway_url}/admin/tenants/{TENANT_ID}/api-key",
                               json={"api_key": "lyrica-tenant-token"}, headers=admin_headers)
            await client.post(f"{gateway_url}/admin/tenants/{TENANT_ID}/keys",
                               json={"key_id": "lyr-k1", "public_key_b64": public_key_b64}, headers=admin_headers)

            pool = await asyncpg.create_pool(DATABASE_URL, min_size=1, max_size=5)

            # ---- AT-01: happy path ----
            event = make_event(f"at01-{RUN_ID}")
            resp = await post_event(client, event, private_key_b64, "lyr-k1", gateway_url)
            ok = resp.status_code == 201
            receipt = resp.json() if ok else {}
            ok = ok and receipt.get("amounts") == {"currency": "USD", "gross": "1.2500", "platform_fee": "0.0000", "net": "1.2500"}
            ok = ok and receipt.get("payouts") == [{"owner_id": "cre_a1b2c3", "amount": "1.2500", "state": "paid"}]
            row = await pool.fetchrow("SELECT * FROM royalty_obligations WHERE event_id=$1", event["event_id"])
            ok = ok and row is not None and row["status"] == "POSTED"
            journal = await pool.fetch(
                """
                SELECT a.code, je.debit_credit, je.amount
                FROM journal_entries je
                JOIN accounts a ON a.id = je.account_id
                WHERE je.transaction_id=$1
                ORDER BY a.code
                """,
                row["ledger_transaction_id"],
            ) if row else []
            debits = sum(float(r["amount"]) for r in journal if r["debit_credit"] == "DEBIT")
            credits = sum(float(r["amount"]) for r in journal if r["debit_credit"] == "CREDIT")
            account_effects = {(r["code"], r["debit_credit"], str(r["amount"])) for r in journal}
            ok = ok and abs(debits - credits) < 0.0001 and abs(debits - 1.25) < 0.0001
            ok = ok and account_effects == {
                ("cre_a1b2c3", "CREDIT", "1.2500"),
                ("royalty_expense", "DEBIT", "1.2500"),
            }
            record("AT-01", "happy path, single owner, $1.2500", ok, f"status={resp.status_code} balanced={debits}=={credits}")
            at01_receipt_id = receipt.get("receipt_id")
            at01_ledger_txn = row["ledger_transaction_id"] if row else None

            # ---- AT-02: idempotent retry x3 ----
            all_match = True
            for _ in range(3):
                resp2 = await post_event(client, event, private_key_b64, "lyr-k1", gateway_url)
                all_match = all_match and resp2.status_code == 200 and resp2.json().get("receipt_id") == at01_receipt_id
            count_row = await pool.fetchrow("SELECT count(*) as c FROM royalty_obligations WHERE event_id=$1", event["event_id"])
            all_match = all_match and count_row["c"] == 1
            record("AT-02", "idempotent retry x3, one obligation row", all_match, f"obligation_count={count_row['c']}")

            # ---- AT-03: idempotency conflict ----
            conflict_event = make_event(f"at01-{RUN_ID}", amount_value="2.0000")
            resp3 = await post_event(client, conflict_event, private_key_b64, "lyr-k1", gateway_url)
            ok3 = resp3.status_code == 409
            record("AT-03", "idempotency conflict on reused key with different payload", ok3, f"status={resp3.status_code}")

            # ---- AT-04: 60/40 split, divides evenly ----
            split_event = make_event(f"at04-{RUN_ID}", splits=[{"owner_id": "cre_a1b2c3", "bps": 6000}, {"owner_id": "cre_d4e5f6", "bps": 4000}])
            resp4 = await post_event(client, split_event, private_key_b64, "lyr-k1", gateway_url)
            payouts4 = {p["owner_id"]: p["amount"] for p in resp4.json().get("payouts", [])} if resp4.status_code == 201 else {}
            ok4 = resp4.status_code == 201 and payouts4 == {"cre_a1b2c3": "0.7500", "cre_d4e5f6": "0.5000"}
            record("AT-04", "60/40 split of $1.25, divides evenly", ok4, str(payouts4))

            # ---- AT-04b: genuine remainder ----
            remainder_event = make_event(
                f"at04b-{RUN_ID}",
                splits=[{"owner_id": "a_owner", "bps": 3333}, {"owner_id": "b_owner", "bps": 3333}, {"owner_id": "c_owner", "bps": 3334}],
            )
            resp4b = await post_event(client, remainder_event, private_key_b64, "lyr-k1", gateway_url)
            payouts4b = {p["owner_id"]: p["amount"] for p in resp4b.json().get("payouts", [])} if resp4b.status_code == 201 else {}
            ok4b = resp4b.status_code == 201 and payouts4b == {"a_owner": "0.4200", "b_owner": "0.4100", "c_owner": "0.4200"}
            record("AT-04b", "largest-remainder split, genuine rounding", ok4b, str(payouts4b))

            # ---- AT-05: tampered signature ----
            tampered_event = make_event(f"at05-{RUN_ID}")
            body_bytes = json.dumps(tampered_event).encode("utf-8")
            bad_sig = sign_with_private_key(private_key_b64, b"not the real body")
            resp5 = await client.post(f"{gateway_url}/api/v1/events", content=body_bytes, headers={
                "Content-Type": "application/json", "Authorization": "Bearer lyrica-tenant-token",
                "X-Empire1-Signature": bad_sig, "X-Empire1-Key-Id": "lyr-k1",
            })
            no_obligation = await pool.fetchrow("SELECT 1 FROM royalty_obligations WHERE event_id=$1", tampered_event["event_id"])
            ok5 = resp5.status_code == 401 and no_obligation is None
            record("AT-05", "tampered signature rejected, zero financial objects", ok5, f"status={resp5.status_code}")

            # ---- AT-06: unregistered key ----
            rogue_priv, _ = generate_tenant_keypair()
            rogue_event = make_event(f"at06-{RUN_ID}")
            rb = json.dumps(rogue_event).encode("utf-8")
            rogue_sig = sign_with_private_key(rogue_priv, rb)
            resp6 = await client.post(f"{gateway_url}/api/v1/events", content=rb, headers={
                "Content-Type": "application/json", "Authorization": "Bearer lyrica-tenant-token",
                "X-Empire1-Signature": rogue_sig, "X-Empire1-Key-Id": "rogue-k9",
            })
            ok6 = resp6.status_code == 403
            record("AT-06", "unregistered key rejected", ok6, f"status={resp6.status_code}")

            # ---- AT-07: ownership invalid (revoked VICS proof) ----
            revoked_event = make_event(f"at07-{RUN_ID}", vics_proof_id="vics_revoked_test_fixture")
            resp7 = await post_event(client, revoked_event, private_key_b64, "lyr-k1", gateway_url)
            body7 = resp7.json()
            ok7 = resp7.status_code == 422 and body7.get("status") == "blocked" and "vics_invalid" in body7.get("status_reasons", [])
            record("AT-07", "ownership invalid -> 422, receipt-shaped blocked body", ok7, f"status={resp7.status_code} body_status={body7.get('status')}")

            # ---- AT-08: high-risk hold ----
            risk_event = make_event(f"at08-{RUN_ID}", actor_id="usr_risk_test_fixture")
            resp8 = await post_event(client, risk_event, private_key_b64, "lyr-k1", gateway_url)
            body8 = resp8.json()
            row8 = await pool.fetchrow("SELECT * FROM royalty_obligations WHERE event_id=$1", risk_event["event_id"])
            journal8 = await pool.fetch(
                """
                SELECT a.code, je.debit_credit, je.amount
                FROM journal_entries je
                JOIN accounts a ON a.id = je.account_id
                WHERE je.transaction_id=$1
                ORDER BY a.code
                """,
                row8["ledger_transaction_id"],
            ) if row8 else []
            effects8 = {(r["code"], r["debit_credit"], str(r["amount"])) for r in journal8}
            ok8 = (resp8.status_code == 201 and body8.get("status") == "held" and row8 is not None
                   and row8["status"] == "HELD" and effects8 == {
                       ("royalty_expense", "DEBIT", "1.2500"),
                       ("royalty_held_liab", "CREDIT", "1.2500"),
                   })
            record("AT-08", "high-risk hold -> held liability, no payable", ok8, f"status={resp8.status_code} db_status={row8['status'] if row8 else None}")
            at08_event_id = risk_event["event_id"]

            # ---- AT-09: release of held event, then release again x2 ----
            missing_auth = await client.post(
                f"{gateway_url}/api/v1/events/{at08_event_id}/release"
            )
            wrong_role = await client.post(
                f"{gateway_url}/api/v1/events/{at08_event_id}/release",
                headers={"Authorization": "Bearer wrong-role-token"},
            )
            wrong_tenant = await client.post(
                f"{gateway_url}/api/v1/events/{at08_event_id}/release",
                headers={
                    "Authorization": "Bearer wrong-tenant-token",
                    "X-Tenant-Id": TENANT_ID,
                },
            )
            held_after_denials = await pool.fetchval(
                "SELECT status FROM royalty_obligations WHERE event_id=$1",
                at08_event_id,
            )
            release_headers = {
                "Authorization": "Bearer policy-admin-token",
                "X-Tenant-Id": TENANT_ID,
            }
            r1 = await client.post(f"{gateway_url}/api/v1/events/{at08_event_id}/release", headers=release_headers)
            r2 = await client.post(f"{gateway_url}/api/v1/events/{at08_event_id}/release", headers=release_headers)
            r3 = await client.post(f"{gateway_url}/api/v1/events/{at08_event_id}/release", headers=release_headers)
            row9 = await pool.fetchrow("SELECT * FROM royalty_obligations WHERE event_id=$1", at08_event_id)
            release_effects = await pool.fetch(
                """
                SELECT a.code, je.debit_credit, je.amount
                FROM journal_entries je
                JOIN accounts a ON a.id = je.account_id
                WHERE je.transaction_id=$1
                ORDER BY a.code
                """,
                row9["release_ledger_transaction_id"] if row9 else None,
            )
            release_effect_set = {
                (r["code"], r["debit_credit"], str(r["amount"]))
                for r in release_effects
            }
            ok9 = (
                missing_auth.status_code == 403
                and wrong_role.status_code == 403
                and wrong_tenant.status_code == 403
                and held_after_denials == "HELD"
                and r1.status_code == 200
                and r2.status_code == 200
                and r3.status_code == 200
                and r1.json() == r2.json() == r3.json()
                and r1.json().get("receipt_id")
                and row9 is not None
                and row9["status"] == "POSTED"
                and row9["initial_ledger_transaction_id"] == row8["ledger_transaction_id"]
                and release_effect_set == {
                    ("cre_a1b2c3", "CREDIT", "1.2500"),
                    ("royalty_held_liab", "DEBIT", "1.2500"),
                }
            )
            record("AT-09", "release held event, repeat release deterministically 200", ok9,
                   f"statuses={r1.status_code},{r2.status_code},{r3.status_code} db_status={row9['status'] if row9 else None}")

            # ---- AT-10: hard policy block ----
            block_event = make_event(
                f"at10-{RUN_ID}",
                trigger_kind="license",
                trigger_source_ref="license_denied_test_fixture",
            )
            resp10 = await post_event(client, block_event, private_key_b64, "lyr-k1", gateway_url)
            resp10_replay = await post_event(
                client, block_event, private_key_b64, "lyr-k1", gateway_url
            )
            block_row = await pool.fetchrow(
                "SELECT * FROM royalty_obligations WHERE event_id=$1",
                block_event["event_id"],
            )
            block_journal = (
                await pool.fetch(
                    "SELECT 1 FROM journal_entries WHERE transaction_id=$1",
                    block_row["ledger_transaction_id"],
                )
                if block_row and block_row["ledger_transaction_id"]
                else []
            )
            ok10 = (
                resp10.status_code == 201
                and resp10_replay.status_code == 200
                and resp10_replay.json() == resp10.json()
                and resp10.json().get("status") == "blocked"
                and resp10.json().get("decision", {}).get("policy") == "license_policy_denied"
                and block_row is not None
                and block_row["status"] == "BLOCKED"
                and block_row["ledger_transaction_id"] is None
                and not block_journal
            )
            record("AT-10", "hard license-policy block -> zero financial entries", ok10, f"status={resp10.status_code}")

            # ---- AT-11: reversal + retry ----
            reversal_body = {
                "reversal_event_id": f"rev_{RUN_ID}",
                "reversal_idempotency_key": f"rev-idem-{RUN_ID}",
                "reason": "test_reversal",
            }
            rev1 = await client.post(f"{gateway_url}/api/v1/events/{event['event_id']}/reverse",
                                      json=reversal_body, headers=release_headers)
            rev2 = await client.post(f"{gateway_url}/api/v1/events/{event['event_id']}/reverse",
                                      json=reversal_body, headers=release_headers)
            row11 = await pool.fetchrow("SELECT * FROM royalty_obligations WHERE event_id=$1", event["event_id"])
            reversal_row = await pool.fetchrow(
                """
                SELECT reversal_ledger_transaction_id
                FROM royalty_reversals
                WHERE organization_id=$1 AND reversal_event_id=$2
                """,
                TENANT_ID,
                reversal_body["reversal_event_id"],
            )
            original_effects = await pool.fetch(
                """
                SELECT account_id, debit_credit, amount
                FROM journal_entries WHERE transaction_id=$1
                """,
                at01_ledger_txn,
            )
            reversal_effects = await pool.fetch(
                """
                SELECT account_id, debit_credit, amount
                FROM journal_entries WHERE transaction_id=$1
                """,
                reversal_row["reversal_ledger_transaction_id"] if reversal_row else None,
            )
            original_set = {
                (str(r["account_id"]), r["debit_credit"], str(r["amount"]))
                for r in original_effects
            }
            inverse_set = {
                (
                    str(r["account_id"]),
                    "CREDIT" if r["debit_credit"] == "DEBIT" else "DEBIT",
                    str(r["amount"]),
                )
                for r in reversal_effects
            }
            ok11 = (
                rev1.status_code == 201
                and rev2.status_code == 200
                and rev2.json() == rev1.json()
                and row11["status"] == "REVERSED"
                and reversal_row is not None
                and original_set == inverse_set
            )
            record("AT-11", "event-specific reversal linkage and deterministic replay", ok11,
                   f"statuses={rev1.status_code},{rev2.status_code} db_status={row11['status']}")

            # ---- AT-13: correlation thread ----
            receipt01 = await client.get(f"{gateway_url}/api/v1/receipts/{at01_receipt_id}")
            corr_id = event["correlation_id"]
            db_corr = await pool.fetchrow(
                "SELECT correlation_id FROM royalty_obligations WHERE event_id=$1",
                event["event_id"],
            )
            gateway_corr = await pool.fetchrow(
                """
                SELECT rr.correlation_id
                FROM royalty_idempotency ri
                JOIN royalty_receipts rr ON rr.receipt_id = ri.receipt_id
                WHERE ri.tenant_id=$1 AND ri.idempotency_key=$2
                """,
                TENANT_ID,
                event["idempotency_key"],
            )
            ledger_corr = await pool.fetchval(
                """
                SELECT metadata->>'correlationId'
                FROM journal_entries
                WHERE transaction_id=$1
                LIMIT 1
                """,
                at01_ledger_txn,
            )
            ok13 = (
                receipt01.status_code == 200
                and receipt01.json()["correlation_id"] == corr_id
                and db_corr["correlation_id"] == corr_id
                and gateway_corr["correlation_id"] == corr_id
                and ledger_corr == corr_id
            )
            record(
                "AT-13",
                "one correlation_id across gateway state, transaction, ledger metadata, and receipt",
                ok13,
                corr_id,
            )

            # ---- AT-15: stale event + boundary values ----
            stale_event = make_event(f"at15-{RUN_ID}", occurred_at=now_iso(timedelta(minutes=-30)))
            resp15 = await post_event(client, stale_event, private_key_b64, "lyr-k1", gateway_url)
            no_row15 = await pool.fetchrow("SELECT 1 FROM royalty_obligations WHERE event_id=$1", stale_event["event_id"])
            no_idempotency15 = await pool.fetchrow(
                "SELECT 1 FROM royalty_idempotency WHERE tenant_id=$1 AND idempotency_key=$2",
                TENANT_ID,
                stale_event["idempotency_key"],
            )
            no_receipt15 = await pool.fetchrow(
                "SELECT 1 FROM royalty_receipts WHERE event_id=$1",
                stale_event["event_id"],
            )
            rejection15 = await pool.fetchrow(
                "SELECT reason FROM royalty_rejections WHERE correlation_id=$1 ORDER BY occurred_at DESC LIMIT 1",
                stale_event["correlation_id"],
            )
            ok15 = (
                resp15.status_code == 422
                and resp15.json().get("detail", {}).get("code") == "stale_event"
                and no_row15 is None
                and no_idempotency15 is None
                and no_receipt15 is None
                and rejection15 is not None
                and rejection15["reason"] == "stale_event"
            )

            boundary_ok_event = make_event(f"at15b-{RUN_ID}", occurred_at=now_iso(timedelta(minutes=-4, seconds=-59)))
            resp15b = await post_event(client, boundary_ok_event, private_key_b64, "lyr-k1", gateway_url)
            boundary_ok = resp15b.status_code == 201

            boundary_stale_event = make_event(f"at15c-{RUN_ID}", occurred_at=now_iso(timedelta(minutes=-5, seconds=-1)))
            resp15c = await post_event(client, boundary_stale_event, private_key_b64, "lyr-k1", gateway_url)
            boundary_stale = resp15c.status_code == 422

            ok15_all = ok15 and boundary_ok and boundary_stale
            record("AT-15", "stale event rejected before any side effect; boundary values", ok15_all,
                   f"stale={resp15.status_code} boundary_ok={resp15b.status_code} boundary_stale={resp15c.status_code}")

            # ---- Feature flag: disabled-by-default fails closed ----
            # (checked separately in run_flag_disabled_check(), not here --
            # this process already has ROYALTY_LOOP_ENABLED=true.)

            await pool.close()
    finally:
        stop_services(procs)

    return all(r["passed"] for r in RESULTS)


async def run_flag_disabled_check() -> bool:
    """Separate process with ROYALTY_LOOP_ENABLED unset -- must fail closed."""
    procs = start_services(royalty_loop_enabled="false")
    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            await wait_healthy(client, f"http://127.0.0.1:{GATEWAY_PORT}/health")
            event = make_event(f"flagoff-{RUN_ID}")
            body_bytes = json.dumps(event).encode("utf-8")
            resp = await client.post(
                f"http://127.0.0.1:{GATEWAY_PORT}/api/v1/events",
                content=body_bytes,
                headers={"Content-Type": "application/json"},
            )
            ok = resp.status_code == 503 and resp.json().get("code") == "retry_later" and resp.json().get("retryable") is True
            return record("feature-flag", "ROYALTY_LOOP_ENABLED=false fails closed with 503 retry_later", ok, str(resp.json()))
    finally:
        stop_services(procs)


async def main():
    flag_ok = await run_flag_disabled_check()
    suite_ok = await run_suite()
    print("\n=== SUMMARY ===")
    for r in RESULTS:
        print(f"{'PASS' if r['passed'] else 'FAIL'}  {r['id']:10s} {r['purpose']}")
    all_ok = flag_ok and suite_ok
    print(f"\nALL {'GREEN' if all_ok else 'FAILURES PRESENT'}")
    sys.exit(0 if all_ok else 1)


if __name__ == "__main__":
    asyncio.run(main())
