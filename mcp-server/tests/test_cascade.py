"""E4: archiving a group cascades to the tasks it leaves with no live group.

The membership rule itself is one SQL statement — a fake cursor cannot execute it, so these tests
pin the two things that CAN rot silently: the statement's live-home definition, and the write
discipline around it (state-only, no `doing` flip, one reversible edit row per cascaded task).
"""
import json
import unittest

from kovault_mcp import server as sv

GID = "99999999-9999-9999-9999-999999999999"
T1 = "11111111-1111-1111-1111-111111111111"
T2 = "22222222-2222-2222-2222-222222222222"
ON = "SET LOCAL kovault.state_only = 'on'"
OFF = "SET LOCAL kovault.state_only = 'off'"


class Cursor:
    """Records SQL + params. The cascade UPDATE returns `cascaded`; every other read returns
    `row`, which is what _drop_unchanged compares against."""

    def __init__(self, cascaded=(), row=None):
        self.sql: list[str] = []
        self.params: list = []
        self.cascaded = list(cascaded)
        self.row = row if row is not None else {"id": GID}
        self._out: list = []
        self.rowcount = 1

    def execute(self, sql, params=None):
        s = " ".join(str(sql).split())
        self.sql.append(s)
        self.params.append(params)
        self._out = [{"id": i} for i in self.cascaded] if "UPDATE tasks t SET lifecycle" in s else []

    def fetchall(self):
        return self._out

    def fetchone(self):
        return self._out[0] if self._out else dict(self.row)

    def edits(self) -> list[dict]:
        return [json.loads(p[3]) for s, p in zip(self.sql, self.params)
                if s.startswith("INSERT INTO edits") and p[3]]

    def cascade_sql(self) -> str:
        return next(s for s in self.sql if "UPDATE tasks t SET lifecycle" in s)


class Case(unittest.TestCase):
    def setUp(self):
        sv._ENUM_CACHE["lifecycle_kind"] = {"live", "archived", "static"}
        self._db = sv._DB

    def tearDown(self):
        sv._ENUM_CACHE.pop("lifecycle_kind", None)
        sv._DB = self._db


class TestCascadeStatement(Case):
    def test_live_home_definition(self):
        cur = Cursor()
        sv._cascade_group_archive(cur, GID, "alice")
        sql = cur.cascade_sql()
        self.assertIn("EXISTS (SELECT 1 FROM group_links gl WHERE gl.entity_id = t.id "
                      "AND gl.group_id = %s)", sql)                    # is a member of this group
        self.assertIn("NOT EXISTS", sql)                               # ... and of no live group
        self.assertIn("g.trashed_at IS NULL AND g.lifecycle <> 'archived'", sql)
        self.assertIn("t.lifecycle <> 'archived' AND t.trashed_at IS NULL", sql)
        self.assertEqual(cur.params[cur.sql.index(sql)], (GID,))

    def test_it_is_a_state_only_write(self):
        # archiving is not a content edit: updated_at must not move, and the row must not look
        # changed-this-week to the embed worker
        cur = Cursor(cascaded=[T1])
        sv._cascade_group_archive(cur, GID, "alice")
        i = cur.sql.index(cur.cascade_sql())
        self.assertEqual(cur.sql[i - 1], ON)
        self.assertEqual(cur.sql[i + 1], OFF)

    def test_nothing_flips_to_doing(self):
        cur = Cursor(cascaded=[T1, T2])
        sv._cascade_group_archive(cur, GID, "alice")
        self.assertFalse(any("doing" in s for s in cur.sql))

    def test_one_reversible_edit_row_per_cascaded_task(self):
        cur = Cursor(cascaded=[T1, T2])
        n = sv._cascade_group_archive(cur, GID, "alice")
        self.assertEqual(n, 2)
        rows = [p for s, p in zip(cur.sql, cur.params) if s.startswith("INSERT INTO edits")]
        self.assertEqual([p[1] for p in rows], [T1, T2])            # row_id
        self.assertEqual([p[5] for p in rows], ["script", "script"])  # actor: the server decided
        self.assertEqual([p[4] for p in rows], ["alice", "alice"])    # edited_by: who caused it
        for payload in cur.edits():
            self.assertEqual(payload, {"lifecycle": "archived", "cascaded_from_group": GID})

    def test_nothing_cascaded_logs_nothing(self):
        cur = Cursor(cascaded=[])
        self.assertEqual(sv._cascade_group_archive(cur, GID, "alice"), 0)
        self.assertEqual(cur.edits(), [])


class TestWritePath(Case):
    def write(self, fields, cascaded=(), row=None, rid=GID):
        cur = Cursor(cascaded=cascaded, row=row)
        return sv._write_group(cur, rid, fields, "alice"), cur

    def test_archiving_cascades_and_says_so(self):
        out, cur = self.write({"lifecycle": "archived"}, cascaded=[T1])
        self.assertIn("archived 1 task(s) left with no live group", out)
        self.assertTrue(cur.cascade_sql())

    def test_a_different_lifecycle_change_does_not_cascade(self):
        _, cur = self.write({"lifecycle": "static"})
        self.assertFalse(any("UPDATE tasks" in s for s in cur.sql))

    def test_re_asserting_an_existing_archive_does_not_cascade(self):
        # _drop_unchanged removes an unchanged lifecycle, so only a write that CHANGES it fires
        _, cur = self.write({"lifecycle": "archived"}, row={"id": GID, "lifecycle": "archived"})
        self.assertFalse(any("UPDATE tasks" in s for s in cur.sql))

    def test_a_content_edit_does_not_cascade(self):
        _, cur = self.write({"name": "Migration"})
        self.assertFalse(any("UPDATE tasks" in s for s in cur.sql))

    def test_a_group_created_already_archived_cascades_after_its_members_land(self):
        out, cur = self.write({"name": "G", "lifecycle": "archived"}, cascaded=[T1], rid=None)
        self.assertIn("archived 1 task(s)", out)
        insert = next(s for s in cur.sql if s.startswith("INSERT INTO groups"))
        self.assertLess(cur.sql.index(insert), cur.sql.index(cur.cascade_sql()))


if __name__ == "__main__":
    unittest.main()
