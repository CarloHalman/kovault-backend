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
    "freshness_days": {"hot": 30, "warm": 90},
    "freshness_auto": {"enabled": True, "cooldown_seconds": 3600},
    "embedding": {"model": "Qwen3-Embedding-8B", "endpoint": "http://embedding:8080", "dims": 4000},
    "embed_worker": {"enabled": True, "poll_seconds": 3, "batch": 32, "max_retries": 3},
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
