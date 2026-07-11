"""Timestamp wording for embedding text.

Rule: timestamps are written as words first, e.g. `10-12-2020 21:20` -> `10th of December
2020 21:20`. Deterministic so /janitor -embed reproduces the exact same text from a row.
Pure module (stdlib only) — unit-tested without a database.
"""
from __future__ import annotations

from datetime import date, datetime

_MONTHS = [
    "January", "February", "March", "April", "May", "June",
    "July", "August", "September", "October", "November", "December",
]


def ordinal(n: int) -> str:
    """1 -> '1st', 2 -> '2nd', 3 -> '3rd', 4 -> '4th', 11..13 -> '*th', 21 -> '21st'."""
    if 10 <= (n % 100) <= 20:
        suffix = "th"
    else:
        suffix = {1: "st", 2: "nd", 3: "rd"}.get(n % 10, "th")
    return f"{n}{suffix}"


def _coerce(value) -> datetime | date | None:
    """Accept a datetime/date, an ISO-8601 string, or None."""
    if value is None or value == "":
        return None
    if isinstance(value, (datetime, date)):
        return value
    if isinstance(value, str):
        try:
            return datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
    return None


def words_datetime(value) -> str:
    """'10th of December 2020 21:20'. Time part appended only when non-midnight.

    Returns '' for None/unparseable so null fields drop out of composed embedding text.
    """
    dt = _coerce(value)
    if dt is None:
        return ""
    day = ordinal(dt.day)
    words = f"{day} of {_MONTHS[dt.month - 1]} {dt.year}"
    if isinstance(dt, datetime) and (dt.hour or dt.minute):
        words += f" {dt.hour:02d}:{dt.minute:02d}"
    return words
