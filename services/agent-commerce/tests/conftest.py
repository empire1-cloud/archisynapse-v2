from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from agent_commerce.authorization import AuthorizationStore
from agent_commerce.receipts import ReceiptSigner, ReceiptStore
from agent_commerce.reputation import ReputationStore
from agent_commerce.storage import Database


@pytest.fixture
def stack(tmp_path):
    db = Database(tmp_path / "commerce.db")
    auth = AuthorizationStore(db)
    signer = ReceiptSigner("r" * 32)
    receipts = ReceiptStore(db, signer)
    reputation = ReputationStore(db, receipts)
    return db, auth, signer, receipts, reputation


@pytest.fixture
def expires_at():
    return (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat().replace("+00:00", "Z")
