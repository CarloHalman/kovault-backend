"""FastMCP app — the fixed script set as MCP tools.

The model fills in tool inputs; the server does the SQL, embedding, RRF, link parsing and
edit logging. Nine tools from the spec (lookup/fetch/snippet/rows/insert/update/delete/link/
group) plus `janitor` (server-side because it needs DB access, which only this server has).

Identity: `edited_by`/`actor` are NOT model-set. The plugin injects them out-of-band via
`X-Kovault-User` / `X-Kovault-Actor` HTTP headers (set by /setup); we fall back to env defaults.
"""
from __future__ import annotations

import logging
import os
import re
from datetime import datetime, timezone

from fastmcp import FastMCP
from psycopg.types.json import Json
from starlette.concurrency import run_in_threadpool
from starlette.requests import Request
from starlette.responses import JSONResponse, Response

from . import blocks as bl
from . import embedding_text as et
from . import export as export_mod
from . import render as rnd
from . import search as se
from .db import Database
from .edits import log_edit
from .embedding import EmbeddingClient
from .links import parse_links, parse_obsidian_links

log = logging.getLogger("kovault_mcp")
mcp = FastMCP("kovault")

# ---- service singletons (configured by main.configure) --------------------------------
_DB: Database | None = None
_EMBED_CACHE: dict = {}
SEARCH_LIMIT = 50            # per-signal candidate cap before fusion
ROWS_LIMIT_CAP = 200        # hard cap for the rows backup tool
ROWS_OPS = {"=", "!=", ">", "<", ">=", "<=", "ilike", "in"}
SUBTYPE_KIND = {"sources": "source", "tasks": "task", "decisions": "decision", "pages": "page"}
DEFAULT_PAGE_TYPE = "note"   # pages.type is free-text (OKF passes it through); default when unset
_COLS_CACHE: dict = {}


def configure(db: Database) -> None:
    global _DB
    _DB = db


def db() -> Database:
    assert _DB is not None, "server not configured"
    return _DB


def _embedder() -> EmbeddingClient:
    s = db().settings()["embedding"]
    key = (s["endpoint"], s["model"], int(s.get("dims", 4000)))
    c = _EMBED_CACHE.get(key)
    if c is None:
        c = EmbeddingClient(endpoint=key[0], model=key[1], dims=key[2])
        _EMBED_CACHE[key] = c
    return c


def _identity() -> tuple[str, str]:
    """(edited_by, actor). From plugin-set HTTP headers; else env defaults. The username is
    normalized (trim + lowercase) here so casing variants (e.g. Alice/alice) don't split
    attribution or owner:* groups (forward-only; existing rows are not backfilled)."""
    def norm(u: str | None) -> str:
        return (u or "").strip().lower()
    try:
        from fastmcp.server.dependencies import get_http_headers
        h = get_http_headers() or {}
        user = h.get("x-kovault-user")
        if user:
            return norm(user), h.get("x-kovault-actor", "ai")
    except Exception:
        pass
    return norm(os.getenv("KOVAULT_DEFAULT_USER", "unknown")), os.getenv("KOVAULT_DEFAULT_ACTOR", "ai")


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _cols(table: str) -> set[str]:
    if table not in _COLS_CACHE:
        rows = db().query(
            "SELECT column_name FROM information_schema.columns WHERE table_name=%s", (table,)
        )
        _COLS_CACHE[table] = {r["column_name"] for r in rows}
    return _COLS_CACHE[table]


# ---- enum validation at the write boundary (F5) ---------------------------------------
# enum columns per table (pages.type is free OKF text, so not listed). Values are checked
# against pg_enum; a known alias is auto-normalized and reported; anything else is a clear error.
_ENUM_COLS = {
    "tasks": {"status": "task_status", "priority": "task_priority", "scope": "task_scope"},
    "pages": {"freshness": "page_freshness"},
    "sources": {"type": "source_type"},
    "groups": {"type": "group_types"},
}
_ENUM_ALIASES = {
    "task_status": {"complete": "done", "completed": "done", "finished": "done",
                    "in progress": "doing", "in_progress": "doing", "wip": "doing",
                    "active": "doing", "open": "todo", "backlog": "todo"},
    "task_priority": {"med": "medium", "normal": "medium", "critical": "urgent"},
}
_ENUM_CACHE: dict = {}


def _enum_values(name: str) -> set[str]:
    if name not in _ENUM_CACHE:
        _ENUM_CACHE[name] = {r["enumlabel"] for r in db().query(
            "SELECT enumlabel FROM pg_enum e JOIN pg_type t ON t.oid=e.enumtypid WHERE t.typname=%s",
            (name,))}
    return _ENUM_CACHE[name]


def _check_enums(table: str, fields: dict) -> tuple[list[str], str | None]:
    """Validate/normalize enum-valued fields in place before a write. Returns (notes, error):
    a known alias (completed->done) is corrected and reported; a truly invalid value returns a
    clear 'valid: [...]' error so the caller self-corrects instead of hitting a raw Postgres error."""
    notes: list[str] = []
    for col, enum_name in _ENUM_COLS.get(table, {}).items():
        if col not in fields or fields[col] is None:
            continue
        val = str(fields[col]).strip()
        valid = _enum_values(enum_name)
        if val in valid:
            fields[col] = val
            continue
        alias = _ENUM_ALIASES.get(enum_name, {}).get(val.lower())
        if alias and alias in valid:
            fields[col] = alias
            notes.append(f"normalized {col} '{val}'->'{alias}'")
            continue
        return notes, f"field '{col}': '{val}' invalid, valid: [{','.join(sorted(valid))}]"
    return notes, None


# =======================================================================================
# Linking (owned by the server, not the model)
# =======================================================================================

def _target_live(cur, kind: str, tid: str) -> bool:
    q = {
        "page": "SELECT 1 FROM pages WHERE id=%s AND freshness<>'trashed'",
        "header": "SELECT 1 FROM headers WHERE id=%s AND trashed_at IS NULL",
        "task": "SELECT 1 FROM tasks WHERE id=%s AND trashed_at IS NULL",
        "decision": "SELECT 1 FROM decisions WHERE id=%s AND trashed_at IS NULL",
        "source": "SELECT 1 FROM sources WHERE id=%s AND trashed_at IS NULL",
    }[kind]
    cur.execute(q, (tid,))
    return cur.fetchone() is not None


def _resolve_title(cur, title: str) -> tuple[str, str] | None:
    """Resolve an Obsidian [[Title]] to a single live entity (kind, id) by exact
    case-insensitive title. Search order page > header > task > decision > source; returns
    None if no table has exactly one live match (missing or ambiguous stays plain text)."""
    for kind, table, live in (
        ("page", "pages", "freshness <> 'trashed'"),
        ("header", "headers", "trashed_at IS NULL"),
        ("task", "tasks", "trashed_at IS NULL"),
        ("decision", "decisions", "trashed_at IS NULL"),
        ("source", "sources", "trashed_at IS NULL"),
    ):
        # exact case-insensitive match (NOT ILIKE: a %/_/trailing-\ in a real title would
        # otherwise be treated as a LIKE pattern and mis-resolve or error)
        cur.execute(f"SELECT id FROM {table} WHERE lower(title) = lower(%s) AND {live} LIMIT 2", (title,))
        rows = cur.fetchall()
        if len(rows) == 1:
            return kind, str(rows[0]["id"])
    return None


# Column caps for the link-bearing text fields; None = unbounded `text` (headers.body).
# A base [label](kind:uuid) link is longer than [[Title]], so conversion can overflow a
# bounded column — skip persisting it there rather than truncating or crashing the write.
_TEXT_COL_MAX = {"body": None, "summary": 512, "description": 1024}


def _convert_obsidian(cur, from_id: str, table: str, text_col: str, text: str) -> tuple[str, list[str]]:
    """Resolve EVERY Obsidian [[wikilink]] in a body to an entity by title, rewrite it into a base
    [label](kind:uuid) markdown link (which also graphs it), and persist the converted text. The
    links stay as markdown in the body. Returns (converted_text, warnings). A title that does not
    resolve to exactly one live entity, or a rewrite that would exceed the column cap, leaves that
    [[link]] as plain text."""
    if not text:
        return text, []
    original = text
    warnings, changed, seen = [], False, set()
    for raw, target, alias in parse_obsidian_links(text):
        if raw in seen:
            continue
        seen.add(raw)
        res = _resolve_title(cur, target)
        if res:
            kind, tid = res
            text = text.replace(raw, f"[{alias or target}]({kind}:{tid})")
            changed = True
        else:
            warnings.append(f"obsidian link [[{target}]] left as text (no unique live match)")
    if changed:
        cap = _TEXT_COL_MAX.get(text_col)
        if cap is not None and len(text) > cap:      # would overflow -> keep original, don't graph
            return original, warnings + [f"obsidian conversion skipped: exceeds {text_col} cap ({cap})"]
        cur.execute(f"UPDATE {table} SET {text_col} = %s WHERE id = %s", (text, from_id))
    return text, warnings


def _sync_links(cur, from_kind: str, from_id: str, text: str | None,
                table: str | None = None, text_col: str | None = None) -> list[str]:
    """Diff base [text](kind:uuid) links in `text` into the links table. When table/text_col are
    given, first convert Obsidian-style bodies (see _convert_obsidian) so those links graph too.
    Returns warnings (conversion + dangling-target)."""
    warnings: list[str] = []
    if table and text_col and text:
        text, warnings = _convert_obsidian(cur, from_id, table, text_col, text)
    new = parse_links(text)
    cur.execute(
        "SELECT to_kind, to_id FROM links WHERE from_kind=%s AND from_id=%s",
        (from_kind, from_id),
    )
    old = {(r["to_kind"], str(r["to_id"])) for r in cur.fetchall()}
    for kind, tid in new - old:
        if _target_live(cur, kind, tid):
            cur.execute(
                "INSERT INTO links (from_kind, from_id, to_kind, to_id) VALUES (%s,%s,%s,%s) "
                "ON CONFLICT DO NOTHING",
                (from_kind, from_id, kind, tid),
            )
        else:
            warnings.append(f"skipped dangling link -> {kind}:{tid}")
    for kind, tid in old - new:
        cur.execute(
            "DELETE FROM links WHERE from_kind=%s AND from_id=%s AND to_kind=%s AND to_id=%s",
            (from_kind, from_id, kind, tid),
        )
    return warnings


def _embed_row(table: str, row: dict) -> str | None:
    """Compose deterministic text for a searchable row and embed it -> pgvector literal."""
    if table not in et.COMPOSERS:
        return None
    text = et.compose(table, row)
    if not text.strip():
        return None
    return _embedder().embed(text)


# =======================================================================================
# lookup — hybrid search
# =======================================================================================

_SEARCH = {
    "headers":   {"kind": "header",   "emb": "embedding",         "disp": "blurb"},
    "tasks":     {"kind": "task",     "emb": "embedding",         "disp": "description"},
    "decisions": {"kind": "decision", "emb": "embedding",         "disp": "description"},
    "sources":   {"kind": "source",   "emb": "summary_embedding", "disp": "summary"},
}


def _bm25_terms(terms: list[str]) -> str:
    """Quote each term as a Tantivy phrase and OR them (safe literal matching)."""
    def esc(t: str) -> str:
        return t.replace("\\", "\\\\").replace('"', '\\"')
    return " OR ".join(f'"{esc(t)}"' for t in terms if t.strip())


def _vector_hits(table: str, qvec: str) -> dict[str, dict]:
    meta = _SEARCH[table]
    if table == "headers":
        sql = f"""
            SELECT h.id, h.page_id, h.title, h.blurb AS disp,
                   1 - (h.embedding <=> %(q)s::halfvec) AS score
            FROM headers h JOIN pages p ON p.id = h.page_id
            WHERE h.trashed_at IS NULL AND h.embedding IS NOT NULL
              AND p.freshness NOT IN ('trashed','archived')
            ORDER BY h.embedding <=> %(q)s::halfvec
            LIMIT %(n)s
        """
    else:
        sql = f"""
            SELECT id, NULL::uuid AS page_id, title, {meta['disp']} AS disp,
                   1 - ({meta['emb']} <=> %(q)s::halfvec) AS score
            FROM {table}
            WHERE trashed_at IS NULL AND {meta['emb']} IS NOT NULL
            ORDER BY {meta['emb']} <=> %(q)s::halfvec
            LIMIT %(n)s
        """
    out = {}
    for r in db().query(sql, {"q": qvec, "n": SEARCH_LIMIT}):
        out[str(r["id"])] = {
            "id": str(r["id"]), "page_id": str(r["page_id"]) if r["page_id"] else None,
            "title": r["title"], "disp": r["disp"], "vector": float(r["score"]),
        }
    return out


def _keyword_hits(table: str, inc: str, exc: str) -> dict[str, dict]:
    if not inc:
        return {}
    meta = _SEARCH[table]
    text_cols = {
        "headers": ["title", "blurb", "body"],
        "tasks": ["title", "description"],
        "decisions": ["title", "description"],
        "sources": ["title", "summary", "reference"],
    }[table]
    inc_pred = " OR ".join(f"{c} @@@ %(inc)s" for c in text_cols)
    exc_pred = " OR ".join(f"{c} @@@ %(exc)s" for c in text_cols)
    not_clause = f" AND NOT ({exc_pred})" if exc else ""
    sub = f"""
        SELECT id, paradedb.score(id) AS score
        FROM {table}
        WHERE trashed_at IS NULL AND ({inc_pred}){not_clause}
        ORDER BY score DESC
        LIMIT %(n)s
    """
    if table == "headers":
        sql = f"""
            SELECT s.id, h.page_id, h.title, h.blurb AS disp, s.score
            FROM ({sub}) s
            JOIN headers h ON h.id = s.id
            JOIN pages p ON p.id = h.page_id
            WHERE p.freshness NOT IN ('trashed','archived')
        """
    else:
        sql = f"""
            SELECT s.id, NULL::uuid AS page_id, t.title, t.{meta['disp']} AS disp, s.score
            FROM ({sub}) s JOIN {table} t ON t.id = s.id
        """
    out = {}
    for r in db().query(sql, {"inc": inc, "exc": exc, "n": SEARCH_LIMIT}):
        out[str(r["id"])] = {
            "id": str(r["id"]), "page_id": str(r["page_id"]) if r["page_id"] else None,
            "title": r["title"], "disp": r["disp"], "keyword": float(r["score"]),
        }
    return out


# normalized-title columns per table for the trigram arm (F2). headers also carry blurb_norm.
_NORM_COLS = {"headers": ["title_norm", "blurb_norm"], "tasks": ["title_norm"],
              "decisions": ["title_norm"], "sources": ["title_norm"]}


def _trigram_hits(table: str, qnorm: str) -> dict[str, dict]:
    """Fuzzy surface-form arm: pg_trgm similarity of the query (normalized the same way) against the
    normalized-title column(s). Catches E-drawing/Edrawing, Emp-Viewer/employee viewer that exact
    BM25 tokens miss. Its own signal — fused as a 4th RRF rank map, never mixed into the BM25 score."""
    if not qnorm:
        return {}
    meta = _SEARCH[table]
    cols = _NORM_COLS[table]
    sim = "GREATEST(" + ",".join(f"similarity({c}, %(q)s)" for c in cols) + ")"
    where = " OR ".join(f"{c} %% %(q)s" for c in cols)   # %% -> literal % (the pg_trgm operator)
    if table == "headers":
        sql = f"""
            SELECT h.id, h.page_id, h.title, h.blurb AS disp, {sim} AS score
            FROM headers h JOIN pages p ON p.id = h.page_id
            WHERE h.trashed_at IS NULL AND ({where})
              AND p.freshness NOT IN ('trashed','archived')
            ORDER BY score DESC LIMIT %(n)s
        """
    else:
        sql = f"""
            SELECT id, NULL::uuid AS page_id, title, {meta['disp']} AS disp, {sim} AS score
            FROM {table}
            WHERE trashed_at IS NULL AND ({where})
            ORDER BY score DESC LIMIT %(n)s
        """
    out = {}
    for r in db().query(sql, {"q": qnorm, "n": SEARCH_LIMIT}):
        out[str(r["id"])] = {
            "id": str(r["id"]), "page_id": str(r["page_id"]) if r["page_id"] else None,
            "title": r["title"], "disp": r["disp"], "trigram": float(r["score"]),
        }
    return out


def _graph_points(include: list[str], exclude: list[str]) -> dict[tuple[str, str], int]:
    """(kind, id) -> summed hop points: +max(0,4-hops) per good topic, -same per bad topic."""
    pts: dict[tuple[str, str], int] = {}
    def run(term: str, sign: int):
        for r in db().query(se.GRAPH_BFS_SQL, {"pat": f"%{term}%"}):
            key = (r["kind"], str(r["id"]))
            pts[key] = pts.get(key, 0) + sign * se.hop_points(int(r["hops"]))
    for t in include:
        if t.strip():
            run(t, +1)
    for t in exclude:
        if t.strip():
            run(t, -1)
    return pts


def _group_entity_sets(names_or_ids: list[str]) -> set[str]:
    """Resolve group names/ids -> set of member entity ids."""
    if not names_or_ids:
        return set()
    ids, names = [], []
    for x in names_or_ids:
        (ids if _looks_uuid(x) else names).append(x)
    clauses, params = [], []
    if ids:
        clauses.append("id = ANY(%s)")
        params.append(ids)
    if names:
        clauses.append("name ILIKE ANY(%s)")
        params.append([f"%{n}%" for n in names])
    gids = [str(r["id"]) for r in db().query(
        f"SELECT id FROM groups WHERE {' OR '.join(clauses)}", params)]
    if not gids:
        return set()
    return {str(r["entity_id"]) for r in db().query(
        "SELECT entity_id FROM group_links WHERE group_id = ANY(%s)", (gids,))}


def _looks_uuid(x: str) -> bool:
    return bool(re.fullmatch(r"[0-9a-fA-F-]{36}", str(x)))


def _maybe_auto_freshness() -> None:
    """Recompute page freshness before a lookup when a cooldown has elapsed. The compute is
    sub-millisecond for the whole table, so this keeps hot/warm/cold honest without a manual
    /janitor -freshness. The cooldown is claimed atomically in a settings row, so only one
    concurrent lookup runs it; any failure is swallowed and never breaks the lookup."""
    try:
        cfg = db().settings().get("freshness_auto") or {}
        if not cfg.get("enabled", True):
            return
        cooldown = int(cfg.get("cooldown_seconds", 3600))
        with db().connection() as conn:
            with conn.cursor() as cur:
                # atomic compare-and-set: update (or first-time insert) the last-run stamp only
                # when the cooldown has passed; RETURNING is empty when another run holds it.
                cur.execute(
                    "INSERT INTO settings (key, value) VALUES "
                    "  ('freshness_last_auto', to_jsonb(now()::text)) "
                    "ON CONFLICT (key) DO UPDATE SET value = to_jsonb(now()::text), updated_at = now() "
                    "  WHERE (settings.value #>> '{}')::timestamptz < now() - make_interval(secs => %s) "
                    "RETURNING key",
                    (cooldown,))
                if cur.fetchone() is not None:      # we claimed this window -> recompute
                    _janitor_freshness(cur, "auto")
            conn.commit()
    except Exception as e:                          # never let upkeep break a read
        log.warning("auto-freshness skipped: %s", e)


_PRECISE_TABLES = ("pages", "headers", "tasks", "decisions", "sources", "groups")
_PRECISE_DISP = {"headers": "blurb", "tasks": "description", "decisions": "description",
                 "sources": "summary", "pages": "summary", "groups": "description"}


def _precise_lookup(tables, filters, count, limit, offset) -> str:
    """Deterministic exact-filter query (F3) — the first-class replacement for reaching to `rows`/`sql`
    for audits. Filters/paginates ONE table with an op whitelist; returns hits:N and a compact list."""
    table = (tables or ["tasks"])[0]
    if table not in _PRECISE_TABLES:
        return f"(precise: table must be one of {', '.join(_PRECISE_TABLES)})"
    cols = _cols(table)
    clauses, params = [], []
    if "trashed_at" in cols:
        clauses.append("trashed_at IS NULL")
    elif table == "pages":
        clauses.append("freshness <> 'trashed'")
    for f in filters or []:
        col, op, val = f.get("column"), (f.get("op") or "=").lower(), f.get("value")
        if col not in cols:
            return f"(precise: unknown column {col} on {table})"
        if op not in ROWS_OPS:
            return f"(precise: op {op} not allowed; use {', '.join(sorted(ROWS_OPS))})"
        if op == "in":
            clauses.append(f"{col} = ANY(%s)")
            params.append(val if isinstance(val, list) else [val])
        elif op == "ilike":
            clauses.append(f"{col} ILIKE %s")
            params.append(val)
        else:
            clauses.append(f"{col} {op} %s")
            params.append(val)
    where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
    total = int(db().query_one(f"SELECT count(*) n FROM {table}{where}", params)["n"])
    if count:
        return f"PRECISE {table}\nhits: {total}"
    lim = max(1, min(int(limit or 50), ROWS_LIMIT_CAP))
    off = max(0, int(offset or 0))
    label = "name" if table == "groups" else "title"
    order = "created_at DESC" if "created_at" in cols else "id"
    rows_ = db().query(
        f"SELECT id, {label} AS label, {_PRECISE_DISP[table]} AS disp FROM {table}{where} "
        f"ORDER BY {order} LIMIT %s OFFSET %s", params + [lim, off])
    out = [f"PRECISE {table}", f"hits: {total} (showing {len(rows_)} from offset {off})",
           "id | label | summary"]
    for r in rows_:
        out.append(f"{r['id']} | {r['label'] or ''} | {_clip(r['disp'])}")
    return "\n".join(out)


@mcp.tool
def lookup(
    tables: list[str] | None = None,
    query: str | None = None,
    include: list[str] | None = None,
    exclude: list[str] | None = None,
    groups_include: list[str] | None = None,
    groups_exclude: list[str] | None = None,
    outline_page: str | None = None,
    scores: bool = False,
    filters: list[dict] | None = None,
    count: bool = False,
    limit: int = 50,
    offset: int = 0,
) -> str:
    """Hybrid search over Kovault. Returns a ranked CHUNKS index and (when headers are
    searched) a PAGES index to fetch from.

    tables: searchable tables to hit — any of headers/tasks/decisions/sources (default headers).
    query: a plain search string ("Search for: X, Exclude: Y") — parsed into include/exclude
        terms (stopwords dropped), merged with any explicit include/exclude below.
    include/exclude: search TERMS (feed vector + BM25 + graph); exclude is BM25 must-not +
        graph negative anchor.
    groups_include/groups_exclude: membership filters over the `groups` table (names or ids),
        NOT search terms — keep/drop rows whose entity (a header's page, or the row itself) is
        in those groups.
    outline_page: instead of searching, return every chunk of one page (id/index/title/blurb)
        so you can pick the right chunk in a large page.
    scores: default off — only the fused rrf score is shown. Set true to also print the
        per-signal vec/kw/graph columns (debugging ranking); costs ~3 extra columns per row.
    filters: PRECISE mode — exact/deterministic filtering instead of ranked search. A list of
        {column, op, value} (op in =,!=,>,<,>=,<=,ilike,in) over the FIRST table in `tables`
        (pages/headers/tasks/decisions/sources/groups). count=true returns just the total;
        limit/offset paginate deterministically. For audits/aggregates the ranked search can't do.
    """
    if filters is not None:
        return _precise_lookup((tables or ["tasks"]), filters, count, limit, offset)
    tables = [t for t in (tables or ["headers"]) if t in _SEARCH]
    include = list(include or [])
    exclude = list(exclude or [])
    if query:
        q_inc, q_exc = se.parse_search_input(query)   # plain "Search for: X, Exclude: Y" (F5)
        include += [t for t in q_inc if t not in include]
        exclude += [t for t in q_exc if t not in exclude]

    _maybe_auto_freshness()   # cooldowned upkeep; cheap, best-effort, never blocks the search

    if outline_page:
        rows = db().query(
            "SELECT id, index, title, blurb FROM headers "
            "WHERE page_id=%s AND trashed_at IS NULL ORDER BY index",
            (outline_page,),
        )
        lines = ["PAGE OUTLINE", "index | id | title | blurb"]
        for r in rows:
            lines.append(f"{r['index']} | {r['id']} | {r['title'] or '(intro)'} | {r['blurb'] or ''}")
        return "\n".join(lines) if rows else "PAGE OUTLINE\n(no live chunks)"

    if not tables:
        return "CHUNKS\n(no searchable tables selected)"

    inc_bm = _bm25_terms(include)
    exc_bm = _bm25_terms(exclude)
    qvec = _embedder().embed(" ".join(include)) if include else None
    qnorm = se.normalize_term(" ".join(include)) if include else ""   # F2 trigram query form
    graph = _graph_points(include, exclude)

    inc_groups = _group_entity_sets(groups_include or [])
    exc_groups = _group_entity_sets(groups_exclude or [])

    # ---- gather candidates per table, attach three signals -------------------------------
    cand: dict[str, dict] = {}          # id -> candidate dict
    for table in tables:
        kind = _SEARCH[table]["kind"]
        vhits = _vector_hits(table, qvec) if qvec else {}
        khits = _keyword_hits(table, inc_bm, exc_bm)
        try:
            thits = _trigram_hits(table, qnorm) if qnorm else {}
        except Exception:                          # a pre-migration DB lacks *_norm cols — degrade, don't break search
            thits = {}
        for cid in set(vhits) | set(khits) | set(thits):
            base = vhits.get(cid) or khits.get(cid) or thits.get(cid)
            page_id = base.get("page_id")
            group_entity = page_id if kind == "header" else cid
            if inc_groups and group_entity not in inc_groups:
                continue
            if group_entity in exc_groups:
                continue
            cand[cid] = {
                "table": table, "kind": kind, "id": cid, "page_id": page_id,
                "title": base.get("title"), "disp": base.get("disp"),
                "vector": vhits.get(cid, {}).get("vector"),
                "keyword": khits.get(cid, {}).get("keyword"),
                "trigram": thits.get(cid, {}).get("trigram"),
                "graph": graph.get((kind, cid), 0),
            }

    # ---- fuse (RRF over per-signal global rankings) --------------------------------------
    settings = db().settings()
    k = int(settings["rrf_k"])
    vmap = se.dense_ranks((c["id"], c["vector"]) for c in cand.values() if c["vector"] is not None)
    kmap = se.dense_ranks((c["id"], c["keyword"]) for c in cand.values() if c["keyword"] is not None)
    gmap = se.dense_ranks((c["id"], c["graph"]) for c in cand.values() if c["graph"] > 0)
    tmap = se.dense_ranks((c["id"], c["trigram"]) for c in cand.values() if c.get("trigram") is not None)
    rrf = se.rrf_fuse([vmap, kmap, gmap, tmap], k)   # 4th arm: trigram surface-form (F2); dense-rank ties
    for cid, score in rrf.items():
        cand[cid]["rrf"] = score
    ranked = [(cid, cand[cid].get("rrf", 0.0)) for cid, _ in se.order_by_score(
        {cid: cand[cid].get("rrf", 0.0) for cid in cand})]
    lc = settings["ladder_chunks"]
    kept = se.apply_ladder(ranked, lc["r"], int(lc["floor"]), int(lc["cap"]))

    sig = " | vec | kw | graph | trg" if scores else ""
    out = ["CHUNKS", f"kind | id | title | blurb/summary | freshness{sig} | rrf"]
    fresh = _page_freshness_map([cand[cid]["page_id"] for cid, _ in kept if cand[cid]["page_id"]])
    for cid, score in kept:
        c = cand[cid]
        f = fresh.get(c["page_id"], "-") if c["kind"] == "header" else "-"
        cols = [c["kind"], c["id"], c["title"] or "(intro)", _clip(c["disp"]), f]
        if scores:
            cols += [_fmt(c["vector"]), _fmt(c["keyword"]), str(c["graph"]), _fmt(c.get("trigram"))]
        cols.append(_fmt(score))
        out.append(" | ".join(cols))

    # ---- PAGES (only when headers were searched) ----------------------------------------
    if "headers" in tables:
        out += _pages_index(
            [c for c in cand.values() if c["kind"] == "header"], k, settings, scores)
    out.append(f"\nhits: {len(cand)}")            # total candidates before the cutoff ladder (F3)
    return "\n".join(out)


def _fmt(x) -> str:
    return "-" if x is None else f"{x:.4f}"


def _clip(text, n: int = 80) -> str:
    """Truncate a blurb/summary for the lookup index at a WORD boundary (never mid-word), with an
    ellipsis. Falls back to a hard cut only for a single word longer than n."""
    t = (text or "").strip()
    if len(t) <= n:
        return t
    head = t[:n]
    cut = head.rsplit(" ", 1)[0] if " " in head else head
    return cut.rstrip() + "…"


def _page_freshness_map(page_ids: list[str]) -> dict[str, str]:
    ids = [p for p in set(page_ids) if p]
    if not ids:
        return {}
    return {str(r["id"]): r["freshness"] for r in db().query(
        "SELECT id, freshness FROM pages WHERE id = ANY(%s)", (ids,))}


def _pages_index(header_cands: list[dict], k: int, settings: dict, scores: bool = False) -> list[str]:
    if not header_cands:
        return ["", "PAGES", "(none)"]
    by_page_v: dict[str, list[float]] = {}
    by_page_k: dict[str, list[float]] = {}
    by_page_g: dict[str, list[float]] = {}
    top_by_page: dict[str, dict] = {}        # best-matching chunk per page (F1: PAGES snippet)
    for c in header_cands:
        pid = c["page_id"]
        if not pid:
            continue
        by_page_v.setdefault(pid, []).append(c["vector"] or 0.0)
        by_page_k.setdefault(pid, []).append(c["keyword"] or 0.0)
        by_page_g.setdefault(pid, []).append(float(c["graph"]))
        if pid not in top_by_page or (c.get("rrf") or 0.0) > (top_by_page[pid].get("rrf") or 0.0):
            top_by_page[pid] = c
    page_ids = list({c["page_id"] for c in header_cands if c["page_id"]})
    live = {str(r["id"]): int(r["n"]) for r in db().query(
        "SELECT page_id AS id, count(*) AS n FROM headers "
        "WHERE trashed_at IS NULL AND page_id = ANY(%s) GROUP BY page_id", (page_ids,))}
    v = se.aggregate_page_signal(by_page_v, live)
    kk = se.aggregate_page_signal(by_page_k, live)
    g = se.aggregate_page_signal(by_page_g, live)
    vmap = se.dense_ranks(v.items())
    kmap = se.dense_ranks(kk.items())
    gmap = se.dense_ranks((p, s) for p, s in g.items() if s > 0)
    rrf = se.rrf_fuse([vmap, kmap, gmap], k)   # dense-rank ties (see RRF-graph-analysis.md)
    ranked = se.order_by_score(rrf)
    lp = settings["ladder_pages"]
    kept = se.apply_ladder(ranked, lp["r"], int(lp["floor"]), int(lp["cap"]))
    meta = {str(r["id"]): r for r in db().query(
        "SELECT id, title, summary, freshness FROM pages WHERE id = ANY(%s)",
        ([pid for pid, _ in kept],))} if kept else {}
    sig = " | vec | kw | graph" if scores else ""
    lines = ["", "PAGES", f"id | title | summary | freshness{sig} | rrf | top chunk"]
    for pid, score in kept:
        m = meta.get(pid, {})
        cols = [pid, m.get("title", ""), _clip(m.get("summary")), m.get("freshness", "-")]
        if scores:
            cols += [_fmt(v.get(pid)), _fmt(kk.get(pid)), _fmt(g.get(pid))]
        cols.append(_fmt(score))
        tc = top_by_page.get(pid)
        snippet = f"{tc['title']} — {tc['disp']}" if tc and tc.get("title") else (tc.get("disp") if tc else "")
        cols.append(_clip(snippet))
        lines.append(" | ".join(cols))
    return lines


# =======================================================================================
# fetch / snippet / rows  (read path)
# =======================================================================================

def _links_of(kind: str, rid: str) -> list[tuple[str, str]]:
    return [(r["to_kind"], str(r["to_id"])) for r in db().query(
        "SELECT to_kind, to_id FROM links WHERE from_kind=%s AND from_id=%s ORDER BY created_at",
        (kind, rid))]


def _full_id(table: str, rid: str):
    """Resolve a partial id (a unique prefix) to the full uuid (F3); a full 36-char id passes through.
    Returns (id, error) — an ambiguous or missing prefix errors rather than guessing."""
    s = str(rid or "")
    if not s or len(s) >= 36:
        return rid, None
    rows = db().query(f"SELECT id FROM {table} WHERE id::text LIKE %s LIMIT 5", (s + "%",))
    if len(rows) == 1:
        return str(rows[0]["id"]), None
    if not rows:
        return None, f"({table[:-1]} id starting '{s}' not found)"
    return None, f"(ambiguous {table[:-1]} id prefix '{s}': {len(rows)} matches)"


@mcp.tool
def fetch(
    pages: list[str] | None = None,
    headers: list[str] | None = None,
    tasks: list[str] | None = None,
    decisions: list[str] | None = None,
    sources: list[str] | None = None,
    groups: list[str] | None = None,
    outline: bool = False,
) -> str:
    """Render full entities by id: whole pages, single chunks (headers), tasks, decisions,
    sources, or groups. Fetch a page/chunk before editing it. Explicit ids can reach trashed
    rows (recovery/history).

    outline: for `pages`, return a cheap chunk index (index | id | title | blurb) instead of the
    whole page — pick the one chunk you need, and get its id for a `write` (a full page fetch has no chunk ids)."""
    parts: list[str] = []
    for pid in pages or []:
        pid, err = _full_id("pages", pid)
        if err:
            parts.append(err)
            continue
        page = db().query_one("SELECT * FROM pages WHERE id=%s", (pid,))
        if not page:
            parts.append(f"(page {pid} not found)")
            continue
        if outline:
            hs = db().query(
                "SELECT id, index, title, blurb FROM headers WHERE page_id=%s AND trashed_at IS NULL "
                "ORDER BY index", (pid,))
            lines = [f"PAGE OUTLINE {page.get('title') or ''} ({pid})", "index | id | title | blurb"]
            lines += [f"{r['index']} | {r['id']} | {r['title'] or '(intro)'} | {r['blurb'] or ''}" for r in hs]
            parts.append("\n".join(lines))
            continue
        hs = db().query(
            "SELECT * FROM headers WHERE page_id=%s AND trashed_at IS NULL ORDER BY index", (pid,))
        parts.append(rnd.render_page(page, hs))     # inline body links carry navigation; no links query
    for hid in headers or []:
        hid, err = _full_id("headers", hid)
        if err:
            parts.append(err)
            continue
        h = db().query_one("SELECT * FROM headers WHERE id=%s", (hid,))
        parts.append(rnd.render_chunk(h) if h else f"(chunk {hid} not found)")
    for tid in tasks or []:
        tid, err = _full_id("tasks", tid)
        if err:
            parts.append(err)
            continue
        t = db().query_one("SELECT * FROM tasks WHERE id=%s", (tid,))
        if not t:
            parts.append(f"(task {tid} not found)")
            continue
        blockers = [r["title"] for r in db().query(
            "SELECT t.title FROM task_dependencies d JOIN tasks t ON t.id=d.blocker "
            "WHERE d.dependent=%s", (tid,))]
        parts.append(rnd.render_task(t, blockers, _links_of("task", tid)))
    for did in decisions or []:
        did, err = _full_id("decisions", did)
        if err:
            parts.append(err)
            continue
        d = db().query_one("SELECT * FROM decisions WHERE id=%s", (did,))
        parts.append(rnd.render_decision(d, _links_of("decision", did)) if d else f"(decision {did} not found)")
    for sid in sources or []:
        sid, err = _full_id("sources", sid)
        if err:
            parts.append(err)
            continue
        s = db().query_one("SELECT * FROM sources WHERE id=%s", (sid,))
        if not s:
            parts.append(f"(source {sid} not found)")
            continue
        ref_by = [str(r["header_id"]) for r in db().query(
            "SELECT header_id FROM header_sources WHERE source_id=%s", (sid,))]
        parts.append(rnd.render_source(s, ref_by))
    for gid in groups or []:
        gid, err = _full_id("groups", gid)
        if err:
            parts.append(err)
            continue
        g = db().query_one("SELECT * FROM groups WHERE id=%s", (gid,))
        if not g:
            parts.append(f"(group {gid} not found)")
            continue
        members = _group_members(gid)
        ids_only = len(members) > _GROUP_IDS_ONLY_MAX      # big rosters render ids-only (F1)
        rendered = rnd.render_group(g, members, ids_only=ids_only)
        if ids_only:
            rendered += (f"\n({len(members)} members, ids only — fetch each id, or use the "
                         f"`group` tool / snippet for labels)\n")
        parts.append(rendered)
    return "\n".join(parts) if parts else "(nothing requested)"


_GROUP_IDS_ONLY_MAX = 25     # a roster larger than this renders ids-only by default


def _group_members(gid: str) -> list[tuple[str, str, str]]:
    rows = db().query(
        """
        SELECT e.kind, e.id,
               coalesce(p.title, t.title, d.title, s.title, s.reference) AS label
        FROM group_links gl JOIN entities e ON e.id = gl.entity_id
        LEFT JOIN pages p ON p.id=e.id LEFT JOIN tasks t ON t.id=e.id
        LEFT JOIN decisions d ON d.id=e.id LEFT JOIN sources s ON s.id=e.id
        WHERE gl.group_id=%s
        """, (gid,))
    return [(r["kind"], str(r["id"]), r["label"] or "") for r in rows]


@mcp.tool
def snippet(requests: list[dict]) -> str:
    """Pull id/title/summary(or blurb)/freshness for ids or titles — to expand Related: links
    or header/source/task references without a full fetch. requests: [{table, ids?, titles?}].
    Titles are not unique; a title match returns every hit."""
    disp = {"headers": "blurb", "tasks": "description", "decisions": "description",
            "sources": "summary", "pages": "summary", "groups": "description"}
    out: list[str] = []
    for req in requests or []:
        table = req.get("table")
        if table not in disp:
            out.append(f"(unknown table {table})")
            continue
        ids = req.get("ids") or []
        titles = req.get("titles") or []
        where, params = [], []
        if ids:
            where.append("id = ANY(%s)")
            params.append(ids)
        if titles:
            col = "name" if table == "groups" else "title"
            where.append(f"{col} ILIKE ANY(%s)")
            params.append([f"%{t}%" for t in titles])
        if not where:
            continue
        namecol = "name" if table == "groups" else "title"
        rows = db().query(
            f"SELECT id, {namecol} AS title, {disp[table]} AS summary FROM {table} "
            f"WHERE {' OR '.join(where)} LIMIT 100", params)
        for r in rows:
            out.append(f"{r['title']} ({table})")
            out.append(str(r["id"]))
            out.append(r["summary"] or "")
            fr = _snippet_freshness(table, str(r["id"]))
            if fr:
                out.append(fr)
            out.append("")
    return "\n".join(out).rstrip() if out else "(no snippet matches)"


def _snippet_freshness(table: str, rid: str) -> str | None:
    if table == "pages":
        r = db().query_one("SELECT freshness FROM pages WHERE id=%s", (rid,))
        return r["freshness"] if r else None
    if table == "headers":
        r = db().query_one(
            "SELECT p.freshness FROM headers h JOIN pages p ON p.id=h.page_id WHERE h.id=%s", (rid,))
        return r["freshness"] if r else None
    return None


@mcp.tool
def rows(table: str, where: list[dict] | None = None, limit: int = 50) -> str:
    """Backup path: raw read of ANY table (incl. edits / janitor_reports). Read-only, op
    whitelist (= != > < >= <= ilike in), hard limit cap. Every call is logged so future tool
    upgrades can learn where the main tools fell short. For exact filters/counts on the main
    entities prefer `lookup(filters=[...], count=...)` (precise mode); use `rows` only for tables
    lookup can't reach (edits / janitor_reports / settings / debug_log). Never write SQL."""
    cols = _cols(table)
    if not cols:
        return f"(unknown table {table})"
    limit = max(1, min(int(limit), ROWS_LIMIT_CAP))
    clauses, params = [], []
    for cond in where or []:
        col, op, val = cond.get("column"), (cond.get("op") or "=").lower(), cond.get("value")
        if col not in cols:
            return f"(unknown column {col} on {table})"
        if op not in ROWS_OPS:
            return f"(op {op} not allowed)"
        if op == "in":
            clauses.append(f"{col} = ANY(%s)")
            params.append(val if isinstance(val, list) else [val])
        elif op == "ilike":
            clauses.append(f"{col} ILIKE %s")
            params.append(val)
        else:
            clauses.append(f"{col} {op} %s")
            params.append(val)
    sql = f"SELECT * FROM {table}"
    if clauses:
        sql += " WHERE " + " AND ".join(clauses)
    sql += " ORDER BY created_at DESC" if "created_at" in cols else ""
    sql += " LIMIT %s"
    params.append(limit)
    log.info("rows tool: table=%s where=%s limit=%s", table, where, limit)   # logged for future tool work
    result = db().query(sql, params)
    lines = []
    for r in result:
        lines.append(" | ".join(f"{c}: {r[c]}" for c in r))
    return "\n".join(lines) if lines else "(no rows)"


_SQL_BLOCK = re.compile(r"\b(insert|update|delete|drop|alter|truncate|create|grant|revoke|copy)\b", re.I)


@mcp.tool
def sql(query: str) -> str:
    """Debug only: run a raw READ-ONLY SQL query (SELECT / WITH) against the Kovault DB, to experiment
    with retrieval the fixed tools don't express — so we can compare your queries to them. Enabled
    only when `debug` is on in /settings (the PreToolUse hook blocks it otherwise), and every call
    is logged to debug_log. Writes and DDL are refused, the query runs in a READ ONLY transaction,
    and the result is capped."""
    q = (query or "").strip().rstrip(";")
    low = q.lower()
    if not (low.startswith("select") or low.startswith("with")):
        return "(only SELECT / WITH queries allowed)"
    if _SQL_BLOCK.search(low):
        return "(read-only: write/DDL keywords are not allowed)"
    log.info("sql tool: %s", q)
    try:
        with db().connection() as conn:
            with conn.cursor() as cur:
                cur.execute("SET TRANSACTION READ ONLY")
                cur.execute(f"SELECT * FROM ({q}) _sub LIMIT {ROWS_LIMIT_CAP}")
                out = cur.fetchall()
            conn.rollback()
    except Exception as e:
        return f"(sql error: {e})"
    if not out:
        return "(no rows)"
    return "\n".join(" | ".join(f"{c}: {r[c]}" for c in r) for r in out)


# =======================================================================================
# insert / update / delete (write path)
# =======================================================================================

def _new_entity(cur, kind: str) -> str:
    cur.execute("INSERT INTO entities (kind) VALUES (%s) RETURNING id", (kind,))
    return str(cur.fetchone()["id"])


def _write_label(data: dict) -> str:
    """Quoted human label for a write confirmation — the title / name / reference, so a write
    echoes WHAT was written, not just an opaque id."""
    lbl = (data.get("title") or data.get("name") or data.get("reference") or "").strip()
    return f'"{lbl}"' if lbl else "(untitled)"


# same normalization as the tasks.title_norm generated column, so `%` hits the trigram index.
_NORM_TITLE = "lower(f_unaccent(regexp_replace(coalesce(%s,''),'[-\\s]+','','g')))"


def _similar_task_warn(cur, title: str | None) -> list[str]:
    """Cheap near-duplicate check on task insert: trigram-match the new title against live tasks
    (uses tasks_title_norm_trgm, no embedding/LLM call). Warns, never blocks."""
    if not title or not title.strip():
        return []
    cur.execute(
        f"SELECT id, title, round(similarity(title_norm, {_NORM_TITLE})::numeric, 2) AS sim "
        f"FROM tasks WHERE trashed_at IS NULL AND title_norm %% {_NORM_TITLE} "
        f"ORDER BY sim DESC LIMIT 3",
        (title, title))
    hits = cur.fetchall()
    if not hits:
        return []
    joined = "; ".join(f'"{r["title"]}" ({r["id"]}, sim {r["sim"]})' for r in hits)
    return [f"similar task(s) already exist — {joined}. Update one instead of duplicating?"]


def _insert_one(cur, table: str, fields: dict, user: str, actor: str) -> tuple[str, list[str]]:
    """Insert one page/header/task/decision/source on an open cursor. Returns (id, warnings)."""
    warnings: list[str] = []
    if table == "pages":
        new_id = _new_entity(cur, "page")
        cur.execute(
            "INSERT INTO pages (id, title, summary, type, freshness, contributors) "
            "VALUES (%s,%s,%s, coalesce(nullif(%s,''),%s), coalesce(%s,'hot')::page_freshness, %s)",
            (new_id, fields.get("title"), fields.get("summary"),
             fields.get("type"), DEFAULT_PAGE_TYPE, fields.get("freshness"),
             fields.get("contributors") or [user]))
    elif table == "headers":
        new_id = _insert_header(cur, fields)
        warnings += _sync_links(cur, "header", new_id, fields.get("body"), "headers", "body")
        # embedding is deferred: the row acks now with embedded_at NULL; the embed worker drains it (F6)
        for sid in fields.get("source_ids") or []:
            cur.execute("INSERT INTO header_sources (header_id, source_id) VALUES (%s,%s) "
                        "ON CONFLICT DO NOTHING", (new_id, sid))
        _touch_contributors(cur, page_id=fields.get("page_id"), user=user)
    else:
        kind = SUBTYPE_KIND[table]
        if table == "tasks":
            warnings += _similar_task_warn(cur, fields.get("title"))   # dedupe hint (cheap, no LLM)
            if not fields.get("responsible"):
                fields["responsible"] = [user]    # default owner to the committing user (F4)
        new_id = _new_entity(cur, kind)
        _insert_subtype(cur, table, new_id, fields)
        text_field = "summary" if table == "sources" else "description"
        warnings += _sync_links(cur, kind, new_id, fields.get(text_field), table, text_field)
        # embedding deferred to the worker (F6) — row acks now with embedded_at NULL
    log_edit(cur, table_name=table, row_id=new_id, operation="insert",
             edited_by=user, actor=actor, changes=fields)
    return new_id, warnings


@mcp.tool
def insert(table: str, fields: dict | None = None, rows: list[dict] | None = None) -> str:
    """Deprecated — prefer the unified `write` tool (kept this release for compatibility).
    Create a page / header / task / decision / source. The server creates the entity row,
    embeds from the row's fields, parses markdown links into the graph, and logs an edit.
    edited_by/actor are stamped from the session — do not set them. For groups use `group`; for
    junction rows use `link`.

    Batch: pass `rows` (a list of field dicts, all for the same `table`) to insert many in ONE
    transaction — the bulk-ingest path. Insert pages before the chunks/rows that link to them so
    [[wikilinks]] resolve. `fields` inserts a single row (use one of the two, not both). A batch
    returns a compact `ids:` list; a single insert echoes the title."""
    if table not in ("pages", "headers", "tasks", "decisions", "sources"):
        return f"(insert not supported for {table}; use group/link tools where appropriate)"
    batch = rows if rows is not None else [fields or {}]
    if not batch:
        return "(nothing to insert)"
    user, actor = _identity()
    results: list[tuple[str, str, list[str]]] = []
    with db().connection() as conn:
        with conn.cursor() as cur:
            for f in batch:
                f = dict(f or {})
                nid, warns = _insert_one(cur, table, f, user, actor)
                results.append((nid, _write_label(f), warns))
        conn.commit()
    warns_all = [w for _, _, ws in results for w in ws]
    if rows is not None:
        out = f"inserted {len(results)} {table}\nids: " + ",".join(nid for nid, _, _ in results)
    else:
        nid, lbl, _ = results[0]
        out = f"inserted {table} {lbl} ({nid})"
    return out + (f"\n{len(warns_all)} warning(s):\n" + "\n".join(warns_all) if warns_all else "")


_INDEX_OFFSET = 1_000_000   # temp offset to shift indexes collision-free


def _make_room(cur, page: str, at_index: int) -> None:
    """Open one slot at `at_index` among LIVE headers, shifting rows >= it up by one — without
    tripping the partial UNIQUE(page_id,index) WHERE trashed_at IS NULL. A plain
    `index = index + 1` can fail because Postgres updates rows in heap order and checks
    uniqueness per row; so move the live tail far out of range first, then renumber it back
    contiguously above the new slot. Position is a live-header concept, so trashed rows are
    left alone. embedded_at is preserved (a reorder doesn't change embedding text)."""
    cur.execute("UPDATE headers SET index = index + %s "
                "WHERE page_id=%s AND trashed_at IS NULL AND index >= %s",
                (_INDEX_OFFSET, page, at_index))
    cur.execute("SELECT id FROM headers WHERE page_id=%s AND trashed_at IS NULL AND index >= %s "
                "ORDER BY index", (page, _INDEX_OFFSET))
    for i, r in enumerate(cur.fetchall()):
        cur.execute("UPDATE headers SET index=%s, "
                    "embedded_at = CASE WHEN embedded_at IS NULL THEN NULL ELSE now() END "
                    "WHERE id=%s", (at_index + 1 + i, r["id"]))


_LEADING_HEADING = re.compile(r"^[ \t]*#{1,6}[ \t]+(.*?)[ \t]*#*[ \t]*(?:\n|$)")


def _strip_dup_title_heading(title: str | None, body: str | None) -> str | None:
    """Drop a leading markdown heading line from `body` when it only repeats the chunk `title`.
    Imported/cherry-picked content kept the `## Heading` inside the body while the same text was
    also lifted into `title`, so `fetch` rendered it twice and the embedding double-counted it
    (the composer already prepends path + blurb, embedding.md). Strips only an EXACT title match;
    a genuinely different leading heading is left alone."""
    if not body or not title:
        return body
    m = _LEADING_HEADING.match(body)
    if not m or m.group(1).strip().lower() != title.strip().lower():
        return body
    return body[m.end():].lstrip("\n")


def _insert_header(cur, f: dict) -> str:
    page = f.get("page_id")
    index = int(f.get("index", 0))
    _make_room(cur, page, index)   # single transaction; keeps UNIQUE(page_id,index)
    path = f.get("path")
    if not path:
        cur.execute("SELECT title FROM pages WHERE id=%s", (page,))
        prow = cur.fetchone()
        ptitle = (prow or {}).get("title") or ""
        path = f"{ptitle} > {f.get('title')}" if f.get("title") else ptitle
    body = _strip_dup_title_heading(f.get("title"), f.get("body"))   # keep title out of body
    cur.execute(
        "INSERT INTO headers (page_id, title, index, level, path, blurb, body) "
        "VALUES (%s,%s,%s,%s,%s,%s,%s) RETURNING id",
        (page, f.get("title"), index, int(f.get("level", 1)), path, f.get("blurb"), body))
    return str(cur.fetchone()["id"])


def _insert_subtype(cur, table: str, new_id: str, f: dict) -> None:
    if table == "sources":
        cur.execute(
            "INSERT INTO sources (id, type, title, reference, sha256, summary) "
            "VALUES (%s,%s,%s,%s,%s,%s)",
            (new_id, f.get("type"), f.get("title"), f.get("reference"), f.get("sha256"),
             f.get("summary")))
    elif table == "tasks":
        # priority/scope are nullable (F4): an unset field stays NULL, distinct from a deliberate
        # choice — no silent 'medium'/'minutes' default. status keeps its 'todo' default.
        cur.execute(
            "INSERT INTO tasks (id, title, description, status, priority, scope, deadline, responsible) "
            "VALUES (%s,%s,%s, coalesce(%s,'todo')::task_status, %s::task_priority, "
            "%s::task_scope, %s, %s)",
            (new_id, f.get("title"), f.get("description"), f.get("status"), f.get("priority"),
             f.get("scope"), f.get("deadline"), f.get("responsible")))
    elif table == "decisions":
        cur.execute(
            "INSERT INTO decisions (id, title, description, decided_by, decided_at) "
            "VALUES (%s,%s,%s,%s,%s)",
            (new_id, f.get("title"), f.get("description"), f.get("decided_by"), f.get("decided_at")))


def _embed_and_set(cur, table: str, rid: str) -> None:
    """Re-read the row, compose deterministic text, embed, store embedding + embedded_at."""
    cur.execute(f"SELECT * FROM {table} WHERE id=%s", (rid,))
    row = cur.fetchone()
    if not row:
        return
    literal = _embed_row(table, dict(row))
    if literal is None:
        return
    col = et.COMPOSERS[table][1]
    cur.execute(
        f"UPDATE {table} SET {col} = %s::halfvec, embedded_at = now() WHERE id=%s", (literal, rid))


def _touch_contributors(cur, *, page_id: str | None, user: str) -> None:
    """Append `user` to pages.contributors iff absent — order-preserving (append-only list)."""
    if not page_id:
        return
    cur.execute(
        "UPDATE pages SET contributors = CASE "
        "  WHEN %s = ANY(coalesce(contributors, '{}'::varchar(64)[])) THEN contributors "
        "  ELSE coalesce(contributors, '{}'::varchar(64)[]) || %s::varchar(64) END "
        "WHERE id=%s", (user, user, page_id))


def _update_one(cur, table: str, rid: str, set_fields: dict, user: str, actor: str):
    """Update one row on an open cursor. Returns (label, warnings), or None if the row was missing
    or no valid fields were given."""
    cols = _cols(table)
    fieldset = {k: v for k, v in dict(set_fields or {}).items() if k in cols}
    if not rid or not fieldset:
        return None
    warnings: list[str] = []
    if table == "headers" and "body" in fieldset:              # keep the title out of the body
        htitle = fieldset.get("title")
        if htitle is None:
            cur.execute("SELECT title FROM headers WHERE id=%s", (rid,))
            r0 = cur.fetchone()
            htitle = r0["title"] if r0 else None
        fieldset["body"] = _strip_dup_title_heading(htitle, fieldset["body"])
    assigns = ", ".join(f"{k} = %s" for k in fieldset)
    cur.execute(f"UPDATE {table} SET {assigns} WHERE id=%s RETURNING *", list(fieldset.values()) + [rid])
    row = cur.fetchone()
    if not row:
        return None
    row = dict(row)
    if table == "pages" and "title" in fieldset:               # rename cascade: rebuild paths, mark stale
        _rename_cascade(cur, rid, fieldset["title"])
    text_field = {"headers": "body", "tasks": "description",
                  "decisions": "description", "sources": "summary"}.get(table)
    if text_field and text_field in fieldset:
        warnings += _sync_links(cur, SUBTYPE_KIND.get(table, "header"), rid, row.get(text_field), table, text_field)
    # embedding deferred: updated_at bumps past embedded_at, so the worker re-embeds this row (F6).
    # (_embedded_field_changed / _embed_and_set stay for janitor -embed, the manual backstop.)
    if table == "pages":
        # an explicit contributors write REPLACES (honored above); skip the auto-append so a
        # rewrite to a single canonical name isn't re-polluted by the connected username.
        if "contributors" not in fieldset:
            _touch_contributors(cur, page_id=rid, user=user)
    elif table == "headers":
        _touch_contributors(cur, page_id=str(row.get("page_id")), user=user)
    log_edit(cur, table_name=table, row_id=rid, operation="update", edited_by=user, actor=actor, changes=fieldset)
    return _write_label(row), warnings


@mcp.tool
def update(table: str, id: str | None = None, set: dict | None = None,
           updates: list[dict] | None = None) -> str:
    """Deprecated — prefer the unified `write` tool (kept this release for compatibility).
    Edit fields on an existing row. Re-embeds if an embedded field changed; re-parses links if a
    text field changed; appends you to the page's contributors; logs an edit. Page-title rename
    rebuilds header paths and marks that page's chunks stale for /janitor -embed. Set
    freshness=static (never-stale reference) or archived (superseded) here.

    Batch: pass `updates` (a list of {id, set} dicts, all for the same `table`) to edit many in ONE
    transaction. `id` + `set` edit a single row (use one form, not both)."""
    if table not in {"pages", "headers", "tasks", "decisions", "sources"}:
        return f"(update not supported for {table})"
    batch = updates if updates is not None else [{"id": id, "set": set}]
    user, actor = _identity()
    done, missing, warns_all = [], [], []
    with db().connection() as conn:
        with conn.cursor() as cur:
            for u in batch:
                rid = u.get("id")
                res = _update_one(cur, table, rid, u.get("set"), user, actor)
                if res is None:
                    missing.append(str(rid))
                else:
                    done.append(f"{res[0]} ({rid})")
                    warns_all += res[1]
        conn.commit()
    if updates is not None:
        out = f"updated {len(done)} {table}" + (f"; {len(missing)} skipped (missing/no fields): {','.join(missing)}" if missing else "")
    elif not done:
        out = f"({table} {id} not updated: missing or no valid fields)"
    else:
        out = f"updated {table} {done[0]}"
    return out + (f"\n{len(warns_all)} warning(s):\n" + "\n".join(warns_all) if warns_all else "")


_EMBEDDED_FIELDS = {
    "headers": {"path", "blurb", "body", "title"},
    "sources": {"type", "title", "summary"},
    "tasks": {"title", "status", "priority", "scope", "deadline", "description"},
    "decisions": {"title", "decided_by", "decided_at", "description"},
}


def _embedded_field_changed(table: str, fieldset: dict) -> bool:
    return bool(_EMBEDDED_FIELDS.get(table, set()) & set(fieldset))


def _rename_cascade(cur, page_id: str, new_title: str) -> None:
    """Rebuild every header path's first segment to the new title; mark chunks stale."""
    cur.execute("SELECT id, path FROM headers WHERE page_id=%s", (page_id,))
    for r in cur.fetchall():
        old = r["path"] or ""
        rest = old.split(" > ", 1)
        newpath = new_title + (" > " + rest[1] if len(rest) > 1 else "")
        cur.execute("UPDATE headers SET path=%s, embedded_at=NULL WHERE id=%s", (newpath, r["id"]))


@mcp.tool
def delete(table: str, ids: list[str]) -> str:
    """Deprecated — prefer the unified `write` tool with `trashed: true` (kept this release).
    Trash rows (nothing is ever hard-deleted). Pages -> freshness='trashed';
    headers/tasks/decisions/sources -> trashed_at=now(). Logs an edit (operation='trash').
    Trashed rows drop out of lookup/snippet but stay fetchable by id. Recover with update."""
    user, actor = _identity()
    if table not in ("pages", "headers", "tasks", "decisions", "sources"):
        return f"(delete not supported for {table})"
    n = 0
    labels: list[str] = []
    with db().connection() as conn:
        with conn.cursor() as cur:
            for rid in ids or []:
                cur.execute(f"SELECT * FROM {table} WHERE id=%s", (rid,))
                row = cur.fetchone()
                if table == "pages":
                    cur.execute("UPDATE pages SET freshness='trashed' WHERE id=%s", (rid,))
                else:
                    cur.execute(f"UPDATE {table} SET trashed_at=now() WHERE id=%s", (rid,))
                if cur.rowcount:
                    labels.append(_write_label(dict(row)) if row else str(rid))
                    log_edit(cur, table_name=table, row_id=rid, operation="trash",
                             edited_by=user, actor=actor)
                    n += 1
        conn.commit()
    return f"trashed {n} {table}: " + ", ".join(labels) if labels else f"trashed 0 rows in {table}"


# =======================================================================================
# link / group  (manual link repair + flexible categories)
# =======================================================================================

@mcp.tool
def link(action: str, table: str, fields: dict | list[dict]) -> str:
    """Prefer `write` for entity links (markdown/[[wikilinks]] in a body). Still the path for
    junction rows write does not cover: task_dependencies, header_sources, group_links.
    Manual repair for auto-linking. action: add|remove. table: links | header_sources |
    task_dependencies | group_links. fields carries that table's columns (e.g. task_dependencies:
    blocker, dependent; links: from_kind, from_id, to_kind, to_id). Pass a LIST of field dicts to
    add/remove many junction rows in one transaction (the relinker path)."""
    allowed = {
        "links": ["from_kind", "from_id", "to_kind", "to_id"],
        "header_sources": ["header_id", "source_id"],
        "task_dependencies": ["blocker", "dependent"],
        "group_links": ["group_id", "entity_id"],
    }
    if table not in allowed:
        return f"(link not supported for {table})"
    if action not in ("add", "remove"):
        return "(action must be add or remove)"
    cols = allowed[table]
    batch = fields if isinstance(fields, list) else [fields]
    ok, bad = 0, 0
    with db().connection() as conn:
        with conn.cursor() as cur:
            for f in batch:
                vals = [(f or {}).get(c) for c in cols]
                if any(v is None for v in vals):
                    bad += 1
                    continue
                if action == "add":
                    ph = ",".join(["%s"] * len(cols))
                    cur.execute(
                        f"INSERT INTO {table} ({','.join(cols)}) VALUES ({ph}) ON CONFLICT DO NOTHING", vals)
                else:
                    cond = " AND ".join(f"{c}=%s" for c in cols)
                    cur.execute(f"DELETE FROM {table} WHERE {cond}", vals)
                ok += 1
        conn.commit()
    if isinstance(fields, list):
        return f"{action} on {table}: {ok} ok" + (f", {bad} skipped (missing fields)" if bad else "")
    return f"{action} on {table}: ok" if ok else f"(need fields: {', '.join(cols)})"


@mcp.tool
def group(
    action: str,
    id: str | None = None,
    name: str | None = None,
    type: str | None = None,
    description: str | None = None,
    participants: list[str] | None = None,
    members: list[str] | None = None,
    set: dict | None = None,
    filter_name: str | None = None,
) -> str:
    """Prefer `write` (a `type: group` block) for group ROW create/update; this tool still handles
    membership (add/remove) and list. Flexible categories over entities (projects/topics/areas). action:
    create (name,type,description,participants) | update (id,set) | add (id,members) |
    remove (id,members) | archive (id) | unarchive (id) | list ([type],[filter_name]). archive
    sets archived_at and drops the group from default `list`s (also via write `trashed: true`);
    unarchive reverses it. Membership is entity ids (pages/tasks/decisions/sources). Groups are
    filterable in lookup and render via fetch/snippet."""
    with db().connection() as conn:
        with conn.cursor() as cur:
            if action == "create":
                cur.execute(
                    "INSERT INTO groups (name, type, description, participants) "
                    "VALUES (%s,%s,%s,%s) RETURNING id",
                    (name, type, description, participants))
                gid = str(cur.fetchone()["id"])
                conn.commit()
                return f'created group "{name}" ({gid})'
            if action == "update":
                s = {k: v for k, v in (set or {}).items()
                     if k in {"name", "type", "description", "participants"}}
                if not s:
                    return "(no valid group fields)"
                cur.execute(f"UPDATE groups SET {', '.join(f'{k}=%s' for k in s)} WHERE id=%s",
                            list(s.values()) + [id])
                conn.commit()
                return f"updated group {id}"
            if action in ("archive", "unarchive"):
                if not id:
                    return "(archive needs a group id)"
                cur.execute("UPDATE groups SET archived_at=%s WHERE id=%s",
                            (None if action == "unarchive" else _now(), id))
                conn.commit()
                return f"(no such group {id})" if not cur.rowcount else f"{action}d group {id}"
            if action in ("add", "remove"):
                for eid in members or []:
                    if action == "add":
                        cur.execute("SELECT 1 FROM entities WHERE id=%s", (eid,))
                        if not cur.fetchone():
                            continue
                        cur.execute("INSERT INTO group_links (group_id, entity_id) VALUES (%s,%s) "
                                    "ON CONFLICT DO NOTHING", (id, eid))
                    else:
                        cur.execute("DELETE FROM group_links WHERE group_id=%s AND entity_id=%s",
                                    (id, eid))
                conn.commit()
                return f"{action} membership on group {id}: ok"
    # list (read-only, no txn needed) — archived groups are hidden by default
    if action == "list":
        clauses, params = ["archived_at IS NULL"], []
        if type:
            clauses.append("type=%s")
            params.append(type)
        if filter_name:
            clauses.append("name ILIKE %s")
            params.append(f"%{filter_name}%")
        sql = "SELECT id, name, type, description FROM groups"
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        sql += " ORDER BY updated_at DESC LIMIT 100"
        out = [f"{r['id']} | {r['name']} | {r['type']} | {r['description'] or ''}"
               for r in db().query(sql, params)]
        return "\n".join(out) if out else "(no groups)"
    return "(unknown group action)"


# =======================================================================================
# write — one template-upsert tool over the write path (folds insert/update/delete/link/group)
# =======================================================================================

def _row_live(cur, table: str, rid: str) -> bool:
    live = "freshness <> 'trashed'" if table == "pages" else "trashed_at IS NULL"
    cur.execute(f"SELECT 1 FROM {table} WHERE id=%s AND {live}", (rid,))
    return cur.fetchone() is not None


def _same(cur_val, new_val) -> bool:
    """Loose equality for the update no-op filter: skip a field whose new value already matches the
    stored one (avoids a needless re-embed / edit-log row). Errs toward writing when unsure."""
    if cur_val is None and new_val is None:
        return True
    return str(cur_val) == str(new_val)


def _drop_unchanged(cur, table: str, rid: str, upd: dict) -> dict:
    cur.execute(f"SELECT * FROM {table} WHERE id=%s", (rid,))
    cur_row = dict(cur.fetchone() or {})
    return {k: v for k, v in upd.items() if not (k in cur_row and _same(cur_row[k], v))}


def _trash_one(cur, table: str, rid: str, user: str, actor: str) -> str:
    cur.execute(f"SELECT * FROM {table} WHERE id=%s", (rid,))
    row = cur.fetchone()
    if not row:
        return f"(error: {table} id {rid} not found)"
    if table == "pages":
        cur.execute("UPDATE pages SET freshness='trashed' WHERE id=%s", (rid,))
    else:
        cur.execute(f"UPDATE {table} SET trashed_at=now() WHERE id=%s", (rid,))
    log_edit(cur, table_name=table, row_id=rid, operation="trash", edited_by=user, actor=actor)
    return f"trashed {table} {_write_label(dict(row))} ({rid})"


def _write_group(cur, rid: str | None, fields: dict, trashed: bool) -> str:
    """Group ROW create/update from a `type: group` block. Membership + listing stay on the
    `group` tool (a rendered member roster is labels, not a clean id set to reconcile from)."""
    if trashed:
        if not rid:
            return "(skip: archiving a group needs an id)"
        cur.execute("UPDATE groups SET archived_at=now() WHERE id=%s", (rid,))
        return f"archived group {rid}" if cur.rowcount else f"(error: group id {rid} not found)"
    s = {k: v for k, v in fields.items() if k in {"name", "type", "description", "participants"}}
    _, err = _check_enums("groups", s)
    if err:
        return f"(error: {err})"
    if rid:
        cur.execute("SELECT 1 FROM groups WHERE id=%s", (rid,))
        if not cur.fetchone():
            return f"(error: group id {rid} not found — omit id to create)"
        if not s:
            return f"(no change: group {rid})"
        cur.execute(f"UPDATE groups SET {', '.join(f'{k}=%s' for k in s)} WHERE id=%s",
                    list(s.values()) + [rid])
        return f"updated group {rid}"
    if not s.get("name"):
        return "(error: a new group needs a name)"
    cur.execute("INSERT INTO groups (name, type, description, participants) VALUES (%s,%s,%s,%s) RETURNING id",
                (s.get("name"), s.get("type"), s.get("description"), s.get("participants")))
    return f'inserted group "{s.get("name")}" ({str(cur.fetchone()["id"])})'


def _warns(ws: list[str]) -> str:
    return ("\n  " + "\n  ".join(ws)) if ws else ""


def _dispatch_block(cur, p: dict, user: str, actor: str) -> str:
    return _dispatch_block_inner(cur, p, user, actor) + _warns(p.get("warnings") or [])


def _dispatch_block_inner(cur, p: dict, user: str, actor: str) -> str:
    table, kind, rid, fields = p["table"], p["kind"], p["id"], p["fields"]
    if kind == "edit":
        # edits are the append-only audit log (no trashed_at) — write supports only hard delete,
        # for pruning bad/noisy log rows. Delete-by-id, trashed:true required.
        if not rid:
            return "(skip: an edit block needs an id — deletion is by id)"
        if not p["trashed"]:
            return "(edits are audit-log rows — write supports only delete via trashed: true)"
        cur.execute("DELETE FROM edits WHERE id=%s", (rid,))
        return f"deleted edit {rid}" if cur.rowcount else f"(error: edit id {rid} not found)"
    if kind == "group":
        return _write_group(cur, rid, fields, p["trashed"])
    if p["trashed"]:
        return _trash_one(cur, table, rid, user, actor) if rid else f"(skip: trash needs an id — {kind})"
    notes, err = _check_enums(table, fields)          # validate/normalize enum values (F5)
    if err:
        return f"(error: {err})"
    tag = ("  [" + "; ".join(notes) + "]") if notes else ""
    if rid:
        if not _row_live(cur, table, rid):
            return f"(error: {kind} id {rid} not found — no update; omit id to insert a new row)"
        upd = dict(fields)
        if kind == "header":                      # moving a chunk (page_id/index) is out of scope
            for k in ("page_id", "index"):
                upd.pop(k, None)
        upd = _drop_unchanged(cur, table, rid, upd)
        if not upd:
            return f"(no change: {table} {rid})"
        res = _update_one(cur, table, rid, upd, user, actor)
        return (f"updated {table} {res[0]} ({rid}){tag}" + _warns(res[1])) if res else f"(error: update failed {rid})"
    ins = {k: v for k, v in fields.items() if v is not None}   # let DB defaults/NULL apply on insert
    if kind == "header" and not ins.get("page_id"):
        return "(error: a header block needs page_id to insert — get it from fetch outline)"
    nid, warns = _insert_one(cur, table, ins, user, actor)
    return f"inserted {table} {_write_label(ins)} ({nid}){tag}" + _warns(warns)


@mcp.tool
def write(blocks: list[str]) -> str:
    """Create or update entities from template blocks — the single write path.

    Each element of `blocks` is ONE `---`-fenced frontmatter template, the SAME shape `fetch`
    returns, so you write what you read. `blocks` is a LIST (batch = several templates, one
    transaction); pass one element for a single write.

    - `type:` marks the kind — task / decision / source / group / header; anything else is a PAGE
      (whose `type:` is its free OKF page type, e.g. note/report). A chunk is `type: header` with
      `page_id`, `index`, `title`, `blurb` in the frontmatter, then the body AFTER the closing `---`.
    - `id:` present and live → UPDATE that row: only the fields you include change; an omitted field
      is left unchanged, an explicitly empty field is cleared. `id:` absent → INSERT. An `id:` that
      matches no live row is an ERROR (never a silent duplicate).
    - Trash a row with `trashed: true` (or `freshness: trashed` on a page). A `type: edit` block
      with an `id` and `trashed: true` HARD-deletes that audit-log row (edits have no trash state) —
      for pruning noisy/bad log entries; get the id from read_sql or the export log.
    - Chunk ids come from `fetch(outline=true)` / `lookup(outline_page=…)` — a whole-page fetch
      does not show them. Links: `[text](kind:uuid)` / `[[wikilinks]]` in the body/description as
      before. Group membership and task blockers still use the `group` / `link` tools.
    """
    user, actor = _identity()
    parsed, lines = [], []
    for i, b in enumerate(blocks or []):
        try:
            parsed.append(bl.parse_block(b))
        except bl.BlockError as e:
            lines.append(f"(error: block {i}: {e})")
    if not parsed:
        return "\n".join(lines) or "(nothing to write)"
    with db().connection() as conn:
        with conn.cursor() as cur:
            for p in parsed:
                lines.append(_dispatch_block(cur, p, user, actor))
        conn.commit()
    return "\n".join(lines)


# =======================================================================================
# janitor  (server-side maintenance — needs DB access)
# =======================================================================================

@mcp.tool
def janitor(flags: list[str] | None = None) -> str:
    """Kovault maintenance. Bare (no flags) = diagnose only: run checks, write a janitor_reports
    row, change nothing. Flags opt into work: -lint (renumber header indexes + prune redundant
    parent/grandparent task-dependency edges), -freshness
    (recompute hot/warm/cold by age; never touches static/archived/trashed), -dedupe (merge
    duplicate sources by sha256 and identical headers -> trash losers), -embed (re-embed rows
    with embedded_at < updated_at or null), -relink (re-resolve [[wikilinks]] over all live rows
    so forward-references graph once their targets exist), -normalize-people (case-fold + dedupe
    person values across contributors/responsible/participants/decided_by). There is no delete
    flag — trash is terminal."""
    flags = [f.lstrip("-").lower() for f in (flags or [])]
    user = "janitor"
    counts: dict = {}
    report: list[str] = []

    # ---- diagnostics (always) ----
    diag = _janitor_diagnose()
    counts["diagnostics"] = diag
    report.append("Diagnostics: " + ", ".join(f"{k}={v}" for k, v in diag.items()))

    with db().connection() as conn:
        with conn.cursor() as cur:
            if "freshness" in flags:
                counts["freshness"] = _janitor_freshness(cur, user)
                report.append(f"Recomputed freshness on {counts['freshness']} page(s).")
            if "embed" in flags:
                counts["embed"] = _janitor_embed(cur, user)
                report.append(f"Re-embedded {counts['embed']} stale/missing row(s).")
            if "lint" in flags:
                counts["lint"] = _janitor_lint(cur, user)
                report.append(f"Renumbered header indexes on {counts['lint']} page(s).")
                counts["pruned_deps"] = _janitor_prune_deps(cur, user)
                report.append(f"Pruned {counts['pruned_deps']} redundant task-dependency edge(s).")
            if "dedupe" in flags:
                counts["dedupe"] = _janitor_dedupe(cur, user)
                report.append(f"Trashed {counts['dedupe']} duplicate row(s).")
            if "normalize-people" in flags:
                counts["normalize_people"] = _janitor_normalize_people(cur, user)
                report.append(f"Normalized people on {counts['normalize_people']} row(s).")
            if "relink" in flags:
                counts["relink"] = _janitor_relink(cur, user)
                report.append(f"Resolved {counts['relink']} dangling wikilink edge(s).")
            if not flags:
                report.append("Diagnose-only run — no changes made. "
                              "Re-run with -embed/-freshness/-lint/-dedupe/-relink to act.")
            # log the run
            cur.execute(
                "INSERT INTO janitor_reports (flags, report, counts) VALUES (%s,%s,%s) RETURNING id",
                (flags or None, "\n".join(report), Json(counts)))
            run_id = str(cur.fetchone()["id"])
        conn.commit()
    return f"janitor run {run_id}\n" + "\n".join(report)


# link-bearing text column per table (mirrors the insert/update resolver call sites)
_RELINK_FIELDS = {"headers": "body", "tasks": "description",
                  "decisions": "description", "sources": "summary"}


def _janitor_relink(cur, user: str) -> int:
    """Re-run [[wikilink]] resolution over every live row so forward-references graph once their
    targets exist. Reuses the insert/update resolver (_sync_links -> _convert_obsidian): it
    bypasses the write-time obsidian-ratio gate (runs on all rows), guards ambiguous single-word
    titles (_resolve_title needs exactly one live match, else stays text), and does NOT re-embed
    — only rows whose body actually changed are rewritten, and that raw UPDATE never marks a row
    embed-stale beyond what a normal body edit would. Returns the net new graph edges."""
    cur.execute("SELECT count(*) n FROM links")
    before = int(cur.fetchone()["n"])
    for table, col in _RELINK_FIELDS.items():
        kind = "header" if table == "headers" else SUBTYPE_KIND[table]
        cur.execute(f"SELECT id, {col} AS txt FROM {table} "
                    f"WHERE trashed_at IS NULL AND {col} IS NOT NULL")
        for r in cur.fetchall():        # fetchall drains the cursor before _sync_links reuses it
            _sync_links(cur, kind, str(r["id"]), r["txt"], table, col)
    cur.execute("SELECT count(*) n FROM links")
    return int(cur.fetchone()["n"]) - before


# A direct 'X blocks Y' edge is REDUNDANT when Y is also reachable from X through an intermediate
# (X blocks ... blocks Y): the parent block already implies the grandparent block. Depth-bounded
# for cycle safety (task deps are meant to be a DAG).
_REDUNDANT_DEPS_SQL = """
    WITH RECURSIVE reach(root, node, depth) AS (
        SELECT blocker, dependent, 1 FROM task_dependencies
        UNION ALL
        SELECT r.root, d.dependent, r.depth + 1
        FROM reach r JOIN task_dependencies d ON d.blocker = r.node
        WHERE r.depth < 50
    )
    SELECT td.blocker, td.dependent
    FROM task_dependencies td
    WHERE EXISTS (
        SELECT 1 FROM reach r
        WHERE r.root = td.blocker AND r.node = td.dependent AND r.depth >= 2
    )
"""


def _janitor_diagnose() -> dict:
    q = db().query_one
    stale = 0
    for t in ("headers", "tasks", "decisions", "sources"):
        col = et.COMPOSERS[t][1]
        r = q(f"SELECT count(*) n FROM {t} WHERE trashed_at IS NULL "
              f"AND (embedded_at IS NULL OR embedded_at < updated_at)")
        stale += int(r["n"])
    trashed = int(q("SELECT count(*) n FROM pages WHERE freshness='trashed'")["n"])
    dangling = int(q(
        "SELECT count(*) n FROM links l WHERE NOT EXISTS ("
        " SELECT 1 FROM headers h WHERE l.to_kind='header' AND h.id=l.to_id AND h.trashed_at IS NULL)"
        " AND l.to_kind='header'")["n"])
    redundant = int(q(f"SELECT count(*) n FROM ({_REDUNDANT_DEPS_SQL}) x")["n"])
    # near-duplicate groups (F7): normalized-name collisions + pairs sharing >=3 members. Report
    # only — never auto-merge, since a real distinction (e.g. two same-named servers) may be intended.
    dup_group_names = int(q(
        "SELECT count(*) n FROM (SELECT lower(regexp_replace(name,'[^a-zA-Z0-9]','','g')) k "
        "FROM groups GROUP BY 1 HAVING count(*) > 1) x")["n"])
    overlapping_groups = int(q(
        "SELECT count(*) n FROM (SELECT a.group_id FROM group_links a "
        "JOIN group_links b ON a.entity_id=b.entity_id AND a.group_id < b.group_id "
        "GROUP BY a.group_id, b.group_id HAVING count(*) >= 3) x")["n"])
    # orphan tasks (F4): live tasks with no graph link and no dependency edge — hard to find/trust.
    orphan_tasks = int(q(
        "SELECT count(*) n FROM tasks t WHERE t.trashed_at IS NULL "
        "AND NOT EXISTS (SELECT 1 FROM links l WHERE (l.from_kind='task' AND l.from_id=t.id) "
        "                                          OR (l.to_kind='task' AND l.to_id=t.id)) "
        "AND NOT EXISTS (SELECT 1 FROM task_dependencies d WHERE d.blocker=t.id OR d.dependent=t.id)")["n"])
    return {"stale_embeddings": stale, "trashed_pages": trashed,
            "dangling_header_links": dangling, "redundant_blocks": redundant,
            "duplicate_group_names": dup_group_names, "overlapping_groups": overlapping_groups,
            "orphan_tasks": orphan_tasks}


def _janitor_freshness(cur, user: str) -> int:
    s = db().settings()["freshness_days"]
    hot, warm = int(s["hot"]), int(s["warm"])
    # Update (and log) ONLY pages whose bucket actually changes — no-op rewrites would both
    # over-count and (via the trigger) needlessly churn. The pages trigger keeps updated_at
    # stable on a freshness-only change so age stays meaningful across runs.
    cur.execute(
        """
        WITH computed AS (
            SELECT id, CASE
                WHEN now() - updated_at <= (%s || ' days')::interval THEN 'hot'
                WHEN now() - updated_at <= (%s || ' days')::interval THEN 'warm'
                ELSE 'cold' END::page_freshness AS newf
            FROM pages WHERE freshness IN ('hot','warm','cold')
        )
        UPDATE pages p SET freshness = c.newf
        FROM computed c
        WHERE p.id = c.id AND p.freshness IS DISTINCT FROM c.newf
        RETURNING p.id
        """, (hot, warm))
    ids = [str(r["id"]) for r in cur.fetchall()]
    for pid in ids:
        log_edit(cur, table_name="pages", row_id=pid, operation="update",
                 edited_by=user, actor="script", changes={"freshness": "recomputed"})
    return len(ids)


def _janitor_embed(cur, user: str) -> int:
    n = 0
    for table in ("headers", "tasks", "decisions", "sources"):
        cur.execute(f"SELECT id FROM {table} WHERE trashed_at IS NULL "
                    f"AND (embedded_at IS NULL OR embedded_at < updated_at)")
        for r in cur.fetchall():
            _embed_and_set(cur, table, str(r["id"]))
            n += 1
    return n


def _janitor_lint(cur, user: str) -> int:
    """Renumber LIVE header indexes to contiguous 0..n-1 per page. Trashed headers keep their
    index and are excluded by the partial UNIQUE index, so no collision. embedded_at preserved."""
    cur.execute("SELECT DISTINCT page_id FROM headers WHERE trashed_at IS NULL")
    pages = [str(r["page_id"]) for r in cur.fetchall()]
    changed = 0
    for pid in pages:
        cur.execute("SELECT id, index FROM headers WHERE page_id=%s AND trashed_at IS NULL "
                    "ORDER BY index", (pid,))
        rows = cur.fetchall()
        if [r["index"] for r in rows] != list(range(len(rows))):
            # bump out of the way, then renumber (avoid live-vs-live collisions mid-update)
            cur.execute("UPDATE headers SET index = index + 100000 WHERE page_id=%s "
                        "AND trashed_at IS NULL", (pid,))
            for newidx, r in enumerate(rows):
                cur.execute("UPDATE headers SET index=%s, "
                            "embedded_at = CASE WHEN embedded_at IS NULL THEN NULL ELSE now() END "
                            "WHERE id=%s", (newidx, r["id"]))
            log_edit(cur, table_name="pages", row_id=pid, operation="update",
                     edited_by=user, actor="script", changes={"lint": "reindexed headers"})
            changed += 1
    return changed


def _janitor_prune_deps(cur, user: str) -> int:
    """Transitive reduction of task_dependencies: drop a direct 'X blocks Y' edge when Y is also
    blocked by X through an intermediate (a redundant parent+grandparent block). The transitive
    block still holds via the path, so no dependency is actually lost. Computed on one snapshot,
    so removing all redundant edges preserves reachability."""
    cur.execute(_REDUNDANT_DEPS_SQL)
    edges = [(e["blocker"], e["dependent"]) for e in cur.fetchall()]
    for blocker, dependent in edges:
        cur.execute("DELETE FROM task_dependencies WHERE blocker=%s AND dependent=%s",
                    (blocker, dependent))
        log_edit(cur, table_name="tasks", row_id=str(dependent), operation="update",
                 edited_by=user, actor="script",
                 changes={"pruned_redundant_blocker": str(blocker)})
    return len(edges)


def _janitor_dedupe(cur, user: str) -> int:
    """Conservative: trash duplicate sources sharing a sha256 (keep earliest) and headers with
    identical (page_id, title, body) (keep earliest). Losers are trashed, never deleted."""
    trashed = 0
    cur.execute(
        "SELECT sha256, array_agg(id ORDER BY created_at, id) ids FROM sources "
        "WHERE trashed_at IS NULL AND sha256 IS NOT NULL GROUP BY sha256 HAVING count(*) > 1")
    for r in cur.fetchall():
        for loser in r["ids"][1:]:
            cur.execute("UPDATE sources SET trashed_at=now() WHERE id=%s", (loser,))
            log_edit(cur, table_name="sources", row_id=str(loser), operation="trash",
                     edited_by=user, actor="script", changes={"reason": "dedupe sha256"})
            trashed += 1
    cur.execute(
        "SELECT array_agg(id ORDER BY created_at, id) ids FROM headers "
        "WHERE trashed_at IS NULL GROUP BY page_id, title, body HAVING count(*) > 1")
    for r in cur.fetchall():
        for loser in r["ids"][1:]:
            cur.execute("UPDATE headers SET trashed_at=now() WHERE id=%s", (loser,))
            log_edit(cur, table_name="headers", row_id=str(loser), operation="trash",
                     edited_by=user, actor="script", changes={"reason": "dedupe identical"})
            trashed += 1
    return trashed


def _janitor_normalize_people(cur, user: str) -> int:
    """Case-fold (lowercase) + dedupe person values across contributors / responsible /
    participants (arrays) and decided_by (scalar). Matches the write-boundary lowercase norm, so
    baked-in case/spelling variants of one person (e.g. Alice/alice, Bob/bob/BobK) collapse. Only
    changed rows are rewritten + logged. Also the one-off backfill for the append-only contributors mess."""
    n = 0
    for table, col in (("pages", "contributors"), ("tasks", "responsible"),
                       ("groups", "participants")):
        cur.execute(
            f"UPDATE {table} t SET {col} = sub.arr "
            f"FROM (SELECT id, ARRAY(SELECT DISTINCT lower(x) FROM unnest({col}) x "
            f"                       WHERE x IS NOT NULL AND x <> '') arr "
            f"      FROM {table} WHERE {col} IS NOT NULL) sub "
            f"WHERE t.id = sub.id AND t.{col} IS DISTINCT FROM sub.arr RETURNING t.id")
        for r in cur.fetchall():
            log_edit(cur, table_name=table, row_id=str(r["id"]), operation="update",
                     edited_by=user, actor="script", changes={col: "normalized"})
            n += 1
    cur.execute("UPDATE decisions SET decided_by = lower(decided_by) "
                "WHERE decided_by IS DISTINCT FROM lower(decided_by) RETURNING id")
    for r in cur.fetchall():
        log_edit(cur, table_name="decisions", row_id=str(r["id"]), operation="update",
                 edited_by=user, actor="script", changes={"decided_by": "normalized"})
        n += 1
    return n


# =======================================================================================
# export  (no-AI OKF bundle — manifest tool + streamed-zip download route)
# =======================================================================================

def _export_scope(tables: list[str] | None, ids: list[str] | None) -> tuple[list[str], list[str] | None]:
    sel = [t for t in (tables or list(export_mod.TABLES)) if t in export_mod.TABLES]
    id_list = [i for i in (ids or []) if i] or None
    return sel, id_list


@mcp.tool
def export(tables: list[str] | None = None, ids: list[str] | None = None,
           wikilinks: bool = False, group: str | None = None, linked_to: str | None = None) -> str:
    """Prepare a no-AI OKF markdown export (pages/tasks/decisions/sources/groups; default all).
    Returns only a MANIFEST — per-table row counts plus the download path — never the file
    contents, so exporting never bloats context. Download the zip out of band with the /export
    command (it curls the path straight to a folder). Scope, narrowest wins: ids (specific rows),
    group (one group's members, exact name preferred), or linked_to (an id + its 1-hop graph
    neighbours) — combine as needed; default is the whole table set. tables: subset to export;
    wikilinks: rewrite [text](kind:uuid) links to [[Title]] wikilinks in the export."""
    sel, id_list = _export_scope(tables, ids)
    if not sel:
        return "(no valid tables; choose from pages,tasks,decisions,sources,groups)"
    if group or linked_to:
        scoped = export_mod.resolve_scope_ids(db(), group, linked_to)
        id_list = list(dict.fromkeys((id_list or []) + (scoped or []))) or scoped
    c = export_mod.counts(db(), sel, id_list)
    qs = ("tables=" + ",".join(sel) + (("&ids=" + ",".join(id_list)) if id_list else "")
          + ("&wikilinks=1" if wikilinks else ""))
    lines = ["EXPORT MANIFEST (no file contents — download out of band)"]
    lines += [f"{t}: {c.get(t, 0)}" for t in sel]
    lines.append(f"total rows: {sum(c.values())} (+ index.md, log.md)")
    lines.append(f"download: GET /export?{qs}")
    lines.append("save it with the /export command; contents never enter context")
    return "\n".join(lines)


@mcp.custom_route("/export", methods=["GET"])
async def export_download(request: Request):
    """Stream the OKF bundle as a zip attachment (read-only; mirrors what fetch/lookup expose).
    Query: tables (comma list, default all), ids (comma list, optional). The client saves the
    zip straight to disk, so bundle contents never enter an AI context."""
    tables = [t.strip() for t in (request.query_params.get("tables") or "").split(",") if t.strip()]
    ids = [i.strip() for i in (request.query_params.get("ids") or "").split(",") if i.strip()]
    wikilinks = (request.query_params.get("wikilinks") or "").lower() in ("1", "true", "yes")
    group = request.query_params.get("group") or None
    linked_to = request.query_params.get("linked_to") or None
    sel, id_list = _export_scope(tables, ids)
    if not sel:
        return JSONResponse({"error": "no valid tables"}, status_code=400)
    if group or linked_to:
        scoped = export_mod.resolve_scope_ids(db(), group, linked_to)
        id_list = list(dict.fromkeys((id_list or []) + (scoped or []))) or scoped
    data = await run_in_threadpool(export_mod.bundle_zip, db(), sel, id_list, wikilinks)
    return Response(
        content=data, media_type="application/zip",
        headers={"Content-Disposition": 'attachment; filename="kovault-export.zip"'})


# =======================================================================================
# relocate  (no-AI folder move — rewrite source-reference prefixes; used by kovault_relocate.py)
# =======================================================================================

def _relocate_source_refs(cur, old_prefix: str, new_prefix: str, user: str) -> int:
    """Repoint every live source.reference that lived under old_prefix to new_prefix. Only the
    path prefix changes (reference is not an embedded field, so no re-embed). References outside
    the Kovault folder (files you were merely pointed at) are left alone."""
    like = old_prefix.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
    cur.execute("SELECT id, reference FROM sources WHERE trashed_at IS NULL AND reference LIKE %s",
                (like,))
    n = 0
    for r in cur.fetchall():
        newref = new_prefix + r["reference"][len(old_prefix):]
        cur.execute("UPDATE sources SET reference=%s WHERE id=%s", (newref, r["id"]))
        log_edit(cur, table_name="sources", row_id=str(r["id"]), operation="update",
                 edited_by=user, actor="script", changes={"reference": "relocated"})
        n += 1
    return n


@mcp.custom_route("/relocate-sources", methods=["POST"])
async def relocate_sources(request: Request):
    """Rewrite source-reference path prefixes after the Kovault folder is moved (JSON body:
    old_prefix, new_prefix). Called by the no-AI kovault_relocate.py -move script. Mutates only
    sources.reference; every change is logged as an edit."""
    body = await request.json()
    old_prefix = (body or {}).get("old_prefix") or ""
    new_prefix = (body or {}).get("new_prefix") or ""
    if not old_prefix or not new_prefix:
        return JSONResponse({"error": "old_prefix and new_prefix required"}, status_code=400)
    user = request.headers.get("x-kovault-user") or os.getenv("KOVAULT_DEFAULT_USER", "script")

    def _do() -> int:
        with db().connection() as conn:
            with conn.cursor() as cur:
                n = _relocate_source_refs(cur, old_prefix, new_prefix, user)
            conn.commit()
        return n

    return JSONResponse({"updated": await run_in_threadpool(_do)})


# =======================================================================================
# page-meta  (cheap freshness probe — the fetch-dedup PreToolUse hook checks updated_at, F1)
# =======================================================================================

@mcp.custom_route("/page-meta", methods=["GET"])
async def page_meta(request: Request):
    """Return {page_id: updated_at_iso} for the given ids. The dedup hook calls this to decide
    whether a page changed since it was last fetched this session (edited -> allow a re-fetch)."""
    ids = [i.strip() for i in (request.query_params.get("ids") or "").split(",") if i.strip()]
    ids = [i for i in ids if _looks_uuid(i)]     # ignore non-uuid input rather than error on the cast
    if not ids:
        return JSONResponse({})

    def _q() -> dict:
        return {str(r["id"]): (r["updated_at"].isoformat() if r["updated_at"] else "")
                for r in db().query("SELECT id, updated_at FROM pages WHERE id = ANY(%s)", (ids,))}

    return JSONResponse(await run_in_threadpool(_q))


# =======================================================================================
# debug-log  (opt-in query trace — written by the plugin's PostToolUse hook, design/settings.md)
# =======================================================================================

@mcp.custom_route("/debug-log", methods=["POST"])
async def debug_log_ingest(request: Request):
    """Record one Kovault tool call in debug_log. Only the client holds the transcript, so the
    PostToolUse hook posts here (tool, inputs, result shape, latency, session, and the user
    message + Claude text that led to the call). Gated client-side by the local `debug` flag."""
    body = await request.json() or {}
    tool = body.get("tool")
    if not tool:
        return JSONResponse({"error": "tool required"}, status_code=400)

    def _do() -> str:
        with db().connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "INSERT INTO debug_log (session_id, edited_by, tool, tool_input, "
                    "result_summary, result, result_tokens, duration_ms, last_user_msg, assistant_text) "
                    "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING id",
                    (body.get("session_id"), body.get("user"), tool,
                     Json(body.get("tool_input")), body.get("result_summary"),
                     body.get("result"), body.get("result_tokens"), body.get("duration_ms"),
                     body.get("last_user_msg"), body.get("assistant_text")))
                rid = str(cur.fetchone()["id"])
            conn.commit()
        return rid

    return JSONResponse({"ok": True, "id": await run_in_threadpool(_do)})
