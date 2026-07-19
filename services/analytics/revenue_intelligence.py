"""
Revenue Intelligence Engine
Real-time revenue analytics, CLV prediction, churn detection, and optimization
"""
import hashlib
import hmac
import json
import logging
import math
import os
import secrets
from datetime import datetime, timedelta
from typing import Optional, List, Dict, Any
from collections import defaultdict

from fastapi import FastAPI, Depends, Header, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field
from sqlalchemy import (
    create_engine, Column, Integer, String, Float, DateTime,
    Boolean, Text, func, JSON, Index
)
from sqlalchemy.orm import sessionmaker, declarative_base, Session


# ============================================================
# Archisynapse Revenue Intelligence Engine
# ============================================================

DATABASE_URL = os.environ.get(
    "ARCHISYNAPSE_DATABASE_URL",
    "sqlite:///./revenue_intelligence.db"
)
APP_PEPPER = os.environ.get("ARCHISYNAPSE_PEPPER", "dev-only-insecure-pepper")

_logger = logging.getLogger("archisynapse.analytics")

if DATABASE_URL.startswith("sqlite"):
    _connect_args = {"check_same_thread": False}
    _engine_kwargs = {}
else:
    _connect_args = {}
    _engine_kwargs = {"pool_size": 10, "max_overflow": 20, "pool_pre_ping": True}

engine = create_engine(DATABASE_URL, connect_args=_connect_args, **_engine_kwargs)
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
Base = declarative_base()


# ============================================================
# Database Models
# ============================================================

class Merchant(Base):
    __tablename__ = "merchants"
    id = Column(Integer, primary_key=True, index=True)
    merchant_id = Column(String, unique=True, index=True)
    name = Column(String)
    api_key_hash = Column(String, index=True)
    plan = Column(String, default="free")  # free, growth, scale, enterprise
    is_active = Column(Boolean, default=True)
    created_at = Column(DateTime, default=datetime.utcnow)


class Transaction(Base):
    __tablename__ = "transactions"
    id = Column(Integer, primary_key=True, index=True)
    merchant_id = Column(String, index=True)
    customer_id = Column(String, index=True)
    amount = Column(Float, default=0.0)
    fee_amount = Column(Float, default=0.0)
    currency = Column(String, default="USD")
    status = Column(String, index=True)  # completed, failed, refunded
    payment_method = Column(String, nullable=True)
    country = Column(String, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow, index=True)

    __table_args__ = (
        Index("idx_merchant_created", "merchant_id", "created_at"),
        Index("idx_customer_created", "customer_id", "created_at"),
    )


class Subscription(Base):
    __tablename__ = "subscriptions"
    id = Column(Integer, primary_key=True, index=True)
    merchant_id = Column(String, index=True)
    customer_id = Column(String, index=True)
    plan = Column(String)  # free, growth, scale, enterprise
    mrr = Column(Float, default=0.0)
    status = Column(String, default="active")  # active, cancelled, paused
    started_at = Column(DateTime, default=datetime.utcnow)
    cancelled_at = Column(DateTime, nullable=True)
    renewal_at = Column(DateTime, nullable=True)


class CustomerEvent(Base):
    __tablename__ = "customer_events"
    id = Column(Integer, primary_key=True, index=True)
    merchant_id = Column(String, index=True)
    customer_id = Column(String, index=True)
    event_type = Column(String, index=True)  # login, feature_use, support_ticket, upgrade, downgrade
    event_data = Column(JSON, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow, index=True)


class RevenueForecast(Base):
    __tablename__ = "revenue_forecasts"
    id = Column(Integer, primary_key=True, index=True)
    merchant_id = Column(String, index=True)
    forecast_date = Column(DateTime, index=True)
    predicted_mrr = Column(Float)
    predicted_arr = Column(Float)
    confidence = Column(Float)  # 0-1
    factors = Column(JSON, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)


Base.metadata.create_all(bind=engine)


# ============================================================
# Schemas
# ============================================================

class MerchantCreateIn(BaseModel):
    merchant_id: str
    name: str
    plan: str = "free"

class MerchantOut(BaseModel):
    merchant_id: str
    name: str
    api_key: str
    plan: str

class TransactionIn(BaseModel):
    customer_id: str
    amount: float
    fee_amount: float = 0.0
    currency: str = "USD"
    status: str = "completed"
    payment_method: Optional[str] = None
    country: Optional[str] = None

class SubscriptionIn(BaseModel):
    customer_id: str
    plan: str
    mrr: float
    status: str = "active"

class CustomerEventIn(BaseModel):
    customer_id: str
    event_type: str
    event_data: Optional[Dict[str, Any]] = None

class RevenueDashboard(BaseModel):
    merchant_id: str
    period: str
    mrr: float
    arr: float
    total_revenue: float
    total_fees: float
    transaction_count: int
    avg_transaction_size: float
    unique_customers: int
    new_customers: int
    churned_customers: int
    retention_rate: float
    net_revenue_retention: float

class CustomerLifetimeValue(BaseModel):
    customer_id: str
    total_revenue: float
    total_transactions: int
    avg_transaction_size: float
    first_purchase: Optional[str]
    last_purchase: Optional[str]
    tenure_days: int
    predicted_ltv: float
    segment: str  # vip, high, medium, low, at_risk

class ChurnPrediction(BaseModel):
    customer_id: str
    churn_probability: float
    risk_level: str  # low, medium, high, critical
    days_since_last_activity: int
    engagement_score: float
    factors: List[str]
    recommended_action: str

class RevenueForecastOut(BaseModel):
    merchant_id: str
    current_mrr: float
    forecast_30d: float
    forecast_90d: float
    forecast_365d: float
    growth_rate: float
    confidence: float
    recommendations: List[str]

class OptimizationInsight(BaseModel):
    category: str  # pricing, retention, expansion, acquisition
    insight: str
    impact: str  # high, medium, low
    estimated_value: float
    action: str

class CohortAnalysis(BaseModel):
    cohort_month: str
    customers: int
    retention_rates: Dict[str, float]  # month_N: retention_rate
    avg_revenue_per_cohort: float


# ============================================================
# Security Helpers
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

def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()

def get_current_merchant(
    x_api_key: str = Header(...),
    db: Session = Depends(get_db)
):
    merchants = db.query(Merchant).filter(Merchant.is_active == True).all()
    for merchant in merchants:
        if verify_api_key(x_api_key, merchant.api_key_hash):
            return merchant
    raise HTTPException(status_code=401, detail="Invalid API key")


# ============================================================
# Revenue Intelligence Engines
# ============================================================

class RevenueAnalyzer:
    """Core revenue analytics and forecasting engine"""

    @staticmethod
    def calculate_mrr(db: Session, merchant_id: str, days: int = 30) -> float:
        """Calculate Monthly Recurring Revenue from subscriptions"""
        active_subs = db.query(func.sum(Subscription.mrr)).filter(
            Subscription.merchant_id == merchant_id,
            Subscription.status == "active"
        ).scalar()
        return float(active_subs or 0)

    @staticmethod
    def calculate_arr(db: Session, merchant_id: str) -> float:
        """Calculate Annual Recurring Revenue"""
        mrr = RevenueAnalyzer.calculate_mrr(db, merchant_id)
        return mrr * 12

    @staticmethod
    def get_revenue_trend(
        db: Session, merchant_id: str, days: int = 30
    ) -> List[Dict[str, Any]]:
        """Get daily revenue trend"""
        cutoff = datetime.utcnow() - timedelta(days=days)
        daily_revenue = db.query(
            func.date(Transaction.created_at).label("date"),
            func.sum(Transaction.amount).label("revenue"),
            func.sum(Transaction.fee_amount).label("fees"),
            func.count(Transaction.id).label("count"),
            func.count(func.distinct(Transaction.customer_id)).label("customers")
        ).filter(
            Transaction.merchant_id == merchant_id,
            Transaction.created_at >= cutoff,
            Transaction.status == "completed"
        ).group_by(
            func.date(Transaction.created_at)
        ).order_by(
            func.date(Transaction.created_at)
        ).all()

        return [
            {
                "date": str(row.date),
                "revenue": float(row.revenue or 0),
                "fees": float(row.fees or 0),
                "transactions": row.count,
                "customers": row.customers
            }
            for row in daily_revenue
        ]


class CustomerAnalyzer:
    """Customer segmentation and lifetime value engine"""

    @staticmethod
    def calculate_clv(
        db: Session, merchant_id: str, customer_id: str
    ) -> CustomerLifetimeValue:
        """Calculate Customer Lifetime Value"""
        stats = db.query(
            func.sum(Transaction.amount).label("total_revenue"),
            func.count(Transaction.id).label("total_transactions"),
            func.avg(Transaction.amount).label("avg_transaction"),
            func.min(Transaction.created_at).label("first_purchase"),
            func.max(Transaction.created_at).label("last_purchase"),
        ).filter(
            Transaction.merchant_id == merchant_id,
            Transaction.customer_id == customer_id,
            Transaction.status == "completed"
        ).first()

        total_revenue = float(stats.total_revenue or 0)
        total_transactions = stats.total_transactions or 0
        avg_transaction = float(stats.avg_transaction or 0)

        first_purchase = stats.first_purchase
        last_purchase = stats.last_purchase
        tenure_days = (datetime.utcnow() - first_purchase).days if first_purchase else 0

        # Predict LTV using BG/NBD simplified model
        if tenure_days > 0 and total_transactions > 1:
            purchase_rate = total_transactions / max(tenure_days, 1) * 30
            predicted_ltv = avg_transaction * purchase_rate * 12
        else:
            predicted_ltv = total_revenue

        # Segment customer
        if total_revenue > 1000:
            segment = "vip"
        elif total_revenue > 500:
            segment = "high"
        elif total_revenue > 100:
            segment = "medium"
        elif total_revenue > 0:
            segment = "low"
        else:
            segment = "at_risk"

        return CustomerLifetimeValue(
            customer_id=customer_id,
            total_revenue=total_revenue,
            total_transactions=total_transactions,
            avg_transaction_size=avg_transaction,
            first_purchase=first_purchase.isoformat() if first_purchase else None,
            last_purchase=last_purchase.isoformat() if last_purchase else None,
            tenure_days=tenure_days,
            predicted_ltv=round(predicted_ltv, 2),
            segment=segment
        )

    @staticmethod
    def get_customer_segments(
        db: Session, merchant_id: str
    ) -> Dict[str, int]:
        """Get customer distribution by segment"""
        customers = db.query(
            Transaction.customer_id,
            func.sum(Transaction.amount).label("total_spent")
        ).filter(
            Transaction.merchant_id == merchant_id,
            Transaction.status == "completed"
        ).group_by(
            Transaction.customer_id
        ).all()

        segments = defaultdict(int)
        for cust in customers:
            total = float(cust.total_spent or 0)
            if total > 1000:
                segments["vip"] += 1
            elif total > 500:
                segments["high"] += 1
            elif total > 100:
                segments["medium"] += 1
            elif total > 0:
                segments["low"] += 1
            else:
                segments["at_risk"] += 1

        return dict(segments)


class ChurnPredictor:
    """Churn prediction engine using engagement signals"""

    @staticmethod
    def predict_churn(
        db: Session, merchant_id: str, customer_id: str
    ) -> ChurnPrediction:
        """Predict customer churn probability"""
        # Get last transaction
        last_tx = db.query(Transaction).filter(
            Transaction.merchant_id == merchant_id,
            Transaction.customer_id == customer_id,
            Transaction.status == "completed"
        ).order_by(Transaction.created_at.desc()).first()

        if not last_tx:
            days_since_last = 999
        else:
            days_since_last = (datetime.utcnow() - last_tx.created_at).days

        # Get transaction frequency (last 30 days)
        cutoff_30d = datetime.utcnow() - timedelta(days=30)
        recent_tx_count = db.query(func.count(Transaction.id)).filter(
            Transaction.merchant_id == merchant_id,
            Transaction.customer_id == customer_id,
            Transaction.created_at >= cutoff_30d
        ).scalar() or 0

        # Get total lifetime transactions
        total_tx = db.query(func.count(Transaction.id)).filter(
            Transaction.merchant_id == merchant_id,
            Transaction.customer_id == customer_id
        ).scalar() or 0

        # Engagement score (0-100)
        engagement_score = min(100, max(0,
            (100 - days_since_last * 2) * 0.5 +
            min(recent_tx_count * 10, 50)
        ))

        # Calculate churn probability
        churn_factors = []
        churn_score = 0

        if days_since_last > 60:
            churn_score += 40
            churn_factors.append("inactive_60_plus_days")
        elif days_since_last > 30:
            churn_score += 25
            churn_factors.append("inactive_30_plus_days")
        elif days_since_last > 14:
            churn_score += 10
            churn_factors.append("inactive_14_plus_days")

        if recent_tx_count == 0 and total_tx > 0:
            churn_score += 30
            churn_factors.append("no_activity_last_30d")
        elif recent_tx_count < 2:
            churn_score += 15
            churn_factors.append("low_recent_activity")

        if total_tx <= 3:
            churn_score += 15
            churn_factors.append("low_total_engagement")

        churn_probability = min(churn_score, 100) / 100

        if churn_probability >= 0.7:
            risk_level = "critical"
            action = "Immediate outreach: personalized offer + success manager call"
        elif churn_probability >= 0.5:
            risk_level = "high"
            action = "Proactive engagement: feature education + check-in email"
        elif churn_probability >= 0.3:
            risk_level = "medium"
            action = "Monitor closely: usage tips + engagement campaign"
        else:
            risk_level = "low"
            action = "Maintain satisfaction: continue regular engagement"

        return ChurnPrediction(
            customer_id=customer_id,
            churn_probability=round(churn_probability, 3),
            risk_level=risk_level,
            days_since_last_activity=days_since_last,
            engagement_score=round(engagement_score, 1),
            factors=churn_factors,
            recommended_action=action
        )


class RevenueOptimizer:
    """Revenue optimization recommendations engine"""

    @staticmethod
    def get_insights(
        db: Session, merchant_id: str
    ) -> List[OptimizationInsight]:
        """Generate actionable revenue optimization insights"""
        insights = []

        # 1. Pricing optimization
        avg_tx = db.query(func.avg(Transaction.amount)).filter(
            Transaction.merchant_id == merchant_id,
            Transaction.status == "completed"
        ).scalar()

        if avg_tx and avg_tx < 50:
            insights.append(OptimizationInsight(
                category="pricing",
                insight=f"Average transaction (${avg_tx:.2f}) is below platform median. Consider premium tiers.",
                impact="high",
                estimated_value=float(avg_tx) * 100,
                action="Launch a Pro tier at $49-99/mo with advanced features"
            ))

        # 2. Fee optimization
        total_fees = db.query(func.sum(Transaction.fee_amount)).filter(
            Transaction.merchant_id == merchant_id,
            Transaction.status == "completed"
        ).scalar() or 0

        total_revenue = db.query(func.sum(Transaction.amount)).filter(
            Transaction.merchant_id == merchant_id,
            Transaction.status == "completed"
        ).scalar() or 0

        if total_revenue > 0:
            effective_rate = float(total_fees) / float(total_revenue) * 100
            if effective_rate > 3.0:
                insights.append(OptimizationInsight(
                    category="pricing",
                    insight=f"Effective fee rate ({effective_rate:.1f}%) is above optimal. Volume discounts could reduce churn.",
                    impact="medium",
                    estimated_value=float(total_revenue) * 0.005,
                    action="Introduce volume-based pricing tiers for high-transaction merchants"
                ))

        # 3. Retention optimization
        customers = db.query(
            Transaction.customer_id,
            func.max(Transaction.created_at).label("last_tx")
        ).filter(
            Transaction.merchant_id == merchant_id,
            Transaction.status == "completed"
        ).group_by(Transaction.customer_id).all()

        inactive_30d = sum(
            1 for c in customers
            if (datetime.utcnow() - c.last_tx).days > 30
        )

        if inactive_30d > 0 and len(customers) > 0:
            churn_rate = inactive_30d / len(customers) * 100
            if churn_rate > 20:
                insights.append(OptimizationInsight(
                    category="retention",
                    insight=f"{inactive_30d} customers ({churn_rate:.0f}%) haven't transacted in 30+ days.",
                    impact="high",
                    estimated_value=float(total_revenue) * 0.15,
                    action="Launch re-engagement campaign with 20% discount for inactive users"
                ))

        # 4. Expansion opportunities
        if total_revenue > 0:
            high_value = db.query(Transaction.customer_id).filter(
                Transaction.merchant_id == merchant_id,
                Transaction.status == "completed"
            ).group_by(Transaction.customer_id).having(
                func.sum(Transaction.amount) > float(total_revenue) / len(customers) * 2
            ).count()

            if high_value > 0:
                insights.append(OptimizationInsight(
                    category="expansion",
                    insight=f"{high_value} customers spend 2x+ average. Upsell opportunity.",
                    impact="high",
                    estimated_value=float(avg_tx or 0) * high_value * 12,
                    action="Create enterprise tier with dedicated support and custom pricing"
                ))

        # 5. Geographic expansion
        countries = db.query(
            Transaction.country,
            func.count(func.distinct(Transaction.customer_id)).label("customers"),
            func.sum(Transaction.amount).label("revenue")
        ).filter(
            Transaction.merchant_id == merchant_id,
            Transaction.status == "completed"
        ).group_by(Transaction.country).all()

        if countries:
            top_country = max(countries, key=lambda x: float(x.revenue or 0))
            insights.append(OptimizationInsight(
                category="acquisition",
                insight=f"{top_country.country or 'Unknown'} is your top market with {top_country.customers} customers.",
                impact="medium",
                estimated_value=float(top_country.revenue or 0) * 0.2,
                action=f"Invest in {top_country.country} marketing: localized landing pages + regional pricing"
            ))

        return sorted(insights, key=lambda x: {"high": 3, "medium": 2, "low": 1}[x.impact], reverse=True)


class ForecastEngine:
    """Revenue forecasting using time series analysis"""

    @staticmethod
    def forecast(
        db: Session, merchant_id: str
    ) -> RevenueForecastOut:
        """Generate revenue forecast"""
        mrr = RevenueAnalyzer.calculate_mrr(db, merchant_id)

        # Get historical revenue (90 days)
        trend = RevenueAnalyzer.get_revenue_trend(db, merchant_id, 90)

        if len(trend) < 7:
            # Not enough data, use conservative estimate
            return RevenueForecastOut(
                merchant_id=merchant_id,
                current_mrr=mrr,
                forecast_30d=mrr,
                forecast_90d=mrr * 3,
                forecast_365d=mrr * 12,
                growth_rate=0.0,
                confidence=0.3,
                recommendations=["Need more data for accurate forecasting"]
            )

        # Calculate growth metrics
        first_week = sum(t["revenue"] for t in trend[:7]) / 7
        last_week = sum(t["revenue"] for t in trend[-7:]) / 7

        if first_week > 0:
            weekly_growth = (last_week - first_week) / first_week
        else:
            weekly_growth = 0

        monthly_growth = weekly_growth * 4
        confidence = min(0.95, 0.5 + len(trend) * 0.005)

        # Forecasts
        forecast_30d = mrr * (1 + monthly_growth)
        forecast_90d = forecast_30d * (1 + monthly_growth * 3)
        forecast_365d = mrr * (1 + monthly_growth * 12)

        # Recommendations
        recommendations = []
        if monthly_growth > 0.1:
            recommendations.append("Strong growth trajectory - consider scaling infrastructure")
        if monthly_growth < 0:
            recommendations.append("Revenue declining - investigate churn drivers")
        if confidence < 0.6:
            recommendations.append("Low confidence - need more transaction data")

        return RevenueForecastOut(
            merchant_id=merchant_id,
            current_mrr=round(mrr, 2),
            forecast_30d=round(forecast_30d, 2),
            forecast_90d=round(forecast_90d, 2),
            forecast_365d=round(forecast_365d, 2),
            growth_rate=round(monthly_growth * 100, 2),
            confidence=round(confidence, 2),
            recommendations=recommendations
        )


# ============================================================
# FastAPI App
# ============================================================

app = FastAPI(
    title="Archisynapse Revenue Intelligence Engine",
    version="1.0.0",
    description="Real-time revenue analytics, CLV prediction, churn detection, and optimization for payment platforms"
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

revenue_analyzer = RevenueAnalyzer()
customer_analyzer = CustomerAnalyzer()
churn_predictor = ChurnPredictor()
revenue_optimizer = RevenueOptimizer()
forecast_engine = ForecastEngine()


@app.get("/health")
def health():
    return {
        "status": "ok",
        "service": "archisynapse-analytics",
        "version": "1.0.0"
    }


# ============================================================
# Merchant Management
# ============================================================

@app.post("/admin/merchants", response_model=MerchantOut)
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
        plan=payload.plan
    )
    db.add(merchant)
    db.commit()

    return {
        "merchant_id": payload.merchant_id,
        "name": payload.name,
        "api_key": raw_key,
        "plan": payload.plan
    }


# ============================================================
# Transaction Tracking
# ============================================================

@app.post("/transactions")
def record_transaction(
    event: TransactionIn,
    merchant: Merchant = Depends(get_current_merchant),
    db: Session = Depends(get_db)
):
    tx = Transaction(
        merchant_id=merchant.merchant_id,
        customer_id=event.customer_id,
        amount=event.amount,
        fee_amount=event.fee_amount,
        currency=event.currency,
        status=event.status,
        payment_method=event.payment_method,
        country=event.country
    )
    db.add(tx)
    db.commit()
    return {"status": "recorded", "transaction_id": tx.id}


@app.post("/subscriptions")
def record_subscription(
    event: SubscriptionIn,
    merchant: Merchant = Depends(get_current_merchant),
    db: Session = Depends(get_db)
):
    sub = Subscription(
        merchant_id=merchant.merchant_id,
        customer_id=event.customer_id,
        plan=event.plan,
        mrr=event.mrr,
        status=event.status
    )
    db.add(sub)
    db.commit()
    return {"status": "recorded", "subscription_id": sub.id}


@app.post("/events")
def record_event(
    event: CustomerEventIn,
    merchant: Merchant = Depends(get_current_merchant),
    db: Session = Depends(get_db)
):
    evt = CustomerEvent(
        merchant_id=merchant.merchant_id,
        customer_id=event.customer_id,
        event_type=event.event_type,
        event_data=event.event_data
    )
    db.add(evt)
    db.commit()
    return {"status": "recorded", "event_id": evt.id}


# ============================================================
# Revenue Dashboard
# ============================================================

@app.get("/dashboard", response_model=RevenueDashboard)
def get_dashboard(
    days: int = Query(30, ge=1, le=365),
    merchant: Merchant = Depends(get_current_merchant),
    db: Session = Depends(get_db)
):
    cutoff = datetime.utcnow() - timedelta(days=days)

    # Revenue metrics
    revenue_stats = db.query(
        func.sum(Transaction.amount).label("total_revenue"),
        func.sum(Transaction.fee_amount).label("total_fees"),
        func.count(Transaction.id).label("tx_count"),
        func.avg(Transaction.amount).label("avg_tx"),
        func.count(func.distinct(Transaction.customer_id)).label("unique_customers")
    ).filter(
        Transaction.merchant_id == merchant.merchant_id,
        Transaction.created_at >= cutoff,
        Transaction.status == "completed"
    ).first()

    total_revenue = float(revenue_stats.total_revenue or 0)
    total_fees = float(revenue_stats.total_fees or 0)
    tx_count = revenue_stats.tx_count or 0
    avg_tx = float(revenue_stats.avg_tx or 0)
    unique_customers = revenue_stats.unique_customers or 0

    # New vs churned customers
    period_start = cutoff
    new_customers = db.query(func.count(func.distinct(Transaction.customer_id))).filter(
        Transaction.merchant_id == merchant.merchant_id,
        Transaction.created_at >= period_start
    ).scalar() - db.query(func.count(func.distinct(Transaction.customer_id))).filter(
        Transaction.merchant_id == merchant.merchant_id,
        Transaction.created_at < period_start
    ).scalar()

    new_customers = max(0, new_customers)

    # Retention rate
    prev_period_start = period_start - timedelta(days=days)
    prev_customers = set(
        row[0] for row in db.query(Transaction.customer_id).filter(
            Transaction.merchant_id == merchant.merchant_id,
            Transaction.created_at >= prev_period_start,
            Transaction.created_at < period_start
        ).distinct().all()
    )

    curr_customers = set(
        row[0] for row in db.query(Transaction.customer_id).filter(
            Transaction.merchant_id == merchant.merchant_id,
            Transaction.created_at >= period_start
        ).distinct().all()
    )

    retained = len(prev_customers & curr_customers)
    retention_rate = (retained / len(prev_customers) * 100) if prev_customers else 100.0
    churned = len(prev_customers - curr_customers)

    # MRR and ARR
    mrr = revenue_analyzer.calculate_mrr(db, merchant.merchant_id)
    arr = mrr * 12

    # Net Revenue Retention
    prev_revenue = db.query(func.sum(Transaction.amount)).filter(
        Transaction.merchant_id == merchant.merchant_id,
        Transaction.created_at >= prev_period_start,
        Transaction.created_at < period_start
    ).scalar() or 1

    nrr = (total_revenue / float(prev_revenue) * 100) if prev_revenue else 100.0

    return RevenueDashboard(
        merchant_id=merchant.merchant_id,
        period=f"last_{days}_days",
        mrr=round(mrr, 2),
        arr=round(arr, 2),
        total_revenue=round(total_revenue, 2),
        total_fees=round(total_fees, 2),
        transaction_count=tx_count,
        avg_transaction_size=round(avg_tx, 2),
        unique_customers=unique_customers,
        new_customers=new_customers,
        churned_customers=churned,
        retention_rate=round(retention_rate, 1),
        net_revenue_retention=round(nrr, 1)
    )


@app.get("/revenue/trend")
def revenue_trend(
    days: int = Query(30, ge=1, le=365),
    merchant: Merchant = Depends(get_current_merchant),
    db: Session = Depends(get_db)
):
    return revenue_analyzer.get_revenue_trend(db, merchant.merchant_id, days)


# ============================================================
# Customer Intelligence
# ============================================================

@app.get("/customers/{customer_id}/clv", response_model=CustomerLifetimeValue)
def get_customer_clv(
    customer_id: str,
    merchant: Merchant = Depends(get_current_merchant),
    db: Session = Depends(get_db)
):
    return customer_analyzer.calculate_clv(db, merchant.merchant_id, customer_id)


@app.get("/customers/segments")
def get_customer_segments(
    merchant: Merchant = Depends(get_current_merchant),
    db: Session = Depends(get_db)
):
    return customer_analyzer.get_customer_segments(db, merchant.merchant_id)


@app.get("/customers/{customer_id}/churn", response_model=ChurnPrediction)
def predict_churn(
    customer_id: str,
    merchant: Merchant = Depends(get_current_merchant),
    db: Session = Depends(get_db)
):
    return churn_predictor.predict_churn(db, merchant.merchant_id, customer_id)


@app.get("/customers/churn-risk")
def get_high_risk_customers(
    limit: int = Query(20, ge=1, le=100),
    merchant: Merchant = Depends(get_current_merchant),
    db: Session = Depends(get_db)
):
    """Get all customers ranked by churn risk"""
    customers = db.query(
        Transaction.customer_id,
        func.max(Transaction.created_at).label("last_tx"),
        func.count(Transaction.id).label("tx_count"),
        func.sum(Transaction.amount).label("total_spent")
    ).filter(
        Transaction.merchant_id == merchant.merchant_id,
        Transaction.status == "completed"
    ).group_by(Transaction.customer_id).all()

    risk_list = []
    for cust in customers:
        days_inactive = (datetime.utcnow() - cust.last_tx).days
        risk_score = min(100, days_inactive * 2 + max(0, 30 - cust.tx_count))

        risk_list.append({
            "customer_id": cust.customer_id,
            "days_inactive": days_inactive,
            "total_transactions": cust.tx_count,
            "total_spent": float(cust.total_spent or 0),
            "churn_risk_score": risk_score,
            "risk_level": "critical" if risk_score >= 70 else "high" if risk_score >= 50 else "medium" if risk_score >= 30 else "low"
        })

    risk_list.sort(key=lambda x: x["churn_risk_score"], reverse=True)
    return risk_list[:limit]


# ============================================================
# Forecasting & Optimization
# ============================================================

@app.get("/forecast", response_model=RevenueForecastOut)
def get_forecast(
    merchant: Merchant = Depends(get_current_merchant),
    db: Session = Depends(get_db)
):
    return forecast_engine.forecast(db, merchant.merchant_id)


@app.get("/insights", response_model=List[OptimizationInsight])
def get_insights(
    merchant: Merchant = Depends(get_current_merchant),
    db: Session = Depends(get_db)
):
    return revenue_optimizer.get_insights(db, merchant.merchant_id)


@app.get("/cohort")
def cohort_analysis(
    months: int = Query(6, ge=1, le=12),
    merchant: Merchant = Depends(get_current_merchant),
    db: Session = Depends(get_db)
):
    """Cohort-based retention analysis"""
    cohorts = []
    now = datetime.utcnow()

    for i in range(months):
        cohort_start = now - timedelta(days=30 * (i + 1))
        cohort_end = now - timedelta(days=30 * i)

        # Customers who first purchased in this cohort period
        cohort_customers = set(
            row[0] for row in db.query(Transaction.customer_id).filter(
                Transaction.merchant_id == merchant.merchant_id,
                Transaction.created_at >= cohort_start,
                Transaction.created_at < cohort_end
            ).distinct().all()
        )

        if not cohort_customers:
            continue

        # Retention rates for subsequent months
        retention_rates = {}
        for m in range(i + 1):
            check_start = cohort_end + timedelta(days=30 * m)
            check_end = check_start + timedelta(days=30)

            active_in_month = set(
                row[0] for row in db.query(Transaction.customer_id).filter(
                    Transaction.merchant_id == merchant.merchant_id,
                    Transaction.customer_id.in_(cohort_customers),
                    Transaction.created_at >= check_start,
                    Transaction.created_at < check_end
                ).distinct().all()
            )

            retention_rates[f"month_{m}"] = round(
                len(active_in_month) / len(cohort_customers) * 100, 1
            )

        # Revenue for cohort
        cohort_revenue = db.query(func.sum(Transaction.amount)).filter(
            Transaction.merchant_id == merchant.merchant_id,
            Transaction.customer_id.in_(cohort_customers)
        ).scalar() or 0

        cohorts.append({
            "cohort_month": cohort_start.strftime("%Y-%m"),
            "customers": len(cohort_customers),
            "retention_rates": retention_rates,
            "avg_revenue_per_cohort": round(float(cohort_revenue) / len(cohort_customers), 2)
        })

    return sorted(cohorts, key=lambda x: x["cohort_month"], reverse=True)
