"""
Lightweight Transaction Service mock — SQLite-backed.
Implements the API surface the orchestrator expects.
Port: 3000
"""

import uuid
from datetime import datetime, timezone
from fastapi import FastAPI, HTTPException, Header
from pydantic import BaseModel
from typing import Optional, Dict
import sqlite3

app = FastAPI(title="Transaction Service (mock)")

DB = sqlite3.connect("/tmp/transactions.db", check_same_thread=False)
DB.execute("""
CREATE TABLE IF NOT EXISTS payments (
    id TEXT PRIMARY KEY,
    organization_id TEXT,
    customer_id TEXT,
    amount TEXT,
    currency TEXT DEFAULT 'USD',
    status TEXT DEFAULT 'succeeded',
    idempotency_key TEXT UNIQUE,
    metadata TEXT DEFAULT '{}',
    created_at TEXT
)
""")
DB.execute("""
CREATE TABLE IF NOT EXISTS refunds (
    id TEXT PRIMARY KEY,
    payment_id TEXT,
    organization_id TEXT,
    amount TEXT,
    reason TEXT,
    status TEXT DEFAULT 'succeeded',
    created_at TEXT
)
""")
DB.commit()


class PaymentIn(BaseModel):
    customerId: str
    amount: str
    currency: str = "USD"
    paymentMethod: dict = {}
    description: Optional[str] = None
    metadata: dict = {}


class RefundIn(BaseModel):
    amount: str
    reason: str


@app.get("/health")
async def health():
    return {"status": "healthy"}


@app.post("/payments")
async def create_payment(
    body: PaymentIn,
    x_organization_id: str = Header("mer_demo"),
    idempotency_key: Optional[str] = Header(None),
):
    # Check idempotency
    if idempotency_key:
        row = DB.execute(
            "SELECT id FROM payments WHERE idempotency_key = ?", (idempotency_key,)
        ).fetchone()
        if row:
            # Return existing payment
            pay = DB.execute("SELECT * FROM payments WHERE id = ?", (row[0],)).fetchone()
            return _payment_dict(pay)

    payment_id = f"txn_{uuid.uuid4().hex[:16]}"
    now = datetime.now(timezone.utc).isoformat()
    DB.execute(
        "INSERT INTO payments (id, organization_id, customer_id, amount, currency, status, idempotency_key, metadata, created_at) VALUES (?, ?, ?, ?, ?, 'succeeded', ?, ?, ?)",
        (payment_id, x_organization_id, body.customerId, body.amount, body.currency, idempotency_key or f"idem_{uuid.uuid4().hex[:8]}", "{}", now),
    )
    DB.commit()

    return {
        "id": payment_id,
        "organizationId": x_organization_id,
        "customerId": body.customerId,
        "amount": body.amount,
        "currency": body.currency,
        "status": "succeeded",
        "createdAt": now,
    }


@app.get("/payments/{payment_id}")
async def get_payment(payment_id: str, x_organization_id: str = Header("mer_demo")):
    row = DB.execute("SELECT * FROM payments WHERE id = ?", (payment_id,)).fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="Payment not found")
    return _payment_dict(row)


@app.post("/payments/{payment_id}/refund")
async def refund_payment(
    payment_id: str,
    body: RefundIn,
    x_organization_id: str = Header("mer_demo"),
):
    pay = DB.execute("SELECT * FROM payments WHERE id = ?", (payment_id,)).fetchone()
    if not pay:
        raise HTTPException(status_code=404, detail="Payment not found")

    refund_id = f"ref_{uuid.uuid4().hex[:16]}"
    now = datetime.now(timezone.utc).isoformat()
    DB.execute(
        "INSERT INTO refunds (id, payment_id, organization_id, amount, reason, status, created_at) VALUES (?, ?, ?, ?, ?, 'succeeded', ?)",
        (refund_id, payment_id, x_organization_id, body.amount, body.reason, now),
    )
    # Update payment status
    DB.execute("UPDATE payments SET status = 'refunded' WHERE id = ?", (payment_id,))
    DB.commit()

    return {
        "id": refund_id,
        "paymentId": payment_id,
        "amount": body.amount,
        "reason": body.reason,
        "status": "succeeded",
        "createdAt": now,
    }


def _payment_dict(row):
    return {
        "id": row[0],
        "organizationId": row[1],
        "customerId": row[2],
        "amount": row[3],
        "currency": row[4],
        "status": row[5],
        "createdAt": row[7],
    }


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=3000)
