"""Archisynapse local proof run. Drives the real gateway, fraud, transaction,
ledger and analytics services against PostgreSQL. The card processor is a
local stand-in for the Stripe test API (no Stripe account, no money)."""
import json, os, sys, uuid, datetime
from pathlib import Path
import httpx, psycopg2, psycopg2.extras

sys.path.insert(0, os.environ["GATEWAY_DIR"])
from receipt_proof import verify_receipt  # noqa: E402

G = "http://127.0.0.1:9000"
OUT = Path(os.environ["OUT_DIR"]); OUT.mkdir(parents=True, exist_ok=True)
ADMIN = os.environ["ARCHISYNAPSE_ADMIN_TOKEN"]
db = psycopg2.connect("dbname=archisynapse user=postgres password=postgres host=127.0.0.1")
db.autocommit = True
c = httpx.Client(timeout=30)
log = []

def save(name, data):
    (OUT / f"{name}.json").write_text(json.dumps(data, indent=2, sort_keys=True, default=str) + "\n")

def q(sql, *args):
    with db.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute(sql, args); return [dict(r) for r in cur.fetchall()]

def counts():
    return {t: q(f"SELECT count(*) AS n FROM {t}")[0]["n"] for t in
            ("payments", "refunds", "transactions", "journal_entries", "gateway_payment_receipts")}

def step(name, ok, detail):
    log.append({"step": name, "passed": bool(ok), "detail": detail})
    print(("PASS " if ok else "FAIL ") + name)

run = {"started_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
       "environment": "local proof run; processor = local Stripe test-API stand-in; no real card network; no money moved",
       "git_commit": os.environ.get("GIT_COMMIT")}
run["status"] = c.get(f"{G}/status").json(); run["health"] = c.get(f"{G}/health").json()
run["proof_key"] = c.get(f"{G}/v1/proof/key").json()
save("00-environment", run)

def merchant(name):
    r = c.post(f"{G}/admin/merchants", headers={"X-Archisynapse-Admin-Token": ADMIN},
               json={"name": name, "plan": "test", "environment": "test"})
    r.raise_for_status(); return r.json()
A = merchant("Proof Merchant A"); B = merchant("Proof Merchant B")
save("01-merchants", {"merchant_a": A["merchant_id"], "merchant_b": B["merchant_id"],
                      "note": "API keys are revealed once at creation and stored only as hashes; not recorded here"})
HA = {"Authorization": f"Bearer {A['api_key']}"}; HB = {"Authorization": f"Bearer {B['api_key']}"}

# 1. Authorized transaction
pay_body = {"customer_id": str(uuid.uuid4()), "amount": "12.50", "fee_amount": "0.50", "currency": "USD",
            "payment_method_type": "CARD", "payment_method_token": "pm_card_visa", "payment_method_last4": "4242", "payment_method_brand": "visa", "description": "Archisynapse proof run"}
key = f"proof-pay-{uuid.uuid4().hex[:8]}"
before = counts()
r = c.post(f"{G}/v1/payments", headers={**HA, "Idempotency-Key": key}, json=pay_body)
receipt = r.json()
ev = c.get(f"{G}/v1/receipts/{receipt.get('event_id')}/evidence", headers=HA).json()
stored = ev.get("receipt", {})
txn_id = receipt.get("ledger_transaction_id")
entries = q("SELECT account_id, debit_credit, amount FROM journal_entries WHERE transaction_id = %s ORDER BY debit_credit, amount", txn_id) if txn_id else []
deb = sum(e["amount"] for e in entries if e["debit_credit"] == "DEBIT"); cre = sum(e["amount"] for e in entries if e["debit_credit"] == "CREDIT")
save("02-authorized-payment", {"http_status": r.status_code, "response": receipt, "evidence_endpoint": ev,
     "ledger_entries": entries, "debits": deb, "credits": cre, "row_counts_before": before, "row_counts_after": counts()})
step("authorized payment returns 201 with a signed, verifiable receipt",
     r.status_code == 201 and receipt.get("status") not in ("failed", "blocked") and receipt.get("ledger_transaction_id") and ev.get("signature_valid") is True, {"status": receipt.get("status"), "event_id": receipt.get("event_id")})
step("payment posts balanced double-entry ledger lines", entries and deb == cre, {"debits": deb, "credits": cre, "lines": len(entries)})

# 2. Offline verification + tamper
ok_offline = verify_receipt(stored)
tampered = dict(stored); tampered["amount"] = "9999.00"
bad = verify_receipt(tampered)
save("03-offline-verification", {"stored_receipt": stored, "offline_verify": ok_offline, "tampered_amount_verify": bad})
step("stored receipt verifies offline; tampered copy fails", ok_offline[0] and not bad[0], {"offline": ok_offline[1], "tampered": bad[1]})

# 3. Idempotency / replay
before = counts()
r2 = c.post(f"{G}/v1/payments", headers={**HA, "Idempotency-Key": key}, json=pay_body)
r3 = c.post(f"{G}/v1/payments", headers={**HA, "Idempotency-Key": key}, json={**pay_body, "amount": "99.00"})
after = counts()
save("04-idempotency-replay", {"replay_status": r2.status_code, "replay_event_id": r2.json().get("event_id"),
     "original_event_id": receipt.get("event_id"), "changed_amount_same_key_status": r3.status_code,
     "changed_amount_response": r3.json(), "row_counts_before": before, "row_counts_after": after})
step("replay with the same key returns the same receipt and creates nothing", r2.json().get("event_id") == receipt.get("event_id") and before == after, {"replay_status": r2.status_code})
step("same key with a different amount is refused (409) and creates nothing", r3.status_code == 409 and before == after, {"status": r3.status_code})

# 4. Denied / blocked actions -> no ledger mutation
before = counts(); denied = {}
denied["no_api_key"] = c.post(f"{G}/v1/payments", headers={"Idempotency-Key": "x-" + uuid.uuid4().hex}, json=pay_body).status_code
denied["forged_api_key"] = c.post(f"{G}/v1/payments", headers={"Authorization": "Bearer ak_test_forged.notakey", "Idempotency-Key": "x-" + uuid.uuid4().hex}, json=pay_body).status_code
denied["merchant_b_reads_a_receipt"] = c.get(f"{G}/v1/receipts/{receipt.get('event_id')}", headers=HB).status_code
denied["merchant_b_refunds_a_payment"] = c.post(f"{G}/v1/payments/{receipt.get('transaction_id')}/refund",
     headers={**HB, "Idempotency-Key": "x-" + uuid.uuid4().hex}, json={"amount": "12.50", "reason": "customer_requested"}).status_code
denied["missing_idempotency_key"] = c.post(f"{G}/v1/payments", headers=HA, json=pay_body).status_code
after = counts()
save("05-denied-actions", {"responses": denied, "row_counts_before": before, "row_counts_after": after,
     "ledger_unchanged": before == after})
step("unauthorized and cross-merchant actions are refused with no ledger, payment or receipt rows written",
     all(s in (401, 403, 404, 422) for s in denied.values()) and before == after, denied)

# 5. Refund / reversal
before = counts()
rr = c.post(f"{G}/v1/payments/{receipt.get('transaction_id')}/refund",
            headers={**HA, "Idempotency-Key": f"proof-refund-{uuid.uuid4().hex[:8]}"}, json={"amount": "12.50", "reason": "customer_requested"})
refund = rr.json()
rev = q("SELECT id, type, status, reference_id FROM transactions WHERE reference_id = %s", txn_id)
rev_entries = q("SELECT account_id, debit_credit, amount FROM journal_entries WHERE transaction_id = %s ORDER BY debit_credit, amount", rev[0]["id"]) if rev else []
rdeb = sum(e["amount"] for e in rev_entries if e["debit_credit"] == "DEBIT"); rcre = sum(e["amount"] for e in rev_entries if e["debit_credit"] == "CREDIT")
orig_status = q("SELECT status FROM transactions WHERE id = %s", txn_id)
pay_row = q("SELECT status FROM payments WHERE id = %s", receipt.get("transaction_id"))
attempt = q("SELECT status, processor_refund_id, ledger_transaction_id FROM processor_refund_attempts WHERE payment_id = %s", receipt.get("transaction_id"))
save("06-refund-reversal", {"http_status": rr.status_code, "response": refund, "reversal_transaction": rev,
     "reversal_entries": rev_entries, "debits": rdeb, "credits": rcre, "original_transaction_status": orig_status,
     "payment_status": pay_row, "processor_refund_attempt": attempt, "row_counts_before": before, "row_counts_after": counts()})
step("refund posts a balanced reversal and marks the original reversed",
     rr.status_code in (200, 201) and rev_entries and rdeb == rcre and orig_status and orig_status[0]["status"] == "REVERSED",
     {"status": rr.status_code, "reversal_lines": len(rev_entries), "debits": rdeb, "credits": rcre})

# 6. Whole-ledger balance check
tb = q("SELECT organization_id, SUM(CASE WHEN debit_credit='DEBIT' THEN amount ELSE -amount END) AS net FROM journal_entries GROUP BY organization_id")
save("07-ledger-balance", {"net_debits_minus_credits_by_organization": tb})
step("every organization's ledger nets to zero", all(row["net"] == 0 for row in tb), tb)

run["finished_at"] = datetime.datetime.now(datetime.timezone.utc).isoformat()
run["steps"] = log; run["all_passed"] = all(s["passed"] for s in log)
save("99-summary", run)
print("ALL PASSED" if run["all_passed"] else "SOME STEPS FAILED")
# Non-zero exit on any failed step, so CI (Docker stack workflow) goes red.
raise SystemExit(0 if run["all_passed"] else 1)
