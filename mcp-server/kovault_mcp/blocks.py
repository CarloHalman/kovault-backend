"""Parse a `write` entity template back into {kind, table, id, fields, body, trashed}.

The `write` tool takes the SAME `---`-fenced frontmatter shape `fetch`/render.py emits, so the
model writes what it reads. This module is the inverse of render.py — pure stdlib (`re`-free),
unit-tested as a round-trip. No YAML dependency: the format is render.py's own `key: value`
frontmatter with `_q` quoting, so parsing mirrors that exactly.

Kind detection: `type:` holds a literal marker for task/decision/source/group/header; a PAGE's
`type:` instead holds its free OKF page type (note/report/…), so anything NOT a marker == page.

Body: only a `header` (chunk) block has a body — everything after the SECOND `---` fence. A body
may itself contain `---` lines; only the first two fences delimit the frontmatter.
"""
from __future__ import annotations

_MARKERS = {"task", "decision", "source", "group", "header", "edit"}

TABLE = {"page": "pages", "task": "tasks", "decision": "decisions",
         "source": "sources", "group": "groups", "header": "headers", "edit": "edits"}

# rendered frontmatter key -> DB column, per kind. Keys absent here are read-only on write
# (created/updated/related/blockers/referenced by/contributors/members). id/type/trashed are
# handled separately; header `body` comes from the post-fence region, not a frontmatter key.
FIELD_MAP = {
    "page":     {"title": "title", "description": "summary", "freshness": "freshness", "type": "type",
                 "contributors": "contributors"},
    "task":     {"title": "title", "description": "description", "status": "status",
                 "priority": "priority", "scope": "scope", "deadline": "deadline",
                 "responsible": "responsible"},
    "decision": {"title": "title", "description": "description", "at": "decided_at", "by": "decided_by"},
    "source":   {"title": "title", "description": "summary", "sourcetype": "type",
                 "reference": "reference", "sha256": "sha256"},
    "group":    {"name": "name", "description": "description", "grouptype": "type",
                 "participants": "participants"},
    "header":   {"title": "title", "blurb": "blurb", "page_id": "page_id",
                 "index": "index", "level": "level"},
    "edit":     {},   # audit-log row: no writable fields — write supports only delete (trashed:true)
}
# columns rendered as a ", "-joined list (render._list) -> split back to a list.
_ARRAY_COLS = {"responsible", "participants", "contributors"}

# Junction-table id rosters a `write` block carries and the server reconciles (not FIELD_MAP
# columns): task blockers -> task_dependencies, group members -> group_links, header sources ->
# header_sources. Rendered as an id list; parse keeps the ids and drops any kind/label sugar.
# Key present -> reconcile to that set (empty value clears all); key absent -> leave unchanged.
_JUNCTION_KEYS = {"task": "blockers", "group": "members", "header": "sources"}

# --- anomaly detection (F: no silent failures) -----------------------------------------
# Keys `fetch` echoes that are read-only metadata or derived from other data — silently
# ignored on write (a full round-trip includes them; warning on each would be noise).
_META_KEYS = {"id", "type", "trashed", "created", "updated", "completed", "related",
              "referenced by"}
# Keys that carry REAL data written through a different tool, not `write`. Empty now that
# blockers/members are first-class write fields (see _JUNCTION_KEYS); kept as the guard point
# if a field is ever moved back out of write.
_OTHER_TOOL: dict[str, str] = {}
# DB column name a user might type instead of the template key (old insert/update API shape).
# Auto-derived: any FIELD_MAP entry whose column differs from its template key.
_RENAME_HINTS = {kind: {col: key for key, col in m.items() if col != key}
                 for kind, m in FIELD_MAP.items()}


def _detect_anomalies(kind: str, raw: dict) -> list[str]:
    """Report frontmatter keys that `write` would silently drop: unknown keys (typos / old
    column names) and other-tool keys carrying a value. Recognized writable + metadata keys
    stay quiet so a clean round-trip reports nothing."""
    recognized = set(FIELD_MAP[kind]) | _META_KEYS
    if kind in _JUNCTION_KEYS:                 # blockers/members/sources: reconciled, not dropped
        recognized.add(_JUNCTION_KEYS[kind])
    if kind == "group":                        # archived round-trip (set/clear archived_at)
        recognized.add("archived")
    hints = _RENAME_HINTS.get(kind, {})
    warns: list[str] = []
    for key, val in raw.items():
        if key in recognized:
            continue
        if key in _OTHER_TOOL:
            if (val or "").strip():
                warns.append(f"'{key}' is set via {_OTHER_TOOL[key]}, not write — value dropped")
            continue
        hint = f" — did you mean '{hints[key]}'?" if key in hints else ""
        warns.append(f"unknown key '{key}' for {kind}{hint} — value dropped")
    return warns


class BlockError(ValueError):
    """A template block that cannot be parsed / classified."""


def _unquote(v: str) -> str:
    r"""Reverse render._q: unwrap a double-quoted scalar and unescape \\ \" \n."""
    v = v.strip()
    if len(v) >= 2 and v[0] == '"' and v[-1] == '"':
        inner, out, i = v[1:-1], [], 0
        while i < len(inner):
            c = inner[i]
            if c == "\\" and i + 1 < len(inner):
                out.append({"n": "\n", '"': '"', "\\": "\\"}.get(inner[i + 1], inner[i + 1]))
                i += 2
            else:
                out.append(c)
                i += 1
        return "".join(out)
    return v


def _looks_uuid(x: str) -> bool:
    return len(x) == 36 and x.count("-") == 4


def _id_list(value: str | None) -> list[str]:
    """Parse a rendered junction roster into entity ids. Handles every render shape:
    'kind: id — label, ...' (members), 'id — title, ...' (blockers), 'id, ...' (sources) — take
    the first uuid-looking token of each comma segment; kind/label sugar is dropped."""
    ids: list[str] = []
    for seg in (value or "").split(","):
        for tok in seg.replace(":", " ").split():
            if _looks_uuid(tok):
                ids.append(tok)
                break
    return ids


def _split(text: str) -> tuple[list[str], str]:
    """(frontmatter_lines, body). Frontmatter = the region between the first two `---` fences;
    body = everything after the second fence. Only the first two fences delimit frontmatter, so a
    body full of `---` rules or embedded YAML never mis-splits."""
    lines = text.strip().splitlines()
    if not lines or lines[0].strip() != "---":
        raise BlockError("block must start with a --- frontmatter fence")
    close = next((i for i in range(1, len(lines)) if lines[i].strip() == "---"), None)
    if close is None:
        raise BlockError("block frontmatter is not closed with a second ---")
    return lines[1:close], "\n".join(lines[close + 1:]).strip("\n")


def _frontmatter(fm_lines: list[str]) -> dict:
    """Parse `key: value` lines. Keys never contain ':' (render owns them), so partition on the
    FIRST ':' is exact even when the (quoted) value contains one. A duplicate key is rejected, not
    last-wins: a stray second `type:` (e.g. `type: topic` where `grouptype:` was meant) would
    silently overwrite the kind marker, misclassify the block as a page, and fail later with a
    cryptic wrong-table error. Reject early with a clear message instead."""
    raw: dict = {}
    for ln in fm_lines:
        if not ln.strip() or ":" not in ln:
            continue
        key, _, rest = ln.partition(":")
        k = key.strip()
        if k in raw:
            raise BlockError(f"duplicate key '{k}' in block frontmatter")
        raw[k] = _unquote(rest.strip())
    return raw


def classify(raw: dict) -> str:
    t = (raw.get("type") or "").strip()
    return t if t in _MARKERS else "page"


def parse_block(text: str) -> dict:
    """One template block -> {kind, table, id, fields, trashed, warnings}. `fields` are DB columns
    (empty value -> None; ", "-list -> list). Raises BlockError on a malformed block."""
    fm_lines, body = _split(text)
    raw = _frontmatter(fm_lines)
    kind = classify(raw)
    fields: dict = {}
    for key, col in FIELD_MAP[kind].items():
        if key in raw:
            val = raw[key] or None
            if col in _ARRAY_COLS and val is not None:
                val = [p.strip() for p in val.split(",") if p.strip()] or None
            fields[col] = val
    if kind == "header":
        fields["body"] = body or None
    trashed = (raw.get("trashed", "").strip().lower() in ("true", "yes", "1")
               or (kind == "page" and raw.get("freshness") == "trashed"))
    out = {"kind": kind, "table": TABLE[kind], "id": raw.get("id") or None,
           "fields": fields, "trashed": trashed, "warnings": _detect_anomalies(kind, raw)}
    jkey = _JUNCTION_KEYS.get(kind)             # present -> reconcile that junction (empty clears all)
    if jkey and jkey in raw:
        out[jkey] = _id_list(raw[jkey])
    if kind == "group" and "archived" in raw:   # present+value -> set archived_at, present+empty -> clear
        out["archived"] = raw["archived"].strip()
    return out
