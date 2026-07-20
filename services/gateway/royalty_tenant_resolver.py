"""
TenantResolver: maps a royalty event's tenant_id to the organization_id
used when calling the transaction service.

v1 default (IdentityTenantResolver) is organization_id == tenant_id.
That is correct, not a stopgap: the transaction service's
organization_id column is TEXT (migration 003 converted it specifically
because merchant/tenant ids are strings like "lyrica" or
"mer_int_1234567890", not UUIDs), so no UUIDv5 derivation is needed.

Call sites depend on the TenantResolver protocol, not on
IdentityTenantResolver directly, so a real SLA113-backed resolver (if
tenant_id and organization_id ever need to diverge) can replace the
module-level `tenant_resolver` instance without touching callers.
"""

from typing import Protocol


class TenantResolver(Protocol):
    def resolve(self, tenant_id: str) -> str:
        """Return the organization_id to use for this tenant."""
        ...


class IdentityTenantResolver:
    def resolve(self, tenant_id: str) -> str:
        return tenant_id


tenant_resolver: TenantResolver = IdentityTenantResolver()
