import hashlib
import hmac
import json
import logging
import os
import secrets
from datetime import datetime, timedelta
from typing import Optional, List

from fastapi import FastAPI, Depends, Header, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field
from sqlalchemy import (
    create_engine,
    Column,
    Integer,
    String,
    Float,
    DateTime,
    Boolean,
    Text,
    func,
)
from sqlalchemy.orm import sessionmaker, declarative_base, Session


# ============================================================
# Archisynapse Fraud MVP
# Defensive fraud/risk intelligence for payments, royalties,
# creator payouts, merchant checkout flows, and ledger events.
# ============================================================

# Env-driven config (scale design Phase 1).
#   ARCHISYNAPSE_DATABASE_URL  e.g. postgresql+psycopg2://user:pass@host:5432/archisynapse
#   ARCHISYNAPSE_PEPPER        secret pepper for identifier hashing (Secret Manager in prod)
DATABASE_URL = os.environ.get("ARCHISYNAPSE_DATABASE_URL", "sqlite:///./archisynapse_fraud.db")
APP_PEPPER = os.environ.get("ARCHISYNAPSE_PEPPER", "")

_logger = logging.getLogger("archisynapse.fraud")

if not APP_PEPPER:
    APP_PEPPER = "dev-only-insecure-pepper"
    _logger.warning(
        "ARCHISYNAPSE_PEPPER not set — using an INSECURE dev pepper. "
        "Set it before any real traffic; changing it later invalidates stored hashes."
    )

if DATABASE_URL.startswith("sqlite"):
    _logger.warning(
        "Running on SQLite (%s). Fine for dev; set ARCHISYNAPSE_DATABASE_URL to Postgres before production.",
        DATABASE_URL,
    )
    _connect_args = {"check_same_thread": False}
    _engine_kwargs = {}
else:
    _connect_args = {}
    _engine_kwargs = {"pool_size": 10, "max_overflow": 20, "pool_pre_ping": True}

engine = create_engine(
    DATABASE_URL,
    connect_args=_connect_args,
    **_engine_kwargs,
)

SessionLocal = sessionmaker(
    autocommit=False,
    autoflush=False,
    bind=engine
)

Base = declarative_base()


# ============================================================
# Models
# ============================================================

class Merchant(Base):
    __tablename__ = "merchants"

    id = Column(Integer, primary_key=True, index=True)
    merchant_id = Column(String, unique=True, index=True)
    name = Column(String)
    api_key_hash = Column(String, index=True)
    webhook_url = Column(String, nullable=True)
    is_active = Column(Boolean, default=True)
    created_at = Column(DateTime, default=datetime.utcnow)


class FraudEvent(Base):
    __tablename__ = "fraud_events"

    id = Column(Integer, primary_key=True, index=True)

    merchant_id = Column(String, index=True)
    event_type = Column(String, index=True)

    user_id_hash = Column(String, index=True, nullable=True)
    creator_id_hash = Column(String, index=True, nullable=True)
    track_id_hash = Column(String, index=True, nullable=True)
    device_id_hash = Column(String, index=True, nullable=True)
    email_hash = Column(String, index=True, nullable=True)
    session_id_hash = Column(String, index=True, nullable=True)
    payout_destination_hash = Column(String, index=True, nullable=True)

    ip_address = Column(String, index=True, nullable=True)
    country = Column(String, nullable=True)
    billing_country = Column(String, nullable=True)
    card_country = Column(String, nullable=True)

    bin_hash = Column(String, index=True, nullable=True)

    amount = Column(Float, default=0.0)
    currency = Column(String, default="USD")

    payment_status = Column(String, index=True, nullable=True)
    failure_reason = Column(String, nullable=True)

    dna_verified = Column(Boolean, default=False)
    soulprint_verified = Column(Boolean, default=False)
    ledger_record_found = Column(Boolean, default=False)

    usage_count = Column(Integer, default=0)
    creator_account_age_days = Column(Integer, default=0)
    payout_method_age_days = Column(Integer, default=0)

    risk_score = Column(Integer, default=0)
    decision = Column(String, default="approve")
    reasons = Column(Text, default="")

    created_at = Column(DateTime, default=datetime.utcnow)


class IdempotencyRecord(Base):
    """Stored response for a previously seen Idempotency-Key.

    Makes POST /risk/* safe to retry (scale design Phase 1, item 2):
    a retried request returns the original decision instead of double-scoring
    and double-counting velocity signals.
    """
    __tablename__ = "idempotency_records"

    id = Column(Integer, primary_key=True, index=True)
    merchant_id = Column(String, index=True, nullable=False)
    idempotency_key = Column(String, index=True, nullable=False)
    endpoint = Column(String, nullable=False)
    response_json = Column(Text, nullable=False)
    created_at = Column(DateTime, default=datetime.utcnow, index=True)


Base.metadata.create_all(bind=engine)


# ============================================================
# Schemas
# ============================================================

class MerchantCreateIn(BaseModel):
    merchant_id: str = Field(..., examples=["merchant_demo_001"])
    name: str = Field(..., examples=["Demo Store"])
    webhook_url: Optional[str] = None


class MerchantCreateOut(BaseModel):
    merchant_id: str
    name: str
    api_key: str
    warning: str


class CheckoutRiskIn(BaseModel):
    event_type: str = "checkout_attempt"

    user_id: Optional[str] = None
    device_id: Optional[str] = None
    email: Optional[str] = None
    session_id: Optional[str] = None

    ip_address: Optional[str] = None
    country: Optional[str] = None
    billing_country: Optional[str] = None
    card_country: Optional[str] = None

    bin_reference: Optional[str] = None

    amount: float = 0.0
    currency: str = "USD"

    payment_status: str = "unknown"
    failure_reason: Optional[str] = None


class RoyaltyRiskIn(BaseModel):
    event_type: str = "royalty_payout_request"

    creator_id: str
    track_id: str
    royalty_event_id: Optional[str] = None

    user_id: Optional[str] = None
    device_id: Optional[str] = None
    email: Optional[str] = None
    session_id: Optional[str] = None
    payout_destination: Optional[str] = None

    ip_address: Optional[str] = None
    country: Optional[str] = None

    amount: float = 0.0
    currency: str = "USD"

    usage_count: int = 0
    creator_account_age_days: int = 0
    payout_method_age_days: int = 0

    dna_verified: bool = False
    soulprint_verified: bool = False
    ledger_record_found: bool = False

    sudden_usage_spike: bool = False
    duplicate_payout_destination: bool = False
    payout_destination_changed_recently: bool = False


class RiskDecisionOut(BaseModel):
    risk_score: int
    decision: str
    reasons: List[str]


class MerchantSummaryOut(BaseModel):
    merchant_id: str
    total_events: int
    checkout_events: int
    royalty_events: int
    failed_events: int
    blocked_events: int
    manual_review_events: int
    hold_payout_events: int
    average_risk_score: float
    estimated_value_protected: float


# ============================================================
# Security helpers
# ============================================================

def generate_api_key() -> str:
    return "ark_" + secrets.token_urlsafe(32)


def hash_value(value: Optional[str]) -> Optional[str]:
    if not value:
        return None

    normalized = value.strip().lower()
    payload = f"{APP_PEPPER}:{normalized}".encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def verify_api_key(raw_key: str, stored_hash: str) -> bool:
    incoming = hash_value(raw_key)
    return hmac.compare_digest(incoming or "", stored_hash)


# ============================================================
# DB dependency + auth
# ============================================================

def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def check_idempotency(
    db: Session,
    merchant_id: str,
    idempotency_key: Optional[str],
    endpoint: str,
) -> Optional[dict]:
    """Return the stored response if this key was already processed."""
    if not idempotency_key:
        return None
    record = db.query(IdempotencyRecord).filter(
        IdempotencyRecord.merchant_id == merchant_id,
        IdempotencyRecord.idempotency_key == idempotency_key,
        IdempotencyRecord.endpoint == endpoint,
    ).first()
    if record:
        return json.loads(record.response_json)
    return None


def store_idempotency(
    db: Session,
    merchant_id: str,
    idempotency_key: Optional[str],
    endpoint: str,
    response: dict,
) -> None:
    if not idempotency_key:
        return
    db.add(IdempotencyRecord(
        merchant_id=merchant_id,
        idempotency_key=idempotency_key,
        endpoint=endpoint,
        response_json=json.dumps(response),
    ))
    db.commit()


def get_current_merchant(
    x_api_key: str = Header(...),
    db: Session = Depends(get_db)
):
    merchants = db.query(Merchant).filter(
        Merchant.is_active == True
    ).all()

    for merchant in merchants:
        if verify_api_key(x_api_key, merchant.api_key_hash):
            return merchant

    raise HTTPException(status_code=401, detail="Invalid API key")


# ============================================================
# Risk Engines
# ============================================================

def calculate_checkout_risk(
    event: CheckoutRiskIn,
    merchant_id: str,
    db: Session
):
    score = 0
    reasons = []

    now = datetime.utcnow()
    short_window = now - timedelta(minutes=10)
    medium_window = now - timedelta(hours=1)

    user_hash = hash_value(event.user_id)
    device_hash = hash_value(event.device_id)
    email_hash = hash_value(event.email)
    session_hash = hash_value(event.session_id)
    bin_hash = hash_value(event.bin_reference)

    if device_hash:
        device_failures = db.query(FraudEvent).filter(
            FraudEvent.merchant_id == merchant_id,
            FraudEvent.device_id_hash == device_hash,
            FraudEvent.payment_status == "failed",
            FraudEvent.created_at >= short_window
        ).count()

        if device_failures >= 3:
            score += 35
            reasons.append("same_device_many_failed_payments")

    if event.ip_address:
        ip_attempts = db.query(FraudEvent).filter(
            FraudEvent.merchant_id == merchant_id,
            FraudEvent.ip_address == event.ip_address,
            FraudEvent.created_at >= short_window
        ).count()

        if ip_attempts >= 10:
            score += 25
            reasons.append("high_ip_velocity")

    if email_hash:
        email_failures = db.query(FraudEvent).filter(
            FraudEvent.merchant_id == merchant_id,
            FraudEvent.email_hash == email_hash,
            FraudEvent.payment_status == "failed",
            FraudEvent.created_at >= medium_window
        ).count()

        if email_failures >= 3:
            score += 20
            reasons.append("same_email_multiple_failures")

    if bin_hash:
        bin_failures = db.query(FraudEvent).filter(
            FraudEvent.merchant_id == merchant_id,
            FraudEvent.bin_hash == bin_hash,
            FraudEvent.payment_status == "failed",
            FraudEvent.created_at >= short_window
        ).count()

        if bin_failures >= 5:
            score += 25
            reasons.append("issuer_pattern_multiple_failures")

    if session_hash:
        session_attempts = db.query(FraudEvent).filter(
            FraudEvent.merchant_id == merchant_id,
            FraudEvent.session_id_hash == session_hash,
            FraudEvent.created_at >= short_window
        ).count()

        if session_attempts >= 6:
            score += 20
            reasons.append("checkout_session_reuse_abuse")

    if event.billing_country and event.card_country:
        if event.billing_country.upper() != event.card_country.upper():
            score += 15
            reasons.append("billing_card_country_mismatch")

    if event.country and event.billing_country:
        if event.country.upper() != event.billing_country.upper():
            score += 15
            reasons.append("ip_billing_country_mismatch")

    if 0 < event.amount <= 5:
        score += 10
        reasons.append("small_amount_testing_pattern")

    if event.payment_status == "failed":
        score += 10
        reasons.append("payment_failed")

    score = min(score, 100)

    if score >= 80:
        decision = "block"
    elif score >= 60:
        decision = "manual_review"
    elif score >= 35:
        decision = "step_up_verification"
    else:
        decision = "approve"

    return score, decision, reasons


def calculate_royalty_risk(
    event: RoyaltyRiskIn,
    merchant_id: str,
    db: Session
):
    score = 0
    reasons = []

    now = datetime.utcnow()
    medium_window = now - timedelta(hours=24)

    creator_hash = hash_value(event.creator_id)
    track_hash = hash_value(event.track_id)
    payout_hash = hash_value(event.payout_destination)
    device_hash = hash_value(event.device_id)
    email_hash = hash_value(event.email)

    # Proof checks
    if not event.dna_verified:
        score += 35
        reasons.append("dna_not_verified")

    if not event.soulprint_verified:
        score += 30
        reasons.append("soulprint_not_verified")

    if not event.ledger_record_found:
        score += 25
        reasons.append("ledger_record_missing")

    # New account/payout risk
    if event.creator_account_age_days < 7:
        score += 20
        reasons.append("new_creator_account")

    if event.payout_method_age_days < 3:
        score += 25
        reasons.append("new_payout_method")

    if event.payout_destination_changed_recently:
        score += 25
        reasons.append("payout_destination_changed_recently")

    if event.duplicate_payout_destination:
        score += 25
        reasons.append("duplicate_payout_destination")

    # Usage behavior
    if event.sudden_usage_spike:
        score += 25
        reasons.append("sudden_usage_spike")

    if event.usage_count >= 10000 and event.creator_account_age_days < 14:
        score += 20
        reasons.append("large_usage_on_new_creator_account")

    # Repeated payout requests
    if creator_hash:
        creator_payout_requests = db.query(FraudEvent).filter(
            FraudEvent.merchant_id == merchant_id,
            FraudEvent.creator_id_hash == creator_hash,
            FraudEvent.event_type == "royalty_payout_request",
            FraudEvent.created_at >= medium_window
        ).count()

        if creator_payout_requests >= 5:
            score += 15
            reasons.append("high_creator_payout_request_velocity")

    if payout_hash:
        payout_destination_events = db.query(FraudEvent).filter(
            FraudEvent.merchant_id == merchant_id,
            FraudEvent.payout_destination_hash == payout_hash,
            FraudEvent.event_type == "royalty_payout_request",
            FraudEvent.created_at >= medium_window
        ).count()

        if payout_destination_events >= 5:
            score += 15
            reasons.append("payout_destination_velocity")

    # Device/email signals
    if device_hash:
        risky_device_events = db.query(FraudEvent).filter(
            FraudEvent.merchant_id == merchant_id,
            FraudEvent.device_id_hash == device_hash,
            FraudEvent.risk_score >= 60,
            FraudEvent.created_at >= medium_window
        ).count()

        if risky_device_events >= 3:
            score += 20
            reasons.append("device_linked_to_prior_risky_events")

    if email_hash:
        risky_email_events = db.query(FraudEvent).filter(
            FraudEvent.merchant_id == merchant_id,
            FraudEvent.email_hash == email_hash,
            FraudEvent.risk_score >= 60,
            FraudEvent.created_at >= medium_window
        ).count()

        if risky_email_events >= 3:
            score += 20
            reasons.append("email_linked_to_prior_risky_events")

    score = min(score, 100)

    if score >= 85:
        decision = "block_payout"
    elif score >= 65:
        decision = "hold_payout_review"
    elif score >= 40:
        decision = "delay_payout_72h"
    else:
        decision = "release_payout"

    return score, decision, reasons


def save_event(
    db: Session,
    merchant_id: str,
    event_type: str,
    risk_score: int,
    decision: str,
    reasons: List[str],
    amount: float = 0.0,
    currency: str = "USD",
    user_id: Optional[str] = None,
    creator_id: Optional[str] = None,
    track_id: Optional[str] = None,
    device_id: Optional[str] = None,
    email: Optional[str] = None,
    session_id: Optional[str] = None,
    payout_destination: Optional[str] = None,
    ip_address: Optional[str] = None,
    country: Optional[str] = None,
    billing_country: Optional[str] = None,
    card_country: Optional[str] = None,
    bin_reference: Optional[str] = None,
    payment_status: Optional[str] = None,
    failure_reason: Optional[str] = None,
    dna_verified: bool = False,
    soulprint_verified: bool = False,
    ledger_record_found: bool = False,
    usage_count: int = 0,
    creator_account_age_days: int = 0,
    payout_method_age_days: int = 0,
):
    fraud_event = FraudEvent(
        merchant_id=merchant_id,
        event_type=event_type,
        user_id_hash=hash_value(user_id),
        creator_id_hash=hash_value(creator_id),
        track_id_hash=hash_value(track_id),
        device_id_hash=hash_value(device_id),
        email_hash=hash_value(email),
        session_id_hash=hash_value(session_id),
        payout_destination_hash=hash_value(payout_destination),
        ip_address=ip_address,
        country=country,
        billing_country=billing_country,
        card_country=card_country,
        bin_hash=hash_value(bin_reference),
        amount=amount,
        currency=currency,
        payment_status=payment_status,
        failure_reason=failure_reason,
        dna_verified=dna_verified,
        soulprint_verified=soulprint_verified,
        ledger_record_found=ledger_record_found,
        usage_count=usage_count,
        creator_account_age_days=creator_account_age_days,
        payout_method_age_days=payout_method_age_days,
        risk_score=risk_score,
        decision=decision,
        reasons=",".join(reasons),
    )

    db.add(fraud_event)
    db.commit()
    db.refresh(fraud_event)

    return fraud_event


# ============================================================
# FastAPI App
# ============================================================

app = FastAPI(
    title="Archisynapse Fraud MVP",
    version="0.1.0",
    description="Defensive fraud/risk intelligence for Archisynapse payments, creator royalties, and payout flows."
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.get("/health")
def health():
    return {
        "status": "ok",
        "service": "archisynapse-fraud",
        "version": "0.1.0"
    }


# Empire Spine receiver: signed events in, birth certificates out.
try:
    from spine_receiver import register as _register_spine
    app.include_router(_register_spine(Base, engine, get_db, get_current_merchant))
    _logger.info("Empire Spine receiver mounted at /api/v1/events")
except Exception as _spine_exc:  # fail-open: fraud service still runs without spine
    _logger.error("Empire Spine receiver failed to mount: %s", _spine_exc)


@app.post("/admin/merchants", response_model=MerchantCreateOut)
def create_merchant(payload: MerchantCreateIn, db: Session = Depends(get_db)):
    existing = db.query(Merchant).filter(
        Merchant.merchant_id == payload.merchant_id
    ).first()

    if existing:
        raise HTTPException(status_code=400, detail="Merchant already exists")

    raw_key = generate_api_key()

    merchant = Merchant(
        merchant_id=payload.merchant_id,
        name=payload.name,
        api_key_hash=hash_value(raw_key),
        webhook_url=payload.webhook_url,
        is_active=True
    )

    db.add(merchant)
    db.commit()

    return {
        "merchant_id": payload.merchant_id,
        "name": payload.name,
        "api_key": raw_key,
        "warning": "Store this API key now. It will not be shown again."
    }


@app.post("/risk/checkout", response_model=RiskDecisionOut)
def risk_checkout(
    event: CheckoutRiskIn,
    merchant: Merchant = Depends(get_current_merchant),
    db: Session = Depends(get_db),
    idempotency_key: Optional[str] = Header(default=None, alias="Idempotency-Key"),
):
    cached = check_idempotency(db, merchant.merchant_id, idempotency_key, "/risk/checkout")
    if cached is not None:
        return cached

    risk_score, decision, reasons = calculate_checkout_risk(
        event=event,
        merchant_id=merchant.merchant_id,
        db=db
    )

    save_event(
        db=db,
        merchant_id=merchant.merchant_id,
        event_type=event.event_type,
        risk_score=risk_score,
        decision=decision,
        reasons=reasons,
        amount=event.amount,
        currency=event.currency,
        user_id=event.user_id,
        device_id=event.device_id,
        email=event.email,
        session_id=event.session_id,
        ip_address=event.ip_address,
        country=event.country,
        billing_country=event.billing_country,
        card_country=event.card_country,
        bin_reference=event.bin_reference,
        payment_status=event.payment_status,
        failure_reason=event.failure_reason,
    )

    response = {
        "risk_score": risk_score,
        "decision": decision,
        "reasons": reasons
    }
    store_idempotency(db, merchant.merchant_id, idempotency_key, "/risk/checkout", response)
    return response


@app.post("/risk/royalty", response_model=RiskDecisionOut)
def risk_royalty(
    event: RoyaltyRiskIn,
    merchant: Merchant = Depends(get_current_merchant),
    db: Session = Depends(get_db),
    idempotency_key: Optional[str] = Header(default=None, alias="Idempotency-Key"),
):
    cached = check_idempotency(db, merchant.merchant_id, idempotency_key, "/risk/royalty")
    if cached is not None:
        return cached

    risk_score, decision, reasons = calculate_royalty_risk(
        event=event,
        merchant_id=merchant.merchant_id,
        db=db
    )

    save_event(
        db=db,
        merchant_id=merchant.merchant_id,
        event_type=event.event_type,
        risk_score=risk_score,
        decision=decision,
        reasons=reasons,
        amount=event.amount,
        currency=event.currency,
        user_id=event.user_id,
        creator_id=event.creator_id,
        track_id=event.track_id,
        device_id=event.device_id,
        email=event.email,
        session_id=event.session_id,
        payout_destination=event.payout_destination,
        ip_address=event.ip_address,
        country=event.country,
        dna_verified=event.dna_verified,
        soulprint_verified=event.soulprint_verified,
        ledger_record_found=event.ledger_record_found,
        usage_count=event.usage_count,
        creator_account_age_days=event.creator_account_age_days,
        payout_method_age_days=event.payout_method_age_days,
    )

    response = {
        "risk_score": risk_score,
        "decision": decision,
        "reasons": reasons
    }
    store_idempotency(db, merchant.merchant_id, idempotency_key, "/risk/royalty", response)
    return response


@app.get("/merchant/summary", response_model=MerchantSummaryOut)
def merchant_summary(
    merchant: Merchant = Depends(get_current_merchant),
    db: Session = Depends(get_db)
):
    events = db.query(FraudEvent).filter(
        FraudEvent.merchant_id == merchant.merchant_id
    )

    total = events.count()
    checkout_events = events.filter(FraudEvent.event_type == "checkout_attempt").count()
    royalty_events = events.filter(FraudEvent.event_type == "royalty_payout_request").count()
    failed = events.filter(FraudEvent.payment_status == "failed").count()

    blocked = events.filter(
        FraudEvent.decision.in_(["block", "block_payout"])
    ).count()

    manual = events.filter(
        FraudEvent.decision.in_(["manual_review", "hold_payout_review"])
    ).count()

    hold_payout = events.filter(
        FraudEvent.decision.in_(["hold_payout_review", "delay_payout_72h", "block_payout"])
    ).count()

    avg_score = db.query(func.avg(FraudEvent.risk_score)).filter(
        FraudEvent.merchant_id == merchant.merchant_id
    ).scalar()

    estimated_value = db.query(func.sum(FraudEvent.amount)).filter(
        FraudEvent.merchant_id == merchant.merchant_id,
        FraudEvent.decision.in_(["block", "block_payout", "hold_payout_review"])
    ).scalar()

    return {
        "merchant_id": merchant.merchant_id,
        "total_events": total,
        "checkout_events": checkout_events,
        "royalty_events": royalty_events,
        "failed_events": failed,
        "blocked_events": blocked,
        "manual_review_events": manual,
        "hold_payout_events": hold_payout,
        "average_risk_score": round(float(avg_score or 0), 2),
        "estimated_value_protected": round(float(estimated_value or 0), 2)
    }


@app.get("/merchant/alerts")
def merchant_alerts(
    merchant: Merchant = Depends(get_current_merchant),
    db: Session = Depends(get_db)
):
    alerts = db.query(FraudEvent).filter(
        FraudEvent.merchant_id == merchant.merchant_id,
        FraudEvent.decision.in_([
            "block",
            "manual_review",
            "block_payout",
            "hold_payout_review",
            "delay_payout_72h"
        ])
    ).order_by(
        FraudEvent.created_at.desc()
    ).limit(50).all()

    return alerts


@app.get("/merchant/risk-reasons")
def merchant_risk_reasons(
    merchant: Merchant = Depends(get_current_merchant),
    db: Session = Depends(get_db)
):
    events = db.query(FraudEvent).filter(
        FraudEvent.merchant_id == merchant.merchant_id
    ).all()

    counts = {}

    for event in events:
        if not event.reasons:
            continue

        for reason in event.reasons.split(","):
            reason = reason.strip()
            if reason:
                counts[reason] = counts.get(reason, 0) + 1

    return [
        {"reason": reason, "count": count}
        for reason, count in sorted(
            counts.items(),
            key=lambda item: item[1],
            reverse=True
        )
    ]


@app.get("/merchant/events/recent")
def recent_events(
    merchant: Merchant = Depends(get_current_merchant),
    db: Session = Depends(get_db)
):
    events = db.query(FraudEvent).filter(
        FraudEvent.merchant_id == merchant.merchant_id
    ).order_by(
        FraudEvent.created_at.desc()
    ).limit(50).all()

    return events


@app.get("/merchant/investigation/{event_id}")
def investigation_report(
    event_id: int,
    merchant: Merchant = Depends(get_current_merchant),
    db: Session = Depends(get_db)
):
    event = db.query(FraudEvent).filter(
        FraudEvent.id == event_id,
        FraudEvent.merchant_id == merchant.merchant_id
    ).first()

    if not event:
        raise HTTPException(status_code=404, detail="Event not found")

    reasons = event.reasons.split(",") if event.reasons else []

    if event.decision in ["approve", "release_payout"]:
        action = "No fraud action required."
    elif event.decision == "step_up_verification":
        action = "Require step-up verification before checkout continues."
    elif event.decision == "manual_review":
        action = "Hold transaction for manual review."
    elif event.decision == "block":
        action = "Block checkout session and monitor related identifiers."
    elif event.decision == "delay_payout_72h":
        action = "Delay payout for 72 hours and monitor usage/claim behavior."
    elif event.decision == "hold_payout_review":
        action = "Hold payout for manual royalty/fraud review."
    elif event.decision == "block_payout":
        action = "Block payout until ownership, ledger, and creator identity are verified."
    else:
        action = "Review event."

    return {
        "title": "Archisynapse Fraud Investigation Report",
        "merchant_id": merchant.merchant_id,
        "event_id": event.id,
        "event_type": event.event_type,
        "created_at": event.created_at,
        "amount": event.amount,
        "currency": event.currency,
        "payment_status": event.payment_status,
        "risk_score": event.risk_score,
        "decision": event.decision,
        "signals": reasons,
        "checks": {
            "dna_verified": event.dna_verified,
            "soulprint_verified": event.soulprint_verified,
            "ledger_record_found": event.ledger_record_found,
        },
        "recommended_action": action,
        "summary": (
            f"Event {event.id} was scored {event.risk_score}/100 "
            f"with decision '{event.decision}'."
        )
    }
