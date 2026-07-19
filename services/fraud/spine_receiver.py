"""
ArchiSynapse Spine Receiver — the trust-plane end of the Empire Spine.

Receives signed `track.generated` events from Lyrica, verifies the HMAC,
dedupes on event_id, and mints the Digital Birth Certificate:

    POST /api/v1/events                      accept + mint (idempotent)
    GET  /api/v1/certificates/{dna_tag}      fetch certificate
    GET  /api/v1/certificates/{dna_tag}/verify   recompute + verify seal
    GET  /api/v1/certificates                list (paginated)

Contract: empire1-lyrica-ecosystem/contracts/track_generated.v1.schema.json
Signing:  HMAC-SHA256 over canonical JSON (sorted keys, no whitespace),
          key = EMPIRE_SPINE_SIGNING_KEY (must match Lyrica's).

Mounted by archisynapse_fraud_mvp.py via `app.include_router(spine_router)`.
Uses the same SQLAlchemy Base/engine as the fraud service — one deploy,
one database, one trust plane.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os
from datetime import datetime
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import Boolean, Column, DateTime, Integer, String, Text
from sqlalchemy.orm import Session

logger = logging.getLogger("archisynapse.spine")

ENV_SIGNING_KEY = "EMPIRE_SPINE_SIGNING_KEY"


# ---------------------------------------------------------------------------
# Models — attached to the host app's Base at include time (see register())
# ---------------------------------------------------------------------------

def build_models(Base):
    class SpineEventRecord(Base):
        __tablename__ = "spine_events"

        id = Column(Integer, primary_key=True, index=True)
        event_id = Column(String, unique=True, index=True, nullable=False)
        event_type = Column(String, index=True, nullable=False)
        dna_tag = Column(String, index=True, nullable=True)
        payload = Column(Text, nullable=False)
        signature_valid = Column(Boolean, default=False)
        received_at = Column(DateTime, default=datetime.utcnow)

    class BirthCertificate(Base):
        __tablename__ = "birth_certificates"

        id = Column(Integer, primary_key=True, index=True)
        dna_tag = Column(String, unique=True, index=True, nullable=False)
        event_id = Column(String, index=True, nullable=False)
        track_title = Column(String, nullable=True)
        core_genre = Column(String, nullable=True)
        cultural_lens = Column(String, nullable=True)
        creator_content_hash = Column(String, nullable=False)   # from Lyrica
        certificate_seal = Column(String, nullable=False)       # minted here
        stakeholders_json = Column(Text, nullable=False)
        royalty_split_json = Column(Text, nullable=False)
        ai_models_json = Column(Text, nullable=True)
        artifacts_json = Column(Text, nullable=True)
        source_transaction_id = Column(String, nullable=True)
        minted_at = Column(DateTime, default=datetime.utcnow, index=True)

    return SpineEventRecord, BirthCertificate


# ---------------------------------------------------------------------------
# Signature verification (mirror of Lyrica's empire_spine.events)
# ---------------------------------------------------------------------------

def canonical_json(obj: dict) -> bytes:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


def verify_signature(event: dict) -> bool:
    key = os.environ.get(ENV_SIGNING_KEY, "")
    if not key:
        logger.error("%s not set — rejecting all spine events", ENV_SIGNING_KEY)
        return False
    provided = event.get("signature", "")
    if not provided.startswith("hmac-sha256:"):
        return False
    body = {k: v for k, v in event.items() if k not in ("signature", "signing_key_id")}
    expected = hmac.new(key.encode("utf-8"), canonical_json(body), hashlib.sha256).hexdigest()
    return hmac.compare_digest(provided[len("hmac-sha256:"):], expected)


def mint_seal(event: dict) -> str:
    """The certificate seal: SHA-256 over the full signed event.

    Anyone holding the event can recompute this. Daily Merkle anchoring of
    seals happens in a separate job (wedge plan, days 15-45).
    """
    return "sha256:" + hashlib.sha256(canonical_json(event)).hexdigest()


# ---------------------------------------------------------------------------
# Router factory
# ---------------------------------------------------------------------------

class EventAccepted(BaseModel):
    status: str
    event_id: str
    dna_tag: Optional[str] = None
    certificate_seal: Optional[str] = None
    duplicate: bool = False


def register(Base, engine, get_db, get_current_merchant) -> APIRouter:
    """Create tables and return the router. Called by the host app."""
    SpineEventRecord, BirthCertificate = build_models(Base)
    Base.metadata.create_all(bind=engine, tables=[
        SpineEventRecord.__table__, BirthCertificate.__table__,
    ])

    router = APIRouter(prefix="/api/v1")

    @router.post("/events", response_model=EventAccepted)
    def receive_event(
        event: dict,
        merchant=Depends(get_current_merchant),
        db: Session = Depends(get_db),
    ):
        event_id = event.get("event_id")
        if not event_id:
            raise HTTPException(status_code=422, detail="event_id required")

        # Idempotency: same event_id -> same answer, no double-mint
        existing = db.query(SpineEventRecord).filter(
            SpineEventRecord.event_id == event_id
        ).first()
        if existing:
            cert = db.query(BirthCertificate).filter(
                BirthCertificate.event_id == event_id
            ).first()
            return EventAccepted(
                status="ok", event_id=event_id,
                dna_tag=existing.dna_tag,
                certificate_seal=cert.certificate_seal if cert else None,
                duplicate=True,
            )

        if not verify_signature(event):
            raise HTTPException(status_code=401, detail="invalid event signature")

        record = SpineEventRecord(
            event_id=event_id,
            event_type=event.get("event_type", "unknown"),
            dna_tag=event.get("dna_tag"),
            payload=json.dumps(event, sort_keys=True, separators=(",", ":")),
            signature_valid=True,
        )
        db.add(record)

        seal = None
        if event.get("event_type") == "track.generated":
            dna_tag = event.get("dna_tag")
            if not dna_tag or not event.get("content_hash"):
                raise HTTPException(status_code=422, detail="dna_tag and content_hash required")

            dup_cert = db.query(BirthCertificate).filter(
                BirthCertificate.dna_tag == dna_tag
            ).first()
            if dup_cert:
                # Same track, different event: refuse silently re-minting under
                # a new event — this is the single-writer-per-dna_tag rule.
                db.commit()
                logger.warning("certificate for %s already exists (event %s); not re-minting",
                               dna_tag, event_id)
                return EventAccepted(
                    status="ok", event_id=event_id, dna_tag=dna_tag,
                    certificate_seal=dup_cert.certificate_seal, duplicate=True,
                )

            seal = mint_seal(event)
            db.add(BirthCertificate(
                dna_tag=dna_tag,
                event_id=event_id,
                track_title=event.get("track_title"),
                core_genre=event.get("core_genre"),
                cultural_lens=event.get("cultural_lens"),
                creator_content_hash=event["content_hash"],
                certificate_seal=seal,
                stakeholders_json=json.dumps(event.get("stakeholders", {})),
                royalty_split_json=json.dumps(event.get("royalty_split", {})),
                ai_models_json=json.dumps(event.get("ai_models_used", [])),
                artifacts_json=json.dumps(event.get("artifacts", {})),
                source_transaction_id=event.get("source_transaction_id"),
            ))
            logger.info("minted birth certificate for %s (seal %s...)", dna_tag, seal[:23])

        db.commit()
        return EventAccepted(status="ok", event_id=event_id,
                             dna_tag=event.get("dna_tag"), certificate_seal=seal)

    @router.get("/certificates/{dna_tag}")
    def get_certificate(dna_tag: str, db: Session = Depends(get_db)):
        cert = db.query(BirthCertificate).filter(
            BirthCertificate.dna_tag == dna_tag
        ).first()
        if not cert:
            raise HTTPException(status_code=404, detail="certificate not found")
        return {
            "title": "Digital Birth Certificate",
            "dna_tag": cert.dna_tag,
            "track_title": cert.track_title,
            "core_genre": cert.core_genre,
            "cultural_lens": cert.cultural_lens,
            "creator_content_hash": cert.creator_content_hash,
            "certificate_seal": cert.certificate_seal,
            "stakeholders": json.loads(cert.stakeholders_json),
            "royalty_split": json.loads(cert.royalty_split_json),
            "ai_models_used": json.loads(cert.ai_models_json or "[]"),
            "artifacts": json.loads(cert.artifacts_json or "{}"),
            "source_transaction_id": cert.source_transaction_id,
            "minted_at": cert.minted_at,
            "event_id": cert.event_id,
        }

    @router.get("/certificates/{dna_tag}/verify")
    def verify_certificate(dna_tag: str, db: Session = Depends(get_db)):
        cert = db.query(BirthCertificate).filter(
            BirthCertificate.dna_tag == dna_tag
        ).first()
        if not cert:
            raise HTTPException(status_code=404, detail="certificate not found")
        record = db.query(SpineEventRecord).filter(
            SpineEventRecord.event_id == cert.event_id
        ).first()
        if not record:
            return {"dna_tag": dna_tag, "valid": False, "reason": "source event missing"}
        event = json.loads(record.payload)
        recomputed = mint_seal(event)
        sig_ok = verify_signature(event)
        seal_ok = hmac.compare_digest(recomputed, cert.certificate_seal)
        return {
            "dna_tag": dna_tag,
            "valid": sig_ok and seal_ok,
            "signature_valid": sig_ok,
            "seal_valid": seal_ok,
            "certificate_seal": cert.certificate_seal,
            "minted_at": cert.minted_at,
        }

    @router.get("/certificates")
    def list_certificates(limit: int = 50, offset: int = 0, db: Session = Depends(get_db)):
        q = db.query(BirthCertificate).order_by(BirthCertificate.minted_at.desc())
        total = q.count()
        rows = q.offset(offset).limit(min(limit, 200)).all()
        return {
            "total": total,
            "data": [
                {
                    "dna_tag": c.dna_tag,
                    "track_title": c.track_title,
                    "certificate_seal": c.certificate_seal,
                    "minted_at": c.minted_at,
                }
                for c in rows
            ],
        }

    return router
