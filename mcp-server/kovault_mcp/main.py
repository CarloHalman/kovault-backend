"""Entrypoint: open the DB pool, wire the server, serve the MCP HTTP endpoint.

Users' plugins connect to http://<host>:<KOVAULT_MCP_PORT>/mcp.
"""
from __future__ import annotations

import logging
import os

from .config import Config
from .db import Database
from . import embed_worker
from . import server


def build() -> tuple[Config, object]:
    cfg = Config()
    database = Database(cfg)
    database.open()
    server.configure(database)
    return cfg, server.mcp


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    cfg, mcp = build()
    try:                                                  # deploy-order guard (F4): migration first
        if not server.db().query("SELECT 1 FROM information_schema.columns "
                                 "WHERE table_name='tasks' AND column_name='completed_at'"):
            logging.getLogger("kovault_mcp").error(
                "SCHEMA OUT OF DATE: tasks.completed_at missing — run migrate_1.3.0.sql before the "
                "1.3.0 server, or task inserts will fail on the old NOT NULL scope/priority.")
    except Exception:
        pass
    if os.getenv("KOVAULT_EMBED_WORKER", "1") != "0":     # background embedding drain (F6)
        s = server.db().settings().get("embed_worker") or {}
        if s.get("enabled", True):
            embed_worker.start(server.db(), server._embedder, poll=float(s.get("poll_seconds", 3)))
    logging.getLogger("kovault_mcp").info("serving MCP on http://%s:%s/mcp", cfg.mcp_host, cfg.mcp_port)
    mcp.run(transport="http", host=cfg.mcp_host, port=cfg.mcp_port)


if __name__ == "__main__":
    main()
