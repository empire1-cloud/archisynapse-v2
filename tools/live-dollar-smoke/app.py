#!/usr/bin/env python3
"""Local-only $1 live card smoke test for Archisynapse.

Card details are collected by Stripe.js in the browser and sent directly to
Stripe. This process receives only a tokenized PaymentMethod id (pm_...).
The Archisynapse merchant API key remains server-side in environment variables.
"""

from __future__ import annotations

import html
import os
import secrets
import uuid
from typing import Any

import httpx
from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, Field

GATEWAY_URL = os.getenv("ARCHISYNAPSE_GATEWAY_URL", "http://127.0.0.1:9000").rstrip("/")
MERCHANT_API_KEY = os.getenv("ARCHISYNAPSE_MERCHANT_API_KEY", "").strip()
STRIPE_PUBLISHABLE_KEY = os.getenv("STRIPE_PUBLISHABLE_KEY", "").strip()
CONFIRMATION = os.getenv("ARCHISYNAPSE_LIVE_SMOKE_TEST_CONFIRM", "").strip()
EXPECTED_CONFIRMATION = "CHARGE_AND_REFUND_ONE_USD"

app = FastAPI(
    title="Archisynapse $1 Live Smoke Test",
    docs_url=None,
    redoc_url=None,
    openapi_url=None,
)

_nonce = secrets.token_urlsafe(32)
_used = False


class RunRequest(BaseModel):
    payment_method_id: str = Field(pattern=r"^pm_[A-Za-z0-9_]+$")
    nonce: str = Field(min_length=20, max_length=200)
    cardholder_name: str = Field(min_length=1, max_length=120)


def _preflight() -> None:
    if CONFIRMATION != EXPECTED_CONFIRMATION:
        raise RuntimeError(
            "Set ARCHISYNAPSE_LIVE_SMOKE_TEST_CONFIRM=CHARGE_AND_REFUND_ONE_USD"
        )
    if not MERCHANT_API_KEY.startswith("arch_live_"):
        raise RuntimeError("ARCHISYNAPSE_MERCHANT_API_KEY must be a live merchant key")
    if not STRIPE_PUBLISHABLE_KEY.startswith("pk_live_"):
        raise RuntimeError("STRIPE_PUBLISHABLE_KEY must be a live publishable key")
    if GATEWAY_URL not in {"http://127.0.0.1:9000", "http://localhost:9000"}:
        if not GATEWAY_URL.startswith("https://"):
            raise RuntimeError("Non-local gateway URLs must use HTTPS")


@app.on_event("startup")
async def startup() -> None:
    _preflight()


@app.get("/", response_class=HTMLResponse)
async def index() -> HTMLResponse:
    _preflight()
    page = f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Archisynapse $1 Live Smoke Test</title>
  <script src="https://js.stripe.com/v3/"></script>
  <style>
    body {{ font-family: system-ui, sans-serif; max-width: 680px; margin: 40px auto;
           padding: 0 20px; background: #090d16; color: #f5f7fb; }}
    .card {{ background: #141b2a; padding: 24px; border-radius: 14px; }}
    #card-element {{ background: white; padding: 14px; border-radius: 8px; }}
    input, button {{ width: 100%; box-sizing: border-box; padding: 12px; margin-top: 14px;
                     border-radius: 8px; border: 1px solid #43506a; }}
    button {{ font-weight: 700; cursor: pointer; }}
    button:disabled {{ opacity: .5; cursor: wait; }}
    pre {{ white-space: pre-wrap; overflow-wrap: anywhere; background: #070a11; padding: 14px;
           border-radius: 8px; }}
    .warning {{ color: #ffd166; }}
  </style>
</head>
<body>
  <h1>Archisynapse $1 Live Smoke Test</h1>
  <div class="card">
    <p>This makes one real <strong>$1.00 USD</strong> charge, verifies the Archisynapse
       receipt, and immediately requests a full refund.</p>
    <p class="warning">A prepaid or crypto-linked card may show a temporary hold. The
       refund or released balance might not appear immediately.</p>
    <label for="name">Cardholder name</label>
    <input id="name" autocomplete="cc-name" maxlength="120">
    <div id="card-element"></div>
    <button id="run">Charge $1 and refund it</button>
    <pre id="result">Ready.</pre>
  </div>
<script>
const stripe = Stripe({STRIPE_PUBLISHABLE_KEY!r});
const elements = stripe.elements();
const card = elements.create('card', {{hidePostalCode: false}});
card.mount('#card-element');
const button = document.getElementById('run');
const output = document.getElementById('result');
button.addEventListener('click', async () => {{
  const name = document.getElementById('name').value.trim();
  if (!name) {{ output.textContent = 'Enter the cardholder name.'; return; }}
  button.disabled = true;
  output.textContent = 'Tokenizing card directly with Stripe...';
  try {{
    const created = await stripe.createPaymentMethod({{
      type: 'card', card, billing_details: {{name}}
    }});
    if (created.error) throw new Error(created.error.message);
    output.textContent = 'Running the $1 Archisynapse charge...';
    const response = await fetch('/run', {{
      method: 'POST',
      headers: {{'Content-Type': 'application/json'}},
      body: JSON.stringify({{
        payment_method_id: created.paymentMethod.id,
        nonce: {_nonce!r},
        cardholder_name: name
      }})
    }});
    const result = await response.json();
    output.textContent = JSON.stringify(result, null, 2);
    if (!response.ok) throw new Error(result.detail || 'Smoke test failed');
  }} catch (error) {{
    output.textContent = 'FAILED: ' + error.message + '\n\n' + output.textContent;
  }} finally {{
    button.disabled = false;
  }}
}});
</script>
</body>
</html>"""
    return HTMLResponse(page)


@app.post("/run")
async def run_smoke_test(request: RunRequest) -> dict[str, Any]:
    global _used
    _preflight()
    if not secrets.compare_digest(request.nonce, _nonce):
        raise HTTPException(status_code=403, detail="invalid one-time smoke-test nonce")
    if _used:
        raise HTTPException(status_code=409, detail="this smoke-test process already ran once")
    _used = True

    charge_key = f"live-smoke-{uuid.uuid4().hex}"
    refund_key = f"live-smoke-refund-{uuid.uuid4().hex}"
    headers = {
        "Authorization": f"Bearer {MERCHANT_API_KEY}",
        "Idempotency-Key": charge_key,
        "Content-Type": "application/json",
    }
    payment_body = {
        "customer_id": "founder-live-smoke",
        "amount": "1.00",
        "fee_amount": "0.00",
        "currency": "USD",
        "payment_method_type": "CARD",
        "payment_method_token": request.payment_method_id,
        "description": "Archisynapse founder $1 live smoke test",
        "metadata": {
            "live_smoke_test": True,
            "auto_refund": True,
            "cardholder_name_present": bool(request.cardholder_name),
        },
    }

    async with httpx.AsyncClient(timeout=45.0) as client:
        charge_response = await client.post(
            f"{GATEWAY_URL}/v1/payments", headers=headers, json=payment_body
        )
        try:
            charge = charge_response.json()
        except Exception as exc:
            raise HTTPException(
                status_code=502,
                detail=f"gateway returned non-JSON for charge: {exc}",
            ) from exc

        if charge_response.status_code not in {200, 201}:
            raise HTTPException(
                status_code=charge_response.status_code,
                detail={"stage": "charge", "gateway": charge},
            )

        event_id = charge.get("event_id")
        payment_id = charge.get("transaction_id")
        if charge.get("status") != "completed" or not event_id or not payment_id:
            return {
                "ok": False,
                "stage": "charge_not_completed",
                "charge": _safe_receipt(charge),
                "refund_requested": False,
            }

        verify_response = await client.get(
            f"{GATEWAY_URL}/v1/receipts/{event_id}/verify",
            headers={"Authorization": f"Bearer {MERCHANT_API_KEY}"},
        )
        verification = verify_response.json()
        if verify_response.status_code != 200 or verification.get("valid") is not True:
            return {
                "ok": False,
                "stage": "receipt_verification_failed",
                "charge": _safe_receipt(charge),
                "verification": verification,
                "refund_requested": False,
                "manual_action": "Refund the payment from the processor dashboard immediately.",
            }

        refund_response = await client.post(
            f"{GATEWAY_URL}/v1/payments/{payment_id}/refund",
            headers={
                "Authorization": f"Bearer {MERCHANT_API_KEY}",
                "Idempotency-Key": refund_key,
                "Content-Type": "application/json",
            },
            json={"amount": "1.00", "reason": "customer_requested"},
        )
        try:
            refund = refund_response.json()
        except Exception as exc:
            raise HTTPException(
                status_code=502,
                detail={
                    "stage": "refund_non_json",
                    "error": str(exc),
                    "manual_action": "Refund the payment from the processor dashboard immediately.",
                },
            ) from exc

        return {
            "ok": refund_response.status_code in {200, 201},
            "charged": "$1.00 USD",
            "charge": _safe_receipt(charge),
            "receipt_signature_valid": True,
            "refund_requested": refund_response.status_code in {200, 201},
            "refund": refund,
            "notice": "The card issuer may take time to display the refund or release the hold.",
        }


def _safe_receipt(receipt: dict[str, Any]) -> dict[str, Any]:
    allowed = {
        "event_id",
        "correlation_id",
        "transaction_id",
        "ledger_transaction_id",
        "amount",
        "fee_amount",
        "currency",
        "status",
        "fraud_decision",
        "analytics_recorded",
        "error",
    }
    return {key: receipt.get(key) for key in allowed if key in receipt}


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="127.0.0.1", port=9010, log_level="info")
