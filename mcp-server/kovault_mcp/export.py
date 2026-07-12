"""No-AI export: render the DB to a Google OKF markdown bundle.

A folder tree of `.md` files with YAML frontmatter (`type` required; title/description/
timestamp recommended), plus index.md (listing) and log.md (from the edits table). Reuses the
`fetch` render format (render.py). OKF registers no central type list — pages.type passes
through as-is. Selectable scope: everything, whole entity tables, or specific rows.

  python -m kovault_mcp.export --out export/out
  python -m kovault_mcp.export --out export/out --tables pages,decisions
  python -m kovault_mcp.export --out export/out --tables tasks --ids <uuid>,<uuid>
"""
from __future__ import annotations

import argparse
import os
import re
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING

from .config import Config
from . import render as rnd

if TYPE_CHECKING:  # avoid importing the psycopg-backed db module for the pure render/pathing code
    from .db import Database

TABLES = ("pages", "tasks", "decisions", "sources", "groups")


def _slug(text: str, fallback: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", (text or "").lower()).strip("-")
    return s or fallback


def _seg(v, fallback: str = "unknown") -> str:
    """A folder-name segment from an enum/label value (status, page type, freshness, grouptype)."""
    return _slug(str(v or ""), "") or fallback


def _ext_seg(reference: str | None) -> str:
    """Extension bucket for a `file` source: '/x/a.MD' -> 'md', no extension -> 'no-ext'."""
    name = (reference or "").split("?")[0].split("#")[0]
    ext = os.path.splitext(name)[1].lstrip(".").lower()
    return _slug(ext, "") or "no-ext"


def _isoweek(v) -> str:
    """ISO year-week folder like '2026-W28' from a datetime/date/ISO string; 'undated' if missing."""
    if v is None:
        return "undated"
    if isinstance(v, str):
        try:
            v = datetime.fromisoformat(v)
        except ValueError:
            return "undated"
    y, w, _ = v.isocalendar()
    return f"{y}-W{w:02d}"


def _uniq(used: set, rel: str) -> str:
    """Collision-proof a relpath: two rows that slug to the same file in the same folder get
    -01, -02 … suffixes, so a distinct DB row is never silently overwritten by another."""
    if rel not in used:
        used.add(rel)
        return rel
    base, _, ext = rel.rpartition(".")
    i = 1
    while f"{base}-{i:02d}.{ext}" in used:
        i += 1
    out = f"{base}-{i:02d}.{ext}"
    used.add(out)
    return out


def _links_of(dbx: Database, kind: str, rid: str) -> list[tuple[str, str]]:
    return [(r["to_kind"], str(r["to_id"])) for r in dbx.query(
        "SELECT to_kind, to_id FROM links WHERE from_kind=%s AND from_id=%s", (kind, rid))]


# ---- optional [text](kind:uuid) -> [[Title]] rewrite (export --wikilinks) --------------------
_LINK_RE = re.compile(r"\[([^\]]*)\]\((page|header|task|decision|source):([0-9a-fA-F-]{36})\)")
_TITLE_SQL = {
    "page": "SELECT title FROM pages WHERE id=%s",
    "header": "SELECT title FROM headers WHERE id=%s",
    "task": "SELECT title FROM tasks WHERE id=%s",
    "decision": "SELECT title FROM decisions WHERE id=%s",
    "source": "SELECT coalesce(title, reference) AS title FROM sources WHERE id=%s",
}


def _resolve_entity_title(dbx: Database, kind: str, uid: str, cache: dict) -> str | None:
    key = (kind, uid)
    if key not in cache:
        row = dbx.query_one(_TITLE_SQL[kind], (uid,)) if kind in _TITLE_SQL else None
        cache[key] = (row or {}).get("title")
    return cache[key]


def _to_wikilinks(dbx: Database, text: str, cache: dict) -> str:
    """Rewrite [label](kind:uuid) markdown links into Obsidian [[Title]] (or [[Title|label]] when
    the label differs), resolving each id to its title. Unresolved ids stay as markdown."""
    def repl(m):
        label, kind, uid = m.group(1), m.group(2), m.group(3)
        title = _resolve_entity_title(dbx, kind, uid, cache)
        if not title:
            return m.group(0)
        return f"[[{title}]]" if label == title else f"[[{title}|{label}]]"
    return _LINK_RE.sub(repl, text)


def _apply_wikilinks(dbx: Database, files: list[tuple[str, str]]) -> list[tuple[str, str]]:
    cache: dict = {}
    return [(rel, _to_wikilinks(dbx, content, cache)) for rel, content in files]


def build_bundle(dbx: Database, tables: list[str], ids: list[str] | None,
                 wikilinks: bool = False) -> list[tuple[str, str]]:
    """Render the selected scope to an in-memory OKF bundle: [(relpath, content), ...], including
    index.md (listing) and log.md (edits). No disk writes, so the same renderer feeds the CLI
    exporter (to a folder) and the /export HTTP route (to a streamed zip).

    Folder tree, so a big vault stays navigable in Obsidian:
      pages/<type>/<freshness>/     tasks/<status>/     decisions/<ISO-week>/
      sources/<sourcetype>/  (a `file` source splits again by extension: sources/file/<ext>/)
      groups/<grouptype>/
    Two rows that slug to the same filename in one folder get -01/-02 suffixes (no silent
    overwrite of a distinct id)."""
    files: list[tuple[str, str]] = []
    listing: list[str] = ["# Kovault export (OKF bundle)", ""]
    id_filter = " AND id = ANY(%s)" if ids else ""
    id_param = [ids] if ids else []
    used: set = set()

    def emit(rel: str, content: str, label: str, kind: str) -> None:
        rel = _uniq(used, rel)
        files.append((rel, content))
        listing.append(f"- [{label}]({rel}) — {kind}")

    if "pages" in tables:
        pages = dbx.query(f"SELECT * FROM pages WHERE freshness <> 'trashed'{id_filter} ORDER BY title", id_param)
        for p in pages:
            hs = dbx.query("SELECT * FROM headers WHERE page_id=%s AND trashed_at IS NULL ORDER BY index", (p["id"],))
            rel = f"pages/{_seg(p.get('type'), 'page')}/{_seg(p.get('freshness'))}/{_slug(p['title'], str(p['id']))}.md"
            emit(rel, rnd.render_page(p, hs), p["title"], p.get("type") or "page")

    if "tasks" in tables:
        for r in dbx.query(f"SELECT * FROM tasks WHERE trashed_at IS NULL{id_filter} ORDER BY created_at", id_param):
            title = r.get("title") or str(r["id"])
            rel = f"tasks/{_seg(r.get('status'))}/{_slug(title, str(r['id']))}.md"
            emit(rel, _render_task_export(dbx, r), title, "task")

    if "decisions" in tables:
        for r in dbx.query(f"SELECT * FROM decisions WHERE trashed_at IS NULL{id_filter} ORDER BY created_at", id_param):
            title = r.get("title") or str(r["id"])
            week = _isoweek(r.get("decided_at") or r.get("created_at"))
            rel = f"decisions/{week}/{_slug(title, str(r['id']))}.md"
            emit(rel, _render_decision_export(dbx, r), title, "decision")

    if "sources" in tables:
        for r in dbx.query(f"SELECT * FROM sources WHERE trashed_at IS NULL{id_filter} ORDER BY created_at", id_param):
            title = r.get("title") or r.get("reference") or str(r["id"])
            st = _seg(r.get("type"))
            sub = f"file/{_ext_seg(r.get('reference'))}" if st == "file" else st
            rel = f"sources/{sub}/{_slug(title, str(r['id']))}.md"
            emit(rel, _render_source_export(dbx, r), title, "source")

    if "groups" in tables:
        gs = dbx.query(f"SELECT * FROM groups{(' WHERE id = ANY(%s)' if ids else '')} ORDER BY name", id_param)
        for g in gs:
            members = dbx.query(
                "SELECT e.kind, e.id, coalesce(p.title,t.title,d.title,s.title,s.reference) label "
                "FROM group_links gl JOIN entities e ON e.id=gl.entity_id "
                "LEFT JOIN pages p ON p.id=e.id LEFT JOIN tasks t ON t.id=e.id "
                "LEFT JOIN decisions d ON d.id=e.id LEFT JOIN sources s ON s.id=e.id "
                "WHERE gl.group_id=%s", (g["id"],))
            rel = f"groups/{_seg(g.get('type'))}/{_slug(g['name'], str(g['id']))}.md"
            emit(rel, rnd.render_group(g, [(m["kind"], str(m["id"]), m["label"] or "") for m in members]),
                 g["name"], "group")

    files.append(("index.md", "\n".join(listing) + "\n"))

    edits = dbx.query("SELECT created_at, table_name, row_id, operation, edited_by, actor "
                      "FROM edits ORDER BY created_at DESC LIMIT 5000")
    loglines = ["# Change log", ""]
    for e in edits:
        loglines.append(f"- {e['created_at']} · {e['operation']} {e['table_name']} {e['row_id']} "
                        f"by {e['edited_by']} ({e['actor']})")
    files.append(("log.md", "\n".join(loglines) + "\n"))
    return _apply_wikilinks(dbx, files) if wikilinks else files


def export(out_dir: str, tables: list[str], ids: list[str] | None, wikilinks: bool = False) -> list[str]:
    """CLI/backup path: render the bundle and write it to a folder tree. Returns file paths."""
    from .db import Database
    dbx = Database(Config())
    dbx.open()
    try:
        files = build_bundle(dbx, tables, ids, wikilinks)
    finally:
        dbx.close()
    out = Path(out_dir)
    written: list[str] = []
    for rel, content in files:
        path = out / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
        written.append(str(path))
    return written


def bundle_zip(dbx: Database, tables: list[str], ids: list[str] | None,
               wikilinks: bool = False) -> bytes:
    """Render the bundle straight to a zip (bytes) for the /export HTTP download. Nothing lands
    on the server's disk and nothing enters an AI context (the client streams it to a folder)."""
    import io
    import zipfile
    files = build_bundle(dbx, tables, ids, wikilinks)
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for rel, content in files:
            zf.writestr(rel, content)
    return buf.getvalue()


def counts(dbx: Database, tables: list[str], ids: list[str] | None) -> dict[str, int]:
    """Row counts per selected table under the same filters build_bundle uses — for the manifest
    the `export` MCP tool returns (so it never has to emit file contents)."""
    id_filter = " AND id = ANY(%s)" if ids else ""
    id_param = [ids] if ids else []
    out: dict[str, int] = {}
    if "pages" in tables:
        out["pages"] = dbx.query_one(
            f"SELECT count(*) n FROM pages WHERE freshness <> 'trashed'{id_filter}", id_param)["n"]
    for table in ("tasks", "decisions", "sources"):
        if table in tables:
            out[table] = dbx.query_one(
                f"SELECT count(*) n FROM {table} WHERE trashed_at IS NULL{id_filter}", id_param)["n"]
    if "groups" in tables:
        out["groups"] = dbx.query_one(
            f"SELECT count(*) n FROM groups{(' WHERE id = ANY(%s)' if ids else '')}", id_param)["n"]
    return out


def _render_task_export(dbx: Database, r: dict) -> str:
    blockers = [x["title"] for x in dbx.query(
        "SELECT t.title FROM task_dependencies d JOIN tasks t ON t.id=d.blocker WHERE d.dependent=%s", (r["id"],))]
    return rnd.render_task(r, blockers, _links_of(dbx, "task", str(r["id"])))


def _render_decision_export(dbx: Database, r: dict) -> str:
    return rnd.render_decision(r, _links_of(dbx, "decision", str(r["id"])))


def _render_source_export(dbx: Database, r: dict) -> str:
    ref_by = [str(x["header_id"]) for x in dbx.query(
        "SELECT header_id FROM header_sources WHERE source_id=%s", (r["id"],))]
    return rnd.render_source(r, ref_by)


def _write(folder: Path, slug: str, body: str, written: list[str]) -> str:
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"{slug}.md"
    path.write_text(body, encoding="utf-8")
    written.append(str(path))
    return f"{folder.name}/{slug}.md"


def main() -> None:
    ap = argparse.ArgumentParser(description="Export the Kovault DB as a Google OKF markdown bundle.")
    ap.add_argument("--out", default="export/out", help="output directory")
    ap.add_argument("--tables", default=",".join(TABLES), help="comma list: pages,tasks,decisions,sources,groups")
    ap.add_argument("--ids", default="", help="optional comma list of row ids to restrict to")
    ap.add_argument("--wikilinks", action="store_true",
                    help="convert [text](kind:uuid) markdown links to [[Title]] wikilinks")
    args = ap.parse_args()
    tables = [t.strip() for t in args.tables.split(",") if t.strip() in TABLES]
    ids = [i.strip() for i in args.ids.split(",") if i.strip()] or None
    written = export(args.out, tables, ids, args.wikilinks)
    print(f"exported {len(written)} file(s) to {args.out}")


if __name__ == "__main__":
    main()
