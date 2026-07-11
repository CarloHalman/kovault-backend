"""Runtime config for the MCP server. DB credentials live ONLY here (from env / secret
file); users' plugins never see them — they talk to the HTTP endpoint.
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


def _secret(value_env: str, file_env: str, default: str = "") -> str:
    """Prefer an inline env var, else read a *_FILE path (docker secret), else default."""
    if os.getenv(value_env):
        return os.environ[value_env]
    path = os.getenv(file_env)
    if path and Path(path).is_file():
        return Path(path).read_text(encoding="utf-8").strip()
    return default


@dataclass
class Config:
    db_host: str = os.getenv("KOVAULT_DB_HOST", "localhost")
    db_port: int = int(os.getenv("KOVAULT_DB_PORT", "5432"))
    db_name: str = os.getenv("KOVAULT_DB_NAME", "kovault")
    db_user: str = os.getenv("KOVAULT_DB_USER", "kovault")
    db_password: str = _secret("KOVAULT_DB_PASSWORD", "KOVAULT_DB_PASSWORD_FILE", "")
    mcp_host: str = os.getenv("KOVAULT_MCP_HOST", "0.0.0.0")
    mcp_port: int = int(os.getenv("KOVAULT_MCP_PORT", "8000"))

    @property
    def dsn(self) -> str:
        return (
            f"host={self.db_host} port={self.db_port} dbname={self.db_name} "
            f"user={self.db_user} password={self.db_password}"
        )
