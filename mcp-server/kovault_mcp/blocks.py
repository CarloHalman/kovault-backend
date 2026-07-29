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
    "page":     {"title": "title", "description": "summary", "lifecycle": "lifecycle", "type": "type",
                 "contributors": "contributors"},
    "task":     {"title": "title", "description": "description", "status": "status",
                 "priority": "priority", "scope": "scope", "deadline": "deadline",
                 "responsible": "responsible", "lifecycle": "lifecycle"},
    "decision": {"title": "title", "description": "description", "at": "decided_at",
                 "by": "decided_by", "lifecycle": "lifecycle"},
    "source":   {"title": "title", "description": "summary", "sourcetype": "type",
                 "reference": "reference", "sha256": "sha256", "lifecycle": "lifecycle"},
    "group":    {"name": "name", "description": "description", "grouptype": "type",
                 "participants": "participants", "status": "status", "lifecycle": "lifecycle"},
    "header":   {"title": "title", "blurb": "blurb", "page_id": "page_id",
                 "index": "index", "level": "level", "lifecycle": "lifecycle"},
    "edit":     {},   # audit-log row: no writable fields — write supports only delete (trashed:true)
}
# columns rendered as a ", "-joined list (render._list) -> split back to a list.
_ARRAY_COLS = {"responsible", "participants", "contributors"}

# Junction-table id rosters a `write` block carries and the server reconciles (not FIELD_MAP
# columns): task blockers -> task_dependencies, group members -> group_links, header sources ->
# header_sources. Rendered as an id list; parse keeps the ids and drops any kind/label sugar.
# Key present -> reconcile to that set (empty value clears all); key absent -> leave unchanged.
_JUNCTION_KEYS = {"task": "blockers", "group": "members", "header": "sources"}

# D1: each roster also takes `<key>_add:` / `<key>_remove:` — touch only the named ids, everyone
# else on the roster is left alone (the full `<key>:` form still reconciles to an exact set).
# Adding one id to an 88-member group is one `members_add:` line instead of resending all 88.

# --- anomaly detection (F: no silent failures) -----------------------------------------
# Keys `fetch` echoes that are read-only metadata or derived from other data — silently
# ignored on write (a full round-trip includes them; warning on each would be noise).
# `path` is rebuilt from the page title (_insert_header / _rename_cascade) and `blocking` is the
# reverse of `blockers`, reconciled from the dependent's side — both render, neither is written.
_META_KEYS = {"id", "type", "trashed", "created", "updated", "completed", "related",
              "referenced by", "path", "blocking"}
# Keys that carry REAL data written through a different tool, not `write`. Empty now that
# blockers/members are first-class write fields (see _JUNCTION_KEYS); kept as the guard point
# if a field is ever moved back out of write.
_OTHER_TOOL: dict[str, str] = {}
# DB column name a user might type instead of the template key (old insert/update API shape).
# Auto-derived: any FIELD_MAP entry whose column differs from its template key.
_RENAME_HINTS = {kind: {col: key for key, col in m.items() if col != key}
                 for kind, m in FIELD_MAP.items()}


def template_key(kind: str, col: str) -> str:
    """DB column -> the frontmatter key the model actually typed ('description', not 'summary'), so
    a write-boundary error names what is in front of them. Unmapped columns pass through."""
    return _RENAME_HINTS.get(kind, {}).get(col, col)


def _detect_anomalies(kind: str, raw: dict) -> list[str]:
    """Report frontmatter keys that `write` would silently drop: unknown keys (typos / old
    column names) and other-tool keys carrying a value. Recognized writable + metadata keys
    stay quiet so a clean round-trip reports nothing."""
    recognized = set(FIELD_MAP[kind]) | _META_KEYS
    if kind in _JUNCTION_KEYS:                 # blockers/members/sources (+ _add/_remove, D1): reconciled, not dropped
        jkey = _JUNCTION_KEYS[kind]
        recognized |= {jkey, f"{jkey}_add", f"{jkey}_remove"}
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


_HEX = set("0123456789abcdef")


def _looks_uuid(x: str) -> bool:
    """A full 36-char/4-dash uuid, or a short (C1) hex id prefix — fetch now renders those in a
    roster line (`blockers:`/`members:`/`sources:`), so this must recognize both to parse a
    round-tripped write back. Kept re-free (a plain hex-charset check) like the rest of this
    module; the real id is always the first token in a segment (kind: id — title), so a
    coincidental hex-looking title WORD is never reached — the loop breaks on the id first."""
    if len(x) == 36 and x.count("-") == 4:
        return True
    return 6 <= len(x) <= 32 and set(x.lower()) <= _HEX


def _flow_items(value: str | None) -> list[str]:
    """Split a rendered comma list into trimmed, non-empty items, tolerating a flow-style
    `[a, b, c]` wrapper.

    The brackets are the whole point. render.py never emits them, but they are the form the tool
    docs show and the form a model reaches for, and every consumer here splits on ',' — so without
    stripping them the FIRST and LAST item arrive as '[a' and 'c]'. That silently dropped ids from
    rosters and, worse, stored '[alice' as a person's name in the array columns. One strip, in the
    one place both paths split, so the two cannot drift apart again."""
    return [p.strip() for p in (value or "").strip().strip("[]").split(",") if p.strip()]


def _id_list(value: str | None) -> tuple[list[str], list[str]]:
    """Parse a rendered junction roster into (entity ids, warnings). Handles every render shape:
    'kind: id — label, ...' (members), 'id — title, ...' (blockers), 'id, ...' (sources) — take
    the first uuid-looking token of each comma segment; kind/label sugar is dropped.

    Splitting goes through _flow_items, so `members_add: [a, b, c]` works — see there for what the
    brackets used to cost.

    A segment that carries text but yields no id is reported, never dropped in silence: a supplied
    id that goes nowhere is the caller's bug to see, not ours to swallow."""
    ids: list[str] = []
    warns: list[str] = []
    for seg in _flow_items(value):
        for tok in seg.replace(":", " ").split():
            if _looks_uuid(tok):
                ids.append(tok)
                break
        else:
            warns.append(f"roster entry '{seg}' has no id in it — dropped")
    return ids, warns


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


def _count_extra_templates(body: str) -> int:
    """Concatenated-template guard (bug #1). Every rendered template leads its frontmatter with a
    `type:` line, so a `---` fence inside `body` whose next non-blank line is a `type:` key marks
    another `---`-fenced template joined into this one array element — `_split` only parses the
    first, and the rest would be silently swallowed into this body and lost. Counts those; a plain
    `---` body rule (next non-blank line is prose) is not one.
    ponytail: `type:`-after-fence heuristic, not full fence-pair validation — a header body that
    literally documents `---`/`type:` lines would false-positive; upgrade to a real fence-pair scan
    if that ever bites."""
    lines = body.splitlines()
    extra = 0
    for i, ln in enumerate(lines):
        if ln.strip() != "---":
            continue
        nxt = next((l for l in lines[i + 1:] if l.strip()), "")   # first non-blank after the fence
        if nxt.split(":", 1)[0].strip() == "type":
            extra += 1
    return extra


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
    """One template block -> {kind, table, id, fields, trashed, revive?, warnings}. `fields` are DB columns
    (empty value -> None; ", "-list -> list). Raises BlockError on a malformed block."""
    fm_lines, body = _split(text)
    extra = _count_extra_templates(body)
    if extra:
        raise BlockError(
            f"this block concatenates {extra + 1} templates — pass one '---'-fenced template per "
            "blocks[] element, not several joined in one string (only the first would be written)")
    raw = _frontmatter(fm_lines)
    kind = classify(raw)
    jkey = _JUNCTION_KEYS.get(kind)
    add_key = rem_key = None
    if jkey:
        add_key, rem_key = f"{jkey}_add", f"{jkey}_remove"
        deltas = [k for k in (add_key, rem_key) if k in raw]
        if jkey in raw and deltas:      # D1 decision 1: both forms is an error, not a merge
            raise BlockError(
                f"block has both '{jkey}' (full roster) and {' and '.join(deltas)} (delta) for "
                "the same roster — send one form, not both")
    fields: dict = {}
    for key, col in FIELD_MAP[kind].items():
        if key in raw:
            val = raw[key] or None
            if col in _ARRAY_COLS and val is not None:
                # Same splitter as the id rosters: `participants: [alice, bob]` used to store
                # '[alice' and 'bob]' as literal names — silent corruption, not a dropped value.
                val = _flow_items(val) or None
            fields[col] = val
    body_warns: list[str] = []
    if kind == "header":
        fields["body"] = body or None
    elif kind != "page" and body.strip():
        # A chunk STORES its body; a page renders its document (title + chunks) after the fence and
        # legitimately round-trips with one, so neither warns. Every other kind renders an empty
        # post-fence region and carries its prose in a frontmatter field instead.
        # Text after the fence used to be discarded without a word, so a task written with its
        # detail in the body inserted with description NULL and still reported `inserted` — content
        # supplied, success reported, content gone. Unknown KEYS already warn; this is the same
        # mistake one line further down, and it costs the whole description, not one field.
        tail = " — put it in 'description:' instead" if "description" in FIELD_MAP[kind] else ""
        body_warns.append(
            f"text after the closing --- is a body only on a header, not on a {kind}{tail} — dropped")
    # `trashed:` is tri-state and identical for every kind (E3): absent -> leave the row's trash
    # state alone; truthy -> trash it; present-but-empty/false -> revive it (clear trashed_at).
    # So an unarchive/untrash is just fetch -> clear the line -> write, no separate tool.
    tv = raw.get("trashed")
    trashed = tv is not None and tv.strip().lower() in ("true", "yes", "1")
    out = {"kind": kind, "table": TABLE[kind], "id": raw.get("id") or None,
           "fields": fields, "trashed": trashed,
           "warnings": _detect_anomalies(kind, raw) + body_warns}
    if tv is not None and not trashed:
        out["revive"] = True
    if jkey and jkey in raw:                    # full roster: present -> reconcile (empty clears all)
        out[jkey], w = _id_list(raw[jkey])
        out["warnings"] += w
    elif jkey:
        # delta (D1): _add/_remove only touch the named ids, everyone else on the roster is left
        # alone — the opposite default of the full roster's empty value, which meaningfully clears
        # everything. So an empty delta value is almost certainly a mistake, not "touch nothing" —
        # warn instead of silently no-op'ing (kept asymmetric from the full-roster case on purpose).
        for dkey in (add_key, rem_key):
            if dkey in raw:
                if not (raw[dkey] or "").strip():
                    out["warnings"].append(f"'{dkey}' was empty — no-op")
                out[dkey], w = _id_list(raw[dkey])
                out["warnings"] += w
    return out
