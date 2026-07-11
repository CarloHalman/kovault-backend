"""Deterministic embedding-text composition.

The insert/update scripts compose the embedding text from a row's OWN fields (no free-form
LLM keywords), so /janitor -embed reproduces the exact same vector. Null fields are skipped;
timestamps are worded via timestamps.words_datetime. Pure module — unit-tested without deps.

  header   -> path + blurb + body            (path = 'page.title > H1 > H2 > H3', stored on row)
  source   -> 'source: <type>' + title + summary        (metadata only, never file contents)
  task     -> 'task:'  + title + status + priority + scope + deadline + description
  decision -> 'decision:' + title + decided_by + decided_at + description
"""
from __future__ import annotations

from .timestamps import words_datetime


def _join(parts) -> str:
    """Join non-empty parts with newlines, skipping null/blank fields."""
    return "\n".join(str(p).strip() for p in parts if p is not None and str(p).strip())


def compose_header_text(row: dict) -> str:
    return _join([row.get("path"), row.get("blurb"), row.get("body")])


def compose_source_text(row: dict) -> str:
    head = f"source: {row['type']}" if row.get("type") else "source:"
    return _join([head, row.get("title"), row.get("summary")])


def compose_task_text(row: dict) -> str:
    return _join([
        "task:",
        row.get("title"),
        row.get("status"),
        row.get("priority"),
        row.get("scope"),
        words_datetime(row.get("deadline")),
        row.get("description"),
    ])


def compose_decision_text(row: dict) -> str:
    return _join([
        "decision:",
        row.get("title"),
        row.get("decided_by"),
        words_datetime(row.get("decided_at")),
        row.get("description"),
    ])


# table name -> (composer, embedding column). The searchable set.
COMPOSERS = {
    "headers":   (compose_header_text,   "embedding"),
    "sources":   (compose_source_text,   "summary_embedding"),
    "tasks":     (compose_task_text,     "embedding"),
    "decisions": (compose_decision_text, "embedding"),
}


def compose(table: str, row: dict) -> str:
    """Compose embedding text for a searchable table's row."""
    composer, _col = COMPOSERS[table]
    return composer(row)
