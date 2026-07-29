"""E5: a todo task moves to doing when someone edits what it SAYS — and never when a maintenance
pass touches it. The gates are the point; the flip itself is one guarded UPDATE.
"""
import unittest

from kovault_mcp import server as sv
from tests.test_people import ScriptedCursor
from tests.test_write_batch import SCHEMA, FakeCursor, FakeDB

TID = "11111111-1111-1111-1111-111111111111"
FLIP = "UPDATE tasks SET status='doing' WHERE id=%s AND status='todo'"


class ParamCursor(FakeCursor):
    """FakeCursor that also keeps the bound params (the edit log's payload)."""

    def __init__(self):
        super().__init__()
        self.calls: list[tuple] = []

    def execute(self, sql, params=None):
        self.calls.append((" ".join(str(sql).split()), params))
        super().execute(sql, params)


class Case(unittest.TestCase):
    def setUp(self):
        sv._COLS_CACHE.update({t: dict(c) for t, c in SCHEMA.items()})
        self._db = sv._DB

    def tearDown(self):
        for t in SCHEMA:
            sv._COLS_CACHE.pop(t, None)
        sv._DB = self._db

    def update(self, table="tasks", fields=None, actor="ai", written=None):
        cur = FakeCursor()
        res = sv._update_one(cur, table, TID, fields or {}, "alice", actor, written=written)
        return res, cur

    def flipped(self, cur) -> bool:
        return any(s == FLIP for s in cur.sql)


class TestFlips(Case):
    def test_description_edit_flips_and_reports(self):
        res, cur = self.update(fields={"description": "now doing this"})
        self.assertTrue(self.flipped(cur))
        self.assertEqual(res[2], ["status todo->doing (task edited)"])

    def test_title_edit_flips(self):
        _, cur = self.update(fields={"title": "renamed"})
        self.assertTrue(self.flipped(cur))

    def test_flip_is_recorded_in_the_edit_log(self):
        cur = ParamCursor()
        sv._update_one(cur, "tasks", TID, {"description": "d"}, "alice", "ai")
        edit = next(p for s, p in cur.calls if s.startswith("INSERT INTO edits"))
        self.assertIn('"status": "doing"', edit[3])   # an unlogged status change is invisible

    def test_no_flip_when_the_task_is_not_todo(self):
        # the WHERE status='todo' guard does the work; rowcount 0 -> nothing reported
        cur = FakeCursor()
        cur.rowcount = 0
        res = sv._update_one(cur, "tasks", TID, {"description": "d"}, "alice", "ai")
        self.assertTrue(self.flipped(cur))          # statement still issued (atomic, no read first)
        self.assertEqual(res[2], [])                # but nothing changed, so nothing is claimed


class TestGates(Case):
    def test_triage_fields_do_not_flip(self):
        for field in ({"priority": "high"}, {"scope": "2 weeks"}, {"deadline": "2026-01-01"},
                      {"responsible": ["alice"]}, {"lifecycle": "static"}):
            _, cur = self.update(fields=field)
            self.assertFalse(self.flipped(cur), field)

    def test_explicit_status_always_wins(self):
        _, cur = self.update(fields={"description": "d", "status": "todo"})
        self.assertFalse(self.flipped(cur))

    def test_echoed_status_from_a_round_trip_still_counts_as_explicit(self):
        # fetch renders `status:` on every task, so a full round trip carries it even when
        # unchanged; _drop_unchanged removes it from the write, `written` remembers it was said
        _, cur = self.update(fields={"description": "d"}, written={"description", "status"})
        self.assertFalse(self.flipped(cur))

    def test_script_actor_never_flips(self):
        _, cur = self.update(fields={"description": "d"}, actor="script")
        self.assertFalse(self.flipped(cur))

    def test_state_only_write_never_flips(self):
        _, cur = self.update(fields={"trashed_at": None})
        self.assertFalse(self.flipped(cur))

    def test_other_tables_are_untouched(self):
        _, cur = self.update(table="pages", fields={"title": "renamed"})
        self.assertFalse(self.flipped(cur))

    def test_gate_helper_is_the_single_decision_point(self):
        cur = FakeCursor()
        for kwargs in (dict(table="pages"), dict(actor="script"), dict(state_only=True),
                       dict(written={"status"}), dict(fieldset={"priority": "high"})):
            args = dict(table="tasks", rid=TID, fieldset={"description": "d"}, written=set(),
                        actor="ai", state_only=False)
            args.update(kwargs)
            self.assertEqual(sv._auto_doing(cur, **args), [], kwargs)


class TestThroughWrite(Case):
    def _write(self, blocks):
        cur = FakeCursor()
        sv._DB = FakeDB(cur)
        return sv.write(blocks), cur

    def test_note_lands_on_the_block_line(self):
        out, cur = self._write([f"---\ntype: task\nid: {TID}\ndescription: real work\n---"])
        self.assertTrue(self.flipped(cur))
        self.assertIn("[status todo->doing (task edited)]", out)
        self.assertEqual(len(out.splitlines()), 1)      # on the line, not a continuation

    def test_insert_never_flips(self):
        out, cur = self._write(["---\ntype: task\ntitle: brand new\ndescription: d\n---"])
        self.assertFalse(self.flipped(cur))
        self.assertIn("inserted tasks", out)

    def test_full_round_trip_block_does_not_flip(self):
        block = (f"---\ntype: task\nid: {TID}\ntitle: T\ndescription: rewritten\n"
                 f"status: todo\npriority: high\n---")
        _, cur = self._write([block])
        self.assertFalse(self.flipped(cur))


class TestMaintenancePassesNeverFlip(Case):
    """The clause the review rated worst: no janitor pass may mass-flip a backlog."""

    def test_normalize_people_issues_no_status_write(self):
        cur = ScriptedCursor([
            [{"key": "alice", "name": "alice"}],
            [], [{"id": TID, "v": ["Alice"]}], [], [],
        ])
        sv._janitor_normalize_people(cur, "janitor")
        self.assertTrue(cur.updates)                                  # it did work ...
        self.assertFalse(any("status" in s for s, _ in cur.updates))  # ... but never status


if __name__ == "__main__":
    unittest.main()
