"""Probe the ledger and transaction services directly, bypassing the gateway.

Run after proof_run.py against the same running stack (service auth enforced).
Writes 08-service-auth-denials.json into OUT_DIR.

Proves, on the running services:
  * a caller that sends only X-Organization-ID (the old trust model) is refused
    by both services, for reads and writes;
  * a wrong token and a token issued for another hop are refused;
  * the gateway's ledger token can read the ledger but cannot post to it;
  * none of the refused calls wrote a row (ledger, payment counts unchanged);
  * no service token appears in any service log.

Environment: OUT_DIR, SERVICE_TOKEN_DIR (the generator's output directory),
LOG_DIR (service logs), DATABASE_URL (psycopg2 form), LEDGER_URL, TRANSACTION_URL.
"""

import json
import os
import uuid
from pathlib import Path

import httpx
import psycopg2

OUT = Path(os.environ["OUT_DIR"])
TOKENS = Path(os.environ["SERVICE_TOKEN_DIR"])
LOGS = Path(os.environ["LOG_DIR"])
DB = os.environ.get("DATABASE_URL", "postgresql://postgres:postgres@127.0.0.1:5432/archisynapse")
LEDGER = os.environ.get("LEDGER_URL", "http://127.0.0.1:3001")
TRANSACTION = os.environ.get("TRANSACTION_URL", "http://127.0.0.1:3000")

token = {name: (TOKENS / f"{name}.token").read_text().strip() for name in (
    "gateway-to-transaction", "gateway-to-ledger", "transaction-to-ledger")}
ORG = "mer_probe_" + uuid.uuid4().hex[:8]


def counts():
    with psycopg2.connect(DB) as conn, conn.cursor() as cur:
        out = {}
        for table in ("journal_entries", "transactions", "accounts", "payments"):
            cur.execute(f"SELECT count(*) FROM {table}")
            out[table] = cur.fetchone()[0]
        return out


def bearer(name):
    return {"Authorization": f"Bearer {token[name]}"}


ledger_entry = {
    "type": "ADJUSTMENT", "referenceId": "probe", "description": "probe",
    "amount": "5.00", "currency": "USD", "idempotencyKey": f"probe-{uuid.uuid4().hex}",
    "entries": [
        {"accountId": str(uuid.uuid4()), "debitCredit": "DEBIT", "amount": "5.00"},
        {"accountId": str(uuid.uuid4()), "debitCredit": "CREDIT", "amount": "5.00"},
    ],
}
payment = {"amount": "5.00", "currency": "USD", "paymentMethod": {"type": "CARD", "token": "tok_probe"}}

cases = [
    ("ledger read with only X-Organization-ID", "GET", f"{LEDGER}/accounts", {}, None, 401),
    ("ledger post with only X-Organization-ID", "POST", f"{LEDGER}/transactions", {}, ledger_entry, 401),
    ("ledger account create with only X-Organization-ID", "POST", f"{LEDGER}/accounts", {},
     {"code": "9999", "name": "Planted", "type": "ASSET"}, 401),
    ("ledger post with a made-up token", "POST", f"{LEDGER}/transactions",
     {"Authorization": "Bearer " + "x" * 64}, ledger_entry, 401),
    ("gateway token posting to the ledger (read-only caller)", "POST", f"{LEDGER}/transactions",
     bearer("gateway-to-ledger"), ledger_entry, 403),
    ("gateway token reading the ledger", "GET", f"{LEDGER}/accounts", bearer("gateway-to-ledger"), None, 200),
    ("payment with only X-Organization-ID", "POST", f"{TRANSACTION}/payments",
     {"Idempotency-Key": f"probe-{uuid.uuid4().hex}"}, payment, 401),
    ("refund with only X-Organization-ID", "POST", f"{TRANSACTION}/payments/{uuid.uuid4()}/refund", {},
     {"amount": "1.00"}, 401),
    ("payment with the transaction->ledger token (wrong hop)", "POST", f"{TRANSACTION}/payments",
     {**bearer("transaction-to-ledger"), "Idempotency-Key": f"probe-{uuid.uuid4().hex}"}, payment, 401),
    ("royalty lookup with only X-Organization-ID", "GET", f"{TRANSACTION}/royalties/evt_probe", {}, None, 401),
]

before = counts()
results = []
with httpx.Client(timeout=10) as client:
    for name, method, url, headers, body, expected in cases:
        response = client.request(method, url, json=body,
                                  headers={"X-Organization-ID": ORG, **headers})
        results.append({"case": name, "method": method, "path": url.split("//", 1)[1].split("/", 1)[1],
                        "expected": expected, "status": response.status_code,
                        "passed": response.status_code == expected})
after = counts()

leaks = []
for log in sorted(LOGS.glob("*.log")):
    text = log.read_text(errors="replace")
    for name, value in token.items():
        if value in text:
            leaks.append({"log": log.name, "token": name})

summary = {
    "claim": "ledger and transaction services refuse callers without a valid service token; "
             "the gateway can read but not post to the ledger; refused calls write nothing; tokens never logged",
    "organization_used": ORG,
    "cases": results,
    "row_counts_before": before,
    "row_counts_after": after,
    "rows_written_by_probe": {k: after[k] - before[k] for k in before},
    "token_values_found_in_logs": leaks,
    "logs_checked": sorted(p.name for p in LOGS.glob("*.log")),
}
summary["passed"] = (
    all(r["passed"] for r in results)
    and all(v == 0 for v in summary["rows_written_by_probe"].values())
    and not leaks
)
OUT.mkdir(parents=True, exist_ok=True)
(OUT / "08-service-auth-denials.json").write_text(json.dumps(summary, indent=2) + "\n")
for r in results:
    print(("PASS " if r["passed"] else "FAIL ") + f"{r['case']}: {r['status']} (expected {r['expected']})")
print("rows written by probe:", summary["rows_written_by_probe"])
print("token values in logs:", leaks or "none")
print("ALL PASSED" if summary["passed"] else "FAILED")
raise SystemExit(0 if summary["passed"] else 1)
