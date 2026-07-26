"""Focused tests for the production Lyrica VICS ownership verifier.

Run:
    pytest services/gateway/test_lyrica_vics_verifier.py

These tests use httpx.MockTransport only. They do not call Lyrica, Postgres,
the fraud service, or the ledger.
"""

import asyncio
import json

import httpx

from royalty_decision import LyricaVicsOwnershipVerifier


EXPECTED = {
    "track_id": "trk_9f3a2b1c",
    "dna_tag": "dna_v2_7c1e",
    "soulprint_hash": "sp_sha256_4b09",
    "vics_proof_id": "vics_01H",
    "creator_id": "cre_a1b2c3",
}


def _run(verifier: LyricaVicsOwnershipVerifier) -> bool:
    return asyncio.run(verifier.verify(**EXPECTED))


def _transport(payload: object, status_code: int = 200):
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "POST"
        assert request.url == httpx.URL("https://lyrica.example/internal/vics/verify")
        assert request.headers["authorization"] == "Bearer svc-secret"
        assert request.headers["x-empire1-service"] == "archisynapse-v2"
        assert json.loads(request.content) == EXPECTED
        return httpx.Response(status_code, json=payload)

    return httpx.MockTransport(handler)


def _valid_payload(**overrides):
    payload = {
        "verified": True,
        "revoked": False,
        **EXPECTED,
    }
    payload.update(overrides)
    return payload


def _verifier(transport: httpx.AsyncBaseTransport):
    return LyricaVicsOwnershipVerifier(
        verify_url="https://lyrica.example/internal/vics/verify",
        service_token="svc-secret",
        timeout_seconds=1.0,
        transport=transport,
    )


def test_valid_exactly_bound_proof_is_accepted():
    assert _run(_verifier(_transport(_valid_payload()))) is True


def test_revoked_proof_is_rejected():
    assert _run(_verifier(_transport(_valid_payload(revoked=True)))) is False


def test_mismatched_dna_is_rejected():
    assert _run(_verifier(_transport(_valid_payload(dna_tag="dna_other")))) is False


def test_mismatched_soulprint_is_rejected():
    assert _run(_verifier(_transport(_valid_payload(soulprint_hash="sp_other")))) is False


def test_mismatched_creator_is_rejected():
    assert _run(_verifier(_transport(_valid_payload(creator_id="cre_other")))) is False


def test_expired_proof_is_rejected():
    payload = _valid_payload(expires_at="2020-01-01T00:00:00Z")
    assert _run(_verifier(_transport(payload))) is False


def test_non_200_response_fails_closed():
    assert _run(_verifier(_transport({"detail": "unavailable"}, status_code=503))) is False


def test_missing_configuration_fails_closed_without_network_call():
    verifier = LyricaVicsOwnershipVerifier(verify_url="", service_token="")
    assert _run(verifier) is False


def test_malformed_json_fails_closed():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"not-json", headers={"content-type": "application/json"})

    verifier = _verifier(httpx.MockTransport(handler))
    assert _run(verifier) is False


def test_timeout_fails_closed():
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("proof service timed out", request=request)

    verifier = _verifier(httpx.MockTransport(handler))
    assert _run(verifier) is False
