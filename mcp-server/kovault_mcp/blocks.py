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

_MARKERS = {"task", "decision", "source", "group", "header"}

TABLE = {"page": "pages", "task": "tasks", "decision": "decisions",
         "source": "sources", "group": "groups", "header": "headers"}

# rendered frontmatter key -> DB column, per kind. Keys absent here are read-only on write
# (created/updated/related/blockers/referenced by/contributors/members). id/type/trashed are
# handled separately; header `body` comes from the post-fence region, not a frontmatter key.
FIELD_MAP = {
    "page":     {"title": "title", "description": "summary", "freshness": "freshness", "type": "type"},
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
}
# columns rendered as a ", "-joined list (render._list) -> split back to a list.
_ARRAY_COLS = {"responsible", "participants"}


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
    FIRST ':' is exact even when the (quoted) value contains one."""
    raw: dict = {}
    for ln in fm_lines:
        if not ln.strip() or ":" not in ln:
            continue
        key, _, rest = ln.partition(":")
        raw[key.strip()] = _unquote(rest.strip())
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
    return {"kind": kind, "table": TABLE[kind], "id": raw.get("id") or None,
            "fields": fields, "trashed": trashed, "warnings": []}
