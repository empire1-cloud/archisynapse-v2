from __future__ import annotations

import hashlib
import re
import time
from dataclasses import asdict
from typing import Protocol
from urllib.parse import quote

import httpx

from .authorization import AuthorizationStore
from .errors import InvalidInvoice, PaymentFailed, PriceMismatch
from .models import DecodedInvoice, DeliveryEvidence, PaymentReceipt, PaymentSettlement
from .receipts import ReceiptStore


AUTH_PARAM_RE = re.compile(r'(macaroon|invoice)="([^"]+)"', re.IGNORECASE)


def parse_l402_challenge(header: str) -> tuple[str, str]:
    if not header or not header.lstrip().upper().startswith("L402"):
        raise InvalidInvoice("missing L402 WWW-Authenticate challenge")
    values = {key.lower(): value for key, value in AUTH_PARAM_RE.findall(header)}
    if not values.get("macaroon") or not values.get("invoice"):
        raise InvalidInvoice("L402 challenge is missing macaroon or invoice")
    if len(values["macaroon"]) > 4096 or len(values["invoice"]) > 8192:
        raise InvalidInvoice("L402 challenge fields exceed safety limits")
    return values["macaroon"], values["invoice"]


class PaymentProvider(Protocol):
    name: str

    async def decode_invoice(self, payment_request: str) -> DecodedInvoice: ...

    async def pay_invoice(self, payment_request: str) -> PaymentSettlement: ...

    async def lookup_payment(self, payment_hash: str) -> PaymentSettlement | None: ...


class LndRestPaymentProvider:
    name = "lnd-rest"

    def __init__(
        self,
        *,
        base_url: str,
        macaroon_hex: str,
        verify_tls: bool = True,
        timeout_seconds: float = 120,
        client: httpx.AsyncClient | None = None,
    ):
        if not base_url or not macaroon_hex:
            raise ValueError("LND base_url and macaroon are required")
        self.base_url = base_url.rstrip("/")
        self.macaroon_hex = macaroon_hex
        self.verify_tls = verify_tls
        self.timeout_seconds = timeout_seconds
        self._client = client

    def _headers(self) -> dict[str, str]:
        return {"Grpc-Metadata-macaroon": self.macaroon_hex, "Content-Type": "application/json"}

    async def _request(self, method: str, path: str, **kwargs) -> httpx.Response:
        if self._client:
            response = await self._client.request(method, f"{self.base_url}{path}", headers=self._headers(), **kwargs)
        else:
            async with httpx.AsyncClient(verify=self.verify_tls, timeout=self.timeout_seconds) as client:
                response = await client.request(method, f"{self.base_url}{path}", headers=self._headers(), **kwargs)
        if response.status_code >= 400:
            raise PaymentFailed(f"LND request failed with status {response.status_code}")
        return response

    async def decode_invoice(self, payment_request: str) -> DecodedInvoice:
        response = await self._request("GET", f"/v1/payreq/{quote(payment_request, safe='')}")
        try:
            payload = response.json()
            amount = int(payload["num_satoshis"])
            payment_hash = str(payload["payment_hash"])
        except (KeyError, TypeError, ValueError) as exc:
            raise InvalidInvoice("LND returned an invalid invoice decode response") from exc
        if amount <= 0 or len(payment_hash) != 64:
            raise InvalidInvoice("zero-amount and malformed invoices are forbidden")
        expires_at = None
        try:
            expires_at = str(int(payload["timestamp"]) + int(payload["expiry"]))
        except (KeyError, TypeError, ValueError):
            pass
        return DecodedInvoice(
            payment_request_sha256=hashlib.sha256(payment_request.encode()).hexdigest(),
            amount_sats=amount,
            payment_hash=payment_hash.lower(),
            description=payload.get("description"),
            expires_at=expires_at,
            raw={
                "destination": payload.get("destination"),
                "cltv_expiry": payload.get("cltv_expiry"),
                "expiry": payload.get("expiry"),
                "timestamp": payload.get("timestamp"),
            },
        )

    async def pay_invoice(self, payment_request: str) -> PaymentSettlement:
        response = await self._request(
            "POST",
            "/v1/channels/transactions",
            json={"payment_request": payment_request},
        )
        payload = response.json()
        if payload.get("payment_error"):
            raise PaymentFailed(f"LND payment failed: {payload['payment_error']}")
        preimage_b64 = payload.get("payment_preimage")
        payment_hash_b64 = payload.get("payment_hash")
        if not preimage_b64:
            raise PaymentFailed("LND reported success without a preimage")
        import base64

        try:
            preimage = base64.b64decode(preimage_b64).hex()
            payment_hash = (
                base64.b64decode(payment_hash_b64).hex()
                if payment_hash_b64
                else hashlib.sha256(bytes.fromhex(preimage)).hexdigest()
            )
        except (ValueError, TypeError) as exc:
            raise PaymentFailed("LND returned malformed settlement evidence") from exc
        route = payload.get("payment_route") or {}
        fee_msat = int(route.get("total_fees_msat") or 0)
        fee_sats = (fee_msat + 999) // 1000
        return PaymentSettlement(
            provider=self.name,
            provider_payment_id=payment_hash,
            payment_hash=payment_hash,
            preimage=preimage,
            route_fee_sats=fee_sats,
            raw_evidence={"payment_route": route},
        )

    async def lookup_payment(self, payment_hash: str) -> PaymentSettlement | None:
        try:
            response = await self._request("GET", f"/v2/router/track/{payment_hash}")
        except PaymentFailed:
            return None
        payload = response.json()
        if payload.get("status") != "SUCCEEDED":
            return None
        preimage = str(payload.get("payment_preimage") or "")
        if len(preimage) != 64:
            return None
        fee_msat = int(payload.get("fee_msat") or 0)
        return PaymentSettlement(
            provider=self.name,
            provider_payment_id=payment_hash,
            payment_hash=payment_hash,
            preimage=preimage,
            route_fee_sats=(fee_msat + 999) // 1000,
            raw_evidence={"lookup_status": payload.get("status")},
        )


class AgentCommerceService:
    def __init__(
        self,
        *,
        authorizations: AuthorizationStore,
        receipts: ReceiptStore,
        payment_provider: PaymentProvider,
        http_client: httpx.AsyncClient | None = None,
        price_tolerance: float = 0.10,
        initial_timeout_seconds: float = 10,
        delivery_timeout_seconds: float = 90,
        max_response_bytes: int = 1_000_000,
    ):
        self.authorizations = authorizations
        self.receipts = receipts
        self.payment_provider = payment_provider
        self.http_client = http_client
        self.price_tolerance = price_tolerance
        self.initial_timeout_seconds = initial_timeout_seconds
        self.delivery_timeout_seconds = delivery_timeout_seconds
        self.max_response_bytes = max_response_bytes

    async def _post(self, url: str, *, json_body: dict, headers: dict | None, timeout: float) -> httpx.Response:
        if self.http_client:
            return await self.http_client.post(url, json=json_body, headers=headers, timeout=timeout)
        async with httpx.AsyncClient(timeout=timeout, follow_redirects=False) as client:
            return await client.post(url, json=json_body, headers=headers)

    async def execute(
        self,
        *,
        authorization_id: str,
        idempotency_key: str,
        orchestration_id: str,
        agent_npub: str,
        specialty: str,
        endpoint: str,
        quoted_sats: int,
        query: str,
        context: str | None = None,
    ) -> tuple[PaymentReceipt, bytes]:
        if not query or len(query) > 2000:
            raise ValueError("query must be 1-2000 characters")
        if context is not None and len(context) > 4000:
            raise ValueError("context must be at most 4000 characters")
        initial_body = {"query": query, **({"context": context} if context else {})}
        initial = await self._post(endpoint, json_body=initial_body, headers=None, timeout=self.initial_timeout_seconds)
        if initial.status_code != 402:
            raise InvalidInvoice(f"paid agent endpoint must return 402 before service, got {initial.status_code}")
        macaroon, payment_request = parse_l402_challenge(initial.headers.get("WWW-Authenticate", ""))
        invoice = await self.payment_provider.decode_invoice(payment_request)
        max_advertised = int(quoted_sats * (1 + self.price_tolerance) + 0.999999)
        if invoice.amount_sats > max_advertised:
            raise PriceMismatch(
                f"invoice asks {invoice.amount_sats} sats but advertised ceiling is {max_advertised}"
            )

        reservation = self.authorizations.reserve(
            authorization_id=authorization_id,
            idempotency_key=idempotency_key,
            orchestration_id=orchestration_id,
            agent_npub=agent_npub,
            specialty=specialty,
            quoted_sats=quoted_sats,
            advertised_tolerance=self.price_tolerance,
        )
        if reservation.status == "finalized" and reservation.receipt_id:
            receipt = self.receipts.get(reservation.receipt_id)
            if not receipt:
                raise PaymentFailed("finalized reservation is missing its receipt")
            return receipt, b""
        try:
            if reservation.status in ("payment_initiated", "paid"):
                raise PaymentFailed(
                    "idempotent call already reached payment state; reconcile the existing payment instead of paying again"
                )
            reservation = self.authorizations.bind_invoice(reservation.id, invoice=invoice)
            if reservation.status == "invoice_bound":
                reservation = self.authorizations.mark_payment_initiated(reservation.id)
            settlement = await self.payment_provider.pay_invoice(payment_request)
            if settlement.payment_hash.lower() != invoice.payment_hash.lower():
                raise PaymentFailed("provider settlement hash does not match decoded invoice")
            try:
                computed_hash = hashlib.sha256(bytes.fromhex(settlement.preimage)).hexdigest()
            except ValueError as exc:
                raise PaymentFailed("provider returned a malformed preimage") from exc
            if computed_hash.lower() != invoice.payment_hash.lower():
                raise PaymentFailed("settlement preimage does not prove the invoice payment hash")
            preimage_hash = hashlib.sha256(settlement.preimage.encode()).hexdigest()
            reservation = self.authorizations.mark_paid(
                reservation.id,
                provider=settlement.provider,
                provider_payment_id=settlement.provider_payment_id,
                route_fee_sats=settlement.route_fee_sats,
                preimage_hash=preimage_hash,
                settled_at=settlement.settled_at,
                settlement_evidence=settlement.raw_evidence,
            )
        except Exception as exc:
            current = self.authorizations.get_reservation(reservation.id)
            if current and current.status not in ("paid", "finalized", "payment_initiated"):
                self.authorizations.release(
                    reservation.id,
                    failure_code=getattr(exc, "code", "payment_pre_settlement_failure"),
                    failure_message=str(exc),
                )
            raise

        start = time.monotonic()
        delivery_error = None
        try:
            response = await self._post(
                endpoint,
                json_body=initial_body,
                headers={"Authorization": f"L402 {macaroon}:{settlement.preimage}"},
                timeout=self.delivery_timeout_seconds,
            )
            body = response.content
            if len(body) > self.max_response_bytes:
                body = body[: self.max_response_bytes]
                delivery_error = "response exceeded max_response_bytes and was truncated"
            delivered = 200 <= response.status_code < 300 and delivery_error is None
            delivery = DeliveryEvidence(
                status="delivered" if delivered else "delivery_failed",
                http_status=response.status_code,
                content_type=response.headers.get("content-type"),
                body_sha256=hashlib.sha256(body).hexdigest(),
                body_bytes=len(body),
                latency_ms=int((time.monotonic() - start) * 1000),
                error=delivery_error or (None if delivered else f"HTTP {response.status_code}"),
            )
        except Exception as exc:
            body = b""
            delivery = DeliveryEvidence(
                status="delivery_failed",
                http_status=None,
                content_type=None,
                body_sha256=None,
                body_bytes=0,
                latency_ms=int((time.monotonic() - start) * 1000),
                error=str(exc)[:500],
            )

        authorization = self.authorizations.get(authorization_id)
        if not authorization:
            raise PaymentFailed("authorization missing after settlement")
        settlement_evidence = {
            "provider": settlement.provider,
            "provider_payment_id": settlement.provider_payment_id,
            "payment_hash": settlement.payment_hash,
            "preimage_hash": hashlib.sha256(settlement.preimage.encode()).hexdigest(),
            "route_fee_sats": settlement.route_fee_sats,
            "state": settlement.state,
            "settled_at": settlement.settled_at,
            "raw_evidence": settlement.raw_evidence,
        }
        receipt = self.receipts.finalize_paid_call(
            reservation=reservation,
            authorization=asdict(authorization),
            endpoint=endpoint,
            invoice=invoice,
            settlement=settlement_evidence,
            delivery=delivery,
        )
        return receipt, body
