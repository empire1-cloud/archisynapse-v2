from __future__ import annotations

import pytest

from agent_commerce.errors import IdentityConfigurationError
from agent_commerce.identity import AgentProfileSigner, validate_public_endpoint


@pytest.mark.parametrize(
    "url",
    [
        "http://localhost:3001/api/task",
        "http://127.0.0.1:3001/api/task",
        "https://10.0.0.1/task",
        "https://169.254.169.254/latest/meta-data",
        "https://user:pass@example.com/task",
    ],
)
def test_private_or_credentialed_endpoints_are_rejected(url):
    with pytest.raises(IdentityConfigurationError):
        validate_public_endpoint(url, production=True)


def test_production_requires_https():
    with pytest.raises(IdentityConfigurationError):
        validate_public_endpoint("http://agents.example.com/task", production=True)


def test_signed_profile_detects_tampering():
    signer = AgentProfileSigner("p" * 32)
    profile = signer.build_profile(
        npub="npub123",
        name="Researcher",
        specialty="research",
        price_sats=25,
        endpoint="https://agents.example.com/api/task",
        production=True,
        capabilities=["web", "citations"],
    )
    assert signer.verify_profile(profile, production=True)
    tampered = {**profile, "price_sats": 1}
    assert not signer.verify_profile(tampered, production=True)
