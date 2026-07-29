"""Postgres access — a small psycopg3 connection pool plus query helpers and the settings
loader. This is the only module that opens DB connections.
"""
from __future__ import annotations

import os
from contextlib import contextmanager
from typing import Any

import psycopg
from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool

from .config import Config

# Fallback if a settings row is missing (mirrors docker/02-init.sql).
DEFAULT_SETTINGS: dict[str, Any] = {
    "rrf_k": 60,
    "ladder_chunks": {"r": 0.70, "floor": 3, "cap": 9},
    "ladder_pages": {"r": 0.75, "floor": 1, "cap": 6},
    # Must stay byte-identical to the row 02-init.sql seeds — this is the fallback used when that
    # row is missing, so a mismatch means the server silently talks to a different endpoint than
    # the one the operator configured. `http://embedding:8080` sat here for months and pointed at
    # nothing; tests/test_deploy_config.py now fails if the two drift again.
    "embedding": {"model": "qwen3-embedding:8b", "endpoint": "http://embedding:11434", "dims": 4000},
    "embed_worker": {"enabled": True, "poll_seconds": 3, "batch": 32, "max_retries": 3},
    "debug": False,   # server-side gate for the raw `sql` tool (B2). Off unless an admin says so.
}


class Database:
    def __init__(self, config: Config):
        self._pool = ConnectionPool(
            conninfo=config.dsn,
            min_size=1,
            max_size=int(os.getenv("KOVAULT_DB_POOL", "8")),
            kwargs={"row_factory": dict_row, "autocommit": False},
            open=False,
        )

    def open(self) -> None:
        self._pool.open()

    def close(self) -> None:
        self._pool.close()

    @contextmanager
    def connection(self):
        """A pooled connection; commits on clean exit, rolls back on exception."""
        with self._pool.connection() as conn:
            yield conn

    def query(self, sql: str, params: Any = None) -> list[dict]:
        # "cached plan must not change result type": a pooled connection prepared this statement
        # before the schema changed under it. Postgres raises once per connection, that connection
        # re-plans, and the next attempt on it succeeds — so a column added to a LIVE server (an
        # extension running its own migration, which is what the `columns` parameter exists to
        # surface) costs a retry rather than erroring until someone restarts the process.
        # Bounded by the pool size, not by 1: a retry can be handed a different stale connection,
        # and each one can only raise once. Measured 2 failures across 30 calls on an 8-connection
        # pool before this, 0 after.
        for attempt in range(self._pool.max_size):
            try:
                return self._query(sql, params)
            except psycopg.errors.FeatureNotSupported:
                if attempt == self._pool.max_size - 1:
                    raise
        raise AssertionError("unreachable")          # loop either returns or re-raises

    def _query(self, sql: str, params: Any = None) -> list[dict]:
        with self._pool.connection() as conn:
            with conn.cursor() as cur:
                cur.execute(sql, params)
                return cur.fetchall() if cur.description else []

    def query_one(self, sql: str, params: Any = None) -> dict | None:
        rows = self.query(sql, params)
        return rows[0] if rows else None

    def settings(self) -> dict[str, Any]:
        """Server settings merged over defaults. Read live — tiny table."""
        merged = dict(DEFAULT_SETTINGS)
        try:
            for row in self.query("SELECT key, value FROM settings"):
                merged[row["key"]] = row["value"]
        except psycopg.Error:
            pass
        return merged
