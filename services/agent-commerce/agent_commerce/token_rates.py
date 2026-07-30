from __future__ import annotations

import math
from typing import Any
from uuid import uuid4

from .errors import RateCardNotFound
from .models import iso_now
from .token_common import _parse_time, _row


class RateCardMixin:
    def put_rate_card(
        self,
        *,
        provider: str,
        model: str,
        input_microusd_per_million: int,
        output_microusd_per_million: int,
        cached_input_microusd_per_million: int = 0,
        reasoning_microusd_per_million: int = 0,
        source_reference: str,
        effective_at: str | None = None,
    ) -> dict[str, Any]:
        if min(
            input_microusd_per_million,
            output_microusd_per_million,
            cached_input_microusd_per_million,
            reasoning_microusd_per_million,
        ) < 0:
            raise ValueError("rate card values cannot be negative")
        if not source_reference.strip():
            raise ValueError("source_reference is required")
        now = iso_now()
        effective = effective_at or now
        _parse_time(effective)
        rate_id = f"rate_{uuid4().hex}"
        with self.db.transaction(immediate=True) as conn:
            conn.execute(
                """
                INSERT INTO ai_rate_cards
                (id, provider, model, input_microusd_per_million,
                 output_microusd_per_million, cached_input_microusd_per_million,
                 reasoning_microusd_per_million, source_reference, effective_at,
                 active, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?)
                """,
                (
                    rate_id,
                    provider.strip().lower(),
                    model.strip(),
                    input_microusd_per_million,
                    output_microusd_per_million,
                    cached_input_microusd_per_million,
                    reasoning_microusd_per_million,
                    source_reference.strip(),
                    effective,
                    now,
                ),
            )
            self.db.append_audit_event(
                conn,
                event_id=f"evt_{uuid4().hex}",
                aggregate_type="ai_rate_card",
                aggregate_id=rate_id,
                event_type="ai_rate_card.created",
                payload={
                    "provider": provider.strip().lower(),
                    "model": model.strip(),
                    "effective_at": effective,
                    "source_reference": source_reference.strip(),
                },
            )
        return self.get_rate_card(provider=provider, model=model, at=effective)

    def get_rate_card(self, *, provider: str, model: str, at: str | None = None) -> dict[str, Any]:
        at_time = at or iso_now()
        conn = self.db.connect()
        try:
            row = conn.execute(
                """
                SELECT * FROM ai_rate_cards
                WHERE provider=? AND model=? AND active=1 AND effective_at<=?
                ORDER BY effective_at DESC LIMIT 1
                """,
                (provider.strip().lower(), model.strip(), at_time),
            ).fetchone()
        finally:
            conn.close()
        if not row:
            raise RateCardNotFound(f"no active rate card for {provider}/{model}")
        return _row(row)

    @staticmethod
    def calculate_cost(
        rate: dict[str, Any],
        *,
        input_tokens: int,
        output_tokens: int,
        cached_input_tokens: int = 0,
        reasoning_tokens: int = 0,
    ) -> int:
        values = (input_tokens, output_tokens, cached_input_tokens, reasoning_tokens)
        if min(values) < 0:
            raise ValueError("token counts cannot be negative")
        numerator = (
            input_tokens * int(rate["input_microusd_per_million"])
            + output_tokens * int(rate["output_microusd_per_million"])
            + cached_input_tokens * int(rate["cached_input_microusd_per_million"])
            + reasoning_tokens * int(rate["reasoning_microusd_per_million"])
        )
        return math.ceil(numerator / 1_000_000)
