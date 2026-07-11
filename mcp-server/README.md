# kovault-mcp

The MCP server for Kovault — a knowledge vault. **The only component that holds DB credentials and
touches Postgres.** Users' Claude Code plugins connect to its HTTP endpoint; the model never
writes SQL — it calls these tools and the server does the SQL, embedding, RRF, link parsing
and edit logging.

## Tools

| Tool | R/W | What |
|------|-----|------|
| `lookup` | read | Hybrid vector + BM25 + graph search, RRF fusion, cutoff ladder; CHUNKS + PAGES + page-outline; group filters. |
| `fetch` | read | Full page / chunk / task / decision / source / group. |
| `snippet` | read | id/title/summary/freshness for expanding links. |
| `rows` | read | Backup raw read of any table (op whitelist, hard limit, every call logged). |
| `insert` | write | Create rows; embeds, parses links, logs edits. |
| `update` | write | Edit rows; re-embeds/re-links, page-rename cascade, appends contributors, logs edits. |
| `delete` | write | Trash only (nothing hard-deletes). |
| `link` | write | Manual fix for auto-linking (links / header_sources / task_dependencies / group_links). |
| `group` | write | Create/manage flexible categories + membership. |
| `janitor` | write | Maintenance: diagnose (bare) + `-lint` / `-freshness` / `-dedupe` / `-embed`. |
| `export` | read | Manifest (per-table counts + download path) for a no-AI OKF bundle. The bundle itself streams as a zip from the `GET /export` route, so its contents never enter context. |

## Layout

```
kovault_mcp/
  config.py          env / secret -> DSN, MCP host/port
  db.py              psycopg3 pool + query helpers + settings loader
  embedding_text.py  deterministic embed-text composition (pure)
  embedding.py       endpoint client (OpenAI-compatible /v1/embeddings)
  timestamps.py      "10th of December 2020" wording (pure)
  links.py           [text](kind:uuid) parser + diff (pure)
  search.py          RRF, cutoff ladder, page aggregation, graph BFS SQL (pure math)
  render.py          fetch/export output formats (pure)
  edits.py           edits-log helper
  server.py          FastMCP app — all tools
  export.py          DB -> OKF bundle: build_bundle (in-memory) feeds the CLI, the zip route, and the export tool
  main.py            entrypoint (HTTP transport)
tests/               unit tests for the pure logic (stdlib unittest, no DB)
```

## Run locally (without Docker)

```bash
python -m venv .venv && . .venv/bin/activate
pip install .
export KOVAULT_DB_HOST=localhost KOVAULT_DB_USER=kovault KOVAULT_DB_NAME=kovault KOVAULT_DB_PASSWORD=...
python -m kovault_mcp.main            # serves http://0.0.0.0:8000/mcp
```

## Identity

`edited_by` / `actor` are never model-set. The plugin injects them via `X-Kovault-User` /
`X-Kovault-Actor` HTTP headers (configured by `/setup-kovault`); the server falls back to
`KOVAULT_DEFAULT_USER` / `KOVAULT_DEFAULT_ACTOR` env.

## Deploy-time check

The BM25 predicate/score API (`col @@@ 'terms'`, `paradedb.score(id)`) and the `USING bm25`
index syntax should be confirmed against the pinned pg_search version in the DB image — this
is the one piece that varies across ParadeDB releases. Everything else is stock PG16 + pgvector.

## Tests

```bash
python -m unittest discover -s tests -v
```
