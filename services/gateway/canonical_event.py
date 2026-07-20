"""
Canonical Event Schema for Archisynapse Revenue Assurance Loop v1.

Every revenue event flows through this schema across all services.
The correlation_id is the single threading identifier that proves
the event was processed by every service in the chain.
"""

import uuid
import hashlib
from decimal import Decimal, ROUND_HALF_UP
from datetime import datetime, timezone
from typing import Optional
from pydantic import BaseModel, Field, field_validator


def dollars_to_minor(dollars: float) -> int:
    """Convert dollar amount to minor units (cents) using Decimal to avoid float drift."""
    return int(Decimal(str(dollars)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP) * 100)


def minor_to_dollars(minor: int) -> float:
    """Convert minor units to dollars as float for JSON transport only."""
    return float(Decimal(str(minor)) / 100)


class CanonicalEvent(BaseModel):
    """
    The single source of truth for a revenue event.
    Every service reads/writes this schema.
    No credentials — merchant_id identifies the merchant, keys stay in env.
    """
    # Identity
    event_id: str = Field(default_factory=lambda: f"evt_{uuid.uuid4().hex[:16]}")
    correlation_id: str = Field(default_factory=lambda: f"corr_{uuid.uuid4().hex[:16]}")

    # Merchant (identity only — no api keys in the event)
    merchant_id: str

    # Transaction
    transaction_id: Optional[str] = None
    idempotency_key: str = Field(default_factory=lambda: f"idem_{uuid.uuid4().hex[:16]}")

    # Customer
    customer_id: str

    # Amount — stored as minor units (int) to avoid float precision issues
    amount_minor: int
    fee_minor: int = 0
    currency: str = "USD"

    # Payment method (no secrets — token reference only)
    payment_method_type: str = "CARD"
    payment_method_last4: str = "4242"
    payment_method_brand: str = "VISA"

    # Fraud signals
    ip_address: Optional[str] = None
    country: Optional[str] = None
    device_id: Optional[str] = None
    email: Optional[str] = None
    session_id: Optional[str] = None

    # Timing
    occurred_at: str = Field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    processed_at: Optional[str] = None

    # Service trace — fraud
    fraud_decision: Optional[str] = None  # approve, block, manual_review
    fraud_score: Optional[float] = None
    fraud_reasons: list = Field(default_factory=list)
    fraud_error: Optional[str] = None

    # Service trace — transaction
    transaction_error: Optional[str] = None

    # Service trace — ledger
    ledger_transaction_id: Optional[str] = None
    ledger_entries: list = Field(default_factory=list)
    ledger_error: Optional[str] = None

    # Service trace — analytics
    analytics_recorded: bool = False
    analytics_transaction_id: Optional[str] = None
    analytics_error: Optional[str] = None

    # Status
    status: str = "pending"  # pending, processing, completed, failed, blocked, review, refunded
    error: Optional[str] = None

    @field_validator('amount_minor')
    @classmethod
    def amount_must_be_positive(cls, v):
        if v <= 0:
            raise ValueError('amount_minor must be positive')
        return v

    def generate_correlation_id(self) -> str:
        """Generate a deterministic correlation ID from event contents."""
        content = f"{self.merchant_id}:{self.customer_id}:{self.amount_minor}:{self.currency}:{self.idempotency_key}"
        return f"corr_{hashlib.sha256(content.encode()).hexdigest()[:16]}"

    def mark_processed(self, service: str, **kwargs):
        """Mark event as processed by a service."""
        self.processed_at = datetime.now(timezone.utc).isoformat()
        for key, value in kwargs.items():
            setattr(self, key, value)

    @property
    def amount_dollars(self) -> float:
        """Convert minor units to dollars for JSON transport."""
        return minor_to_dollars(self.amount_minor)

    @property
    def fee_dollars(self) -> float:
        return minor_to_dollars(self.fee_minor)

    @property
    def net_amount_minor(self) -> int:
        return self.amount_minor - self.fee_minor

    @property
    def net_amount_dollars(self) -> float:
        return minor_to_dollars(self.net_amount_minor)


class PaymentRequest(BaseModel):
    """External payment request from merchant. No credentials in the event."""
    merchant_id: str
    customer_id: str
    amount: float  # Dollars — converted to minor units immediately
    fee_amount: float = 0.0
    currency: str = "USD"
    payment_method_type: str = "CARD"
    payment_method_token: str = "tok_test_card"
    payment_method_last4: str = "4242"
    payment_method_brand: str = "VISA"
    description: Optional[str] = None
    metadata: dict = Field(default_factory=dict)

    # Fraud signals
    ip_address: Optional[str] = None
    country: Optional[str] = None
    device_id: Optional[str] = None
    email: Optional[str] = None
    session_id: Optional[str] = None
    fraud_api_key: Optional[str] = None
    analytics_api_key: Optional[str] = None

    def to_canonical_event(self, idempotency_key: str) -> CanonicalEvent:
        """Convert payment request to canonical event."""
        return CanonicalEvent(
            merchant_id=self.merchant_id,
            customer_id=self.customer_id,
            amount_minor=dollars_to_minor(self.amount),
            fee_minor=dollars_to_minor(self.fee_amount),
            currency=self.currency,
            payment_method_type=self.payment_method_type,
            payment_method_last4=self.payment_method_last4,
            payment_method_brand=self.payment_method_brand,
            ip_address=self.ip_address,
            country=self.country,
            device_id=self.device_id,
            email=self.email,
            session_id=self.session_id,
            idempotency_key=idempotency_key,
        )


class UnifiedReceipt(BaseModel):
    """Final receipt after all services process the event."""
    event_id: str
    correlation_id: str
    merchant_id: str
    customer_id: str
    transaction_id: Optional[str] = None
    amount: float
    fee_amount: float = 0.0
    currency: str
    status: str

    # Fraud
    fraud_decision: Optional[str] = None
    fraud_score: Optional[float] = None
    fraud_reasons: list = []
    fraud_error: Optional[str] = None

    # Transaction
    transaction_error: Optional[str] = None

    # Ledger
    ledger_transaction_id: Optional[str] = None
    ledger_entries: list = []
    ledger_error: Optional[str] = None

    # Analytics
    analytics_recorded: bool = False
    analytics_transaction_id: Optional[str] = None
    analytics_error: Optional[str] = None

    # Timing
    occurred_at: str
    processed_at: Optional[str] = None

    # Idempotency
    idempotency_key: str

    # Top-level error
    error: Optional[str] = None

    @classmethod
    def from_event(cls, event: CanonicalEvent) -> "UnifiedReceipt":
        """Create receipt from canonical event."""
        return cls(
            event_id=event.event_id,
            correlation_id=event.correlation_id,
            merchant_id=event.merchant_id,
            customer_id=event.customer_id,
            transaction_id=event.transaction_id,
            amount=event.amount_dollars,
            fee_amount=event.fee_dollars,
            currency=event.currency,
            status=event.status,
            fraud_decision=event.fraud_decision,
            fraud_score=event.fraud_score,
            fraud_reasons=event.fraud_reasons,
            fraud_error=event.fraud_error,
            transaction_error=event.transaction_error,
            ledger_transaction_id=event.ledger_transaction_id,
            ledger_entries=event.ledger_entries,
            ledger_error=event.ledger_error,
            analytics_recorded=event.analytics_recorded,
            analytics_transaction_id=event.analytics_transaction_id,
            analytics_error=event.analytics_error,
            occurred_at=event.occurred_at,
            processed_at=event.processed_at,
            idempotency_key=event.idempotency_key,
            error=event.error,
        )
