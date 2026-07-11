"""Markdown-link parsing for the graph.

The model just writes normal markdown; on insert/update the server parses the body/
description for links of the form `[text](<kind>:<uuid>)` (kinds: page/header/task/decision/
source) and diffs them into the `links` edge table. Plain http(s) links stay text — they
never become edges. Pure module (stdlib `re` only) — unit-tested without a database.
"""
from __future__ import annotations

import re

KINDS = ("page", "header", "task", "decision", "source")

# [anything](kind:uuid) — uuid validated loosely (8-4-4-4-12 hex); kind constrained.
_LINK_RE = re.compile(
    r"\[[^\]]*\]\(\s*(?P<kind>page|header|task|decision|source)\s*:\s*"
    r"(?P<id>[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12})\s*\)",
    re.IGNORECASE,
)


def parse_links(text: str | None) -> set[tuple[str, str]]:
    """Return the set of (kind, uuid) targets referenced by markdown links in `text`.

    kind is lower-cased; uuid is lower-cased. Deduped. Empty/None -> empty set.
    """
    if not text:
        return set()
    out: set[tuple[str, str]] = set()
    for m in _LINK_RE.finditer(text):
        out.add((m.group("kind").lower(), m.group("id").lower()))
    return out


def diff_links(
    old: set[tuple[str, str]], new: set[tuple[str, str]]
) -> tuple[set[tuple[str, str]], set[tuple[str, str]]]:
    """(to_add, to_remove) for syncing the links table from parsed body text."""
    return new - old, old - new


# ---------------------------------------------------------------------------------------
# Obsidian wiki-links. Base links are [text](kind:uuid); Obsidian notes instead use
# [[Title]] / [[Title|alias]] / [[Title#heading]], which reference by TITLE. When a body is
# mostly Obsidian links (see obsidian_ratio), the server resolves those titles to entity ids
# and rewrites them into base links so they graph (database-interaction.md → Automatic linking).
# ---------------------------------------------------------------------------------------
_OBSIDIAN_RE = re.compile(
    r"\[\[\s*(?P<target>[^\]|#\r\n]+?)\s*(?:#[^\]|\r\n]+)?\s*(?:\|\s*(?P<alias>[^\]\r\n]+?)\s*)?\]\]"
)


def parse_obsidian_links(text: str | None) -> list[tuple[str, str, str | None]]:
    """Return [(raw, target_title, alias_or_None)] for each [[...]] link, in order.

    Instances (not deduped) — the ratio counts occurrences. `#heading` is dropped from the
    resolution target; `raw` is the exact substring to replace when converting.
    """
    if not text:
        return []
    out: list[tuple[str, str, str | None]] = []
    for m in _OBSIDIAN_RE.finditer(text):
        out.append((m.group(0), m.group("target").strip(),
                    (m.group("alias") or "").strip() or None))
    return out


def base_link_count(text: str | None) -> int:
    """Count of base [text](kind:uuid) link instances (not unique)."""
    if not text:
        return 0
    return sum(1 for _ in _LINK_RE.finditer(text))


def obsidian_ratio(text: str | None) -> float:
    """Fraction of a body's entity links that are Obsidian [[...]] rather than base links.

    0.0 when there are no links of either kind. The server converts when this exceeds a
    threshold (default 20%), i.e. the note is authored in Obsidian's linking style.
    """
    obs = len(parse_obsidian_links(text))
    total = obs + base_link_count(text)
    return (obs / total) if total else 0.0
