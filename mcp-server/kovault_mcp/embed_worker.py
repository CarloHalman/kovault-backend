"""Background embedding worker (F6).

Writes now ack immediately with `embedded_at` left NULL; this daemon is the "waiting list": it
drains the pending set (`embedded_at IS NULL OR embedded_at < updated_at`) oldest-first, embeds a
whole batch in ONE endpoint request, and writes the vectors back. `janitor -embed` stays the manual
backstop and a restart just resumes draining.

Robustness:
- rows are READ unlocked; the write-back UPDATE guards on `updated_at`, so a row edited mid-embed
  re-queues instead of storing a stale vector (no DB lock held across the slow HTTP call).
- a transient endpoint problem (connect/timeout) backs off instead of spinning and does NOT count
  against a row; a genuine per-row failure (e.g. oversized text) is isolated and skipped after
  `max_retries`, so one bad row can never starve everything behind it.
"""
from __future__ import annotations

import logging
import threading

from . import embedding_text as et

log = logging.getLogger("kovault_mcp.embed_worker")

_TABLES = ("headers", "tasks", "decisions", "sources")
_TRANSIENT = {"ConnectError", "ConnectTimeout", "ReadTimeout", "WriteTimeout",
              "TimeoutException", "PoolTimeout", "ReadError", "RemoteProtocolError"}


def _pending(db, table: str, limit: int) -> list[dict]:
    return db.query(
        f"SELECT id FROM {table} WHERE trashed_at IS NULL "
        f"AND (embedded_at IS NULL OR embedded_at < updated_at) ORDER BY updated_at LIMIT %s",
        (limit,))


def _embed_batch(db, embedder, table: str, ids: list) -> int:
    """Compose + embed `ids` in one request and write back. Returns rows actually updated."""
    col = et.COMPOSERS[table][1]
    rows = db.query(f"SELECT * FROM {table} WHERE id = ANY(%s)", ([str(i) for i in ids],))
    payload = [(str(r["id"]), et.compose(table, dict(r)), r.get("updated_at")) for r in rows]
    payload = [(rid, text, ua) for rid, text, ua in payload if text.strip()]
    if not payload:
        return 0
    vecs = embedder.embed_many([text for _, text, _ in payload])
    n = 0
    with db.connection() as conn:
        with conn.cursor() as cur:
            for (rid, _text, ua), literal in zip(payload, vecs):
                cur.execute(
                    f"UPDATE {table} SET {col} = %s::halfvec, embedded_at = now() "
                    f"WHERE id = %s AND updated_at = %s", (literal, rid, ua))
                n += cur.rowcount
        conn.commit()
    return n


def run(db, embedder_factory, stop: threading.Event,
        poll: float = 3.0, batch: int = 32, max_retries: int = 3) -> None:
    fails: dict = {}
    while not stop.is_set():
        did = 0
        try:
            embedder = embedder_factory()
            for table in _TABLES:
                ids = [r["id"] for r in _pending(db, table, batch)
                       if fails.get((table, str(r["id"])), 0) < max_retries]
                if not ids:
                    continue
                try:
                    did += _embed_batch(db, embedder, table, ids)
                    for i in ids:
                        fails.pop((table, str(i)), None)
                except Exception as e:
                    if e.__class__.__name__ in _TRANSIENT:
                        log.warning("embed endpoint issue (%s); backing off: %s", table, e)
                        stop.wait(min(poll * 4, 30))       # down -> wait, don't penalize rows
                    else:                                   # isolate the bad row(s)
                        for i in ids:
                            try:
                                did += _embed_batch(db, embedder, table, [i])
                                fails.pop((table, str(i)), None)
                            except Exception as e2:
                                fails[(table, str(i))] = fails.get((table, str(i)), 0) + 1
                                log.warning("row %s embed failed (%d): %s",
                                            i, fails[(table, str(i))], e2)
        except Exception as e:                              # never let the loop die
            log.warning("embed worker loop error: %s", e)
            stop.wait(poll)
        if did == 0:
            stop.wait(poll)


def start(db, embedder_factory, poll: float = 3.0) -> threading.Event:
    """Spawn the worker as a daemon thread (dies with the process). Returns its stop Event."""
    stop = threading.Event()
    threading.Thread(target=run, args=(db, embedder_factory, stop), kwargs={"poll": poll},
                     name="kovault-embed-worker", daemon=True).start()
    log.info("embed worker started (poll=%ss)", poll)
    return stop
