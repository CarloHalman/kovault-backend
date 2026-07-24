"""Kovault MCP server — the only component that holds DB credentials and touches Postgres.

Exposes the fixed script set (lookup/fetch/snippet/rows/insert/update/delete/link/group,
plus janitor) as MCP tools over an HTTP endpoint. The model never writes SQL; it fills in
tool inputs and the server does the heavy lifting (embedding, RRF, link parsing, edits log).
"""

__version__ = "1.4.1"
