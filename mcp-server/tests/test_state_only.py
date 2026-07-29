"""The updated_at rule (E3): a state-only write must bracket its UPDATE with the session flag the
set_updated_at() trigger reads, and a content write must not. Driven with a fake cursor — no DB.

This is the one thing that silently rots: revert any of these call sites to a bare cur.execute()
and everything still "works", except every trash/archive/janitor pass starts bumping updated_at
again and marking rows embed-stale.
"""
import unittest

from kovault_mcp import server as sv

RID = "11111111-1111-1111-1111-111111111111"
ON = "SET LOCAL kovault.state_only = 'on'"
OFF = "SET LOCAL kovault.state_only = 'off'"


def _cols(*names) -> dict:
    """Minimal _cols() reflection stub: names only, no limits/arrays (see test_reflection)."""
    return {n: {"type": "text", "max_len": None, "is_array": False, "is_generated": False}
            for n in names}


class FakeCursor:
    """Records SQL; returns one canned row for SELECT/RETURNING."""

    def __init__(self, row=None):
        self.sql = []
        self.row = row if row is not None else {"id": RID, "title": "T", "trashed_at": None}
        self.rowcount = 1

    def execute(self, sql, params=None):
        self.sql.append(" ".join(sql.split()))

    def fetchone(self):
        return dict(self.row)

    def fetchall(self):
        return []


def _bracketed(cur, needle: str) -> bool:
    """True when the statement containing `needle` sits between an ON and an OFF flag."""
    depth = 0
    for s in cur.sql:
        if s == ON:
            depth += 1
        elif s == OFF:
            depth -= 1
        elif needle in s:
            return depth == 1
    raise AssertionError(f"no statement containing {needle!r} in {cur.sql}")


class TestStateOnlyWrites(unittest.TestCase):
    def setUp(self):
        # prime the column cache so _update_one never reaches for a database
        sv._COLS_CACHE.update({
            "tasks": _cols("id", "title", "description", "lifecycle", "trashed_at"),
            "pages": _cols("id", "title", "summary", "contributors", "lifecycle", "trashed_at"),
        })

    def tearDown(self):
        for t in ("tasks", "pages"):
            sv._COLS_CACHE.pop(t, None)      # don't leak the stub into any other test

    def test_trash_is_state_only(self):
        cur = FakeCursor()
        sv._trash_one(cur, "tasks", RID, "alice", "ai")
        self.assertTrue(_bracketed(cur, "SET trashed_at=now()"))

    def test_revive_is_state_only(self):
        cur = FakeCursor()
        sv._revive_one(cur, "pages", RID, "alice", "ai")
        self.assertTrue(_bracketed(cur, "SET trashed_at=NULL"))

    def test_lifecycle_only_update_is_state_only(self):
        cur = FakeCursor()
        sv._update_one(cur, "tasks", RID, {"lifecycle": "archived"}, "alice", "ai")
        self.assertTrue(_bracketed(cur, "UPDATE tasks SET lifecycle"))

    def test_content_update_still_bumps(self):
        cur = FakeCursor()
        sv._update_one(cur, "tasks", RID, {"title": "new"}, "alice", "ai")
        self.assertFalse(_bracketed(cur, "UPDATE tasks SET title"))
        self.assertNotIn(ON, cur.sql)

    def test_mixed_write_counts_as_content(self):
        # lifecycle changed alongside a real field -> the content edit wins, updated_at bumps
        cur = FakeCursor()
        sv._update_one(cur, "tasks", RID, {"lifecycle": "archived", "title": "new"}, "alice", "ai")
        self.assertNotIn(ON, cur.sql)

    def test_state_only_write_does_not_touch_contributors(self):
        # the contributors append is itself a content UPDATE on pages — it would bump updated_at
        # straight back after the state-only write suppressed it
        cur = FakeCursor(row={"id": RID, "title": "T", "trashed_at": None, "contributors": []})
        sv._update_one(cur, "pages", RID, {"lifecycle": "static"}, "alice", "ai")
        self.assertFalse(any("contributors =" in s or "SET contributors" in s for s in cur.sql))

    def test_flag_is_reset_after_the_block(self):
        cur = FakeCursor()
        with sv._state_only(cur):
            cur.execute("UPDATE x SET y=1")
        self.assertEqual(cur.sql, [ON, "UPDATE x SET y=1", OFF])

    def test_flag_is_skipped_when_off(self):
        cur = FakeCursor()
        with sv._state_only(cur, False):
            cur.execute("UPDATE x SET y=1")
        self.assertEqual(cur.sql, ["UPDATE x SET y=1"])


if __name__ == "__main__":
    unittest.main()
