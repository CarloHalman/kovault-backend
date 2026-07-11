"""Entrypoint: open the DB pool, wire the server, serve the MCP HTTP endpoint.

Users' plugins connect to http://<host>:<KOVAULT_MCP_PORT>/mcp.
"""
from __future__ import annotations

import logging

from .config import Config
from .db import Database
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
    logging.getLogger("kovault_mcp").info("serving MCP on http://%s:%s/mcp", cfg.mcp_host, cfg.mcp_port)
    mcp.run(transport="http", host=cfg.mcp_host, port=cfg.mcp_port)


if __name__ == "__main__":
    main()
