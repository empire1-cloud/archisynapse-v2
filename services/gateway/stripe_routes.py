"""
Archisynapse Gateway - Real Stripe card-acquiring layer.

This is the missing "real money" edge for the Revenue Assurance Loop.

Flow:
    1. Client hits  GET  /v1/stripe/pay        -> minimal hosted "Pay" page
    2. Client hits  POST /v1/stripe/checkout   -> creates a Stripe Checkout Session
                                                  (real hosted card form) and returns its URL
    3. Customer pays on Stripe's hosted page (PCI handled by Stripe)
    4. Stripe calls POST /v1/stripe/webhook    -> signature-verified; on a completed
                                                  payment we replay it into the existing
                                                  /v1/revenue/process loop so it lands in
                                                  fraud -> transaction -> ledger -> analytics.
                                                  THIS closes the loop.

Design notes:
    - Fails CLOSED. If STRIPE_SECRET_KEY is unset the router still mounts but every
      endpoint returns a clear 503 explaining what to configure. Nothing silently no-ops.
    - No secrets in code. Everything comes from environment variables.
    - Works in Stripe TEST mode (sk_test_...) or LIVE mode (sk_live_...) transparently --
      the mode is whatever key you export. Test with 4242 4242 4242 4242 first.
    - Additive only: this module is included by main.py, it does not modify existing routes.
"""

import logging
import os
from typing import Optional

import httpx
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse
from pydantic import BaseModel, Field

logger = logging.getLogger("archisynapse.gateway.stripe")

# --- Configuration (all via env, no secrets in code) ------------------------
STRIPE_SECRET_KEY = os.getenv("STRIPE_SECRET_KEY", "")
STRIPE_PUBLISHABLE_KEY = os.getenv("STRIPE_PUBLISHABLE_KEY", "")
STRIPE_WEBHOOK_SECRET = os.getenv("STRIPE_WEBHOOK_SECRET", "")
# Public base URL Stripe should redirect back to (e.g. https://archisynapse.vercel.app
# or your ngrok/https tunnel while testing locally).
PUBLIC_BASE_URL = os.getenv("PUBLIC_BASE_URL", "http://localhost:9000")
# Where the gateway can reach ITSELF to replay the webhook into the revenue loop.
GATEWAY_SELF_URL = os.getenv("GATEWAY_SELF_URL", "http://localhost:9000")
# Merchant that owns loop-closing test payments. UUID string.
STRIPE_DEFAULT_MERCHANT_ID = os.getenv(
    "STRIPE_DEFAULT_MERCHANT_ID", "00000000-0000-0000-0000-000000000001"
)

try:  # stripe is optional at import time so the gateway still boots without it
    import stripe  # type: ignore

    if STRIPE_SECRET_KEY:
        stripe.api_key = STRIPE_SECRET_KEY
    _STRIPE_IMPORT_OK = True
except Exception:  # pragma: no cover
    stripe = None  # type: ignore
    _STRIPE_IMPORT_OK = False


router = APIRouter(prefix="/v1/stripe", tags=["stripe"])


def _require_stripe() -> None:
    if not _STRIPE_IMPORT_OK:
        raise HTTPException(
            status_code=503,
            detail="stripe library not installed. Run: pip install stripe (see requirements.txt).",
        )
    if not STRIPE_SECRET_KEY:
        raise HTTPException(
            status_code=503,
            detail="STRIPE_SECRET_KEY is not set. Export it (sk_test_... to test, sk_live_... for real).",
        )


class CheckoutRequest(BaseModel):
    # Amount in MAJOR units (dollars). Defaults to a $1.00 loop-closing test.
    amount: float = Field(default=1.00, gt=0)
    currency: str = "usd"
    customer_email: Optional[str] = None
    description: str = "Archisynapse loop-close test charge"
    merchant_id: Optional[str] = None


@router.get("/config")
async def stripe_config():
    """Non-secret status: is the card layer ready to charge?"""
    mode = "unset"
    if STRIPE_SECRET_KEY.startswith("sk_live_"):
        mode = "LIVE"
    elif STRIPE_SECRET_KEY.startswith("sk_test_"):
        mode = "TEST"
    return {
        "stripe_library_installed": _STRIPE_IMPORT_OK,
        "secret_key_set": bool(STRIPE_SECRET_KEY),
        "publishable_key_set": bool(STRIPE_PUBLISHABLE_KEY),
        "webhook_secret_set": bool(STRIPE_WEBHOOK_SECRET),
        "mode": mode,
        "public_base_url": PUBLIC_BASE_URL,
        "ready_to_charge": bool(_STRIPE_IMPORT_OK and STRIPE_SECRET_KEY),
    }


@router.post("/checkout")
async def create_checkout(request: CheckoutRequest):
    """Create a Stripe Checkout Session (hosted, real card form). Returns its URL."""
    _require_stripe()
    amount_cents = int(round(request.amount * 100))
    if amount_cents < 50:
        # Stripe's minimum charge is 50 cents USD. A $1 test is fine.
        raise HTTPException(status_code=400, detail="Amount below Stripe minimum (0.50 USD).")

    merchant_id = request.merchant_id or STRIPE_DEFAULT_MERCHANT_ID
    try:
        session = stripe.checkout.Session.create(
            mode="payment",
            line_items=[
                {
                    "price_data": {
                        "currency": request.currency,
                        "product_data": {"name": request.description},
                        "unit_amount": amount_cents,
                    },
                    "quantity": 1,
                }
            ],
            customer_email=request.customer_email,
            success_url=f"{PUBLIC_BASE_URL}/v1/stripe/success?session_id={{CHECKOUT_SESSION_ID}}",
            cancel_url=f"{PUBLIC_BASE_URL}/v1/stripe/cancel",
            payment_intent_data={"description": request.description},
            metadata={
                "archisynapse_merchant_id": merchant_id,
                "source": "loop_close_test",
            },
        )
    except Exception as exc:  # noqa: BLE001
        logger.exception("Stripe checkout session creation failed")
        raise HTTPException(status_code=502, detail=f"Stripe error: {exc}")

    return {"checkout_url": session.url, "session_id": session.id}


@router.get("/pay", response_class=HTMLResponse)
async def pay_page():
    """Minimal one-button page that starts a $1 checkout."""
    return """<!DOCTYPE html><html><head><meta charset="utf-8">
<title>Archisynapse - Pay</title>
<style>
body{font-family:-apple-system,system-ui,sans-serif;background:#0b0b14;color:#eee;
display:flex;min-height:100vh;align-items:center;justify-content:center;margin:0}
.card{background:#15151f;border:1px solid #2a2a3a;border-radius:16px;padding:40px;
max-width:360px;text-align:center;box-shadow:0 8px 40px rgba(0,0,0,.5)}
h1{margin:0 0 6px;font-size:20px}p{color:#9a9ab0;margin:0 0 24px;font-size:14px}
button{background:#635bff;color:#fff;border:0;border-radius:10px;padding:14px 22px;
font-size:16px;font-weight:600;cursor:pointer;width:100%}
button:hover{background:#524af5}button:disabled{opacity:.6;cursor:wait}
.err{color:#ff6b6b;font-size:13px;margin-top:14px;min-height:16px}
</style></head><body>
<div class="card">
<h1>Archisynapse</h1>
<p>Close the loop &mdash; charge a real card for <b>$1.00</b></p>
<button id="pay">Pay $1.00</button>
<div class="err" id="err"></div>
</div>
<script>
document.getElementById('pay').onclick=async function(){
  var b=this;b.disabled=true;b.textContent='Redirecting...';
  document.getElementById('err').textContent='';
  try{
    var r=await fetch('/v1/stripe/checkout',{method:'POST',
      headers:{'Content-Type':'application/json'},body:JSON.stringify({amount:1.00})});
    var j=await r.json();
    if(j.checkout_url){window.location=j.checkout_url;}
    else{throw new Error(j.detail||'Could not start checkout');}
  }catch(e){document.getElementById('err').textContent=e.message;
    b.disabled=false;b.textContent='Pay $1.00';}
};
</script></body></html>"""


@router.get("/success", response_class=HTMLResponse)
async def success_page(session_id: Optional[str] = None):
    return f"""<!DOCTYPE html><html><head><meta charset="utf-8"><title>Paid</title>
<style>body{{font-family:-apple-system,system-ui,sans-serif;background:#0b0b14;color:#eee;
display:flex;min-height:100vh;align-items:center;justify-content:center;margin:0;text-align:center}}
.c{{background:#15151f;border:1px solid #2a2a3a;border-radius:16px;padding:40px;max-width:380px}}
.ok{{font-size:44px}}code{{color:#9a9ab0;font-size:12px}}</style></head><body>
<div class="c"><div class="ok">&#9989;</div><h1>Payment received</h1>
<p>The loop is closing &mdash; this charge is being posted to the ledger.</p>
<code>{session_id or ''}</code></div></body></html>"""


@router.get("/cancel", response_class=HTMLResponse)
async def cancel_page():
    return """<!DOCTYPE html><html><head><meta charset="utf-8"><title>Cancelled</title>
<style>body{font-family:-apple-system,system-ui,sans-serif;background:#0b0b14;color:#eee;
display:flex;min-height:100vh;align-items:center;justify-content:center;margin:0;text-align:center}
.c{background:#15151f;border:1px solid #2a2a3a;border-radius:16px;padding:40px;max-width:380px}
</style></head><body><div class="c"><h1>Payment cancelled</h1>
<p>No card was charged.</p></div></body></html>"""


async def _post_to_revenue_loop(payload: dict) -> None:
    """Replay a settled Stripe payment into the existing Revenue Assurance Loop."""
    url = f"{GATEWAY_SELF_URL}/v1/revenue/process"
    async with httpx.AsyncClient(timeout=30.0) as client:
        resp = await client.post(url, json=payload)
    if resp.status_code >= 300:
        logger.error("Revenue loop replay failed (%s): %s", resp.status_code, resp.text[:400])
    else:
        logger.info("Revenue loop replay OK for idempotency_key=%s", payload.get("idempotency_key"))


def _build_loop_payload(session: dict, charge: Optional[dict]) -> dict:
    """Map a Stripe checkout session (+ charge) onto ProcessPaymentRequest."""
    metadata = session.get("metadata") or {}
    merchant_id = metadata.get("archisynapse_merchant_id") or STRIPE_DEFAULT_MERCHANT_ID
    amount_total = (session.get("amount_total") or 0) / 100.0
    currency = (session.get("currency") or "usd").upper()
    customer_details = session.get("customer_details") or {}
    email = customer_details.get("email") or session.get("customer_email")

    last4, brand, token = "0000", "UNKNOWN", session.get("payment_intent") or session.get("id")
    if charge:
        pmd = (charge.get("payment_method_details") or {}).get("card") or {}
        last4 = pmd.get("last4", last4)
        brand = (pmd.get("brand") or brand).upper()
        token = charge.get("payment_method") or token

    return {
        "merchant_id": merchant_id,
        "customer_id": email or (session.get("customer") or "stripe_guest"),
        "amount": amount_total,
        "currency": currency,
        "payment_method_type": "CARD",
        "payment_method_token": str(token),
        "payment_method_last4": last4,
        "payment_method_brand": brand,
        "description": "Stripe Checkout (loop-close)",
        "email": email,
        "metadata": {"stripe_session_id": session.get("id"), "source": "stripe_checkout"},
        # Idempotent on the Stripe session id so replays/retries never double-post.
        "idempotency_key": f"stripe_{session.get('id')}",
    }


@router.post("/webhook")
async def stripe_webhook(request: Request):
    """Signature-verified Stripe webhook. On a completed payment, closes the loop."""
    _require_stripe()
    payload = await request.body()
    sig_header = request.headers.get("stripe-signature", "")

    if not STRIPE_WEBHOOK_SECRET:
        raise HTTPException(
            status_code=503,
            detail="STRIPE_WEBHOOK_SECRET is not set; refusing unverified webhooks.",
        )

    try:
        event = stripe.Webhook.construct_event(payload, sig_header, STRIPE_WEBHOOK_SECRET)
    except Exception as exc:  # noqa: BLE001 - includes SignatureVerificationError
        logger.warning("Rejected Stripe webhook: %s", exc)
        raise HTTPException(status_code=400, detail=f"Invalid signature: {exc}")

    event_type = event.get("type")
    logger.info("Stripe webhook received: %s", event_type)

    if event_type == "checkout.session.completed":
        session = event["data"]["object"]
        if session.get("payment_status") != "paid":
            return JSONResponse({"received": True, "handled": False, "reason": "not paid"})

        # Pull the charge so we get real card brand/last4.
        charge = None
        try:
            pi_id = session.get("payment_intent")
            if pi_id:
                pi = stripe.PaymentIntent.retrieve(pi_id, expand=["latest_charge"])
                charge = pi.get("latest_charge")
                if isinstance(charge, str):
                    charge = stripe.Charge.retrieve(charge)
        except Exception:  # noqa: BLE001
            logger.exception("Could not expand charge; proceeding with minimal detail")

        payload_out = _build_loop_payload(session, charge)
        await _post_to_revenue_loop(payload_out)
        return JSONResponse({"received": True, "handled": True, "loop": "posted"})

    # Acknowledge everything else so Stripe stops retrying.
    return JSONResponse({"received": True, "handled": False, "event": event_type})
