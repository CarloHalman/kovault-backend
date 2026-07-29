"""D1: _sync_junction's delta mode (add_ids/remove_ids) beside its original full-roster mode.

Driven with a minimal fake cursor that models one junction table in memory: fetchall() returns the
live roster, INSERT/DELETE mutate it. No DB — this pins the set-diff logic itself.
"""
import unittest

from kovault_mcp import server as sv

A = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
B = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"
C = "cccccccc-cccc-4ccc-8ccc-cccccccccccc"


class FakeJunctionCursor:
    """Models a two-column junction (fixed_id is constant across a test): `roster` is the live set
    of `other_col` values. `missing` names ids that fail the exists_table check."""

    def __init__(self, roster=(), missing=()):
        self.roster = set(roster)
        self.missing = set(missing)
        self.sql: list[str] = []
        self._exists = True

    def execute(self, sql, params=None):
        s = " ".join(str(sql).split())
        self.sql.append(s)
        if s.startswith("SELECT 1 FROM"):
            self._exists = params[0] not in self.missing
        elif s.startswith("INSERT INTO"):
            self.roster.add(params[1])
        elif s.startswith("DELETE FROM"):
            self.roster.discard(params[1])

    def fetchone(self):
        return {"x": 1} if self._exists else None

    def fetchall(self):
        return [{"entity_id": e} for e in self.roster]


def sync(cur, **kw):
    return sv._sync_junction(cur, "group_links", "group_id", "G", "entity_id",
                             exists_table="entities", **kw)


class TestFullRosterModeUnchanged(unittest.TestCase):
    """Regression guard: passing new_ids still reconciles to exactly that set."""

    def test_diffs_to_the_given_set(self):
        cur = FakeJunctionCursor({A, B})
        warns = sync(cur, new_ids=[B, C])
        self.assertEqual(warns, [])
        self.assertEqual(cur.roster, {B, C})


class TestDeltaMode(unittest.TestCase):
    """Acceptance: adding one member to an 88-member group only touches that one id."""

    def test_add_leaves_the_rest_of_the_roster_untouched(self):
        cur = FakeJunctionCursor({A, B})
        warns = sync(cur, add_ids=[C], remove_ids=None)
        self.assertEqual(warns, [])
        self.assertEqual(cur.roster, {A, B, C})
        self.assertFalse(any(s.startswith("DELETE") for s in cur.sql))   # nobody else was touched

    def test_remove_drops_exactly_one(self):
        cur = FakeJunctionCursor({A, B, C})
        warns = sync(cur, add_ids=None, remove_ids=[B])
        self.assertEqual(warns, [])
        self.assertEqual(cur.roster, {A, C})

    def test_remove_of_an_absent_id_is_a_no_op_warning_not_an_error(self):
        cur = FakeJunctionCursor({A})
        warns = sync(cur, add_ids=None, remove_ids=[B])   # B was never on the roster
        self.assertEqual(cur.roster, {A})                 # unchanged
        self.assertTrue(any("not in roster" in w for w in warns))

    def test_add_of_a_missing_entity_is_skipped_like_full_roster_add(self):
        cur = FakeJunctionCursor(set(), missing={C})
        warns = sync(cur, add_ids=[C], remove_ids=None)
        self.assertEqual(cur.roster, set())
        self.assertTrue(any("not found" in w for w in warns))

    def test_add_and_remove_in_the_same_call(self):
        cur = FakeJunctionCursor({A, B})
        warns = sync(cur, add_ids=[C], remove_ids=[A])
        self.assertEqual(warns, [])
        self.assertEqual(cur.roster, {B, C})

    def test_re_adding_an_already_present_id_is_a_silent_no_op(self):
        cur = FakeJunctionCursor({A})
        warns = sync(cur, add_ids=[A], remove_ids=None)
        self.assertEqual(warns, [])
        self.assertEqual(cur.roster, {A})


if __name__ == "__main__":
    unittest.main()
