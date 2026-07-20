"""
Pydantic schemas for the Lyrica royalty receipt loop — see
spec/SPEC-royalty-loop-v1.md §3 (event) and §6 (receipt).

Unknown top-level fields on the incoming event are ignored, never
rejected (extra="ignore"), per the "WE EVOLVE" schema-evolution rule.
"""

import re
from typing import Literal, Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

AMOUNT_RE = re.compile(r"^\d+\.\d{4}$")

ReceiptStatus = Literal["processing", "paid", "held", "blocked", "reversed", "rejected"]


class VicsProof(BaseModel):
    model_config = ConfigDict(extra="ignore")
    proof_id: str
    issued_at: str
    chain_ref: str


class TrackInfo(BaseModel):
    model_config = ConfigDict(extra="ignore")
    track_id: str
    dna_tag: str
    soulprint_hash: str
    vics_proof: VicsProof


class CreatorRef(BaseModel):
    model_config = ConfigDict(extra="ignore")
    creator_id: str
    identity_ref: str


class Split(BaseModel):
    model_config = ConfigDict(extra="ignore")
    owner_id: str
    bps: int


class Trigger(BaseModel):
    model_config = ConfigDict(extra="ignore")
    kind: Literal["play", "remix", "license"]
    source_ref: str
    actor_id: str


class Amount(BaseModel):
    model_config = ConfigDict(extra="ignore")
    currency: str
    value: str

    @field_validator("value")
    @classmethod
    def four_decimal_places(cls, v: str) -> str:
        if not AMOUNT_RE.match(v):
            raise ValueError("amount.value must be a fixed-point string with exactly 4 decimal places")
        return v


class RoyaltyObligationCreated(BaseModel):
    """royalty.obligation.created, schema_version 1.0."""

    model_config = ConfigDict(extra="ignore")

    schema_version: str
    event_id: str
    event_type: Literal["royalty.obligation.created"]
    occurred_at: str
    correlation_id: str
    idempotency_key: str
    tenant_id: str
    track: TrackInfo
    creator: CreatorRef
    splits: list[Split]
    trigger: Trigger
    amount: Amount

    @model_validator(mode="after")
    def splits_sum_to_10000(self) -> "RoyaltyObligationCreated":
        total = sum(s.bps for s in self.splits)
        if total != 10000:
            raise ValueError(f"splits[].bps must sum to exactly 10000, got {total}")
        return self


class RoyaltyObligationReversed(BaseModel):
    """royalty.obligation.reversed — references an earlier event by event_id."""

    model_config = ConfigDict(extra="ignore")

    schema_version: str
    event_id: str
    event_type: Literal["royalty.obligation.reversed"]
    occurred_at: str
    correlation_id: str
    idempotency_key: str
    tenant_id: str
    reverses_event_id: str


class Payout(BaseModel):
    owner_id: str
    amount: str
    state: str


class Amounts(BaseModel):
    currency: str
    gross: str
    platform_fee: str
    net: str


class Decision(BaseModel):
    policy: str
    risk_score: float
    checks: list[str]


class Signature(BaseModel):
    alg: str
    key_id: str
    value: str


class UnifiedReceipt(BaseModel):
    """The signed receipt returned synchronously and via GET /api/v1/receipts/{id}."""

    schema_version: str = "1.0"
    receipt_id: str
    status: ReceiptStatus
    status_reasons: list[str] = Field(default_factory=list)
    event_id: str
    correlation_id: str
    transaction_id: Optional[str] = None
    ledger_transaction_id: Optional[str] = None
    amounts: Amounts
    payouts: list[Payout] = Field(default_factory=list)
    decision: Decision
    issued_at: str
    signature: Signature
