"""FastMCP app — the fixed script set as MCP tools.

The model fills in tool inputs; the server does the SQL, embedding, RRF, link parsing and
edit logging. Nine tools from the spec (lookup/fetch/snippet/rows/insert/update/delete/link/
group) plus `janitor` (server-side because it needs DB access, which only this server has).

Identity: `edited_by`/`actor` are NOT model-set. The plugin injects them out-of-band via
`X-Kovault-User` / `X-Kovault-Actor` HTTP headers (set by /setup); we fall back to env defaults.
Those headers are CLAIMED identity, not proven — anyone holding the shared token can send any
name. Per-user tokens (see token_identity) are what would make them proven.
"""
from __future__ import annotations

import logging
import os
import re
import secrets as _secrets
from contextlib import contextmanager

from fastmcp import FastMCP
from psycopg.types.json import Json
from starlette.concurrency import run_in_threadpool
from starlette.middleware import Middleware
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


# =======================================================================================
# auth (B2) — one gate in front of EVERY route
# =======================================================================================
# The whole surface is sensitive, not just the obvious parts: /mcp reads and writes the vault,
# GET /export streams the entire vault as a zip, POST /relocate-sources rewrites source paths,
# and /page-meta — which looks harmless — lists every page id and timestamp in the vault. So the
# gate goes in ONE place that everything passes through, rather than a decorator per route that
# the next route can forget to wear.
SHARED_TOKEN_USER = "kovault"     # the identity every shared-token caller resolves to


def token_identity(presented: str, tokens: list[str]) -> str | None:
    """Which user is this token? `None` = not a valid token.

    Every configured token is currently the one shared vault token, so any match resolves to the
    same identity and the caller's NAME still comes from the X-Kovault-User header (_identity).
    That header is a claim, not proof. Per-user tokens are what would make it proof, and this
    function is the single place that would change to deliver them: return the user the token
    belongs to instead of the shared constant, and _identity prefers that over the header.

    compare_digest, never `==`: a plain string compare returns early on the first differing byte,
    which leaks the token's content through response timing. Compared as bytes because
    compare_digest rejects non-ASCII str, and a secret file can contain anything."""
    p = presented.encode("utf-8")
    for known in tokens:
        if _secrets.compare_digest(p, known.encode("utf-8")):
            return SHARED_TOKEN_USER
    return None


def bearer_token(scope) -> str:
    """The token out of `Authorization: Bearer <token>`, or "" — a conventional scheme rather than
    a Kovault-specific one, so `claude mcp add --header` and every HTTP client already speak it."""
    for key, value in scope.get("headers") or []:
        if key == b"authorization":
            v = value.decode("latin-1").strip()
            return v[7:].strip() if v[:7].lower() == "bearer " else ""
    return ""


class BearerAuthMiddleware:
    """Raw ASGI, NOT starlette's BaseHTTPMiddleware: that one buffers the response body, which
    would break /mcp's streaming transport and /export's zip. This never touches the body — it
    either answers 401 itself or steps out of the way entirely."""

    def __init__(self, app, tokens: list[str]):
        self.app = app
        self.tokens = list(tokens)

    async def __call__(self, scope, receive, send):
        if scope.get("type") == "http" and token_identity(bearer_token(scope), self.tokens) is None:
            log.warning("401 %s %s — missing or invalid bearer token",
                        scope.get("method"), scope.get("path"))
            await JSONResponse(
                {"error": "unauthorized",
                 "detail": "Kovault needs `Authorization: Bearer <token>` on every request."},
                status_code=401, headers={"WWW-Authenticate": "Bearer"})(scope, receive, send)
            return
        await self.app(scope, receive, send)


def http_middleware(tokens: list[str]) -> list:
    """Starlette middleware for the whole ASGI app, hooked in at main.main's mcp.run(). Applied
    before routing, so it covers /mcp and every custom route with no per-route opt-in. An empty
    token list installs nothing: the server runs open, and main shouts about it."""
    return [Middleware(BearerAuthMiddleware, tokens=tokens)] if tokens else []

# ---- service singletons (configured by main.configure) --------------------------------
_DB: Database | None = None
_EMBED_CACHE: dict = {}
SEARCH_LIMIT = 50            # per-signal candidate cap before fusion
ROWS_LIMIT_CAP = 200        # hard cap for the rows backup tool
ROWS_OPS = {"=", "!=", ">", "<", ">=", "<=", "ilike", "in"}
SUBTYPE_KIND = {"sources": "source", "tasks": "task", "decisions": "decision", "pages": "page"}
_KIND_OF = {t: k for k, t in bl.TABLE.items()}   # pages -> page, headers -> header, ... (I1)
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


# ---- canonical person form (D3/D4) -----------------------------------------------------
# Person columns, arrays and the one scalar. Every write of one of these goes through
# _canon_people, so "canonical" is defined ONCE — three sites each encoding their own rule is
# what let Alice/alice split in the first place.
_PEOPLE_COLS = {"responsible", "participants", "contributors", "decided_by"}


def _canon_people(value):
    """Canonical person form: KEEP the case the client sent, drop duplicates that differ only in
    case — first spelling wins. `["Alice","alice","ALICE"]` -> `["Alice"]`; `Bob` stays
    `Bob`. Takes a scalar or a list and returns the same shape, so `_identity`, the write
    boundary and `/janitor -normalize-people` can all share this one definition.

    Storage therefore no longer guarantees a single casing, so anything that COMPARES or GROUPS
    people must fold case at the comparison (see _touch_contributors, _janitor_normalize_people)."""
    if value is None:
        return None
    if not isinstance(value, list):
        return str(value).strip()
    out, seen = [], set()
    for v in value:
        s = str(v or "").strip()
        if s and s.lower() not in seen:
            seen.add(s.lower())
            out.append(s)
    return out


def _canon_people_fields(fields: dict) -> dict:
    """Apply _canon_people to every person column of a write, in place (D4)."""
    for col in _PEOPLE_COLS.intersection(fields):
        fields[col] = _canon_people(fields[col])
    return fields


def _identity() -> tuple[str, str]:
    """(edited_by, actor). From plugin-set HTTP headers; else env defaults. The username gets the
    same canonical treatment as every other person value — trimmed, case preserved, so a user
    whose config says `Bob` is not silently renamed. Nothing compares edited_by (there is no
    `WHERE edited_by = …` in the tree), so preserving case splits nothing."""
    try:
        from fastmcp.server.dependencies import get_http_headers
        h = get_http_headers() or {}
        user = h.get("x-kovault-user")
        if user:
            return _canon_people(user), h.get("x-kovault-actor", "ai")
    except Exception:
        pass
    return (_canon_people(os.getenv("KOVAULT_DEFAULT_USER", "unknown")),
            os.getenv("KOVAULT_DEFAULT_ACTOR", "ai"))


def _live(alias: str = "") -> str:
    """The one live predicate (E3), identical on every content table: not trashed and not archived.
    Archived rows stay fetchable by id but drop out of ranked search."""
    p = f"{alias}." if alias else ""
    return f"{p}trashed_at IS NULL AND {p}lifecycle <> 'archived'"


# A write touching ONLY these columns is a state change, not a content edit (E3).
_STATE_COLS = {"lifecycle", "trashed_at"}


@contextmanager
def _state_only(cur, on: bool = True):
    """Mark the statements inside as a STATE-ONLY write (trash / revive / lifecycle / janitor).

    `kovault.state_only` tells the set_updated_at() trigger to leave updated_at alone (E3), so a
    state flip never makes a row look edited — and never marks it embed-stale via
    `embedded_at < updated_at`. Scoped to this block, not the whole transaction: a `write` batch
    may mix a trash block with a content edit, and the content edit must still bump. Read any
    rowcount/RETURNING rows INSIDE the block — the closing SET clears the cursor's result.

    No `finally`, deliberately: if the body raises, the caller unwinds to a SAVEPOINT taken before
    this block (`write`) or aborts the whole transaction (`janitor`), and Postgres reverts a
    SET LOCAL made after a savepoint when it rolls back to it. So the flag cannot survive a failed
    block and silently stop the NEXT block's content write from bumping updated_at. A `finally`
    would instead fire a statement on an already-poisoned transaction and mask the real error."""
    if on:
        cur.execute("SET LOCAL kovault.state_only = 'on'")
    yield
    if on:
        cur.execute("SET LOCAL kovault.state_only = 'off'")


def _cols(table: str, refresh: bool = False) -> dict[str, dict]:
    """Reflected columns: {name: {type, udt, max_len, is_array, is_generated}}. Keyed by name, so
    every `col in _cols(table)` membership check reads exactly as it did when this was a name set.

    `udt` is the concrete type name, and it is not redundant with `type`: data_type reports
    'USER-DEFINED' for a halfvec AND for every enum, so only udt_name separates an embedding
    column from `tasks.status` (A6).

    One query, one cache, three consumers: length validation (A4), array-aware filters (A5) and the
    server-managed deny list (I1) — built separately they would be three queries and three caches.
    max_len for an array comes from element_types: information_schema.columns reports NULL there,
    but varchar(64)[] still caps every element at 64."""
    # `refresh` re-reads a table whose shape may have changed since startup (see _columns_for).
    # The cached entry is replaced only after the new one is built: dropping it first would leave
    # the cache empty if the re-read fails, turning one failed lookup into a broken table.
    if refresh or table not in _COLS_CACHE:
        rows = db().query(
            "SELECT c.column_name AS name, c.data_type AS type, c.udt_name AS udt, "
            "       coalesce(c.character_maximum_length, e.character_maximum_length) AS max_len, "
            "       c.data_type = 'ARRAY' AS is_array, "
            "       c.is_generated <> 'NEVER' AS is_generated "
            "FROM information_schema.columns c "
            "LEFT JOIN information_schema.element_types e "
            "  ON (c.table_catalog, c.table_schema, c.table_name, 'TABLE', c.dtd_identifier) "
            "   = (e.object_catalog, e.object_schema, e.object_name, e.object_type, "
            "      e.collection_type_identifier) "
            "WHERE c.table_name = %s", (table,))
        _COLS_CACHE[table] = {r.pop("name"): r for r in rows}
    return _COLS_CACHE[table]


# Server-managed columns: stamped by a trigger, by the embed worker, or GENERATED by Postgres
# (the *_norm ones — writing those is a hard Postgres error, not a soft failure). The deny list is
# the write boundary's own guard, not a side effect of what FIELD_MAP happens to expose (I1).
_DENY_COLS = {"created_at", "updated_at", "embedded_at", "embedding", "summary_embedding"}

# pgvector types, matched on udt_name because data_type says 'USER-DEFINED' for enums too (A6).
_VECTOR_UDTS = {"halfvec", "vector", "sparsevec"}


def _check_columns(table: str, fields: dict, kind: str | None = None) -> list[str]:
    """EVERY write-boundary column problem on one row, all at once — a caller fixing three fields
    should need one round trip, not three. Two checks off the same reflection:
    A4: a value longer than the column allows (limits read from the live schema, so they cannot
        drift); an array is checked per element, since the cap is per element.
    I1: a server-managed or generated column named by hand.
    `kind` (when the row came from a template) reports the frontmatter key, not the DB column."""
    out: list[str] = []
    cols = _cols(table)
    for col, val in fields.items():
        meta = cols.get(col)
        if meta is None:
            continue                          # unknown columns are already reported by the parser
        key = bl.template_key(kind, col) if kind else col
        if col in _DENY_COLS or meta["is_generated"]:
            out.append(f"{key} is set by the server and cannot be written")
            continue
        if val is None or not meta["max_len"]:
            continue
        for v in (val if isinstance(val, list) else [val]):
            n = len(str(v))
            if n > meta["max_len"]:
                out.append(f"{key} is {n} chars, limit is {meta['max_len']}")
    return out


# ---- enum validation at the write boundary (F5) ---------------------------------------
# enum columns per table (pages.type is free OKF text, so not listed). Values are checked
# against pg_enum; a known alias is auto-normalized and reported; anything else is a clear error.
_ENUM_COLS = {
    "tasks": {"status": "task_status", "priority": "task_priority", "lifecycle": "lifecycle_kind"},
    "pages": {"lifecycle": "lifecycle_kind"},
    "headers": {"lifecycle": "lifecycle_kind"},
    "decisions": {"lifecycle": "lifecycle_kind"},
    "sources": {"type": "source_type", "lifecycle": "lifecycle_kind"},
    "groups": {"type": "group_types", "status": "group_status", "lifecycle": "lifecycle_kind"},
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
    cur.execute(f"SELECT 1 FROM {bl.TABLE[kind]} WHERE id=%s AND trashed_at IS NULL", (tid,))
    return cur.fetchone() is not None


def _resolve_title(cur, title: str) -> tuple[str, str] | None:
    """Resolve an Obsidian [[Title]] to a single live entity (kind, id) by exact
    case-insensitive title. Search order page > header > task > decision > source; returns
    None if no table has exactly one live match (missing or ambiguous stays plain text)."""
    for kind in ("page", "header", "task", "decision", "source"):
        # exact case-insensitive match (NOT ILIKE: a %/_/trailing-\ in a real title would
        # otherwise be treated as a LIKE pattern and mis-resolve or error)
        cur.execute(f"SELECT id FROM {bl.TABLE[kind]} WHERE lower(title) = lower(%s) "
                    f"AND trashed_at IS NULL LIMIT 2", (title,))
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


_UNSET = object()   # "field absent" sentinel — distinct from '' (clear) and a value (set)


def _sync_junction(cur, table: str, fixed_col: str, fixed_id: str, other_col: str,
                   new_ids=None, exists_table: str | None = None,
                   add_ids=None, remove_ids=None) -> list[str]:
    """Reconcile a two-column junction (group_links / task_dependencies / header_sources). Two
    mutually exclusive modes (callers pass one, never both):
      - full roster (`new_ids`): reconcile to exactly this set — add the missing, delete the gone.
      - delta (`add_ids`/`remove_ids`, D1): touch only the named ids; every other id already on the
        roster is left alone (adding one member to an 88-member group is one id, not all 88).
    `exists_table` (when given) is checked before an insert so a stale id is skipped with a
    warning, not an FK error — same for a full roster or a delta add. A delta `remove_ids` id that
    is not currently on the roster is a no-op + warning, not an error: removal must be idempotent,
    so resending the same remove after it already landed cannot fail. Table/column names are
    internal constants (never user input), so interpolating them is safe. Returns warnings."""
    cur.execute(f"SELECT {other_col} FROM {table} WHERE {fixed_col}=%s", (fixed_id,))
    old = {str(r[other_col]) for r in cur.fetchall()}
    warns: list[str] = []
    if add_ids is not None or remove_ids is not None:
        to_add = {str(x) for x in (add_ids or [])} - old
        want_remove = {str(x) for x in (remove_ids or [])}
        for oid in want_remove - old:
            warns.append(f"skipped {table} remove: {other_col} {oid} not in roster")
        to_remove = want_remove & old
    else:
        new = {str(x) for x in (new_ids or [])}
        to_add = new - old
        to_remove = old - new
    for oid in to_add:
        if exists_table:
            cur.execute(f"SELECT 1 FROM {exists_table} WHERE id=%s", (oid,))
            if not cur.fetchone():
                warns.append(f"skipped {table}: {other_col} {oid} not found")
                continue
        cur.execute(f"INSERT INTO {table} ({fixed_col},{other_col}) VALUES (%s,%s) "
                    "ON CONFLICT DO NOTHING", (fixed_id, oid))
    for oid in to_remove:
        cur.execute(f"DELETE FROM {table} WHERE {fixed_col}=%s AND {other_col}=%s", (fixed_id, oid))
    return warns


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
            WHERE {_live('h')} AND h.embedding IS NOT NULL AND {_live('p')}
            ORDER BY h.embedding <=> %(q)s::halfvec
            LIMIT %(n)s
        """
    else:
        sql = f"""
            SELECT id, NULL::uuid AS page_id, title, {meta['disp']} AS disp,
                   1 - ({meta['emb']} <=> %(q)s::halfvec) AS score
            FROM {table}
            WHERE {_live()} AND {meta['emb']} IS NOT NULL
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
        WHERE {_live()} AND ({inc_pred}){not_clause}
        ORDER BY score DESC
        LIMIT %(n)s
    """
    if table == "headers":
        sql = f"""
            SELECT s.id, h.page_id, h.title, h.blurb AS disp, s.score
            FROM ({sub}) s
            JOIN headers h ON h.id = s.id
            JOIN pages p ON p.id = h.page_id
            WHERE {_live('p')}
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
            WHERE {_live('h')} AND ({where}) AND {_live('p')}
            ORDER BY score DESC LIMIT %(n)s
        """
    else:
        sql = f"""
            SELECT id, NULL::uuid AS page_id, title, {meta['disp']} AS disp, {sim} AS score
            FROM {table}
            WHERE {_live()} AND ({where})
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


def _group_entity_sets(names_or_ids: list[str]) -> tuple[set[str], str | None]:
    """Resolve group names/ids -> set of member entity ids. An id (full, or a short F3/C1 prefix)
    is resolved through the batch resolver; anything else is a name (ILIKE). Returns
    (entity_ids, error): an ambiguous id prefix is never guessed, it aborts naming itself."""
    if not names_or_ids:
        return set(), None
    ids, names = [], []
    for x in names_or_ids:
        (ids if _looks_id_like(x) else names).append(x)
    resolved, _missing, amb_err = _full_ids("groups", ids) if ids else ({}, [], None)
    if amb_err:
        return set(), amb_err
    clauses, params = [], []
    if resolved:
        clauses.append("id = ANY(%s)")
        params.append(list(resolved.values()))
    if names:
        clauses.append("name ILIKE ANY(%s)")
        params.append([f"%{n}%" for n in names])
    if not clauses:
        return set(), None
    gids = [str(r["id"]) for r in db().query(
        f"SELECT id FROM groups WHERE {' OR '.join(clauses)}", params)]
    if not gids:
        return set(), None
    return {str(r["entity_id"]) for r in db().query(
        "SELECT entity_id FROM group_links WHERE group_id = ANY(%s)", (gids,))}, None


def _looks_uuid(x: str) -> bool:
    """A full 36-char uuid — used where a caller-supplied value goes straight into a uuid-typed
    `= ANY()` comparison (page_meta) and MUST NOT be a short prefix, or that cast crashes."""
    return bool(re.fullmatch(r"[0-9a-fA-F-]{36}", str(x)))


def _looks_id_like(x: str) -> bool:
    """Loose id classifier for _group_entity_sets's names-or-ids split: a full uuid OR a short
    (F3/C1) hex prefix. Kept separate from _looks_uuid (which page_meta relies on to mean
    'definitely full, safe straight into id = ANY()') — broadening that one would let a short id
    slip in there and crash the same uuid cast this whole feature exists to stop."""
    return bool(re.fullmatch(r"[0-9a-fA-F-]{1,36}", str(x)))


def _filter_clause(table: str, col: str, op: str, val) -> tuple[str, object]:
    """One (clause, param) for a {column, op, value} filter — shared by precise `lookup` and `rows`,
    which had near-identical loops, which is why an array-column filter silently returned nothing in
    BOTH (A5). An array column (tasks.responsible, groups.participants, pages.contributors,
    janitor_reports.flags) needs containment, not comparison: `col = %s` against a text[] matches
    nothing and `col ILIKE %s` is a type error.
    ponytail: only `=` and `ilike` are array-aware; other ops fall through to the scalar form and
    Postgres reports the mismatch. Add `!=`/`in` containment when something actually asks for it
    (`!=` also has to decide whether an empty array counts as "not alice")."""
    if op == "in":
        return f"{col} = ANY(%s)", (val if isinstance(val, list) else [val])
    if (_cols(table).get(col) or {}).get("is_array"):
        if op == "=":
            return f"%s = ANY({col})", val
        if op == "ilike":
            return f"EXISTS (SELECT 1 FROM unnest({col}) x WHERE x ILIKE %s)", val
    if op == "ilike":
        return f"{col} ILIKE %s", val
    return f"{col} {op} %s", val


_PRECISE_TABLES = ("pages", "headers", "tasks", "decisions", "sources", "groups")
_PRECISE_DISP = {"headers": "blurb", "tasks": "description", "decisions": "description",
                 "sources": "summary", "pages": "summary", "groups": "description"}


# ---- column modes (I1) ------------------------------------------------------------------
# Columns a block cannot lose and still parse back: its id, and — for a PAGE, whose `type:` is its
# own free OKF value — the type column. Every other kind's `type:` marker is written by the
# renderer, not read from a column, so it survives any selection on its own.
def _always_cols(kind: str) -> set[str]:
    return {"id", "type"} if kind == "page" else {"id"}


def _columns_for(table: str, kind: str, build, columns: list[str] | None,
                 always: set[str] = frozenset({"id"})) -> tuple[list[str], str | None]:
    """`_resolve_columns` against the reflection, re-reflecting ONCE if a name comes back unknown.

    `_COLS_CACHE` lives as long as the process, so a column added while the server is running —
    an extension running its own migration, which is the whole case I1 exists for (R31) — would
    otherwise stay invisible until a restart, even though `fetch` already renders it (the renderer
    reads `SELECT *`, not the cache). The retry costs one query and only on a name we do not
    recognise, so a caller typing garbage repeatedly pays a query each time and nothing else."""
    keep, err = _resolve_columns(kind, *build(_cols(table)), columns, always)
    if err and err.startswith("unknown column"):
        try:
            return _resolve_columns(kind, *build(_cols(table, refresh=True)), columns, always)
        except Exception as e:                       # noqa: BLE001 — see below
            # Re-reflection is a best-effort second opinion. If it fails, the caller still gets
            # the precise "unknown column 'x'" they were always going to get; turning that into
            # a raised exception would be a worse answer than the one we already have.
            log.warning("column reflection refresh failed for %s: %s", table, e)
    return keep, err


def _resolve_columns(kind: str, available, default: list[str], columns: list[str] | None,
                     always: set[str] = frozenset({"id"})) -> tuple[list[str], str | None]:
    """Turn the `columns` parameter into the ordered list of columns to show (I1). Three modes:
    absent -> `default` unchanged; every entry signed (`+a`, `-b`) -> default plus/minus; every
    entry bare (`a`, `b`) -> exactly those. Mixing signed and bare entries is an ERROR, not a
    guess — same call as D1's rosters, and for the same reason.

    Names resolve through render.column_of, so `description` and `summary` both reach pages.summary
    and a caller can use whichever they read in the output. Every rejection is reported, never a
    silent drop: an unknown column, a machine column (render.hidden — the RENDER-hide set, which is
    NOT phase 2's write deny list: created_at is write-denied yet sensible to show, an embedding is
    writable-ish yet must never be printed), or an attempt to drop a column the block needs."""
    if not columns:
        return list(default), None
    entries = [str(c).strip() for c in columns if str(c).strip()]
    signed = [c for c in entries if c[0] in ("+", "-")]
    if signed and len(signed) != len(entries):
        return [], ("mixed column syntax — use either signed entries (+a,-b) to adjust the "
                    "default or a bare list (a,b,c) to replace it, not both")
    keep = list(default) if signed else []
    for entry in entries:
        drop = entry[0] == "-"
        col = rnd.column_of(kind, entry[1:].strip() if signed else entry)
        if col not in available:
            return [], f"unknown column '{col}' on {kind}"
        if rnd.hidden(col):
            return [], (f"column '{col}' is never rendered — it is embedding or generated data, "
                        f"not content")
        if drop:
            if col in always:
                return [], f"column '{col}' cannot be dropped: the block would not parse back"
            keep = [c for c in keep if c != col]
        elif col not in keep:
            keep.append(col)
    return keep + [c for c in always if c in available and c not in keep], None


def _pick(row: dict, keep: set | None) -> dict:
    """Apply a resolved column mode to a row. The renderer is driven by `row.keys()`, so a filtered
    row IS a column mode — no parallel mechanism to teach it, and render.py stays a pure module
    that export.py can go on using. `None` means "whole row", i.e. exactly today's output."""
    return row if keep is None else {k: v for k, v in row.items() if k in keep}


# Junction rosters a fetch renders per kind. They are rendered KEYS rather than columns, but a
# caller reading the output cannot tell the difference, so a column mode names them the same way:
# a bare list drops the ones it does not name ("exactly those"), the signed form keeps them.
_ROSTER_KEYS = {"task": ("blockers", "blocking", "related"), "decision": ("related",),
                "source": ("referenced by",), "group": ("members",)}


def _keep_roster(name: str, value, keep: set | None):
    """None makes the renderer omit that line entirely (render._roster), which is the difference
    between "this roster is empty" and "you did not ask for this roster"."""
    return value if keep is None or name in keep else None


def _norm_table(t: str | None) -> str | None:
    """Strip wrapping quotes/whitespace from a table-name argument (A7). History: an MCP client
    once double-JSON-encoded a scalar argument, so the server received the literal characters
    '"tasks"' — quotes included — instead of tasks. A ranked/keyword read shrugged that off (no
    match, empty result) while a write failed outright, so the symptom read as "reads work, writes
    fail". One shared strip, used at every surface that takes a raw table name (precise-mode
    `tables`, `snippet` requests[].table, `rows.table`) so the fix cannot quietly go missing from
    just one of them again. Does NOT alias singular->plural ('page'->'pages'); an argument that is
    still wrong after stripping should fail loud, not get silently guessed at."""
    if t is None:
        return t
    t = t.strip()
    if len(t) >= 2 and t[0] == t[-1] and t[0] in "\"'":
        t = t[1:-1].strip()
    return t


def _precise_lookup(tables, filters, count, limit, offset, columns=None) -> str:
    """Deterministic exact-filter query (F3) — the first-class replacement for reaching to `rows`/`sql`
    for audits. Filters/paginates ONE table with an op whitelist; returns hits:N and a compact list."""
    table = _norm_table((tables or ["tasks"])[0])
    if table not in _PRECISE_TABLES:
        return f"(precise: table {table!r} not recognized; must be one of {', '.join(_PRECISE_TABLES)})"
    cols = _cols(table)
    clauses, params = [], []
    if "trashed_at" in cols:       # every content table carries it (E3); archived stays visible
        clauses.append("trashed_at IS NULL")   #   here — precise mode is the audit path
    for f in filters or []:
        col, op, val = f.get("column"), (f.get("op") or "=").lower(), f.get("value")
        if col not in cols:
            return f"(precise: unknown column {col} on {table})"
        if op not in ROWS_OPS:
            return f"(precise: op {op} not allowed; use {', '.join(sorted(ROWS_OPS))})"
        clause, param = _filter_clause(table, col, op, val)
        clauses.append(clause)
        params.append(param)
    where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
    total = int(db().query_one(f"SELECT count(*) n FROM {table}{where}", params)["n"])
    if count:
        return f"PRECISE {table}\nhits: {total}"
    lim = max(1, min(int(limit or 50), ROWS_LIMIT_CAP))
    off = max(0, int(offset or 0))
    label = "name" if table == "groups" else "title"
    order = "created_at DESC" if "created_at" in cols else "id"
    # `status` is what an audit of groups (E2) or tasks is actually asking about, and the
    # reflection already says which tables have one — no per-table list to keep in step.
    stat = ", status" if "status" in cols else ""
    if columns:      # I1: same parameter, same syntax, same refusals as fetch — the columns
        kind = _KIND_OF[table]
        default = [label, _PRECISE_DISP[table]] + (["status"] if stat else []) + ["id"]
        sel, err = _columns_for(table, kind, lambda c: (c, default), columns)
        if err:
            return f"(precise columns: {err})"
        # Safe to interpolate: _resolve_columns rejects any name not in the reflected column set,
        # so nothing reaches the SELECT that Postgres did not report as a column of this table.
        rows_ = db().query(
            f"SELECT {', '.join(sel)} FROM {table}{where} ORDER BY {order} LIMIT %s OFFSET %s",
            params + [lim, off])
        out = [f"PRECISE {table}", f"hits: {total} (showing {len(rows_)} from offset {off})",
               " | ".join(sel)]
        for r in rows_:
            out.append(" | ".join(_cell(c, r[c]) for c in sel))
        return "\n".join(out)
    rows_ = db().query(
        f"SELECT id, {label} AS label, {_PRECISE_DISP[table]} AS disp{stat} FROM {table}{where} "
        f"ORDER BY {order} LIMIT %s OFFSET %s", params + [lim, off])
    out = [f"PRECISE {table}", f"hits: {total} (showing {len(rows_)} from offset {off})",
           "label | summary" + (" | status" if stat else "") + " | id"]   # title first, id last (C1)
    for r in rows_:
        line = f"{r['label'] or ''} | {_clip(r['disp'])}"
        if stat:
            line += f" | {r['status'] or ''}"
        out.append(f"{line} | {_short_id(r['id'])}")
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
    columns: list[str] | None = None,
) -> str:
    """Hybrid search over Kovault. Returns a ranked CHUNKS index and (when headers are
    searched) a PAGES index to fetch from.

    tables: searchable tables to hit — any of headers/tasks/decisions/sources (default headers;
        `filters` mode below defaults to tasks instead if `tables` is omitted).
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
    columns: PRECISE mode only — which columns the listing shows, same syntax as `fetch`.
        `["+planned_start","-summary"]` adjusts the default (every entry signed) and reaches a
        column an extension added to the table; `["title","status"]` returns exactly those (every
        entry bare). Do not mix the two forms. The ranked CHUNKS/PAGES index has a fixed shape.
    """
    if filters is not None:
        return _precise_lookup((tables or ["tasks"]), filters, count, limit, offset, columns)
    tables = [t for t in (tables or ["headers"]) if t in _SEARCH]
    include = list(include or [])
    exclude = list(exclude or [])
    if query:
        q_inc, q_exc = se.parse_search_input(query)   # plain "Search for: X, Exclude: Y" (F5)
        include += [t for t in q_inc if t not in include]
        exclude += [t for t in q_exc if t not in exclude]

    if outline_page:
        rows = db().query(
            "SELECT id, index, title, blurb FROM headers "
            "WHERE page_id=%s AND trashed_at IS NULL ORDER BY index",
            (outline_page,),
        )
        lines = ["PAGE OUTLINE", "index | id | title | blurb"]
        for r in rows:
            lines.append(f"{r['index']} | {_short_id(r['id'])} | {r['title'] or '(intro)'} | {r['blurb'] or ''}")
        return "\n".join(lines) if rows else "PAGE OUTLINE\n(no live chunks)"

    if not tables:
        return "CHUNKS\n(no searchable tables selected)"

    inc_bm = _bm25_terms(include)
    exc_bm = _bm25_terms(exclude)
    qvec = _embedder().embed(" ".join(include)) if include else None
    qnorm = se.normalize_term(" ".join(include)) if include else ""   # F2 trigram query form
    graph = _graph_points(include, exclude)

    inc_groups, err = _group_entity_sets(groups_include or [])
    if err:
        return err
    exc_groups, err = _group_entity_sets(groups_exclude or [])
    if err:
        return err

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
    out = ["CHUNKS", f"title | kind | blurb/summary{sig} | rrf | id"]   # title first, id last (C1)
    for cid, score in kept:
        c = cand[cid]
        cols = [c["title"] or "(intro)", c["kind"], _clip(c["disp"])]
        if scores:
            cols += [_fmt(c["vector"]), _fmt(c["keyword"]), str(c["graph"]), _fmt(c.get("trigram"))]
        cols.append(_fmt(score))
        cols.append(_short_id(c["id"]))
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
        "SELECT id, title, summary FROM pages WHERE id = ANY(%s)",
        ([pid for pid, _ in kept],))} if kept else {}
    sig = " | vec | kw | graph" if scores else ""
    lines = ["", "PAGES", f"title | summary{sig} | rrf | top chunk | id"]   # title first, id last (C1)
    for pid, score in kept:
        m = meta.get(pid, {})
        cols = [m.get("title", ""), _clip(m.get("summary"))]
        if scores:
            cols += [_fmt(v.get(pid)), _fmt(kk.get(pid)), _fmt(g.get(pid))]
        cols.append(_fmt(score))
        tc = top_by_page.get(pid)
        snippet = f"{tc['title']} — {tc['disp']}" if tc and tc.get("title") else (tc.get("disp") if tc else "")
        cols.append(_clip(snippet))
        cols.append(_short_id(pid))
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


def _full_ids(table: str, ids: list[str]) -> tuple[dict[str, str], list[str], str | None]:
    """Batch version of _full_id (F3/C1): resolves every SHORT prefix in `ids` in ONE query — a
    full 36-char id needs no lookup, so those are filtered out first and pass straight through.
    Written for the junction rosters (a 142-member roster would otherwise be 142 round trips).
    Returns (resolved, missing, ambiguous_error):
      resolved: {given -> full id}, for a full id and for every short prefix matching exactly one row.
      missing: given ids that matched zero rows — the caller decides what a stale id means for it.
      ambiguous_error: set the instant ANY given id matches 2+ rows — always fatal, never a guess,
        same wording as _full_id, naming the offending prefix."""
    seen = list(dict.fromkeys(str(x) for x in (ids or []) if x))    # de-dup, keep order
    resolved = {i: i for i in seen if len(i) >= 36}
    short = [i for i in seen if len(i) < 36]
    if not short:
        return resolved, [], None
    rows = db().query(
        f"SELECT id::text AS id FROM {table} WHERE " + " OR ".join(["id::text LIKE %s"] * len(short)),
        [s + "%" for s in short])
    all_ids = [r["id"] for r in rows]
    missing: list[str] = []
    for s in short:
        matches = [i for i in all_ids if i.startswith(s)]
        if len(matches) > 1:
            return {}, [], f"(ambiguous {table[:-1]} id prefix '{s}': {len(matches)} matches)"
        if matches:
            resolved[s] = matches[0]
        else:
            missing.append(s)
    return resolved, missing, None


def _resolve_roster_ids(table: str | None, ids) -> tuple[list[str], list[str]]:
    """Short-id resolution for a junction roster (D1's blockers/members/sources, full or delta),
    via the batch resolver. A prefix matching zero rows is dropped with a warning — the same 'stale
    id, skip it' treatment a nonexistent full id already gets from _sync_junction's exists_table
    check. A prefix matching 2+ rows is never a guess: raises, which write()'s per-block savepoint
    turns into that block's own (error: ...), same as any other dispatch failure. Returns
    (resolved ids, warnings); order preserved, duplicates dropped."""
    ids = list(dict.fromkeys(str(x) for x in (ids or []) if x))
    if not ids or not table:
        return ids, []
    resolved, missing, amb_err = _full_ids(table, ids)
    if amb_err:
        raise ValueError(amb_err)
    warns = [f"skipped {table}: {m} not found" for m in missing]
    return [resolved[i] for i in ids if i in resolved], warns


def _cell(col: str, v) -> str:
    """One precise-mode table cell under a column mode (I1): ids short (C1), arrays joined, long
    text clipped — a 50-row listing must not become the expensive part of the answer."""
    if v is None:
        return ""
    if col == "id" or col.endswith("_id"):
        return _short_id(v)
    return _clip(", ".join(str(x) for x in v) if isinstance(v, (list, tuple)) else str(v))


def _short_id(v) -> str:
    """8-char display id for a read-path index/roster line (C1) — CHUNKS/PAGES/PAGE OUTLINE/
    precise mode/blockers/blocking/group members. Never used in render.py or export.py: those
    build the entity template's own `id:` line and every export artifact, which must stay a full,
    restorable 36-char id."""
    return str(v)[:8]


@mcp.tool
def fetch(
    pages: list[str] | None = None,
    headers: list[str] | None = None,
    tasks: list[str] | None = None,
    decisions: list[str] | None = None,
    sources: list[str] | None = None,
    groups: list[str] | None = None,
    outline: bool = False,
    columns: list[str] | None = None,
    members: str | None = None,
) -> str:
    """Render full entities by id: whole pages, single chunks (headers), tasks, decisions,
    sources, or groups. Every id list (`pages`, `headers`, etc.) may hold a short unique prefix
    instead of the full uuid. Fetch a page/chunk before editing it. Explicit ids can reach trashed
    rows (recovery/history).

    outline: for `pages`, return a cheap chunk index (index | id | title | blurb) instead of the
    whole page — pick the one chunk you need, and get its id for a `write` (a full page fetch has no chunk ids).
    columns: which fields to render. Omit for the full block. `["+planned_start","-blurb"]` adjusts
        it — every entry signed — and pulls in a column an extension added to the table; `["title",
        "status"]` returns exactly those — every entry bare. Do not mix the two forms. Name a field
        by the key you saw (`description`) or by its column (`summary`). `id` always survives, and
        a field that is never rendered (embedding / generated data) is refused, not ignored.
    members: for `groups` only (C2) — forces the member roster's shape instead of the size
        threshold deciding it. 'full' = labels, 'ids' = ids only, 'count' = just the number, no
        roster at all (cheapest — nothing to page through for a caller who only wants the size).
        Omit to keep today's behaviour (labels under 25 members, ids-only above). `columns=
        ["-members"]` drops the roster entirely first; combined with that, `members` has nothing
        left to act on and is a no-op."""
    if members not in (None, "full", "ids", "count"):
        return f"(fetch: members must be 'full', 'ids' or 'count' — got {members!r})"
    parts: list[str] = []
    # Resolved ONCE per kind — the answer is the same for every row of a table — and only when the
    # caller asked, so the default path is untouched (I1).
    keep: dict[str, set | None] = {}
    for kind, ids in (("page", pages), ("header", headers), ("task", tasks),
                      ("decision", decisions), ("source", sources), ("group", groups)):
        if not ids or not columns:
            continue
        rosters = _ROSTER_KEYS.get(kind, ())
        sel, err = _columns_for(
            bl.TABLE[kind], kind,
            lambda avail: (set(avail) | set(rosters),
                           [c for c in avail if not rnd.hidden(c)] + list(rosters)),
            columns, _always_cols(kind))
        if err:
            return f"(fetch columns: {err})"
        keep[kind] = set(sel)
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
            lines = [f"PAGE OUTLINE {page.get('title') or ''} ({_short_id(pid)})", "index | id | title | blurb"]
            lines += [f"{r['index']} | {_short_id(r['id'])} | {r['title'] or '(intro)'} | {r['blurb'] or ''}" for r in hs]
            parts.append("\n".join(lines))
            continue
        hs = db().query(
            "SELECT * FROM headers WHERE page_id=%s AND trashed_at IS NULL ORDER BY index", (pid,))
        parts.append(rnd.render_page(_pick(page, keep.get("page")), hs))   # inline body links navigate
    for hid in headers or []:
        hid, err = _full_id("headers", hid)
        if err:
            parts.append(err)
            continue
        h = db().query_one("SELECT * FROM headers WHERE id=%s", (hid,))
        parts.append(rnd.render_chunk(_pick(h, keep.get("header"))) if h else f"(chunk {hid} not found)")
    for tid in tasks or []:
        tid, err = _full_id("tasks", tid)
        if err:
            parts.append(err)
            continue
        t = db().query_one("SELECT * FROM tasks WHERE id=%s", (tid,))
        if not t:
            parts.append(f"(task {tid} not found)")
            continue
        blockers = [f"{_short_id(r['id'])} — {r['title']}" for r in db().query(
            "SELECT d.blocker AS id, t.title FROM task_dependencies d JOIN tasks t ON t.id=d.blocker "
            "WHERE d.dependent=%s", (tid,))]
        blocking = [f"{_short_id(r['id'])} — {r['title']}" for r in db().query(   # reverse edge (I4)
            "SELECT d.dependent AS id, t.title FROM task_dependencies d JOIN tasks t ON t.id=d.dependent "
            "WHERE d.blocker=%s", (tid,))]
        k = keep.get("task")
        parts.append(rnd.render_task(
            _pick(t, k), _keep_roster("blockers", blockers, k),
            _keep_roster("related", _links_of("task", tid), k),
            _keep_roster("blocking", blocking, k)))
    for did in decisions or []:
        did, err = _full_id("decisions", did)
        if err:
            parts.append(err)
            continue
        d = db().query_one("SELECT * FROM decisions WHERE id=%s", (did,))
        parts.append(rnd.render_decision(
            _pick(d, keep.get("decision")),
            _keep_roster("related", _links_of("decision", did), keep.get("decision")))
            if d else f"(decision {did} not found)")
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
        parts.append(rnd.render_source(_pick(s, keep.get("source")),
                                      _keep_roster("referenced by", ref_by, keep.get("source"))))
    for gid in groups or []:
        gid, err = _full_id("groups", gid)
        if err:
            parts.append(err)
            continue
        g = db().query_one("SELECT * FROM groups WHERE id=%s", (gid,))
        if not g:
            parts.append(f"(group {gid} not found)")
            continue
        roster = _group_members(gid)
        mem = _keep_roster("members", roster, keep.get("group"))   # None if columns dropped it (I1)
        if mem is None or members == "count":
            # count-only (C2), or columns=["-members"] already dropped the roster: render_group
            # gets members=None so it never emits a `members:` line at all. The key must be ABSENT,
            # not present-and-empty — an empty `members:` line is what write reads as "clear every
            # member" (blocks._JUNCTION_KEYS/D1), which a count-only fetch must never trigger on a
            # round-trip. The count itself goes OUTSIDE the frontmatter fence, in the same spot the
            # ids-only note below already uses: blocks.parse_block discards anything after a
            # non-header block's closing fence, so there is nothing there to parse or warn about.
            rendered = rnd.render_group(_pick(g, keep.get("group")), None)
            if mem is not None:      # columns didn't drop it — the count is still wanted
                rendered += f"\n({len(roster)} members — members='full' or 'ids' lists them)\n"
            parts.append(rendered)
            continue
        # 'ids' forces ids-only below the threshold too; 'full' forces labels above it; absent
        # keeps the existing size-threshold default byte-identical (decision 5).
        ids_only = members == "ids" or (members != "full" and len(roster) > _GROUP_IDS_ONLY_MAX)
        rendered = rnd.render_group(_pick(g, keep.get("group")), mem, ids_only=ids_only)
        if ids_only:
            rendered += (f"\n({len(roster)} members, ids only — fetch each id, or use "
                         f"`snippet` for labels)\n")
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
    return [(r["kind"], _short_id(r["id"]), r["label"] or "") for r in rows]   # short id (C1); fetch-only, not export


@mcp.tool
def snippet(requests: list[dict]) -> str:
    """Pull id/title/summary(or blurb) for ids or titles — to expand Related: links
    or header/source/task references without a full fetch. requests: [{table, ids?, titles?}] —
    `ids` and `titles` are LISTS, even for one item (`ids: ["abc123"]`, never `id: "abc123"`); a
    singular key is silently ignored and the request returns no match. ids may be a short (F3/C1)
    prefix, same as fetch. Titles are not unique; a title match returns every hit."""
    disp = {"headers": "blurb", "tasks": "description", "decisions": "description",
            "sources": "summary", "pages": "summary", "groups": "description"}
    out: list[str] = []
    for req in requests or []:
        table = _norm_table(req.get("table"))
        if table not in disp:
            out.append(f"(unknown table {table})")
            continue
        ids = req.get("ids") or []
        titles = req.get("titles") or []
        if ids:
            # batch-resolve through the same id::text LIKE path _full_id uses — never a raw
            # uuid comparison, so a malformed/short id can't crash this on an invalid-uuid cast
            # (the bug this fixes); an ambiguous prefix still errors, named, per request.
            resolved, _missing, amb_err = _full_ids(table, ids)
            if amb_err:
                out.append(amb_err)
                continue
            ids = list(resolved.values())
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
            out.append("")
    return "\n".join(out).rstrip() if out else "(no snippet matches)"


@mcp.tool
def rows(table: str, where: list[dict] | None = None, limit: int = 50) -> str:
    """Backup path: raw read of ANY table (incl. edits / janitor_reports). where: a LIST of
    {column, op, value} filters (op whitelist = != > < >= <= ilike in), hard limit cap. Never
    returns embedding/vector columns (too large to print). Every call is logged so future tool
    upgrades can learn where the main tools fell short. For exact filters/counts on the main
    entities prefer `lookup(filters=[...], count=...)` (precise mode); use `rows` only for tables
    lookup can't reach (edits / janitor_reports / settings / debug_log). Never write SQL."""
    table = _norm_table(table)
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
        clause, param = _filter_clause(table, col, op, val)
        clauses.append(clause)
        params.append(param)
    # A6: never SELECT * here. One embedded chunk serialises its halfvec(4000) to ~13k tokens, and
    # the default limit is 50 — a single `rows` call could return ~650k tokens and blow up the
    # session. Driven by the reflection's udt, so a new vector column is excluded automatically.
    shown = [c for c, m in cols.items() if m.get("udt") not in _VECTOR_UDTS]
    sql = f"SELECT {', '.join(shown)} FROM {table}"
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
    with retrieval the fixed tools don't express — so we can compare your queries to them. Off
    unless the SERVER's `debug` setting is on, and every call is logged to debug_log. Writes and
    DDL are refused, the query runs in a READ ONLY transaction, and the result is capped."""
    # The gate is server-side (B2). It used to be the plugin's PreToolUse hook, which a non-plugin
    # client simply does not run: read-only-ness was enforced here, but whether the tool could be
    # called at all was decided by the caller. A gate the caller controls is not a gate.
    # Fail closed: ONLY the JSON boolean `true` opens this. Not truthiness — every other setting
    # in this table is an object, so an admin reaching for the familiar shape and writing
    # {"enabled": false} would, under a truthiness test, turn raw SQL access ON while believing
    # they had turned it off. A security gate must never read a disabled value as enabled.
    if db().settings().get("debug") is not True:
        return ("(sql is off: it is a debug tool. An admin turns it on server-side with "
                "UPDATE settings SET value='true' WHERE key='debug';  — the value must be the "
                "bare boolean true, not an object)")
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


# E6: probe decisions + headers (not pages — pages has no title_norm/trgm index, and a page's
# actual content lives in its chunks, so a header hit is the more useful signal anyway; reported
# as the page it belongs to). Same trigram mechanism as _similar_task_warn, two tables via UNION ALL.
_TASK_HINT_SQL = (
    f"SELECT 'decision' AS kind, id, title, "
    f"       round(similarity(title_norm, {_NORM_TITLE})::numeric, 2) AS sim "
    f"FROM decisions WHERE trashed_at IS NULL AND title_norm %% {_NORM_TITLE} "
    f"UNION ALL "
    f"SELECT 'page' AS kind, h.page_id AS id, p.title AS title, "
    f"       round(similarity(h.title_norm, {_NORM_TITLE})::numeric, 2) AS sim "
    f"FROM headers h JOIN pages p ON p.id = h.page_id "
    f"WHERE h.trashed_at IS NULL AND p.trashed_at IS NULL AND h.title_norm %% {_NORM_TITLE} "
    f"ORDER BY sim DESC LIMIT 1"
)

# E7: "looks planned" = any of these three is set — someone who dated, sized or prioritized the
# work has planned it; a bare-title inbox capture has not, and warning there would be noise.
_PLANNED_COLS = ("deadline", "scope", "priority")


def _task_gap_warns(cur, fields: dict, default_owner: str = "") -> list[str]:
    """E6+E7, one warning pass alongside _similar_task_warn's dedupe hint — cheap, cursor-based,
    trigram-only (no embedding/LLM call), warns and never blocks.
    E6: the description carries no reference — no [text](kind:uuid), no [[wikilink]] — so
    trigram-probe the task's own title for a plausible decision/page to point at instead of
    leaving the task floating with no context.
    E7: the task looks planned (_PLANNED_COLS) but no `responsible` was named."""
    warns: list[str] = []
    title, desc = fields.get("title"), fields.get("description")
    if title and title.strip() and not parse_links(desc) and not parse_obsidian_links(desc):
        cur.execute(_TASK_HINT_SQL, (title, title, title, title))
        hits = cur.fetchall()      # fetchall, like _similar_task_warn — not fetchone (LIMIT 1 already caps it)
        if hits:
            hit = hits[0]
            warns.append(f'no link/wikilink in description — maybe related to {hit["kind"]} '
                        f'"{hit["title"]}" ({hit["id"]}, sim {hit["sim"]})?')
    if not fields.get("responsible") and any(fields.get(c) for c in _PLANNED_COLS):
        # Name the default owner the insert is about to apply. "no responsible named" alone reads
        # as "this field is empty", but the row lands owned by the writer — a reader who believed
        # the warning would go looking for an unowned task that does not exist.
        landed = f' — defaulting to "{default_owner}"' if default_owner else ""
        warns.append(f"planned (deadline/scope/priority set) but no responsible named{landed}"
                     ". Name the real owner if it is someone else.")
    return warns


def _insert_one(cur, table: str, fields: dict, user: str, actor: str) -> tuple[str, list[str]]:
    """Insert one page/header/task/decision/source on an open cursor. Returns (id, warnings)."""
    warnings: list[str] = []
    _canon_people_fields(fields)                 # D4: every write, every person column
    if table == "pages":
        new_id = _new_entity(cur, "page")
        cur.execute(
            "INSERT INTO pages (id, title, summary, type, lifecycle, contributors) "
            "VALUES (%s,%s,%s, coalesce(nullif(%s,''),%s), coalesce(%s,'live')::lifecycle_kind, %s)",
            (new_id, fields.get("title"), fields.get("summary"),
             fields.get("type"), DEFAULT_PAGE_TYPE, fields.get("lifecycle"),
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
            warnings += _task_gap_warns(cur, fields, user)   # E6/E7: unlinked / unowned plan
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
        # priority is nullable (F4): an unset field stays NULL, distinct from a deliberate choice —
        # no silent 'medium' default. status keeps its 'todo' default. scope is freeform varchar(16)
        # (E1): no enum, no default — an unset scope just stays NULL.
        cur.execute(
            "INSERT INTO tasks (id, title, description, status, priority, scope, deadline, responsible) "
            "VALUES (%s,%s,%s, coalesce(%s,'todo')::task_status, %s::task_priority, %s, %s, %s)",
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
    """Append `user` to pages.contributors iff absent — order-preserving (append-only list).
    Presence is tested case-INSENSITIVELY: since D3/D4 the stored casing is whatever the client
    sent, so a plain `= ANY(...)` would append `Alice` next to an existing `alice`."""
    if not page_id:
        return
    cur.execute(
        "UPDATE pages SET contributors = CASE "
        "  WHEN EXISTS (SELECT 1 FROM unnest(coalesce(contributors, '{}'::varchar(64)[])) x "
        "               WHERE lower(x) = lower(%s)) THEN contributors "
        "  ELSE coalesce(contributors, '{}'::varchar(64)[]) || %s::varchar(64) END "
        "WHERE id=%s", (user, user, page_id))


# E5: what counts as "someone is working on this" — an edit to what the task SAYS. Deliberately
# NOT priority/scope/deadline/responsible: those are triage, and a model tidying a backlog's
# priorities during planning would otherwise mark the whole backlog in-progress. A board that lies
# is worse than a board that is merely stale.
_DOING_TRIGGER_COLS = {"title", "description"}


def _auto_doing(cur, table: str, rid: str, fieldset: dict, written: set,
                actor: str, state_only: bool) -> list[str]:
    """Flip a `todo` task to `doing` when a person or a model edits what it says (E5). Returns the
    note to report — an unrequested status change the caller cannot see is a bug, not a feature.

    NOT a trigger, deliberately: in the set_task_completed_at() style it would fire on EVERY
    UPDATE and cannot see the actor, so the phase-4 backfill, `-normalize-people`, `-relink` and
    E4's cascade would each mass-flip a whole backlog to doing. Four gates instead:
      actor == 'script'   every janitor pass and every scripted backfill stamps it (actor_kind)
      state_only          a trash / revive / lifecycle flip is not work on the task (E3)
      'status' in written the caller spoke about status — an explicit value always wins, including
                          an explicit `status: todo` on a full fetch->edit->write round trip
      title/description   and it must have actually CHANGED (fieldset, after the no-op drop), not
                          merely been echoed back unaltered
    An insert never reaches here: creating a task is not starting it."""
    if (table != "tasks" or actor == "script" or state_only or "status" in written
            or not _DOING_TRIGGER_COLS & set(fieldset)):
        return []
    cur.execute("UPDATE tasks SET status='doing' WHERE id=%s AND status='todo'", (rid,))
    return ["status todo->doing (task edited)"] if cur.rowcount else []


def _update_one(cur, table: str, rid: str, set_fields: dict, user: str, actor: str,
                written: set | None = None):
    """Update one row on an open cursor. Returns (label, warnings, notes), or None if the row was
    missing or no valid fields were given. `written` is what the CALLER actually wrote, before
    unchanged fields were dropped — E5 needs to tell "the block said status: todo" apart from
    "status was never mentioned"; it defaults to set_fields, which is already the raw input for
    every caller that does not pre-filter."""
    written = set(written if written is not None else (set_fields or {}))
    cols = _cols(table)
    fieldset = _canon_people_fields({k: v for k, v in dict(set_fields or {}).items() if k in cols})
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
    state_only = set(fieldset) <= _STATE_COLS      # lifecycle/trash flip: not a content edit (E3)
    with _state_only(cur, state_only):
        cur.execute(f"UPDATE {table} SET {assigns} WHERE id=%s RETURNING *",
                    list(fieldset.values()) + [rid])
        row = cur.fetchone()
    if not row:
        return None
    row = dict(row)
    notes = _auto_doing(cur, table, rid, fieldset, written, actor, state_only)   # E5
    if table == "pages" and "title" in fieldset:               # rename cascade: rebuild paths, mark stale
        _rename_cascade(cur, rid, fieldset["title"])
    text_field = {"headers": "body", "tasks": "description",
                  "decisions": "description", "sources": "summary"}.get(table)
    if text_field and text_field in fieldset:
        warnings += _sync_links(cur, SUBTYPE_KIND.get(table, "header"), rid, row.get(text_field), table, text_field)
    # embedding deferred: updated_at bumps past embedded_at, so the worker re-embeds this row (F6).
    # `janitor -embed` uses _embed_and_set as the manual backstop.
    if state_only:
        pass                    # a state flip is not authorship, and its UPDATE would bump the
                                # page's updated_at right back (E3)
    elif table == "pages":
        # an explicit contributors write REPLACES (honored above); skip the auto-append so a
        # rewrite to a single canonical name isn't re-polluted by the connected username.
        if "contributors" not in fieldset:
            _touch_contributors(cur, page_id=rid, user=user)
    elif table == "headers":
        _touch_contributors(cur, page_id=str(row.get("page_id")), user=user)
    log_edit(cur, table_name=table, row_id=rid, operation="update", edited_by=user, actor=actor,
             changes={**fieldset, "status": "doing"} if notes else fieldset)   # E5 flip is history
    return _write_label(row), warnings, notes



def _rename_cascade(cur, page_id: str, new_title: str) -> None:
    """Rebuild every header path's first segment to the new title; mark chunks stale."""
    cur.execute("SELECT id, path FROM headers WHERE page_id=%s", (page_id,))
    for r in cur.fetchall():
        old = r["path"] or ""
        rest = old.split(" > ", 1)
        newpath = new_title + (" > " + rest[1] if len(rest) > 1 else "")
        cur.execute("UPDATE headers SET path=%s, embedded_at=NULL WHERE id=%s", (newpath, r["id"]))


# =======================================================================================
# write — one template-upsert tool over the write path (folds what used to be five separate
# tools: insert/update/delete/link/group, all removed once `write` covered every case but one —
# see `lookup` over the groups table for the read `group list` used to serve)
# =======================================================================================

def _row_state(cur, table: str, rid: str) -> str | None:
    """'live' / 'trashed', or None when there is no such row. One query for every table now that
    trashed_at is universal (E3) — the pages special case is gone."""
    cur.execute(f"SELECT trashed_at FROM {table} WHERE id=%s", (rid,))
    r = cur.fetchone()
    return None if r is None else ("trashed" if r["trashed_at"] else "live")


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
    with _state_only(cur):
        cur.execute(f"UPDATE {table} SET trashed_at=now() WHERE id=%s", (rid,))
    log_edit(cur, table_name=table, row_id=rid, operation="trash", edited_by=user, actor=actor)
    return f"trashed {table} {_write_label(dict(row))} ({rid})"


def _revive_one(cur, table: str, rid: str, user: str, actor: str) -> None:
    """Clear trashed_at — the exact inverse of _trash_one, same on every table (E3)."""
    with _state_only(cur):
        cur.execute(f"UPDATE {table} SET trashed_at=NULL WHERE id=%s", (rid,))
    log_edit(cur, table_name=table, row_id=rid, operation="update", edited_by=user, actor=actor,
             changes={"trashed_at": None})


# A task's LIVE home: a group_links row to a group that is neither archived nor trashed. A trashed
# group is not a home — it is on its way out, so a task whose only other group is trashed has
# nowhere left to live. Evaluated AFTER the archiving UPDATE lands, so the group being archived
# no longer counts as its own live home; that is what makes the third rule work (archiving the
# last live group of a multi-group task archives the task, even though it was never alone).
_CASCADE_ARCHIVE_SQL = """
    UPDATE tasks t SET lifecycle = 'archived'
    WHERE t.lifecycle <> 'archived' AND t.trashed_at IS NULL
      AND EXISTS (SELECT 1 FROM group_links gl
                  WHERE gl.entity_id = t.id AND gl.group_id = %s)
      AND NOT EXISTS (SELECT 1 FROM group_links gl JOIN groups g ON g.id = gl.group_id
                      WHERE gl.entity_id = t.id
                        AND g.trashed_at IS NULL AND g.lifecycle <> 'archived')
    RETURNING t.id
"""


def _cascade_group_archive(cur, gid: str, user: str) -> int:
    """Archive the tasks that this group's archive just left homeless (E4). A task in another live
    group is untouched; a task whose groups are now all archived comes along.

    State-only: archiving is not a content edit, so updated_at must not move and the row must not
    look changed-this-week. E5 cannot fire here either — it lives in _update_one and this is a
    direct UPDATE — which is deliberate: a cascade that flipped a backlog to `doing` was one of the
    failure modes this release exists to remove.

    Every cascaded task gets an edit row naming the group that caused it, because otherwise an
    unarchive cannot tell a task that came along for the ride from one somebody archived on
    purpose. A future un-cascade (NOT built — out of this release's scope) would ask:
        SELECT DISTINCT row_id FROM edits
        WHERE table_name = 'tasks' AND changes->>'cascaded_from_group' = <group id>
    and, for each, check that the row's LATEST lifecycle edit is still that cascade row before
    reviving it — a task archived again by hand afterwards must stay archived."""
    with _state_only(cur):
        cur.execute(_CASCADE_ARCHIVE_SQL, (gid,))
        ids = [str(r["id"]) for r in cur.fetchall()]
    for tid in ids:
        # actor is 'script' by definition, never the caller's: the user archived the GROUP, the
        # server decided about the task. That alone separates a cascade from a deliberate archive.
        log_edit(cur, table_name="tasks", row_id=tid, operation="update", edited_by=user,
                 actor="script",
                 changes={"lifecycle": "archived", "cascaded_from_group": gid})
    return len(ids)


def _write_group(cur, rid: str | None, fields: dict, user: str, members=None,
                 members_add=None, members_remove=None) -> str:
    """Group ROW create/update + membership from a `type: group` block. `members` (entity ids;
    None = leave untouched) reconciles group_links to exactly that set; `members_add`/
    `members_remove` (D1) instead touch only the named ids, so adding one member to an 88-member
    group is one id, not a resend of all 88 — `parse_block` already rejects a block that carries
    both forms. Trash/revive and `lifecycle:` (archive) are NOT handled here — they run through
    the same generic paths every other entity uses (E3)."""
    s = _canon_people_fields({k: v for k, v in fields.items()
                              if k in {"name", "type", "description", "participants",
                                       "status", "lifecycle"}})
    _, err = _check_enums("groups", s)
    if err:
        return f"(error: {err})"
    if rid:
        s = _drop_unchanged(cur, "groups", rid, s)    # no-op writes don't churn (as every table)
        if s:
            with _state_only(cur, set(s) <= _STATE_COLS):   # lifecycle-only = state, not content
                cur.execute(f"UPDATE groups SET {', '.join(f'{k}=%s' for k in s)} WHERE id=%s",
                            list(s.values()) + [rid])
        gid, verb = rid, "updated"
    else:
        if not s.get("name"):
            return "(error: a new group needs a name)"
        cur.execute("INSERT INTO groups (name, type, description, participants, status, lifecycle) "
                    "VALUES (%s,%s,%s,%s,%s, coalesce(%s,'live')::lifecycle_kind) RETURNING id",
                    (s.get("name"), s.get("type"), s.get("description"), s.get("participants"),
                     s.get("status"), s.get("lifecycle")))
        gid, verb = str(cur.fetchone()["id"]), "inserted"
    if members is not None:
        members, warns = _resolve_roster_ids("entities", members)   # short ids (F3/C1)
        warns += _sync_junction(cur, "group_links", "group_id", gid, "entity_id", members,
                               exists_table="entities")
    elif members_add is not None or members_remove is not None:
        members_add, w1 = _resolve_roster_ids("entities", members_add)
        members_remove, w2 = _resolve_roster_ids("entities", members_remove)
        warns = w1 + w2 + _sync_junction(cur, "group_links", "group_id", gid, "entity_id",
                               exists_table="entities", add_ids=members_add, remove_ids=members_remove)
    else:
        warns = []
    # After the member sync, so a group created already-archived cascades to the members it was
    # created with. `lifecycle` survived _drop_unchanged only if THIS write changed it, so
    # re-asserting `lifecycle: archived` on an already-archived group cascades nothing (E4).
    if s.get("lifecycle") == "archived":
        n = _cascade_group_archive(cur, gid, user)
        if n:
            warns.append(f"archived {n} task(s) left with no live group")
    return f'{verb} group {_write_label(fields)} ({gid})' + _warns(warns)


def _warns(ws: list[str]) -> str:
    return ("\n  " + "\n  ".join(ws)) if ws else ""


def _tag(notes: list[str]) -> str:
    """Inline `[…]` note on a block's OWN line: what the caller did not ask for but got — an enum
    value normalized (F5), a todo task flipped to doing (E5)."""
    return ("  [" + "; ".join(notes) + "]") if notes else ""


def _failed(line: str) -> str:
    """A block outcome that must be rolled back / counted as failed. `(error: …)` is the contract
    the plugin hooks and the tests key off; a `(skip: …)` or `(no change: …)` wrote nothing but did
    not fail. Warnings can follow on indented continuation lines, hence the lstrip."""
    return line.lstrip().startswith("(error:")


def _dispatch_block(cur, p: dict, user: str, actor: str) -> str:
    return _dispatch_block_inner(cur, p, user, actor) + _warns(p.get("warnings") or [])


def _has_junction(kind: str, p: dict) -> bool:
    """A junction roster (full or delta, D1) was written on this block (present, even if empty ==
    clear-all for the full form)."""
    if kind == "task":
        return any(k in p for k in ("blockers", "blockers_add", "blockers_remove"))
    if kind == "header":
        return any(k in p for k in ("sources", "sources_add", "sources_remove"))
    return False


def _sync_block_junctions(cur, kind: str, rid: str, p: dict) -> list[str]:
    """Reconcile a task's blockers / a header's sources after its row was written (gaps 2, 4).
    Groups are handled inside _write_group. No-op when the block carried no roster. Each junction
    is either the full roster or a delta (D1) — parse_block already rejected a block carrying both.
    Roster ids may be short prefixes (F3/C1): resolved via _resolve_roster_ids BEFORE the
    self-blocker filter, so a short id that names the task itself is still caught."""
    if kind == "task":
        if "blockers" in p:
            ids, warns = _resolve_roster_ids("tasks", p["blockers"])
            ids = [b for b in ids if b != rid]     # a task can't block itself
            return warns + _sync_junction(cur, "task_dependencies", "dependent", rid, "blocker", ids, exists_table="tasks")
        if "blockers_add" in p or "blockers_remove" in p:
            add, w1 = _resolve_roster_ids("tasks", p.get("blockers_add"))
            add = [b for b in add if b != rid]
            rem, w2 = _resolve_roster_ids("tasks", p.get("blockers_remove"))
            return w1 + w2 + _sync_junction(cur, "task_dependencies", "dependent", rid, "blocker",
                                  exists_table="tasks", add_ids=add, remove_ids=rem)
    if kind == "header":
        if "sources" in p:
            ids, warns = _resolve_roster_ids("sources", p["sources"])
            return warns + _sync_junction(cur, "header_sources", "header_id", rid, "source_id", ids, exists_table="sources")
        if "sources_add" in p or "sources_remove" in p:
            add, w1 = _resolve_roster_ids("sources", p.get("sources_add"))
            rem, w2 = _resolve_roster_ids("sources", p.get("sources_remove"))
            return w1 + w2 + _sync_junction(cur, "header_sources", "header_id", rid, "source_id",
                                  exists_table="sources", add_ids=add,
                                  remove_ids=rem)
    return []


def _dispatch_block_inner(cur, p: dict, user: str, actor: str) -> str:
    table, kind, rid, fields = p["table"], p["kind"], p["id"], p["fields"]
    if rid:
        rid, err = _full_id(table, rid)        # a block's own id: may be a short prefix (F3/C1)
        if err:
            return f"(error: {err})"
    if kind == "edit":
        # edits are the append-only audit log (no trashed_at) — write supports only hard delete,
        # for pruning bad/noisy log rows. Delete-by-id, trashed:true required.
        if not rid:
            return "(skip: an edit block needs an id — deletion is by id)"
        if not p["trashed"]:
            return "(edits are audit-log rows — write supports only delete via trashed: true)"
        cur.execute("DELETE FROM edits WHERE id=%s", (rid,))
        return f"deleted edit {rid}" if cur.rowcount else f"(error: edit id {rid} not found)"
    if p["trashed"]:                                  # same on every table now (E3)
        return _trash_one(cur, table, rid, user, actor) if rid else f"(skip: trash needs an id — {kind})"
    if fields.get("lifecycle", _UNSET) is None:
        fields.pop("lifecycle")   # NOT NULL: an empty `lifecycle:` line means unchanged, not NULL
    notes, err = _check_enums(table, fields)          # validate/normalize enum values (F5)
    if err:
        return f"(error: {err})"
    revived = False
    if rid:
        state = _row_state(cur, table, rid)
        if state is None:
            return f"(error: {kind} id {rid} not found — no update; omit id to insert a new row)"
        if state == "trashed":
            if not p.get("revive"):
                return (f"(error: {kind} id {rid} is trashed — write it back with an empty "
                        f"`trashed:` line to revive it, or omit id to insert a new row)")
            _revive_one(cur, table, rid, user, actor)
            revived = True
    if kind == "group":                   # row + members; trash/revive handled above
        return _write_group(cur, rid, fields, user, p.get("members"),
                           p.get("members_add"), p.get("members_remove"))
    if rid:
        upd = dict(fields)
        if kind == "header":                      # moving a chunk (page_id/index) is out of scope
            for k in ("page_id", "index"):
                upd.pop(k, None)
        upd = _drop_unchanged(cur, table, rid, upd)
        # `written=fields` keeps E5 honest: an echoed `status: todo` that _drop_unchanged removed
        # as a no-op is still the caller having spoken about status.
        res = _update_one(cur, table, rid, upd, user, actor, written=set(fields)) if upd else None
        jwarns = _sync_block_junctions(cur, kind, rid, p)     # blockers / sources (gaps 2, 4)
        if res is None and not jwarns and not _has_junction(kind, p):
            return f"revived {table} ({rid})" if revived else f"(no change: {table} {rid})"
        verb = "revived+updated" if revived else "updated"
        base = f"{verb} {table} {res[0]} ({rid})" if res else f"{verb} {table} ({rid})"
        return base + _tag(notes + (res[2] if res else [])) + _warns((res[1] if res else []) + jwarns)
    ins = {k: v for k, v in fields.items() if v is not None}   # let DB defaults/NULL apply on insert
    if kind == "header" and ins.get("page_id"):
        pid, err = _full_id("pages", ins["page_id"])   # page_id: may be a short prefix (F3/C1)
        if err:
            return f"(error: {err})"
        ins["page_id"] = pid
    if kind == "header" and not ins.get("page_id"):
        return "(error: a header block needs page_id to insert — get it from fetch outline)"
    nid, warns = _insert_one(cur, table, ins, user, actor)
    warns += _sync_block_junctions(cur, kind, nid, p)         # blockers / sources on a fresh row
    return f"inserted {table} {_write_label(ins)} ({nid}){_tag(notes)}" + _warns(warns)


@mcp.tool
def write(blocks: list[str]) -> str:
    """Create or update entities from template blocks — the single write path.

    Each element of `blocks` is ONE `---`-fenced frontmatter template, the SAME shape `fetch`
    returns, so you write what you read. `blocks` is a LIST (batch = several templates, one
    transaction); pass one element for a single write, e.g.
    `blocks=["---\\ntype: task\\ntitle: Ship it\\n---", "---\\ntitle: Release notes\\n---"]`
    writes two rows in one call.

    - `type:` marks the kind — task / decision / source / group / header; anything else is a PAGE
      (whose `type:` is its free OKF page type, e.g. note/report). A chunk is `type: header` with
      `page_id`, `index`, `title`, `blurb` in the frontmatter, then the body AFTER the closing `---`.
    - `id:` present and live → UPDATE that row: only the fields you include change; an omitted field
      is left unchanged, an explicitly empty field is cleared. `id:` (and every roster id below) may
      be a short unique prefix, not just the full uuid. `id:` absent → INSERT. An `id:` that matches
      no live row is an ERROR (never a silent duplicate).
    - `trashed:` is tri-state: omit it to leave the row's trash state alone; `trashed: true` trashes
      it, identical on every entity; an EMPTY `trashed:` line revives it. `lifecycle:` is the
      orthogonal state: live / archived (superseded — stays fetchable, drops out of search) /
      static (never-stale reference). Archiving a GROUP cascades: any task of that group left with
      no live group is archived too, and says so. Unarchiving the group does not bring them back.
      Trash, revive and lifecycle changes do NOT bump `updated_at`; only content edits do, so
      `updated_at` means "content last changed" (finer history lives in the `edits` log).
      A `type: edit` block with an `id` and `trashed: true` HARD-deletes that audit-log row (edits
      have no trash state) — for pruning noisy/bad log entries; get the id from `rows` or the
      export log.
    - Chunk ids come from `fetch(outline=true)` / `lookup(outline_page=…)` — a whole-page fetch
      does not show them. Links: `[text](kind:uuid)` / `[[wikilinks]]` in the body/description as
      before.
    - Junction rosters are LISTS of ids. Full form (present = set to exactly that list, empty =
      clear all, absent = leave alone): a task's `blockers:` (dependencies), a group's `members:`
      (entity ids), a header's `sources:` (source ids). Each also takes `<key>_add:`/
      `<key>_remove:` (e.g. `members_add:`, `members_remove:`) to touch only those ids and leave
      the rest of the roster alone — send one form per block, not both.
    - Editing a `todo` task's title or description moves it to `doing` — editing what a task says
      is the signal that someone is in it. Triage-only edits (priority/scope/deadline/responsible)
      do not, and any `status:` you write wins. The response says so on that block's line.
    - Write is fire-and-forget: ~5s delay before a new/updated row is searchable via `lookup`.
      `fetch` by id is instant. To confirm a write immediately, `fetch` the id — do not `lookup`.
    - Per block, not all-or-nothing (A1): a bad block fails ALONE and every valid sibling is still
      written. One line per block in block order, then a `N committed, M failed` summary — so a
      resend carries only the blocks that failed, never the whole batch.
    """
    user, actor = _identity()
    if not blocks:
        return "(nothing to write)"
    # Phase 1: parse + validate every block (nothing is written; the column reflection is a cached
    # read). A parse error and an over-length/denied field fail that ONE block, exactly like a
    # dispatch error below — a typo in block 3 must not cost the other eight a round trip.
    results: list[str] = [""] * len(blocks)
    parsed: list[tuple[int, dict]] = []
    for i, b in enumerate(blocks):
        try:
            p = bl.parse_block(b)
        except bl.BlockError as e:
            results[i] = f"(error: block {i}: {e})"
            continue
        probs = _check_columns(p["table"], p["fields"], p["kind"])
        if probs:                       # every problem on this block, one line (A4/I1)
            results[i] = f"(error: block {i} ({p['kind']}): " + "; ".join(probs) + ")"
        else:
            parsed.append((i, p))
    # Phase 2: dispatch each surviving block inside its OWN savepoint. The blocks share one cursor
    # and psycopg poisons the whole transaction after ANY SQL error, so an FK miss or the
    # UNIQUE(page_id,index) partial index would take every later block down with it. ROLLBACK TO
    # undoes that block's writes (junctions and the contributors touch included) and, because
    # SET LOCAL is reverted with it, can never leave _state_only's flag stuck on for the next block.
    if parsed:
        with db().connection() as conn:
            with conn.cursor() as cur:
                for i, p in parsed:
                    sp = f"blk{i}"                      # distinct per block; released or rolled
                    cur.execute(f"SAVEPOINT {sp}")      #   back exactly once, never restacked
                    try:
                        line = _dispatch_block(cur, p, user, actor)
                    except Exception as e:              # psycopg puts DETAIL/HINT on their own
                        line = (f"(error: block {i} ({p['kind']}): "  # lines; keep it to one
                                f"{' '.join(str(e).split())})")
                    cur.execute(f"ROLLBACK TO SAVEPOINT {sp}" if _failed(line)
                                else f"RELEASE SAVEPOINT {sp}")
                    results[i] = line
            conn.commit()
    failed = sum(1 for r in results if _failed(r))
    out = "\n".join(results)
    if failed or len(blocks) > 1:       # a single clean write says it plainly enough on its own
        out += f"\n{len(blocks) - failed} committed, {failed} failed"
    return out


# =======================================================================================
# janitor  (server-side maintenance — needs DB access)
# =======================================================================================

_JANITOR_FLAGS = {"lint", "dedupe", "embed", "relink", "normalize-people"}


@mcp.tool
def janitor(flags: list[str] | None = None) -> str:
    """Kovault maintenance. Bare (no flags) = diagnose only: run checks, write a janitor_reports
    row, change nothing. Flags opt into work: -lint (renumber header indexes + prune redundant
    parent/grandparent task-dependency edges), -dedupe (merge
    duplicate sources by sha256 and identical headers -> trash losers), -embed (re-embed rows
    with embedded_at < updated_at or null), -relink (re-resolve [[wikilinks]] over all live rows
    so forward-references graph once their targets exist), -normalize-people (collapse casing
    variants of one person to the spelling the vault uses most, across
    contributors/responsible/participants/decided_by). No pass ever bumps
    updated_at — upkeep is not a content edit (E3). There is no delete flag — trash is terminal."""
    flags = [f.lstrip("-").lower() for f in (flags or [])]
    user = "janitor"
    counts: dict = {}
    report: list[str] = []
    # An unrecognised flag is reported, never ignored. `-freshness` was removed in 1.5.0 along with
    # the column it recomputed, and someone with muscle memory typing it must not be left believing
    # a pass ran. Same rule the block parser follows for unknown frontmatter keys.
    unknown = [f for f in flags if f not in _JANITOR_FLAGS]
    if unknown:
        report.append(f"Ignored unknown flag(s): {', '.join('-' + f for f in unknown)}. "
                      f"Valid: {', '.join('-' + f for f in sorted(_JANITOR_FLAGS))}."
                      + (" `-freshness` was removed in 1.5.0 with the freshness column; page age"
                         " now lives in lifecycle + updated_at." if "freshness" in unknown else ""))

    # ---- diagnostics (always) ----
    diag = _janitor_diagnose()
    counts["diagnostics"] = diag
    scalar = {k: v for k, v in diag.items() if not isinstance(v, list)}
    report.append("Diagnostics: " + ", ".join(f"{k}={v}" for k, v in scalar.items()))
    for p in diag["duplicate_groups_content"]:
        report.append(f'Duplicate? content overlap {p["pct"]}%: "{p["name_a"]}" ({p["id_a"]}, '
                      f'{p["size_a"]} members) / "{p["name_b"]}" ({p["id_b"]}, {p["size_b"]} '
                      f'members) — {p["shared"]} shared')
    for p in diag["duplicate_groups_name"]:
        report.append(f'Duplicate? name similarity {p["pct"]}%: "{p["name_a"]}" ({p["id_a"]}, '
                      f'{p["size_a"]} members) / "{p["name_b"]}" ({p["id_b"]}, {p["size_b"]} members)')
    if diag["orphan_task_sample"]:
        shown = len(diag["orphan_task_sample"])
        report.append(f"Orphan tasks (showing {shown} of {diag['orphan_tasks']}): " + ", ".join(
            f'{t["title"]} ({t["id"]})' for t in diag["orphan_task_sample"]))

    with db().connection() as conn:
        with conn.cursor() as cur:
            # Every janitor pass runs state-only (E3): upkeep must not make rows look edited this
            # week, nor mark them embed-stale. One block, so the flag covers every pass.
            with _state_only(cur):
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
                                  "Re-run with -embed/-lint/-dedupe/-relink to act.")
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


# ---- near-duplicate groups (F1+F2): two independent signals, each an actionable capped list of
# pairs — never a merge, never a trash, the comment below stands. Profiling the real vault (not
# the plan's assumed example) showed content-overlap and name-similarity catch DIFFERENT pairs
# (the real duplicate here shares zero members), so both run; neither alone would find both.
_DUP_GROUP_PAIR_CAP = 10          # "top N worst pairs" — enough to act on, not a wall of noise
# Jaccard (shared / union), not containment: containment reads ~100% for legitimate nesting (a
# topic that is a strict subset of its area), which would swamp the report. Restricting to pairs
# of the SAME `type` already keeps an area/topic pair out (they're different types); 0.5 says "at
# least half the combined membership is shared" — a coincidental overlap between unrelated groups
# essentially never crosses that, a real duplicate almost always does.
_GROUP_CONTENT_OVERLAP_THRESHOLD = 0.5
# Trigram name similarity. The real pairs in this vault measured at 0.64 and 0.48 — 0.4 clears both
# with margin. The noise this would otherwise catch (bench-legacy-grp-0..7 / bench-write-grp-0..7,
# 0.80-0.81) is removed by the numbered-series exclusion below, not by this number: raising the
# threshold to dodge the noise would also cut the real 0.48 pair.
_GROUP_NAME_SIM_THRESHOLD = 0.4
_ORPHAN_TASK_SAMPLE_CAP = 20      # named orphan tasks shown; the total count stays uncapped


def _dup_groups_by_content(cap: int = _DUP_GROUP_PAIR_CAP) -> list[dict]:
    """Two live groups of the SAME type sharing >=50% of their combined membership (Jaccard).
    Same-type is what keeps a legitimate area/topic nesting out of this list."""
    rows = db().query(f"""
        WITH gl AS (
            SELECT gl.group_id, gl.entity_id FROM group_links gl
            JOIN groups g ON g.id = gl.group_id WHERE {_live('g')}
        ), sizes AS (SELECT group_id, count(*) n FROM gl GROUP BY group_id),
        shared AS (
            SELECT a.group_id AS ga, b.group_id AS gb, count(*) AS n
            FROM gl a JOIN gl b ON a.entity_id = b.entity_id AND a.group_id < b.group_id
            GROUP BY a.group_id, b.group_id
        )
        SELECT ga.name AS name_a, ga.id AS id_a, gb.name AS name_b, gb.id AS id_b,
               sa.n AS size_a, sb.n AS size_b, s.n AS shared,
               round(100.0 * s.n::numeric / (sa.n + sb.n - s.n), 1) AS pct
        FROM shared s
        JOIN sizes sa ON sa.group_id = s.ga JOIN sizes sb ON sb.group_id = s.gb
        JOIN groups ga ON ga.id = s.ga JOIN groups gb ON gb.id = s.gb
        WHERE ga.type = gb.type AND s.n::numeric / (sa.n + sb.n - s.n) >= %(thresh)s
        ORDER BY pct DESC LIMIT %(cap)s
        """, {"thresh": _GROUP_CONTENT_OVERLAP_THRESHOLD, "cap": cap})
    return [{"name_a": r["name_a"], "id_a": _short_id(r["id_a"]), "size_a": int(r["size_a"]),
             "name_b": r["name_b"], "id_b": _short_id(r["id_b"]), "size_b": int(r["size_b"]),
             "shared": int(r["shared"]), "pct": float(r["pct"])} for r in rows]


def _dup_groups_by_name(cap: int = _DUP_GROUP_PAIR_CAP) -> list[dict]:
    """Two live groups whose names are similar (trigram) but are NOT the same numbered series —
    stripping trailing digits/separators reduces 'bench-legacy-grp-4' and '...-grp-6' to the same
    stem, and an identical stem means series, not duplicate. Finds what content-overlap structurally
    cannot: this vault's real duplicates share zero members. Sizes are along for context (a merge
    decision wants them) even though they play no part in the name-similarity score itself.

    An EMPTY group whose name matches a populated one sorts first, ahead of a higher trigram score.
    It is the highest-confidence finding here — an abandoned shell, not a deliberate sibling — and
    the cheapest to act on, since trashing it loses nothing. Without this a real family of related
    groups (`x`, `x-logic`, `x-design`, ...) fills the cap with pairs the owner created on purpose
    and pushes the abandoned one off the end, which is how a report stops being read."""
    rows = db().query(f"""
        SELECT a.name AS name_a, a.id AS id_a, b.name AS name_b, b.id AS id_b,
               coalesce(sa.n, 0) AS size_a, coalesce(sb.n, 0) AS size_b,
               round(100.0 * similarity(a.name, b.name)::numeric, 1) AS pct
        FROM groups a JOIN groups b ON a.id < b.id
        LEFT JOIN (SELECT group_id, count(*) n FROM group_links GROUP BY group_id) sa ON sa.group_id = a.id
        LEFT JOIN (SELECT group_id, count(*) n FROM group_links GROUP BY group_id) sb ON sb.group_id = b.id
        WHERE {_live('a')} AND {_live('b')}
          AND similarity(a.name, b.name) >= %(thresh)s
          AND regexp_replace(lower(a.name), '[-_\\s]*[0-9]+$', '')
           <> regexp_replace(lower(b.name), '[-_\\s]*[0-9]+$', '')
        ORDER BY (least(coalesce(sa.n, 0), coalesce(sb.n, 0)) = 0) DESC, pct DESC
        LIMIT %(cap)s
        """, {"thresh": _GROUP_NAME_SIM_THRESHOLD, "cap": cap})
    return [{"name_a": r["name_a"], "id_a": _short_id(r["id_a"]), "size_a": int(r["size_a"]),
             "name_b": r["name_b"], "id_b": _short_id(r["id_b"]), "size_b": int(r["size_b"]),
             "pct": float(r["pct"])} for r in rows]


def _janitor_diagnose() -> dict:
    q = db().query_one
    stale = 0
    for t in ("headers", "tasks", "decisions", "sources"):
        col = et.COMPOSERS[t][1]
        r = q(f"SELECT count(*) n FROM {t} WHERE trashed_at IS NULL "
              f"AND (embedded_at IS NULL OR embedded_at < updated_at)")
        stale += int(r["n"])
    trashed = int(q("SELECT count(*) n FROM pages WHERE trashed_at IS NOT NULL")["n"])
    dangling = int(q(
        "SELECT count(*) n FROM links l WHERE NOT EXISTS ("
        " SELECT 1 FROM headers h WHERE l.to_kind='header' AND h.id=l.to_id AND h.trashed_at IS NULL)"
        " AND l.to_kind='header'")["n"])
    redundant = int(q(f"SELECT count(*) n FROM ({_REDUNDANT_DEPS_SQL}) x")["n"])
    # near-duplicate groups (F1+F2): report only — never auto-merge, since a real distinction
    # (e.g. two same-named servers) may be intended. `write`'s members_remove: (D1) makes merging
    # by hand cheap once a human has looked at the evidence below.
    dup_content = _dup_groups_by_content()
    dup_name = _dup_groups_by_name()
    # orphan tasks (F4): live tasks with no graph link and no dependency edge — hard to find/trust.
    # Archived tasks are excluded: they are deliberately out of circulation, not lost (E3).
    orphan_tasks = int(q(
        f"SELECT count(*) n FROM tasks t WHERE {_live('t')} "
        "AND NOT EXISTS (SELECT 1 FROM links l WHERE (l.from_kind='task' AND l.from_id=t.id) "
        "                                          OR (l.to_kind='task' AND l.to_id=t.id)) "
        "AND NOT EXISTS (SELECT 1 FROM task_dependencies d WHERE d.blocker=t.id OR d.dependent=t.id)")["n"])
    orphan_sample = [{"id": _short_id(r["id"]), "title": r["title"]} for r in db().query(
        f"SELECT id, title FROM tasks t WHERE {_live('t')} "
        "AND NOT EXISTS (SELECT 1 FROM links l WHERE (l.from_kind='task' AND l.from_id=t.id) "
        "                                          OR (l.to_kind='task' AND l.to_id=t.id)) "
        "AND NOT EXISTS (SELECT 1 FROM task_dependencies d WHERE d.blocker=t.id OR d.dependent=t.id) "
        "ORDER BY created_at LIMIT %(cap)s", {"cap": _ORPHAN_TASK_SAMPLE_CAP})]
    return {"stale_embeddings": stale, "trashed_pages": trashed,
            "dangling_header_links": dangling, "redundant_blocks": redundant,
            "duplicate_groups_content": dup_content, "duplicate_groups_name": dup_name,
            "orphan_tasks": orphan_tasks, "orphan_task_sample": orphan_sample}


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


# Every person occurrence in the vault, best spelling first per person. The winner is the casing
# used MOST OFTEN; ties go to the earliest row that used it, then alphabetically — deterministic
# without needing a house style. `min(created_at)` is what makes "first seen" mean first seen.
_PEOPLE_CENSUS_SQL = """
    WITH people AS (
        SELECT unnest(contributors) AS name, created_at FROM pages   WHERE contributors IS NOT NULL
        UNION ALL
        SELECT unnest(responsible),   created_at FROM tasks     WHERE responsible IS NOT NULL
        UNION ALL
        SELECT unnest(participants),  created_at FROM groups    WHERE participants IS NOT NULL
        UNION ALL
        SELECT decided_by,            created_at FROM decisions WHERE decided_by IS NOT NULL
    )
    SELECT lower(btrim(name)) AS key, btrim(name) AS name
    FROM people WHERE btrim(name) <> ''
    GROUP BY 1, 2
    ORDER BY key, count(*) DESC, min(created_at), name
"""

_PEOPLE_TABLES = (("pages", "contributors"), ("tasks", "responsible"),
                  ("groups", "participants"), ("decisions", "decided_by"))


def _janitor_normalize_people(cur, user: str) -> int:
    """Collapse casing variants of one person to a single spelling, vault-wide, across
    contributors / responsible / participants (arrays) and decided_by (scalar).

    The target is NO LONGER lowercase (D3/D4): the write boundary now keeps the case the client
    sends, so this pass follows the vault instead of imposing a house style — the winning spelling
    is the one used most often (see _PEOPLE_CENSUS_SQL). Dedupe, trimming and empty-dropping come
    from _canon_people, the same function the write boundary uses, so this pass cannot drift from
    it — re-encoding the rule in SQL is what caused the split it exists to clean up. Only changed
    rows are rewritten + logged; the janitor's state-only flag keeps updated_at untouched."""
    cur.execute(_PEOPLE_CENSUS_SQL)
    winner: dict[str, str] = {}
    for r in cur.fetchall():
        winner.setdefault(r["key"], r["name"])        # query is ordered best-first per person
    n = 0
    for table, col in _PEOPLE_TABLES:
        cur.execute(f"SELECT id, {col} AS v FROM {table} WHERE {col} IS NOT NULL")
        rows = [(r["id"], r["v"]) for r in cur.fetchall()]     # drain before reusing the cursor
        for rid, val in rows:
            is_list = isinstance(val, list)
            picked = [winner.get(str(x or "").strip().lower(), x) for x in (val if is_list else [val])]
            new = _canon_people(picked if is_list else picked[0])
            if new == val:
                continue
            cur.execute(f"UPDATE {table} SET {col} = %s WHERE id = %s", (new, rid))
            log_edit(cur, table_name=table, row_id=str(rid), operation="update",
                     edited_by=user, actor="script", changes={col: "normalized"})
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
# page-meta  (cheap staleness probe — the fetch-dedup PreToolUse hook checks updated_at, F1)
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
                # duration_ms is client-measured tool-call round-trip latency (plugin PostToolUse
                # hook), NOT server compute time — do not read it as server write time.
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
