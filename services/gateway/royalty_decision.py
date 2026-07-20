"""
Decision engine for the royalty receipt loop. A structurally valid
event must NEVER automatically become Allow — see
spec/ACCEPTANCE-royalty-loop-v1.md AT-07/AT-08/AT-10, and the
architecture review that flagged this exact gap.

Two independent checks, both real:
  - Ownership: does the track's VICS proof actually verify? No real
    VICS service exists yet, so the default OwnershipVerifier fails
    CLOSED (every event is unverified until a real backend is wired
    in) — it never treats "the fields are present" as "the proof is
    valid". A failed ownership check is a hard 422 (request-level
    rejection), separate from the fraud-service's business decision.
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
without a real VICS backend or fabricated account history, live behind
the same interfaces as production, and activate ONLY when
ROYALTY_TEST_FIXTURES_ENABLED=true is set on purpose — never on by
default, never silently.
"""

import os
from dataclasses import dataclass
from typing import Protocol

import httpx

FRAUD_SERVICE_URL = os.getenv("FRAUD_SERVICE_URL", "http://127.0.0.1:8080")
ROYALTY_TEST_FIXTURES_ENABLED = os.getenv("ROYALTY_TEST_FIXTURES_ENABLED", "false").lower() == "true"

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
    async def verify(self, track_id: str, dna_tag: str, soulprint_hash: str, vics_proof_id: str) -> bool:
        ...


class FailClosedOwnershipVerifier:
    """Production default. No real VICS backend exists yet -> always unverified."""

    async def verify(self, track_id: str, dna_tag: str, soulprint_hash: str, vics_proof_id: str) -> bool:
        return False


class TestFixtureOwnershipVerifier:
    """TEST-ONLY — see module docstring. Never selected unless explicitly enabled."""

    REVOKED_PROOF_IDS = {"vics_revoked_test_fixture"}

    async def verify(self, track_id: str, dna_tag: str, soulprint_hash: str, vics_proof_id: str) -> bool:
        return vics_proof_id not in self.REVOKED_PROOF_IDS


class _RiskFixture:
    """TEST-ONLY high-risk actor list — see module docstring."""

    HIGH_RISK_ACTOR_IDS = {"usr_risk_test_fixture"}


def _select_ownership_verifier() -> OwnershipVerifier:
    if ROYALTY_TEST_FIXTURES_ENABLED:
        return TestFixtureOwnershipVerifier()
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
    ownership_ok = await ownership_verifier.verify(track_id, dna_tag, soulprint_hash, vics_proof_id)
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
