"""
FAST INNER LOOP — pure unit tests, no network, no Postgres, no
subprocesses. Pre-commit gate. Covers schema validation (AT-01 partial,
AT-03, AT-15 partial), signature verification (AT-05/06 mechanics),
and TenantResolver — the parts of the loop that don't need a live
service to verify correctly.

Run: pytest services/gateway/test_royalty_fast_suite.py
"""

import asyncio

import pytest
from pydantic import ValidationError

from royalty_events import RoyaltyObligationCreated
from royalty_authz import (
    AuthorizationDenied,
    TestFixtureAuthorizationAdapter,
    authorize_policy_action,
)
import royalty_authz
from royalty_keys import generate_tenant_keypair, sign_with_private_key, verify_event_signature
from royalty_tenant_resolver import IdentityTenantResolver
from royalty_signing_keys import InMemorySigningKeyProvider, SigningKeyUnavailable


def _valid_event_dict(**overrides):
    base = {
        "schema_version": "1.0",
        "event_id": "evt_1",
        "event_type": "royalty.obligation.created",
        "occurred_at": "2026-07-20T18:00:00Z",
        "correlation_id": "corr_1",
        "idempotency_key": "idem_1",
        "tenant_id": "lyrica",
        "track": {
            "track_id": "trk_1",
            "dna_tag": "dna1",
            "soulprint_hash": "sp1",
            "vics_proof": {"proof_id": "vics_1", "issued_at": "2026-07-01T12:00:00Z", "chain_ref": "ref"},
        },
        "creator": {"creator_id": "cre_1", "identity_ref": "ref"},
        "splits": [{"owner_id": "cre_1", "bps": 10000}],
        "trigger": {"kind": "remix", "source_ref": "ref", "actor_id": "usr_1"},
        "amount": {"currency": "USD", "value": "1.2500"},
    }
    base.update(overrides)
    return base


def test_valid_event_parses():
    event = RoyaltyObligationCreated.model_validate(_valid_event_dict())
    assert event.event_id == "evt_1"


def test_unknown_top_level_fields_are_ignored_not_rejected():
    body = _valid_event_dict()
    body["some_future_field"] = {"nested": "value"}
    event = RoyaltyObligationCreated.model_validate(body)
    assert not hasattr(event, "some_future_field")


def test_splits_must_sum_to_exactly_10000():
    body = _valid_event_dict(splits=[{"owner_id": "a", "bps": 5000}])
    with pytest.raises(ValidationError):
        RoyaltyObligationCreated.model_validate(body)


def test_amount_must_have_exactly_four_decimal_places():
    for bad_value in ("1.25", "1.250", "1.25000", "1", "abc"):
        body = _valid_event_dict(amount={"currency": "USD", "value": bad_value})
        with pytest.raises(ValidationError):
            RoyaltyObligationCreated.model_validate(body)


def test_trigger_kind_must_be_known_value():
    body = _valid_event_dict(trigger={"kind": "stream", "source_ref": "ref", "actor_id": "usr_1"})
    with pytest.raises(ValidationError):
        RoyaltyObligationCreated.model_validate(body)


def test_signature_round_trip_valid():
    priv, pub = generate_tenant_keypair()
    body = b'{"event_id":"evt_1"}'
    sig = sign_with_private_key(priv, body)
    assert verify_event_signature(body, sig, pub) is True


def test_signature_rejects_tampered_body():
    priv, pub = generate_tenant_keypair()
    sig = sign_with_private_key(priv, b'{"event_id":"evt_1"}')
    assert verify_event_signature(b'{"event_id":"evt_2"}', sig, pub) is False


def test_signature_rejects_wrong_key():
    priv_a, _ = generate_tenant_keypair()
    _, pub_b = generate_tenant_keypair()
    body = b'{"event_id":"evt_1"}'
    sig = sign_with_private_key(priv_a, body)
    assert verify_event_signature(body, sig, pub_b) is False


def test_signature_verify_never_raises_on_malformed_header():
    _, pub = generate_tenant_keypair()
    assert verify_event_signature(b"body", "not-a-valid-header", pub) is False
    assert verify_event_signature(b"body", "ed25519=not-base64!!!", pub) is False
    assert verify_event_signature(b"body", "", pub) is False


def test_identity_tenant_resolver_is_passthrough():
    resolver = IdentityTenantResolver()
    assert resolver.resolve("lyrica") == "lyrica"
    assert resolver.resolve("mer_int_1234") == "mer_int_1234"


def test_release_authorization_binds_role_and_persisted_tenant(monkeypatch):
    adapter = TestFixtureAuthorizationAdapter()
    adapter.register("good", "lyrica", "policy_admin")
    adapter.register("wrong-role", "lyrica", "viewer")
    adapter.register("wrong-tenant", "other", "policy_admin")
    monkeypatch.setattr(royalty_authz, "authorization_adapter", adapter)

    principal = asyncio.run(authorize_policy_action("good", "lyrica"))
    assert principal.tenant_id == "lyrica"

    for token, reason in (
        (None, "missing_auth"),
        ("unknown", "invalid_or_unknown_token"),
        ("wrong-role", "wrong_role"),
        ("wrong-tenant", "wrong_tenant"),
    ):
        with pytest.raises(AuthorizationDenied) as exc:
            asyncio.run(authorize_policy_action(token, "lyrica"))
        assert exc.value.reason == reason


def test_outbox_signing_provider_uses_reference_not_database_key():
    provider = InMemorySigningKeyProvider({"test://key": "private-material"})
    assert asyncio.run(provider.resolve_private_key("test://key")) == "private-material"
    with pytest.raises(SigningKeyUnavailable):
        asyncio.run(provider.resolve_private_key("test://missing"))
