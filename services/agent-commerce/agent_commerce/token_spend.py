from __future__ import annotations

from .receipts import ReceiptSigner
from .storage import Database
from .token_common import TOKEN_SCHEMA
from .token_rates import RateCardMixin
from .token_policy import PolicyMixin
from .token_preflight import PreflightMixin
from .token_usage import UsageMixin
from .token_reconcile import ReconcileMixin


class TokenSpendStore(RateCardMixin, PolicyMixin, PreflightMixin, UsageMixin, ReconcileMixin):
    def __init__(self, db: Database, signer: ReceiptSigner):
        self.db = db
        self.signer = signer
        conn = self.db.connect()
        try:
            conn.executescript(TOKEN_SCHEMA)
        finally:
            conn.close()
