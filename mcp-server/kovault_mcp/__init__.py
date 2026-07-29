"""Kovault MCP server — the only component that holds DB credentials and touches Postgres.

Exposes the fixed script set (lookup/fetch/snippet/rows/sql/write, plus janitor and
export) as MCP tools over an HTTP endpoint. The model never writes SQL; it fills in
tool inputs and the server does the heavy lifting (embedding, RRF, link parsing, edits log).
"""

__version__ = "1.5.0"
