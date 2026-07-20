"""
Postgres connection pool for the gateway's own royalty-loop state
(receipts, idempotency, rejections, tenant keys). This is NOT financial
truth -- the ledger stays the sole source of that -- but it must be
durable across process restarts, which a JSON file on one machine is not.
"""

import os
from typing import Optional

import asyncpg

DATABASE_URL = os.getenv(
    "DATABASE_URL", "postgresql://postgres:postgres@localhost:5432/archisynapse"
)

_pool: Optional[asyncpg.Pool] = None


async def init_pool() -> asyncpg.Pool:
    global _pool
    if _pool is None:
        _pool = await asyncpg.create_pool(DATABASE_URL, min_size=1, max_size=10)
    return _pool


def get_pool() -> asyncpg.Pool:
    if _pool is None:
        raise RuntimeError("royalty_db pool not initialized -- call init_pool() at startup")
    return _pool


async def close_pool() -> None:
    global _pool
    if _pool is not None:
        await _pool.close()
        _pool = None
