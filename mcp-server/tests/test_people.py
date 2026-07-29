"""D3/D4: one canonical person form, shared by _identity, the write boundary and the janitor."""
import os
import unittest
from datetime import datetime

from kovault_mcp import server as sv

RID = "11111111-1111-1111-1111-111111111111"


class TestCanonPeople(unittest.TestCase):
    def test_case_is_preserved(self):
        self.assertEqual(sv._canon_people(["Bob"]), ["Bob"])
        self.assertEqual(sv._canon_people("Bob"), "Bob")

    def test_case_insensitive_dedupe_first_spelling_wins(self):
        self.assertEqual(sv._canon_people(["Alice", "alice", "ALICE"]), ["Alice"])
        self.assertEqual(sv._canon_people(["alice", "Alice"]), ["alice"])

    def test_order_is_kept(self):
        self.assertEqual(sv._canon_people(["Zoe", "Al", "zoe"]), ["Zoe", "Al"])

    def test_trims_and_drops_blanks(self):
        self.assertEqual(sv._canon_people(["  Alice  ", "", "   ", None]), ["Alice"])
        self.assertEqual(sv._canon_people("  Alice "), "Alice")

    def test_none_and_empty_keep_their_shape(self):
        self.assertIsNone(sv._canon_people(None))
        self.assertEqual(sv._canon_people([]), [])

    def test_fields_applies_to_every_person_column_only(self):
        f = {"responsible": ["A", "a"], "participants": ["B", "b"], "contributors": ["C", "c"],
             "decided_by": " D ", "title": " not a person "}
        self.assertEqual(sv._canon_people_fields(f),
                         {"responsible": ["A"], "participants": ["B"], "contributors": ["C"],
                          "decided_by": "D", "title": " not a person "})


class TestIdentity(unittest.TestCase):
    def test_env_username_keeps_its_case(self):
        old = os.environ.get("KOVAULT_DEFAULT_USER")
        os.environ["KOVAULT_DEFAULT_USER"] = "  Bob "
        try:
            self.assertEqual(sv._identity()[0], "Bob")
        finally:
            os.environ.pop("KOVAULT_DEFAULT_USER", None)
            if old is not None:
                os.environ["KOVAULT_DEFAULT_USER"] = old


class ScriptedCursor:
    """Returns a queued result per execute(); records the UPDATEs the janitor issues."""

    def __init__(self, results):
        self.results = list(results)
        self.updates: list[tuple] = []
        self._out: list = []

    def execute(self, sql, params=None):
        s = " ".join(str(sql).split())
        if s.startswith(("SELECT", "WITH")):        # only reads consume a queued result — the
            self._out = self.results.pop(0) if self.results else []   # edit-log INSERTs must not
            return
        if s.startswith("UPDATE"):
            self.updates.append((s, params))
        self._out = []

    def fetchall(self):
        return self._out

    def fetchone(self):
        return self._out[0] if self._out else None


class TestJanitorNormalizePeople(unittest.TestCase):
    """The winner is the most-used spelling; ties go to the earliest row, then alphabetically."""

    CENSUS = [  # already ordered by the census SQL: key, then count DESC, min(created_at), name
        {"key": "alice", "name": "alice"},   # the majority spelling
        {"key": "alice", "name": "Alice"},   # a minority casing of the same person
        {"key": "bob", "name": "Bob"},       # only one spelling, so it wins by default
    ]

    def _run(self, rows_per_table):
        cur = ScriptedCursor([self.CENSUS] + rows_per_table)
        n = sv._janitor_normalize_people(cur, "janitor")
        return n, cur

    def test_minority_casing_is_rewritten_to_the_winner(self):
        n, cur = self._run([
            [{"id": RID, "v": ["Alice", "unknown"]}],      # pages.contributors
            [{"id": RID, "v": ["Alice"]}],                  # tasks.responsible
            [],                                             # groups.participants
            [{"id": RID, "v": "Alice"}],                    # decisions.decided_by
        ])
        self.assertEqual(n, 3)
        self.assertEqual([p[0] for _, p in cur.updates],
                         [["alice", "unknown"], ["alice"], "alice"])

    def test_winning_casing_is_left_alone(self):
        n, cur = self._run([[{"id": RID, "v": ["alice"]}], [], [], []])
        self.assertEqual(n, 0)
        self.assertEqual(cur.updates, [])

    def test_a_person_with_one_spelling_keeps_it(self):
        # Bob is written `Bob` everywhere -> the janitor must NOT lowercase it any more
        n, cur = self._run([[{"id": RID, "v": ["Bob"]}], [], [], []])
        self.assertEqual((n, cur.updates), (0, []))

    def test_duplicate_casings_in_one_row_collapse(self):
        n, cur = self._run([[{"id": RID, "v": ["alice", "Alice", "ALICE"]}], [], [], []])
        self.assertEqual(n, 1)
        self.assertEqual(cur.updates[0][1][0], ["alice"])

    def test_unknown_person_is_left_as_written(self):
        n, cur = self._run([[{"id": RID, "v": ["Nobody"]}], [], [], []])
        self.assertEqual((n, cur.updates), (0, []))

    def test_empty_decided_by_is_not_touched(self):
        n, cur = self._run([[], [], [], [{"id": RID, "v": ""}]])
        self.assertEqual((n, cur.updates), (0, []))


class TestCensusOrdering(unittest.TestCase):
    def test_sql_orders_by_frequency_then_first_seen_then_name(self):
        # the winner rule lives in the ORDER BY, so pin it
        sql = " ".join(sv._PEOPLE_CENSUS_SQL.split())
        self.assertIn("ORDER BY key, count(*) DESC, min(created_at), name", sql)
        for col in ("contributors", "responsible", "participants", "decided_by"):
            self.assertIn(col, sql)          # all four person columns vote

    def test_created_at_is_available_on_every_source_table(self):
        # min(created_at) is the first-seen tiebreak; a table without it would be a runtime error
        self.assertEqual(sv._PEOPLE_CENSUS_SQL.count("created_at"), 5)
        _ = datetime  # (kept for the reader: created_at is a timestamptz on all four tables)


if __name__ == "__main__":
    unittest.main()
