"""Render DB rows into the fetch output format.

Reflective: the row dict handed in IS the column reflection — every caller passes a `SELECT *`
row, which carries one key per column — so a column added to a table renders here with no code
change, and no hand-maintained field list can drift from the schema. Three inputs decide a block:

  blocks.FIELD_MAP   the template-key <-> column mapping (`description`->summary, `at`->decided_at).
                     It is the PARSE side of this same format, so using it here means there is one
                     mapping, not two drifting apart.
  _KEY_ORDER         where a known key goes. Order only; it never decides membership.
  the row itself     everything else, rendered under its own column name.

Pure module — plain dicts/lists in, text out; the server supplies the row data and the junction
rosters. That is deliberate: export.py drives the same renderer without a DB handle.
"""
from __future__ import annotations

from datetime import date, datetime

from . import blocks as bl


def _ts(v) -> str:
    if v is None:
        return ""
    if isinstance(v, (datetime, date)):
        return v.isoformat()
    return str(v)


def _q(v) -> str:
    """YAML-safe scalar for a frontmatter value. Plain when safe; double-quoted + escaped when the
    text holds a YAML indicator that would break the property block: a ': ' (colon-space, e.g. a
    title/description with a colon, or a group `members:` line), a leading indicator char, a '#',
    or a newline. Without this, Obsidian reads the second key as nested and the block breaks."""
    s = "" if v is None else str(v)
    if not s:
        return ""
    if (": " in s or s[0] in "-?:#&*!|>'\"%@`[]{}," or s.endswith(":")
            or s.strip() != s or "\n" in s or "#" in s):
        return '"' + s.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n") + '"'
    return s


def _trashed(v) -> str:
    """`trashed:` is a flag, not a date: 'true' when trashed_at is set, empty when the row is live.
    The exact trash time lives in the edits log. Round-tripping a block keeps the state as it was;
    CLEARING the line revives the row (blocks.parse_block), the same on every entity (E3)."""
    return "true" if v else ""


def _list(v) -> str:
    if not v:
        return ""
    return ", ".join(str(x) for x in v)


def _related(links: list[tuple[str, str]]) -> str:
    """[(to_kind, to_id), ...] -> 'kind:id, kind:id'."""
    return ", ".join(f"{k}:{i}" for k, i in (links or []))


def _roster(v, fmt=None) -> str | None:
    """A junction roster the caller never queried (None) is not rendered at all, while one that
    came back empty ([]) renders as an empty line. The difference matters: export.py cannot query
    the reverse dependency edge, and an empty `blocking:` there would claim "nothing depends on
    this" when in truth nobody asked."""
    return None if v is None else _q((fmt or _list)(v))


def _val(v) -> str:
    """One formatter for every field: the VALUE's type decides, so a column this build has never
    heard of still renders correctly without being declared anywhere."""
    if v is None:
        return ""
    if isinstance(v, (datetime, date)):
        return _ts(v)
    if isinstance(v, (list, tuple)):
        return _q(_list(v))
    return _q(v)


# ---- what may be SHOWN ------------------------------------------------------------------
# This is NOT server._DENY_COLS. That set says what may not be WRITTEN, and `created_at` /
# `updated_at` are on it while still being two of the most useful things to render — they are how
# a model sees recency. This set is the reverse: machine state that means nothing to a reader.
# Merge the two and you either print 4000-dim vectors into the model's context or stop printing
# timestamps. They overlap; they are not the same rule.
_HIDE_EXACT = {"embedded_at"}


def hidden(col: str) -> bool:
    """ponytail: matched by the schema's naming conventions — `*_norm` for the GENERATED trigram
    columns, `*embedding` for the halfvec ones — rather than by DB reflection, so this module
    stays pure and export.py can drive it without a DB handle. A generated or vector column that
    breaks the convention would leak into the render; pass the phase-2 reflection in if it does."""
    return col in _HIDE_EXACT or col.endswith("_norm") or col.endswith("embedding")


# Read-only keys every kind shares: rendered from a column, never written from one (blocks
# ignores them on parse). `trashed` is the flag form of trashed_at, see _trashed.
_META_COLS = {"id": "id", "created": "created_at", "updated": "updated_at",
              "completed": "completed_at", "trashed": "trashed_at"}

# Frontmatter key ORDER: identity first, then the text a reader wants next, then timestamps, then
# state, then the row's own fields, then the junction rosters last (longest lines). Membership is
# NOT decided here — a key missing from this tuple still renders, just after the known block.
_KEY_ORDER = ("type", "sourcetype", "grouptype", "title", "name", "reference", "id",
              "description", "blurb", "created", "updated", "trashed", "lifecycle", "status",
              "priority", "scope", "deadline", "completed", "at", "by", "sha256",
              "page_id", "index", "level", "path",
              "responsible", "participants", "contributors",
              "blockers", "blocking", "sources", "members", "referenced by", "related")


def column_of(kind: str, name: str) -> str:
    """The COLUMN a rendered frontmatter key names — `description` is pages.summary, `created` is
    created_at, `at` is decided_at. A name that is already a column passes straight through, so a
    caller selecting columns (I1) can use whichever of the two they saw in the output. This is the
    same FIELD_MAP the renderer and the parser share; there is no third mapping."""
    return _META_COLS.get(name) or bl.FIELD_MAP.get(kind, {}).get(name) or name


def _frontmatter(kind: str, row: dict, rosters: dict | None = None,
                 skip: frozenset = frozenset()) -> list[str]:
    """The `---`-fenced block for one row, as lines. Keys are gathered in precedence order —
    read-only metadata, then blocks.FIELD_MAP, then every remaining column the row carries — and
    laid out by _KEY_ORDER. `rosters` are the junction lists only the server can supply."""
    out: dict[str, str] = {}
    used = set(skip)
    for key, col in _META_COLS.items():
        if col in row:
            out[key] = _trashed(row[col]) if key == "trashed" else _val(row[col])
            used.add(col)
    for key, col in bl.FIELD_MAP.get(kind, {}).items():
        if col in row:
            out[key] = _val(row[col])
        used.add(col)                 # claimed even when absent: never re-emit under its column name
    if kind != "page":                # a PAGE's `type:` is its own free OKF value (blocks.classify)
        out["type"] = kind
    for col, v in row.items():        # reflected: whatever this build has never heard of
        if col not in used and not hidden(col):
            out[col] = _val(v)
    out.update({k: v for k, v in (rosters or {}).items() if v is not None})   # see _roster
    ordered = [k for k in _KEY_ORDER if k in out] + [k for k in out if k not in _KEY_ORDER]
    return ["---"] + [f"{k}: {out[k]}" for k in ordered] + ["---"]


def render_chunk(h: dict, standalone: bool = True) -> str:
    """A chunk = its title then its body. Navigation lives in the body itself as inline
    [text](kind:uuid) links (no separate Related line, no per-chunk summary).

    Standalone — a direct chunk fetch — leads with the same frontmatter every other kind gets, so
    the chunk's id, page, index and trashed/lifecycle state are visible (A3) and the block can be
    edited and written straight back. The title is a frontmatter key there, NOT a line above the
    body: everything after the fence is the body on the way back in, so repeating the title would
    graft it into the body on every round trip. Inside a full page render there is no frontmatter
    at all — the page owns the one block, and a fence per chunk would both bury the content and
    read as several concatenated templates when written back."""
    lines = (_frontmatter("header", h, skip=frozenset({"body"})) if standalone
             else [h.get("title") or "(intro)"])
    lines.append("")                      # blank line so a body starting with a table renders
    lines.append(h.get("body") or "")
    return "\n".join(lines).rstrip() + "\n"


def render_page(page: dict, headers: list[dict]) -> str:
    """Full page: frontmatter + title + every live header (title + body) in index order.
    Inline [text](kind:uuid) links in the bodies carry the graph navigation."""
    fm = _frontmatter("page", page) + [page.get("title") or "", ""]
    body = [render_chunk(h, standalone=False) for h in headers]
    return "\n".join(fm) + "\n".join(body)


def render_task(t: dict, blockers: list[str] | None = None,
                links: list[tuple[str, str]] | None = None,
                blocking: list[str] | None = None) -> str:
    """`blockers:` = the tasks that block this one; `blocking:` = the reverse edge, the tasks this
    one blocks (I4). Both render `id — title`. Only `blockers:` is writable — one dependency edge
    is reconciled from the dependent's side, so writing it from both would be two ways to say the
    same thing."""
    return "\n".join(_frontmatter("task", t, {
        "blockers": _roster(blockers),
        "blocking": _roster(blocking),
        "related": _roster(links, _related),
    })) + "\n"


def render_decision(d: dict, links: list[tuple[str, str]] | None = None) -> str:
    return "\n".join(_frontmatter("decision", d, {"related": _roster(links, _related)})) + "\n"


def render_source(s: dict, referenced_by: list[str] | None = None) -> str:
    return "\n".join(_frontmatter("source", s, {
        "referenced by": _roster(referenced_by)})) + "\n"


def render_group(g: dict, members: list[tuple[str, str, str]] | None = None,
                 ids_only: bool = False) -> str:
    """members = [(entity_kind, id, title/name), ...] from group_links via snippet data.
    ids_only drops the ` — label` (a large roster's labels cost tokens; fetch ids for detail)."""
    def fmt(ms):
        return ", ".join(f"{k}: {i}" if ids_only else f"{k}: {i} — {label}" for k, i, label in ms)
    return "\n".join(_frontmatter("group", g, {"members": _roster(members, fmt)})) + "\n"
