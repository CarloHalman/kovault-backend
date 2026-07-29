"""A1: per-block outcomes. One bad block must not cost its siblings, and the savepoint that makes
that true must also unwind `_state_only`'s SET LOCAL.

Driven with a fake connection that models the two things that matter: psycopg poisons the whole
transaction after any SQL error, and only ROLLBACK TO SAVEPOINT recovers it.
"""
import unittest
from contextlib import contextmanager

from kovault_mcp import server as sv

PID = "22222222-2222-2222-2222-222222222222"
TID = "11111111-1111-1111-1111-111111111111"


def _col(max_len=None, is_array=False, is_generated=False):
    return {"type": "text", "max_len": max_len, "is_array": is_array, "is_generated": is_generated}


SCHEMA = {
    "tasks": {"id": _col(), "title": _col(64), "description": _col(1024), "lifecycle": _col(),
              "trashed_at": _col(), "status": _col(), "responsible": _col(64, is_array=True)},
    "pages": {"id": _col(), "title": _col(64), "summary": _col(512), "type": _col(32),
              "lifecycle": _col(), "trashed_at": _col(), "contributors": _col(64, is_array=True)},
    "headers": {"id": _col(), "title": _col(64), "blurb": _col(256), "body": _col(),
                "page_id": _col(), "index": _col(), "level": _col(), "path": _col(512),
                "lifecycle": _col(), "trashed_at": _col()},
}

ROW = {"id": TID, "title": "T", "trashed_at": None, "page_id": PID, "index": 0,
       "lifecycle": "live", "contributors": [], "summary": "s", "body": None}


class FakeCursor:
    """Records SQL. `fail_on` raises once on the first statement containing it and then behaves
    like a poisoned psycopg transaction: every further statement errors until ROLLBACK TO."""

    def __init__(self, fail_on=None):
        self.sql: list[str] = []
        self.fail_on = fail_on
        self.poisoned = False
        self.rowcount = 1

    def execute(self, sql, params=None):
        s = " ".join(str(sql).split())
        self.sql.append(s)
        if s.startswith("ROLLBACK TO SAVEPOINT"):
            self.poisoned = False
            return
        if self.poisoned:
            raise RuntimeError("current transaction is aborted, commands ignored until "
                               "end of transaction block")
        if self.fail_on and self.fail_on in s:
            self.fail_on = None
            self.poisoned = True
            raise RuntimeError('duplicate key value violates unique constraint '
                               '"headers_page_id_index_idx" DETAIL: Key (page_id, index) exists.')

    def fetchone(self):
        return dict(ROW)

    def fetchall(self):
        return []


class FakeDB:
    def __init__(self, cur):
        self.cur = cur
        self.committed = False

    @contextmanager
    def connection(self):
        outer = self

        class Conn:
            @contextmanager
            def cursor(self):
                yield outer.cur

            def commit(self):
                outer.committed = True

            def rollback(self):
                outer.committed = False

        yield Conn()

    def query(self, sql, params=None):
        return []


def flag_when(sql: list[str], needle: str) -> str:
    """Replay the statement stream the way Postgres scopes SET LOCAL, and return the effective
    value of kovault.state_only at the first statement containing `needle`. ROLLBACK TO restores
    the value the GUC had when that savepoint was taken — which is the whole reason a failed trash
    block cannot leave the flag stuck on."""
    flag, saved = "", {}
    for s in sql:
        if s.startswith("SAVEPOINT "):
            saved[s.split()[1]] = flag
        elif s.startswith("ROLLBACK TO SAVEPOINT"):
            flag = saved[s.split()[-1]]
        elif s.startswith("SET LOCAL kovault.state_only"):
            flag = s.rsplit("=", 1)[1].strip().strip("'")
        elif needle in s:
            return flag
    raise AssertionError(f"no statement containing {needle!r} in {sql}")


class BatchCase(unittest.TestCase):
    def setUp(self):
        sv._COLS_CACHE.update({t: dict(c) for t, c in SCHEMA.items()})
        self._db = sv._DB

    def tearDown(self):
        for t in SCHEMA:
            sv._COLS_CACHE.pop(t, None)
        sv._DB = self._db

    def run_write(self, blocks, fail_on=None):
        cur = FakeCursor(fail_on)
        sv._DB = FakeDB(cur)
        return sv.write(blocks), cur


TASK = f"---\ntype: task\nid: {TID}\ntitle: edited\n---"
TRASH = f"---\ntype: task\nid: {TID}\ntrashed: true\n---"
HEADER = f"---\ntype: header\npage_id: {PID}\nindex: 0\ntitle: A\n---\nbody"


class TestPerBlockOutcomes(BatchCase):
    def test_sql_failure_does_not_kill_the_rest_of_the_batch(self):
        # block 1 hits the UNIQUE(page_id,index) partial index; block 2 must still be written
        out, cur = self.run_write([TASK, HEADER, TASK], fail_on="INSERT INTO headers")
        lines = out.splitlines()
        self.assertTrue(lines[0].startswith("updated tasks"))
        self.assertIn("(error: block 1 (header): duplicate key", lines[1])
        self.assertNotIn("\n", lines[1])                      # DETAIL folded onto one line
        self.assertTrue(lines[2].startswith("updated tasks"))
        self.assertEqual(lines[-1], "2 committed, 1 failed")
        self.assertTrue(cur.committed if hasattr(cur, "committed") else True)

    def test_failed_block_is_rolled_back_to_its_own_savepoint(self):
        _, cur = self.run_write([TASK, HEADER], fail_on="INSERT INTO headers")
        self.assertIn("RELEASE SAVEPOINT blk0", cur.sql)       # good block: released, not stacked
        self.assertIn("SAVEPOINT blk1", cur.sql)
        self.assertIn("ROLLBACK TO SAVEPOINT blk1", cur.sql)
        self.assertNotIn("RELEASE SAVEPOINT blk1", cur.sql)    # exactly one of the two

    def test_logical_error_also_rolls_its_block_back(self):
        # a dead id never reaches SQL, but the block may already have written (revive/junctions),
        # so an `(error: …)` return unwinds the savepoint just like a raised one
        dead = f"---\ntype: task\nid: {TID}\ntitle: x\n---"
        cur = FakeCursor()
        cur.fetchone = lambda: None                            # _row_state -> no such row
        sv._DB = FakeDB(cur)
        out = sv.write([dead])
        self.assertIn("not found", out)
        self.assertIn("ROLLBACK TO SAVEPOINT blk0", cur.sql)
        self.assertEqual(out.splitlines()[-1], "0 committed, 1 failed")

    def test_parse_failure_fails_alone(self):
        out, _ = self.run_write([TASK, "not a template", TASK])
        lines = out.splitlines()
        self.assertTrue(lines[0].startswith("updated tasks"))
        self.assertTrue(lines[1].startswith("(error: block 1:"))
        self.assertTrue(lines[2].startswith("updated tasks"))
        self.assertEqual(lines[-1], "2 committed, 1 failed")

    def test_validation_failure_fails_alone(self):
        out, _ = self.run_write([f"---\ntype: task\nid: {TID}\ntitle: {'x' * 70}\n---", TASK])
        lines = out.splitlines()
        self.assertEqual(lines[0], "(error: block 0 (task): title is 70 chars, limit is 64)")
        self.assertTrue(lines[1].startswith("updated tasks"))
        self.assertEqual(lines[-1], "1 committed, 1 failed")

    def test_all_blocks_bad_never_opens_a_connection(self):
        sv._DB = None                                          # any DB touch would assert
        out = sv.write(["nope", "also nope"])
        self.assertEqual(out.splitlines()[-1], "0 committed, 2 failed")

    def test_single_clean_write_has_no_summary_line(self):
        out, _ = self.run_write([TASK])
        self.assertEqual(len(out.splitlines()), 1)
        self.assertNotIn("committed", out)

    def test_empty_batch(self):
        sv._DB = None
        self.assertEqual(sv.write([]), "(nothing to write)")


class TestStateOnlyAcrossSavepoints(BatchCase):
    """Decision 5: a trash block that fails must not leave kovault.state_only on for block 2."""

    def test_failed_trash_block_does_not_leak_the_flag(self):
        _, cur = self.run_write([TRASH, TASK], fail_on="SET trashed_at=now()")
        # the savepoint is taken BEFORE _state_only's SET LOCAL, so ROLLBACK TO reverts it
        self.assertLess(cur.sql.index("SAVEPOINT blk0"),
                        cur.sql.index("SET LOCAL kovault.state_only = 'on'"))
        self.assertIn("ROLLBACK TO SAVEPOINT blk0", cur.sql)
        # ... and the next block's content UPDATE therefore runs with the flag clear, so it bumps
        self.assertEqual(flag_when(cur.sql, "UPDATE tasks SET title"), "")
        self.assertNotIn("SET LOCAL kovault.state_only = 'off'", cur.sql)   # never reached

    def test_successful_trash_block_leaves_the_flag_off_for_the_next(self):
        _, cur = self.run_write([TRASH, TASK])
        self.assertEqual(flag_when(cur.sql, "UPDATE tasks SET title"), "off")

    def test_state_write_itself_still_runs_with_the_flag_on(self):
        _, cur = self.run_write([TRASH])
        self.assertEqual(flag_when(cur.sql, "SET trashed_at=now()"), "on")


if __name__ == "__main__":
    unittest.main()
