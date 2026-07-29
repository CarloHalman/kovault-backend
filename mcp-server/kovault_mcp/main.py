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
    try:                                                  # deploy-order guard: migration first
        if not server.db().query("SELECT 1 FROM information_schema.columns "
                                 "WHERE table_name='pages' AND column_name='lifecycle'"):
            logging.getLogger("kovault_mcp").error(
                "SCHEMA OUT OF DATE: pages.lifecycle missing — run migrate_1.5.0.sql before the "
                "1.5.0 server. Until then every read and write of a page fails: this server "
                "trashes via pages.trashed_at and filters on pages.lifecycle, neither of which "
                "exists in the pre-1.5.0 freshness schema.")
    except Exception:
        pass
    if os.getenv("KOVAULT_EMBED_WORKER", "1") != "0":     # background embedding drain (F6)
        s = server.db().settings().get("embed_worker") or {}
        if s.get("enabled", True):
            embed_worker.start(server.db(), server._embedder, poll=float(s.get("poll_seconds", 3)))
    tokens = cfg.auth_tokens
    _warn_if_open(tokens, cfg)
    log = logging.getLogger("kovault_mcp")
    log.info("serving MCP on http://%s:%s/mcp (%s)", cfg.mcp_host, cfg.mcp_port,
             f"bearer auth, {len(tokens)} token(s) accepted" if tokens else "NO AUTH")
    mcp.run(transport="http", host=cfg.mcp_host, port=cfg.mcp_port,
            middleware=server.http_middleware(tokens))


def _warn_if_open(tokens: list[str], cfg: Config) -> None:
    """No token configured is a supported state — an upgrade must never lock someone out of their
    own vault, and they cannot fix that without a shell. It is not a quiet one: the operator has
    to be able to see, in one screenful of log, exactly what is exposed to anyone who can reach
    the port."""
    if tokens:
        return
    log = logging.getLogger("kovault_mcp")
    for line in (
        "=" * 78,
        "KOVAULT IS RUNNING WITHOUT AUTHENTICATION",
        "",
        "No KOVAULT_AUTH_TOKEN / KOVAULT_AUTH_TOKEN_FILE is set, so ANY caller that can reach",
        f"http://{cfg.mcp_host}:{cfg.mcp_port} can, with no credential at all:",
        "  - read AND WRITE every page, task, decision and source   (/mcp)",
        "  - download the ENTIRE vault as a zip                     (GET  /export)",
        "  - rewrite the file paths of every source                 (POST /relocate-sources)",
        "  - list every page id and modification time               (GET  /page-meta)",
        "  - write into the debug log                               (POST /debug-log)",
        "",
        "To close it: put a token in docker/secrets/kovault_auth_token.txt (see the .example),",
        "restart the stack, and give clients:  Authorization: Bearer <token>",
        "Several comma-separated tokens are accepted at once, so rotation needs no downtime.",
        "=" * 78,
    ):
        log.warning(line)


if __name__ == "__main__":
    main()
