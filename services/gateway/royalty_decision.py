"""
Decision engine for the royalty receipt loop. A structurally valid
event must NEVER automatically become Allow — see
spec/ACCEPTANCE-royalty-loop-v1.md AT-07/AT-08/AT-10, and the
architecture review that flagged this exact gap.

Two independent checks, both real:
  - Ownership: does the track's VICS proof actually verify? Production
    can use LyricaVicsOwnershipVerifier, which calls a configured Lyrica
    service-to-service proof endpoint and requires every returned proof
    binding to match the event. Missing configuration, network errors,
    malformed responses, revoked/expired proofs, or mismatches all fail
    CLOSED. Test fixtures remain explicitly opt-in and isolated.
  - Risk: calls the REAL fraud-service's purpose-built
    POST /risk/royalty endpoint (archisynapse_fraud_mvp.py) — not the
    generic /risk/checkout used by the card-payment loop. That model
    scores dna_verified/soulprint_verified/ledger_record_found,
    account/payout age, and usage-spike signals into
    release_payout / delay_payout_72h / hold_payout_review /
    block_payout. If the fraud service is unreachable or errors, that
    is an AMBIGUOUS dependency failure and fails closed to `hold`.

The royalty event schema does not yet carry creator-account-age,
payout-method-age, or usage-velocity signals (Lyrica doesn't transmit
them in v1) — those risk dimensions default to "no signal" values
here. That is a real, flagged limitation, not something this pass
pretends to solve.

Test-only fixtures (a deterministic revoked-proof id, a deterministic
high-risk actor id) exist ONLY to make AT-07/AT-08/AT-10 exercisable
without fabricated production state, live behind the same interfaces
as production, and activate ONLY when ROYALTY_TEST_FIXTURES_ENABLED=true
is set on purpose — never on by default, never silently.
"""

import logging
import os
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional, Protocol

import httpx

FRAUD_SERVICE_URL = os.getenv("FRAUD_SERVICE_URL", "http://127.0.0.1:8080")
ROYALTY_TEST_FIXTURES_ENABLED = os.getenv("ROYALTY_TEST_FIXTURES_ENABLED", "false").lower() == "true"
LYRICA_VICS_VERIFIER_ENABLED = os.getenv("LYRICA_VICS_VERIFIER_ENABLED", "false").lower() == "true"
LYRICA_VICS_VERIFY_URL = os.getenv("LYRICA_VICS_VERIFY_URL", "").strip()
LYRICA_VICS_SERVICE_TOKEN = os.getenv("LYRICA_VICS_SERVICE_TOKEN", "").strip()

try:
    LYRICA_VICS_VERIFY_TIMEOUT_SECONDS = float(os.getenv("LYRICA_VICS_VERIFY_TIMEOUT_SECONDS", "5"))
except ValueError:
    LYRICA_VICS_VERIFY_TIMEOUT_SECONDS = 5.0

logger = logging.getLogger("archisynapse.royalty_decision")

_DECISION_MAP = {
    "release_payout": "allow",
    "delay_payout_72h": "hold",
    "hold_payout_review": "hold",
    "block_payout": "block",
}

# In-memory cache of tenant_id -> fraud-service api_key for this process's
# lifetime. Bootstrapping (not credential storage) — the fraud service
# itself is the source of truth; losing this cache just means one extra
# bootstrap call on next use, no data loss.
_fraud_merchant_cache: dict[str, str] = {}


@dataclass
class Decision:
    outcome: str  # "allow" | "hold" | "block"
    policy: str
    risk_score: float
    reasons: list[str]


class OwnershipVerifier(Protocol):
    async def verify(
        self,
        track_id: str,
        dna_tag: str,
        soulprint_hash: str,
        vics_proof_id: str,
        creator_id: str,
    ) -> bool:
        ...


class FailClosedOwnershipVerifier:
    """Production-safe fallback: unavailable ownership proof is never valid."""

    async def verify(
        self,
        track_id: str,
        dna_tag: str,
        soulprint_hash: str,
        vics_proof_id: str,
        creator_id: str,
    ) -> bool:
        return False


class LyricaVicsOwnershipVerifier:
    """Verify a Lyrica proof through an authenticated service endpoint.

    Expected response contract::

        {
          "verified": true,
          "revoked": false,
          "track_id": "trk_...",
          "dna_tag": "dna_...",
          "soulprint_hash": "sp_sha256_...",
          "vics_proof_id": "vics_...",
          "creator_id": "cre_...",
          "expires_at": "2026-12-31T23:59:59Z"  // optional
        }

    The response must bind every identity/proof field to the incoming event.
    A truthy status without exact binding is rejected.
    """

    def __init__(
        self,
        verify_url: str,
        service_token: str,
        timeout_seconds: float = 5.0,
        transport: Optional[httpx.AsyncBaseTransport] = None,
    ):
        self.verify_url = verify_url.strip()
        self.service_token = service_token.strip()
        self.timeout_seconds = timeout_seconds
        self.transport = transport

    @staticmethod
    def _not_expired(expires_at: object) -> bool:
        if expires_at in (None, ""):
            return True
        if not isinstance(expires_at, str):
            return False
        try:
            expiry = datetime.fromisoformat(expires_at.replace("Z", "+00:00"))
        except ValueError:
            return False
        if expiry.tzinfo is None:
            expiry = expiry.replace(tzinfo=timezone.utc)
        return expiry > datetime.now(timezone.utc)

    async def verify(
        self,
        track_id: str,
        dna_tag: str,
        soulprint_hash: str,
        vics_proof_id: str,
        creator_id: str,
    ) -> bool:
        if not self.verify_url or not self.service_token:
            return False

        request_body = {
            "track_id": track_id,
            "dna_tag": dna_tag,
            "soulprint_hash": soulprint_hash,
            "vics_proof_id": vics_proof_id,
            "creator_id": creator_id,
        }
        headers = {
            "Authorization": f"Bearer {self.service_token}",
            "X-Empire1-Service": "archisynapse-v2",
            "Content-Type": "application/json",
        }

        try:
            async with httpx.AsyncClient(
                timeout=self.timeout_seconds,
                transport=self.transport,
            ) as client:
                response = await client.post(self.verify_url, json=request_body, headers=headers)
            if response.status_code != 200:
                return False
            payload = response.json()
        except (httpx.HTTPError, ValueError, TypeError):
            return False

        if not isinstance(payload, dict):
            return False
        if payload.get("verified") is not True or payload.get("revoked") is True:
            return False

        expected_bindings = {
            "track_id": track_id,
            "dna_tag": dna_tag,
            "soulprint_hash": soulprint_hash,
            "vics_proof_id": vics_proof_id,
            "creator_id": creator_id,
        }
        if any(payload.get(field) != expected for field, expected in expected_bindings.items()):
            return False
        if not self._not_expired(payload.get("expires_at")):
            return False

        return True


class TestFixtureOwnershipVerifier:
    """TEST-ONLY — see module docstring. Never selected unless explicitly enabled."""

    REVOKED_PROOF_IDS = {"vics_revoked_test_fixture"}

    async def verify(
        self,
        track_id: str,
        dna_tag: str,
        soulprint_hash: str,
        vics_proof_id: str,
        creator_id: str,
    ) -> bool:
        return vics_proof_id not in self.REVOKED_PROOF_IDS


class _RiskFixture:
    """TEST-ONLY high-risk actor list — see module docstring."""

    HIGH_RISK_ACTOR_IDS = {"usr_risk_test_fixture"}


def _select_ownership_verifier() -> OwnershipVerifier:
    if ROYALTY_TEST_FIXTURES_ENABLED:
        return TestFixtureOwnershipVerifier()
    if LYRICA_VICS_VERIFIER_ENABLED:
        if not LYRICA_VICS_VERIFY_URL or not LYRICA_VICS_SERVICE_TOKEN:
            logger.error(
                "LYRICA_VICS_VERIFIER_ENABLED=true but URL/token configuration is incomplete; failing closed"
            )
            return FailClosedOwnershipVerifier()
        return LyricaVicsOwnershipVerifier(
            verify_url=LYRICA_VICS_VERIFY_URL,
            service_token=LYRICA_VICS_SERVICE_TOKEN,
            timeout_seconds=LYRICA_VICS_VERIFY_TIMEOUT_SECONDS,
        )
    return FailClosedOwnershipVerifier()


ownership_verifier: OwnershipVerifier = _select_ownership_verifier()


async def _ensure_fraud_merchant(client: httpx.AsyncClient, tenant_id: str) -> str:
    if tenant_id in _fraud_merchant_cache:
        return _fraud_merchant_cache[tenant_id]

    response = await client.post(
        f"{FRAUD_SERVICE_URL}/admin/merchants",
        json={"merchant_id": tenant_id, "name": f"Royalty tenant {tenant_id}"},
    )
    if response.status_code == 200:
        api_key = response.json()["api_key"]
        _fraud_merchant_cache[tenant_id] = api_key
        return api_key
    if response.status_code == 400:
        # Already exists from a prior process/run -- there is no
        # "get existing api key" endpoint, so a fresh key cannot be
        # recovered here. This is a real v1 limitation: merchant
        # bootstrap is not idempotent across gateway restarts.
        raise RuntimeError(f"fraud merchant {tenant_id} already exists and its key is not recoverable")
    raise RuntimeError(f"fraud merchant bootstrap failed: {response.status_code} {response.text}")


async def evaluate_decision(
    tenant_id: str,
    track_id: str,
    dna_tag: str,
    soulprint_hash: str,
    vics_proof_id: str,
    creator_id: str,
    idempotency_key: str,
    trigger_actor_id: str,
    amount: str,
) -> Decision:
    ownership_ok = await ownership_verifier.verify(
        track_id,
        dna_tag,
        soulprint_hash,
        vics_proof_id,
        creator_id,
    )
    if not ownership_ok:
        return Decision(outcome="block", policy="ownership_invalid", risk_score=1.0, reasons=["vics_invalid"])

    is_high_risk_fixture = ROYALTY_TEST_FIXTURES_ENABLED and trigger_actor_id in _RiskFixture.HIGH_RISK_ACTOR_IDS
    # sudden_usage_spike alone (+25) doesn't cross even the lowest real
    # threshold (40). The fixture needs to look like a genuinely risky
    # payout -- new account + new payout method + a usage spike -- to
    # actually exercise the hold path, not just nudge the score.
    creator_account_age_days = 0 if is_high_risk_fixture else 30
    payout_method_age_days = 0 if is_high_risk_fixture else 30

    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            api_key = await _ensure_fraud_merchant(client, tenant_id)
            response = await client.post(
                f"{FRAUD_SERVICE_URL}/risk/royalty",
                json={
                    "event_type": "royalty_payout_request",
                    "creator_id": creator_id,
                    "track_id": track_id,
                    "user_id": trigger_actor_id,
                    "amount": float(amount),
                    "currency": "USD",
                    # Ownership already gated above -> these reflect that.
                    "dna_verified": True,
                    "soulprint_verified": True,
                    "ledger_record_found": True,
                    # Not yet transmitted by the royalty event schema --
                    # "no signal" defaults, overridable only by test fixtures.
                    "creator_account_age_days": creator_account_age_days,
                    "payout_method_age_days": payout_method_age_days,
                    "sudden_usage_spike": is_high_risk_fixture,
                },
                headers={"X-Api-Key": api_key, "Idempotency-Key": idempotency_key},
            )
        if response.status_code != 200:
            return Decision(
                outcome="hold", policy="fraud_service_error", risk_score=0.5, reasons=["fraud_service_error"]
            )
        payload = response.json()
        fraud_decision = payload.get("decision", "block_payout")
        risk_score = payload.get("risk_score", 100) / 100.0
        reasons = payload.get("reasons", [])
    except (httpx.RequestError, RuntimeError):
        return Decision(
            outcome="hold", policy="fraud_service_unavailable", risk_score=0.5, reasons=["fraud_service_unreachable"]
        )

    outcome = _DECISION_MAP.get(fraud_decision, "block")
    policy = "allow" if outcome == "allow" else f"fraud_{fraud_decision}"
    return Decision(outcome=outcome, policy=policy, risk_score=risk_score, reasons=reasons)
