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
    lines.append(h.get("body") or "")
    return "\n".join(lines).rstrip() + "\n"


def render_page(page: dict, headers: list[dict]) -> str:
    """Full page: frontmatter + title + every live header (title + body) in index order.
    Inline [text](kind:uuid) links in the bodies carry the graph navigation."""
    fm = [
        "---",
        f"type: {page.get('type') or ''}",
        f"title: {page.get('title') or ''}",
        f"id: {page.get('id')}",
        f"description: {page.get('summary') or ''}",
        f"created: {_ts(page.get('created_at'))}",
        f"updated: {_ts(page.get('updated_at'))}",
        f"freshness: {page.get('freshness') or ''}",
        f"contributors: {_list(page.get('contributors'))}",
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
        f"title: {t.get('title') or ''}",
        f"id: {t.get('id')}",
        f"description: {t.get('description') or ''}",
        f"created: {_ts(t.get('created_at'))}",
        f"updated: {_ts(t.get('updated_at'))}",
        f"status: {t.get('status') or ''}",
        f"priority: {t.get('priority') or ''}",
        f"scope: {t.get('scope') or ''}",
        f"deadline: {_ts(t.get('deadline'))}",
        f"responsible: {_list(t.get('responsible'))}",
        f"blockers: {_list(blockers)}",
        f"related: {_related(links)}",
        "---",
    ]) + "\n"


def render_decision(d: dict, links: list[tuple[str, str]] | None = None) -> str:
    return "\n".join([
        "---",
        "type: decision",
        f"title: {d.get('title') or ''}",
        f"id: {d.get('id')}",
        f"description: {d.get('description') or ''}",
        f"created: {_ts(d.get('created_at'))}",
        f"updated: {_ts(d.get('updated_at'))}",
        f"at: {_ts(d.get('decided_at'))}",
        f"by: {d.get('decided_by') or ''}",
        f"related: {_related(links)}",
        "---",
    ]) + "\n"


def render_source(s: dict, referenced_by: list[str] | None = None) -> str:
    return "\n".join([
        "---",
        "type: source",
        f"sourcetype: {s.get('type') or ''}",
        f"title: {s.get('title') or ''}",
        f"reference: {s.get('reference') or ''}",
        f"id: {s.get('id')}",
        f"description: {s.get('summary') or ''}",
        f"created: {_ts(s.get('created_at'))}",
        f"updated: {_ts(s.get('updated_at'))}",
        f"sha256: {s.get('sha256') or ''}",
        f"referenced by: {_list(referenced_by)}",
        "---",
    ]) + "\n"


def render_group(g: dict, members: list[tuple[str, str, str]] | None = None) -> str:
    """members = [(entity_kind, id, title/name), ...] from group_links via snippet data."""
    member_str = ", ".join(f"{k}: {i} — {label}" for k, i, label in (members or []))
    return "\n".join([
        "---",
        "type: group",
        f"grouptype: {g.get('type') or ''}",
        f"name: {g.get('name') or ''}",
        f"id: {g.get('id')}",
        f"description: {g.get('description') or ''}",
        f"participants: {_list(g.get('participants'))}",
        f"members: {member_str}",
        "---",
    ]) + "\n"
