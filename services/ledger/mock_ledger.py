"""
Lightweight Ledger Service mock — SQLite-backed.
Implements the API surface the orchestrator expects.
Port: 3001
"""

import uuid
from datetime import datetime, timezone
from fastapi import FastAPI, HTTPException, Header
from pydantic import BaseModel
from typing import Optional, List
import sqlite3

app = FastAPI(title="Ledger Service (mock)")

DB = sqlite3.connect("/tmp/ledger.db", check_same_thread=False)
DB.execute("""
CREATE TABLE IF NOT EXISTS accounts (
    id TEXT PRIMARY KEY,
    organization_id TEXT,
    code TEXT,
    name TEXT,
    type TEXT,
    currency TEXT DEFAULT 'USD',
    balance REAL DEFAULT 0,
    created_at TEXT
)
""")
DB.execute("""
CREATE TABLE IF NOT EXISTS transactions (
    id TEXT PRIMARY KEY,
    organization_id TEXT,
    type TEXT,
    reference_id TEXT,
    description TEXT,
    amount TEXT,
    currency TEXT DEFAULT 'USD',
    status TEXT DEFAULT 'POSTED',
    idempotency_key TEXT UNIQUE,
    created_at TEXT
)
""")
DB.execute("""
CREATE TABLE IF NOT EXISTS journal_entries (
    id TEXT PRIMARY KEY,
    transaction_id TEXT,
    organization_id TEXT,
    account_id TEXT,
    debit_credit TEXT,
    amount TEXT,
    description TEXT,
    created_at TEXT
)
""")
DB.commit()


class AccountIn(BaseModel):
    code: str
    name: str
    type: str
    currency: str = "USD"


class LedgerEntry(BaseModel):
    accountId: str
    debitCredit: str
    amount: str
    description: str = ""


class LedgerTransactionIn(BaseModel):
    type: str
    referenceId: Optional[str] = None
    description: str = ""
    amount: str
    currency: str = "USD"
    idempotencyKey: Optional[str] = None
    entries: List[LedgerEntry] = []
    metadata: dict = {}


@app.get("/health")
async def health():
    return {"status": "healthy"}


@app.get("/accounts")
async def list_accounts(x_organization_id: str = Header("mer_demo")):
    rows = DB.execute(
        "SELECT * FROM accounts WHERE organization_id = ?", (x_organization_id,)
    ).fetchall()
    return [_account_dict(r) for r in rows]


@app.post("/accounts")
async def create_account(body: AccountIn, x_organization_id: str = Header("mer_demo")):
    # Check if exists
    existing = DB.execute(
        "SELECT id FROM accounts WHERE organization_id = ? AND code = ?",
        (x_organization_id, body.code),
    ).fetchone()
    if existing:
        return _account_dict(
            DB.execute("SELECT * FROM accounts WHERE id = ?", (existing[0],)).fetchone()
        )

    account_id = f"acc_{uuid.uuid4().hex[:12]}"
    now = datetime.now(timezone.utc).isoformat()
    DB.execute(
        "INSERT INTO accounts (id, organization_id, code, name, type, currency, balance, created_at) VALUES (?, ?, ?, ?, ?, ?, 0, ?)",
        (account_id, x_organization_id, body.code, body.name, body.type, body.currency, now),
    )
    DB.commit()
    return {
        "id": account_id,
        "code": body.code,
        "name": body.name,
        "type": body.type,
        "currency": body.currency,
        "balance": 0,
        "createdAt": now,
    }


@app.post("/transactions")
async def create_transaction(body: LedgerTransactionIn, x_organization_id: str = Header("mer_demo")):
    # Check idempotency
    if body.idempotencyKey:
        existing = DB.execute(
            "SELECT id FROM transactions WHERE idempotency_key = ?", (body.idempotencyKey,)
        ).fetchone()
        if existing:
            txn = DB.execute("SELECT * FROM transactions WHERE id = ?", (existing[0],)).fetchone()
            entries = DB.execute(
                "SELECT * FROM journal_entries WHERE transaction_id = ?", (existing[0],)
            ).fetchall()
            return {
                "id": txn[0],
                "type": txn[2],
                "referenceId": txn[3],
                "description": txn[4],
                "amount": txn[5],
                "currency": txn[6],
                "status": txn[7],
                "entries": [_entry_dict(e) for e in entries],
                "createdAt": txn[9],
            }

    # Validate double-entry: sum of debits must equal sum of credits
    debits = sum(float(e.amount) for e in body.entries if e.debitCredit == "DEBIT")
    credits = sum(float(e.amount) for e in body.entries if e.debitCredit == "CREDIT")
    if abs(debits - credits) > 0.001:
        raise HTTPException(status_code=400, detail=f"Debits ({debits}) != Credits ({credits})")

    txn_id = f"led_{uuid.uuid4().hex[:12]}"
    now = datetime.now(timezone.utc).isoformat()

    DB.execute(
        "INSERT INTO transactions (id, organization_id, type, reference_id, description, amount, currency, status, idempotency_key, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, 'POSTED', ?, ?)",
        (txn_id, x_organization_id, body.type, body.referenceId, body.description, body.amount, body.currency, body.idempotencyKey, now),
    )

    entries_out = []
    for e in body.entries:
        entry_id = f"je_{uuid.uuid4().hex[:12]}"
        DB.execute(
            "INSERT INTO journal_entries (id, transaction_id, organization_id, account_id, debit_credit, amount, description, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (entry_id, txn_id, x_organization_id, e.accountId, e.debitCredit, e.amount, e.description, now),
        )
        entries_out.append({
            "id": entry_id,
            "accountId": e.accountId,
            "debitCredit": e.debitCredit,
            "amount": e.amount,
            "description": e.description,
        })

    # Update account balances
    for e in body.entries:
        if e.debitCredit == "DEBIT":
            DB.execute("UPDATE accounts SET balance = balance + ? WHERE id = ?", (float(e.amount), e.accountId))
        else:
            DB.execute("UPDATE accounts SET balance = balance - ? WHERE id = ?", (float(e.amount), e.accountId))

    DB.commit()

    return {
        "id": txn_id,
        "type": body.type,
        "referenceId": body.referenceId,
        "description": body.description,
        "amount": body.amount,
        "currency": body.currency,
        "status": "POSTED",
        "entries": entries_out,
        "createdAt": now,
    }


@app.get("/transactions/{txn_id}")
async def get_transaction(txn_id: str, x_organization_id: str = Header("mer_demo")):
    txn = DB.execute("SELECT * FROM transactions WHERE id = ? AND organization_id = ?", (txn_id, x_organization_id)).fetchone()
    if not txn:
        raise HTTPException(status_code=404, detail="Transaction not found")
    entries = DB.execute("SELECT * FROM journal_entries WHERE transaction_id = ?", (txn_id,)).fetchall()
    return {
        "id": txn[0],
        "type": txn[2],
        "referenceId": txn[3],
        "description": txn[4],
        "amount": txn[5],
        "currency": txn[6],
        "status": txn[7],
        "entries": [_entry_dict(e) for e in entries],
        "createdAt": txn[9],
    }


@app.post("/transactions/{txn_id}/reverse")
async def reverse_transaction(txn_id: str, body: dict = {}, x_organization_id: str = Header("mer_demo")):
    original = DB.execute("SELECT * FROM transactions WHERE id = ?", (txn_id,)).fetchone()
    if not original:
        raise HTTPException(status_code=404, detail="Transaction not found")

    entries = DB.execute(
        "SELECT * FROM journal_entries WHERE transaction_id = ?", (txn_id,)
    ).fetchall()

    # Create reversal
    rev_id = f"led_{uuid.uuid4().hex[:12]}"
    now = datetime.now(timezone.utc).isoformat()
    DB.execute(
        "INSERT INTO transactions (id, organization_id, type, reference_id, description, amount, currency, status, idempotency_key, created_at) VALUES (?, ?, 'REVERSAL', ?, ?, ?, ?, 'POSTED', ?, ?)",
        (rev_id, x_organization_id, txn_id, f"Reversal of {txn_id}", original[5], original[6], f"rev_{uuid.uuid4().hex[:8]}", now),
    )

    # Reverse entries
    for e in entries:
        entry_id = f"je_{uuid.uuid4().hex[:12]}"
        reversed_dc = "CREDIT" if e[5] == "DEBIT" else "DEBIT"
        DB.execute(
            "INSERT INTO journal_entries (id, transaction_id, organization_id, account_id, debit_credit, amount, description, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (entry_id, rev_id, x_organization_id, e[4], reversed_dc, e[6], f"Reversal: {e[7]}", now),
        )

    DB.execute("UPDATE transactions SET status = 'REVERSED' WHERE id = ?", (txn_id,))
    DB.commit()

    return {"id": rev_id, "status": "POSTED", "reverses": txn_id}


@app.get("/trial-balance")
async def trial_balance(x_organization_id: str = Header("mer_demo")):
    rows = DB.execute(
        "SELECT * FROM accounts WHERE organization_id = ?", (x_organization_id,)
    ).fetchall()
    return {"asOf": datetime.now(timezone.utc).isoformat(), "accounts": [_account_dict(r) for r in rows], "isBalanced": True}


def _account_dict(row):
    return {"id": row[0], "code": row[2], "name": row[3], "type": row[4], "currency": row[5], "balance": row[6]}


def _entry_dict(row):
    return {"id": row[0], "transactionId": row[1], "accountId": row[3], "debitCredit": row[4], "amount": row[5], "description": row[6]}


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=3001)
