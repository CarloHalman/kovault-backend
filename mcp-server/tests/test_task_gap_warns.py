"""E6+E7: _task_gap_warns, the one warning pass alongside _similar_task_warn on a task insert.
Warns only, never blocks. Direct unit tests drive it with a minimal fake cursor (no DB); one
integration test goes through the real `write()` batch path to confirm the warnings survive A1's
per-block rendering (indented continuation lines, never mistaken for `(error: ...)`).
"""
import unittest
from contextlib import contextmanager

from kovault_mcp import server as sv

PID = "22222222-2222-2222-2222-222222222222"
DID = "33333333-3333-3333-3333-333333333333"
TID = "11111111-1111-1111-1111-111111111111"


class FakeHintCursor:
    """cur.execute just records the call; cur.fetchall returns a canned probe hit (or none) —
    _task_gap_warns uses fetchall (LIMIT 1 already caps it), matching _similar_task_warn's mechanism."""

    def __init__(self, hit=None):
        self.hit = hit
        self.sql: list[str] = []

    def execute(self, sql, params=None):
        self.sql.append(" ".join(str(sql).split()))

    def fetchall(self):
        return [self.hit] if self.hit else []


class TestE6UnlinkedDescription(unittest.TestCase):
    def test_no_reference_and_a_hit_warns_naming_it(self):
        cur = FakeHintCursor({"kind": "page", "id": PID, "title": "Deploy Guide", "sim": 0.42})
        warns = sv._task_gap_warns(cur, {"title": "fix deploy", "description": "just do it"})
        self.assertTrue(any("Deploy Guide" in w and PID in w for w in warns))
        self.assertTrue(cur.sql)   # the probe ran

    def test_no_reference_and_no_hit_is_quiet(self):
        cur = FakeHintCursor(None)
        warns = sv._task_gap_warns(cur, {"title": "fix deploy", "description": "just do it"})
        self.assertEqual(warns, [])

    def test_base_link_present_stays_quiet_and_skips_the_probe(self):
        cur = FakeHintCursor({"kind": "page", "id": PID, "title": "X", "sim": 0.9})
        warns = sv._task_gap_warns(
            cur, {"title": "fix deploy", "description": f"see [guide](page:{PID})"})
        self.assertEqual(warns, [])
        self.assertEqual(cur.sql, [])   # probe never ran

    def test_wikilink_present_stays_quiet_and_skips_the_probe(self):
        cur = FakeHintCursor({"kind": "page", "id": PID, "title": "X", "sim": 0.9})
        warns = sv._task_gap_warns(cur, {"title": "fix deploy", "description": "see [[Deploy Guide]]"})
        self.assertEqual(warns, [])
        self.assertEqual(cur.sql, [])

    def test_no_title_skips_the_probe(self):
        cur = FakeHintCursor({"kind": "page", "id": PID, "title": "X", "sim": 0.9})
        warns = sv._task_gap_warns(cur, {"title": "", "description": ""})
        self.assertEqual(warns, [])
        self.assertEqual(cur.sql, [])


class TestE7UnplannedOwner(unittest.TestCase):
    def test_planned_without_responsible_warns(self):
        for extra in ({"deadline": "2026-08-01"}, {"scope": "2 weeks"}, {"priority": "high"}):
            cur = FakeHintCursor(None)
            fields = {"title": "x", "description": f"[y](task:{DID})", **extra}
            warns = sv._task_gap_warns(cur, fields)
            self.assertTrue(any("no responsible named" in w for w in warns), extra)

    def test_bare_task_with_none_of_those_is_quiet(self):
        cur = FakeHintCursor(None)
        warns = sv._task_gap_warns(cur, {"title": "x", "description": f"[y](task:{DID})"})
        self.assertEqual(warns, [])

    def test_planned_with_responsible_is_quiet(self):
        cur = FakeHintCursor(None)
        fields = {"title": "x", "description": f"[y](task:{DID})", "deadline": "2026-08-01",
                  "responsible": ["alice"]}
        self.assertEqual(sv._task_gap_warns(cur, fields), [])

    def test_both_warnings_can_fire_on_the_same_block(self):
        cur = FakeHintCursor({"kind": "decision", "id": DID, "title": "Pick B", "sim": 0.5})
        warns = sv._task_gap_warns(
            cur, {"title": "x", "description": "no ref here", "deadline": "2026-08-01"})
        self.assertEqual(len(warns), 2)


# ---- integration: through write(), warnings must not read as failure (decision 7) -------------

class FakeCursor:
    def __init__(self):
        self.sql: list[str] = []

    def execute(self, sql, params=None):
        self.sql.append(" ".join(str(sql).split()))

    def fetchone(self):
        return {"id": TID}

    def fetchall(self):
        return []


class FakeDB:
    def __init__(self, cur):
        self.cur = cur

    @contextmanager
    def connection(self):
        outer = self

        class Conn:
            @contextmanager
            def cursor(self):
                yield outer.cur

            def commit(self):
                pass

        yield Conn()

    def query(self, sql, params=None):
        return []


class TestWarningsSurviveWrite(unittest.TestCase):
    def setUp(self):
        sv._COLS_CACHE["tasks"] = {
            "id": {"type": "uuid", "max_len": None, "is_array": False, "is_generated": False},
            "title": {"type": "character varying", "max_len": 64, "is_array": False, "is_generated": False},
            "description": {"type": "character varying", "max_len": 1024, "is_array": False, "is_generated": False},
        }
        self._db = sv._DB

    def tearDown(self):
        sv._COLS_CACHE.pop("tasks", None)
        sv._DB = self._db

    def test_planned_task_inserts_with_the_e7_warning_not_an_error(self):
        # no responsible + a deadline (E7). Description carries a link so E6's probe never runs —
        # FakeCursor.fetchone always returns a bare {"id": ...} (what _new_entity/_target_live
        # need), which isn't shaped like an E6 hit; the point here is only that a warning line
        # survives A1's per-block rendering without being read as a failure.
        cur = FakeCursor()
        sv._DB = FakeDB(cur)
        block = (f"---\ntype: task\ntitle: ship it\ndescription: see [it](task:{DID})\n"
                 f"deadline: 2026-08-01\n---")
        out = sv.write([block])
        self.assertTrue(out.startswith("inserted tasks"))
        self.assertFalse(sv._failed(out))
        self.assertIn("no responsible named", out)


if __name__ == "__main__":
    unittest.main()
