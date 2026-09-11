"""
Database access layer for National Volleyball League.

Replaces the old SQLite (nvl.db) local file. The bot now talks
directly to the same Supabase Postgres database the site uses, via a
plain Postgres connection (asyncpg) - no local "shadow" copy, no
sync bridge. There is exactly one source of truth per table.

Every function here is a thin async wrapper around a shared
connection pool. Call sites look almost the same as the old sqlite3
helpers, except every call needs `await` and placeholders are
Postgres-style ($1, $2, ...) instead of SQLite's "?".

Row objects (asyncpg.Record) support the same dict-style access the
old sqlite3.Row did (row["column_name"]), so most call sites that
read result rows do not need to change.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from typing import Any, AsyncIterator

import asyncpg

import config

_pool: asyncpg.Pool | None = None


async def init_pool() -> None:
    """Create the shared connection pool. Call once from bot.py's setup_hook."""
    global _pool
    if _pool is not None:
        return
    if not config.DATABASE_URL:
        raise RuntimeError(
            "DATABASE_URL is not set. Copy .env.example, fill in your Supabase "
            "Postgres connection string, and restart the bot."
        )
    _pool = await asyncpg.create_pool(
        dsn=config.DATABASE_URL,
        min_size=1,
        max_size=5,
        # Required for compatibility with Supabase's PgBouncer pooler
        # (transaction mode does not support server-side prepared
        # statement caching). Harmless if you use the direct/session
        # connection instead.
        statement_cache_size=0,
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
        raise RuntimeError("Database pool is not initialized. Call database.init_pool() first.")
    return _pool


async def execute(query: str, *params: Any) -> str:
    """Run an INSERT/UPDATE/DELETE (or DDL). Returns the driver status string."""
    pool = _require_pool()
    async with pool.acquire() as conn:
        return await conn.execute(query, *params)


async def fetchval(query: str, *params: Any) -> Any:
    """Run a query and return a single scalar value, e.g. `... RETURNING id`."""
    pool = _require_pool()
    async with pool.acquire() as conn:
        return await conn.fetchval(query, *params)


async def fetchone(query: str, *params: Any) -> asyncpg.Record | None:
    pool = _require_pool()
    async with pool.acquire() as conn:
        return await conn.fetchrow(query, *params)


async def fetchall(query: str, *params: Any) -> list[asyncpg.Record]:
    pool = _require_pool()
    async with pool.acquire() as conn:
        return await conn.fetch(query, *params)


@asynccontextmanager
async def transaction() -> AsyncIterator[asyncpg.Connection]:
    """
    Use for multi-statement operations that must succeed or fail
    together (e.g. finishing a ranked match and updating every
    participant's ELO). Usage:

        async with database.transaction() as conn:
            await conn.execute(...)
            await conn.execute(...)
    """
    pool = _require_pool()
    async with pool.acquire() as conn:
        async with conn.transaction():
            yield conn


async def insert_returning(table: str, values: dict[str, Any]) -> asyncpg.Record | None:
    """
    INSERT INTO <table> (...) VALUES (...) RETURNING *, built from a plain
    dict - mirrors the `.insert(payload)` style already used on the site
    side. Keeps call sites readable and avoids hand-counting $1, $2, ...
    placeholders (a common source of bugs when porting SQLite queries).
    """
    columns = list(values.keys())
    placeholders = ", ".join(f"${i + 1}" for i in range(len(columns)))
    query = f'INSERT INTO {table} ({", ".join(columns)}) VALUES ({placeholders}) RETURNING *'
    return await fetchone(query, *[values[c] for c in columns])


async def update_returning(
    table: str, values: dict[str, Any], where: dict[str, Any]
) -> asyncpg.Record | None:
    """UPDATE <table> SET ... WHERE ... RETURNING *, built from plain dicts."""
    set_columns = list(values.keys())
    where_columns = list(where.keys())
    set_clause = ", ".join(f"{c} = ${i + 1}" for i, c in enumerate(set_columns))
    where_clause = " AND ".join(
        f"{c} = ${i + 1 + len(set_columns)}" for i, c in enumerate(where_columns)
    )
    query = f"UPDATE {table} SET {set_clause} WHERE {where_clause} RETURNING *"
    params = [values[c] for c in set_columns] + [where[c] for c in where_columns]
    return await fetchone(query, *params)


def did(value: Any) -> str | None:
    """
    Normalize a Discord snowflake (int, str, discord.abc.Snowflake, or
    None) to the text form used by every discord_id-style column.
    Every value written to or compared against a discord_id column
    should be passed through this first.
    """
    if value is None:
        return None
    return str(int(value)) if isinstance(value, str) and value.isdigit() else str(value)
