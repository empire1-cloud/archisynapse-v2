class AgentCommerceError(RuntimeError):
    """Base error with a stable machine-readable code."""

    code = "agent_commerce_error"

    def __init__(self, message: str, *, details: dict | None = None):
        super().__init__(message)
        self.details = details or {}


class AuthorizationDenied(AgentCommerceError):
    code = "authorization_denied"


class AuthorizationExpired(AuthorizationDenied):
    code = "authorization_expired"


class AuthorizationRevoked(AuthorizationDenied):
    code = "authorization_revoked"


class BudgetExceeded(AuthorizationDenied):
    code = "budget_exceeded"


class IdempotencyConflict(AgentCommerceError):
    code = "idempotency_conflict"


class InvalidInvoice(AgentCommerceError):
    code = "invalid_invoice"


class PriceMismatch(AgentCommerceError):
    code = "price_mismatch"


class PaymentFailed(AgentCommerceError):
    code = "payment_failed"


class DeliveryFailed(AgentCommerceError):
    code = "delivery_failed"


class ReceiptIntegrityError(AgentCommerceError):
    code = "receipt_integrity_error"


class IdentityConfigurationError(AgentCommerceError):
    code = "identity_configuration_error"


class RateCardNotFound(AgentCommerceError):
    code = "rate_card_not_found"


class PolicyPaused(AuthorizationDenied):
    code = "ai_spend_policy_paused"


class ModelRouteDenied(AuthorizationDenied):
    code = "model_route_denied"


class UsageReconciliationError(AgentCommerceError):
    code = "usage_reconciliation_error"


class ServiceContractViolation(AgentCommerceError):
    code = "service_contract_violation"


class ServiceContractNotFound(ServiceContractViolation):
    code = "service_contract_not_found"
