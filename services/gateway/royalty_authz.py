"""
Authorization for release/reversal — spec/SPEC-royalty-loop-v1.md §5:
"authorized via SLA113 policy role only". A caller-supplied
X-Tenant-Id header is NOT authorization (anyone can set a header) —
this module resolves a bearer token to a real principal and the
orchestrator checks that principal against the PERSISTED obligation's
actual tenant_id (looked up under the resolved tenant, not the
caller's claim), so a token for tenant A can never touch tenant B's
obligation regardless of what the caller asserts.

FailClosedAuthorizationAdapter is the production default: no real
SLA113 integration exists yet, so every token is denied. A caller
gets 403, never a silent allow. TestFixtureAuthorizationAdapter is
explicitly test-only, gated behind ROYALTY_TEST_FIXTURES_ENABLED,
and holds a small fixed token->principal map — never activates by
accident.
"""

import os
from dataclasses import dataclass
from typing import Optional, Protocol

ROYALTY_TEST_FIXTURES_ENABLED = os.getenv("ROYALTY_TEST_FIXTURES_ENABLED", "false").lower() == "true"
RELEASE_REQUIRED_ROLE = "policy_admin"


@dataclass
class AuthorizedPrincipal:
    tenant_id: str
    role: str


class AuthorizationAdapter(Protocol):
    async def authorize(self, bearer_token: str) -> Optional[AuthorizedPrincipal]:
        """Returns the resolved principal, or None if the token is invalid/unknown."""
        ...


class FailClosedAuthorizationAdapter:
    """Production default. No real SLA113 backend exists yet -> deny everything."""

    async def authorize(self, bearer_token: str) -> Optional[AuthorizedPrincipal]:
        return None


class TestFixtureAuthorizationAdapter:
    """TEST-ONLY — see module docstring. Never selected unless explicitly enabled."""

    def __init__(self):
        self._tokens: dict[str, AuthorizedPrincipal] = {}

    def register(self, token: str, tenant_id: str, role: str) -> None:
        self._tokens[token] = AuthorizedPrincipal(tenant_id=tenant_id, role=role)

    async def authorize(self, bearer_token: str) -> Optional[AuthorizedPrincipal]:
        return self._tokens.get(bearer_token)


def _select_adapter() -> AuthorizationAdapter:
    if ROYALTY_TEST_FIXTURES_ENABLED:
        return TestFixtureAuthorizationAdapter()
    return FailClosedAuthorizationAdapter()


authorization_adapter: AuthorizationAdapter = _select_adapter()


class AuthorizationDenied(Exception):
    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


async def authorize_policy_action(bearer_token: Optional[str], obligation_tenant_id: str) -> AuthorizedPrincipal:
    """
    Raises AuthorizationDenied (never returns a falsy/ambiguous result) unless
    the token resolves to a principal with the required role AND whose
    tenant_id matches the PERSISTED obligation's tenant_id.
    """
    if not bearer_token:
        raise AuthorizationDenied("missing_auth")

    principal = await authorization_adapter.authorize(bearer_token)
    if principal is None:
        raise AuthorizationDenied("invalid_or_unknown_token")

    if principal.role != RELEASE_REQUIRED_ROLE:
        raise AuthorizationDenied("wrong_role")

    if principal.tenant_id != obligation_tenant_id:
        raise AuthorizationDenied("wrong_tenant")

    return principal
