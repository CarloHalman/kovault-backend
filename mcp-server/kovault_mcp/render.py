"""Render DB rows into the fetch output format.

The same format the no-AI export reuses (export.py). Pure module — takes plain dicts/lists,
returns text; the server supplies the row data (and related-link / member lists).
"""
from __future__ import annotations

from datetime import date, datetime


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


def _list(v) -> str:
    if not v:
        return ""
    return ", ".join(str(x) for x in v)


def _related(links: list[tuple[str, str]]) -> str:
    """[(to_kind, to_id), ...] -> 'kind:id, kind:id'."""
    return ", ".join(f"{k}:{i}" for k, i in (links or []))


def render_chunk(h: dict, standalone: bool = True) -> str:
    """A chunk = its title then its body. Navigation lives in the body itself as inline
    [text](kind:uuid) links (no separate Related line, no per-chunk summary, no header id).
    A standalone chunk keeps a one-line locator so a lone chunk still knows its home page."""
    lines = [h.get("title") or "(intro)"]
    if standalone:
        lines.append(f"> page: {h.get('page_id')} · index: {h.get('index')}")
    lines.append("")                      # blank line so a body starting with a table renders
    lines.append(h.get("body") or "")
    return "\n".join(lines).rstrip() + "\n"


def render_page(page: dict, headers: list[dict]) -> str:
    """Full page: frontmatter + title + every live header (title + body) in index order.
    Inline [text](kind:uuid) links in the bodies carry the graph navigation."""
    fm = [
        "---",
        f"type: {page.get('type') or ''}",
        f"title: {_q(page.get('title') or '')}",
        f"id: {page.get('id')}",
        f"description: {_q(page.get('summary') or '')}",
        f"created: {_ts(page.get('created_at'))}",
        f"updated: {_ts(page.get('updated_at'))}",
        f"freshness: {page.get('freshness') or ''}",
        f"contributors: {_q(_list(page.get('contributors')))}",
        "---",
        page.get("title") or "",
        "",
    ]
    body = [render_chunk(h, standalone=False) for h in headers]
    return "\n".join(fm) + "\n".join(body)


def render_task(t: dict, blockers: list[str] | None = None, links: list[tuple[str, str]] | None = None) -> str:
    return "\n".join([
        "---",
        "type: task",
        f"title: {_q(t.get('title') or '')}",
        f"id: {t.get('id')}",
        f"description: {_q(t.get('description') or '')}",
        f"created: {_ts(t.get('created_at'))}",
        f"updated: {_ts(t.get('updated_at'))}",
        f"status: {t.get('status') or ''}",
        f"priority: {t.get('priority') or ''}",
        f"scope: {t.get('scope') or ''}",
        f"deadline: {_ts(t.get('deadline'))}",
        f"completed: {_ts(t.get('completed_at'))}",
        f"responsible: {_q(_list(t.get('responsible')))}",
        f"blockers: {_q(_list(blockers))}",
        f"related: {_q(_related(links))}",
        "---",
    ]) + "\n"


def render_decision(d: dict, links: list[tuple[str, str]] | None = None) -> str:
    return "\n".join([
        "---",
        "type: decision",
        f"title: {_q(d.get('title') or '')}",
        f"id: {d.get('id')}",
        f"description: {_q(d.get('description') or '')}",
        f"created: {_ts(d.get('created_at'))}",
        f"updated: {_ts(d.get('updated_at'))}",
        f"at: {_ts(d.get('decided_at'))}",
        f"by: {_q(d.get('decided_by') or '')}",
        f"related: {_q(_related(links))}",
        "---",
    ]) + "\n"


def render_source(s: dict, referenced_by: list[str] | None = None) -> str:
    return "\n".join([
        "---",
        "type: source",
        f"sourcetype: {s.get('type') or ''}",
        f"title: {_q(s.get('title') or '')}",
        f"reference: {_q(s.get('reference') or '')}",
        f"id: {s.get('id')}",
        f"description: {_q(s.get('summary') or '')}",
        f"created: {_ts(s.get('created_at'))}",
        f"updated: {_ts(s.get('updated_at'))}",
        f"sha256: {s.get('sha256') or ''}",
        f"referenced by: {_q(_list(referenced_by))}",
        "---",
    ]) + "\n"


def render_group(g: dict, members: list[tuple[str, str, str]] | None = None,
                 ids_only: bool = False) -> str:
    """members = [(entity_kind, id, title/name), ...] from group_links via snippet data.
    ids_only drops the ` — label` (a large roster's labels cost tokens; fetch ids for detail)."""
    if ids_only:
        member_str = ", ".join(f"{k}: {i}" for k, i, _ in (members or []))
    else:
        member_str = ", ".join(f"{k}: {i} — {label}" for k, i, label in (members or []))
    return "\n".join([
        "---",
        "type: group",
        f"grouptype: {g.get('type') or ''}",
        f"name: {_q(g.get('name') or '')}",
        f"id: {g.get('id')}",
        f"description: {_q(g.get('description') or '')}",
        f"participants: {_q(_list(g.get('participants')))}",
        f"members: {_q(member_str)}",
        "---",
    ]) + "\n"
