from __future__ import annotations

import hashlib
from dataclasses import asdict

from .authorization import AuthorizationStore
from .l402 import PaymentProvider
from .models import DecodedInvoice, DeliveryEvidence
from .receipts import ReceiptStore


async def reconcile_initiated_payment(
    *,
    authorizations: AuthorizationStore,
    receipts: ReceiptStore,
    payment_provider: PaymentProvider,
    reservation_id: str,
):
    """Reconcile without ever issuing a second payment.

    If the provider proves settlement, the reservation becomes paid and is finalized
    with `delivery_unknown`; the original delivery credential existed only in memory.
    """
    reservation = authorizations.get_reservation(reservation_id)
    if not reservation:
        return None
    if reservation.status == "finalized" and reservation.receipt_id:
        return receipts.get(reservation.receipt_id)
    if reservation.status == "payment_initiated":
        if not reservation.payment_hash:
            return None
        settlement = await payment_provider.lookup_payment(reservation.payment_hash)
        if not settlement:
            return None
        computed = hashlib.sha256(bytes.fromhex(settlement.preimage)).hexdigest()
        if computed.lower() != reservation.payment_hash.lower():
            return None
        reservation = authorizations.mark_paid(
            reservation_id,
            provider=settlement.provider,
            provider_payment_id=settlement.provider_payment_id,
            route_fee_sats=settlement.route_fee_sats,
            preimage_hash=hashlib.sha256(settlement.preimage.encode()).hexdigest(),
            settled_at=settlement.settled_at,
            settlement_evidence=settlement.raw_evidence,
        )
    if reservation.status != "paid" or not reservation.invoice_payload:
        return None
    authorization = authorizations.get(reservation.authorization_id)
    if not authorization:
        return None
    invoice = DecodedInvoice(**reservation.invoice_payload)
    settlement_evidence = {
        "provider": reservation.provider,
        "provider_payment_id": reservation.provider_payment_id,
        "payment_hash": reservation.payment_hash,
        "preimage_hash": reservation.preimage_hash,
        "route_fee_sats": reservation.route_fee_sats or 0,
        "state": "settled",
        "settled_at": reservation.settled_at,
        "raw_evidence": reservation.settlement_evidence or {},
        "reconciled": True,
    }
    delivery = DeliveryEvidence(
        status="delivery_unknown",
        http_status=None,
        content_type=None,
        body_sha256=None,
        body_bytes=0,
        latency_ms=0,
        error="process recovery: payment proven, original delivery evidence unavailable",
    )
    return receipts.finalize_paid_call(
        reservation=reservation,
        authorization=asdict(authorization),
        endpoint="recovery://unknown-original-endpoint",
        invoice=invoice,
        settlement=settlement_evidence,
        delivery=delivery,
    )
