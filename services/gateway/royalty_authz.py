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

import json
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

    __test__ = False

    def __init__(self, principals: Optional[dict[str, AuthorizedPrincipal]] = None):
        self._tokens: dict[str, AuthorizedPrincipal] = principals or {}

    def register(self, token: str, tenant_id: str, role: str) -> None:
        self._tokens[token] = AuthorizedPrincipal(tenant_id=tenant_id, role=role)

    async def authorize(self, bearer_token: str) -> Optional[AuthorizedPrincipal]:
        return self._tokens.get(bearer_token)


def _load_test_principals() -> dict[str, AuthorizedPrincipal]:
    """Load explicit test-only principals from a JSON environment value.

    The value is deliberately ignored unless ROYALTY_TEST_FIXTURES_ENABLED
    is true. This lets subprocess acceptance tests configure principals
    without adding a production token registry or hard-coded credentials.
    """
    raw = os.getenv("ROYALTY_TEST_AUTHZ_PRINCIPALS", "{}")
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise RuntimeError("ROYALTY_TEST_AUTHZ_PRINCIPALS must be valid JSON") from exc
    if not isinstance(parsed, dict):
        raise RuntimeError("ROYALTY_TEST_AUTHZ_PRINCIPALS must be a JSON object")

    principals: dict[str, AuthorizedPrincipal] = {}
    for token, value in parsed.items():
        if not isinstance(token, str) or not isinstance(value, dict):
            raise RuntimeError("test authorization principals must map token strings to objects")
        tenant_id = value.get("tenant_id")
        role = value.get("role")
        if not isinstance(tenant_id, str) or not isinstance(role, str):
            raise RuntimeError("each test authorization principal needs tenant_id and role")
        principals[token] = AuthorizedPrincipal(tenant_id=tenant_id, role=role)
    return principals


def _select_adapter() -> AuthorizationAdapter:
    if ROYALTY_TEST_FIXTURES_ENABLED:
        return TestFixtureAuthorizationAdapter(_load_test_principals())
    return FailClosedAuthorizationAdapter()


authorization_adapter: AuthorizationAdapter = _select_adapter()


class AuthorizationDenied(Exception):
    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


async def resolve_policy_principal(bearer_token: Optional[str]) -> AuthorizedPrincipal:
    """Resolve a policy-admin principal without trusting caller tenant headers."""
    if not bearer_token:
        raise AuthorizationDenied("missing_auth")

    principal = await authorization_adapter.authorize(bearer_token)
    if principal is None:
        raise AuthorizationDenied("invalid_or_unknown_token")

    if principal.role != RELEASE_REQUIRED_ROLE:
        raise AuthorizationDenied("wrong_role")

    return principal


def require_obligation_tenant(
    principal: AuthorizedPrincipal, obligation_tenant_id: str
) -> AuthorizedPrincipal:
    """Bind an already-authorized principal to persisted obligation tenancy."""
    if principal.tenant_id != obligation_tenant_id:
        raise AuthorizationDenied("wrong_tenant")
    return principal


async def authorize_policy_action(
    bearer_token: Optional[str], obligation_tenant_id: str
) -> AuthorizedPrincipal:
    """
    Raises AuthorizationDenied (never returns a falsy/ambiguous result) unless
    the token resolves to a principal with the required role AND whose
    tenant_id matches the PERSISTED obligation's tenant_id.
    """
    principal = await resolve_policy_principal(bearer_token)
    return require_obligation_tenant(principal, obligation_tenant_id)
