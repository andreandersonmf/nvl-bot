"""
Database access layer — NVL Bot.

Uses asyncpg with a shared connection pool. The key design principle:
callers that need multiple queries in one operation should use the
`conn()` context manager to acquire ONE connection and reuse it,
instead of letting each helper acquire/release separately.

This eliminates pool contention (the main cause of perceived latency)
when several queries happen in sequence inside a single interaction.
"""
from __future__ import annotations

from contextlib import asynccontextmanager
from typing import Any, AsyncIterator

import asyncpg
import config

_pool: asyncpg.Pool | None = None


async def init_pool() -> None:
    global _pool
    if _pool is not None:
        return
    if not config.DATABASE_URL:
        raise RuntimeError("DATABASE_URL is not set.")
    _pool = await asyncpg.create_pool(
        dsn=config.DATABASE_URL,
        min_size=2,
        max_size=10,          # raised from 5 — handles concurrent interactions
        statement_cache_size=0,  # required for Supabase PgBouncer
    )


async def close_pool() -> None:
    global _pool
    if _pool is not None:
        await _pool.close()
        _pool = None


def is_ready() -> bool:
    return _pool is not None


def _require_pool() -> asyncpg.Pool:
    if _pool is None:
        raise RuntimeError("Database pool not initialized.")
    return _pool


# ---------------------------------------------------------------------------
# Single-connection context manager
# Use this when you have multiple queries in one operation so they all share
# one pool slot instead of each queuing for their own.
# ---------------------------------------------------------------------------

@asynccontextmanager
async def conn() -> AsyncIterator[asyncpg.Connection]:
    """Acquire one connection for the duration of the block."""
    async with _require_pool().acquire() as c:
        yield c


@asynccontextmanager
async def transaction() -> AsyncIterator[asyncpg.Connection]:
    """Acquire one connection and wrap everything in a transaction."""
    async with _require_pool().acquire() as c:
        async with c.transaction():
            yield c


# ---------------------------------------------------------------------------
# Convenience one-shot helpers (each acquires its own connection).
# Fine for isolated single queries; prefer conn() for multi-query blocks.
# ---------------------------------------------------------------------------

async def execute(query: str, *params: Any) -> str:
    async with _require_pool().acquire() as c:
        return await c.execute(query, *params)


async def fetchval(query: str, *params: Any) -> Any:
    async with _require_pool().acquire() as c:
        return await c.fetchval(query, *params)


async def fetchone(query: str, *params: Any) -> asyncpg.Record | None:
    async with _require_pool().acquire() as c:
        return await c.fetchrow(query, *params)


async def fetchall(query: str, *params: Any) -> list[asyncpg.Record]:
    async with _require_pool().acquire() as c:
        return await c.fetch(query, *params)


def did(value: Any) -> str | None:
    """Normalise a Discord snowflake to the text form used in every discord_id column."""
    if value is None:
        return None
    if isinstance(value, str):
        return value if value.isdigit() else value
    return str(value)