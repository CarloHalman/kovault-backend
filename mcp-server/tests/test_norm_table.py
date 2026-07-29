"""A7: the double-encode guard. A client that double-JSON-encodes a scalar sends the literal
characters '"tasks"' — quotes included — instead of tasks. `_norm_table` strips that at every
surface that takes a raw table-name argument; this pins all three so the fix cannot quietly
disappear from one of them again (history: it did, once, in the v1.3 rewrite).
"""
import unittest

from kovault_mcp import server as sv

TID = "11111111-1111-1111-1111-111111111111"


def _col(max_len=None, is_array=False, is_generated=False):
    return {"type": "text", "max_len": max_len, "is_array": is_array, "is_generated": is_generated}


SCHEMA = {
    "tasks": {"id": _col(), "title": _col(64), "created_at": _col()},
}


class FakeDB:
    """Enough of Database for rows()/lookup(filters=...)/snippet() to run past the table check."""

    def query(self, sql, params=None):
        self.last_sql = sql
        return []

    def query_one(self, sql, params=None):
        self.last_sql = sql
        return {"n": 0}


class NormTableCase(unittest.TestCase):
    def setUp(self):
        sv._COLS_CACHE.update({t: dict(c) for t, c in SCHEMA.items()})
        self._db = sv._DB
        sv._DB = FakeDB()

    def tearDown(self):
        for t in SCHEMA:
            sv._COLS_CACHE.pop(t, None)
        sv._DB = self._db


class TestHelper(unittest.TestCase):
    def test_strips_double_and_single_quotes_and_whitespace(self):
        self.assertEqual(sv._norm_table('"tasks"'), "tasks")
        self.assertEqual(sv._norm_table("'tasks'"), "tasks")
        self.assertEqual(sv._norm_table("  tasks  "), "tasks")
        self.assertEqual(sv._norm_table('  "tasks"  '), "tasks")

    def test_clean_value_and_none_pass_through(self):
        self.assertEqual(sv._norm_table("tasks"), "tasks")
        self.assertIsNone(sv._norm_table(None))

    def test_does_not_alias_singular_to_plural(self):
        # decision 2: no singular->plural guess, a genuinely wrong argument must still fail loud
        self.assertEqual(sv._norm_table('"page"'), "page")


class TestRowsSurface(NormTableCase):
    def test_double_encoded_table_behaves_as_if_unquoted(self):
        out = sv.rows('"tasks"')
        self.assertNotIn("unknown table", out)

    def test_unrecognized_table_names_the_received_value(self):
        out = sv.rows('"nope"')
        self.assertIn("nope", out)
        self.assertNotIn('"nope"', out)   # quotes were stripped before the message was built


class TestSnippetSurface(NormTableCase):
    def test_double_encoded_table_behaves_as_if_unquoted(self):
        out = sv.snippet([{"table": '"tasks"', "ids": [TID]}])
        self.assertNotIn("unknown table", out)

    def test_unrecognized_table_names_the_received_value(self):
        out = sv.snippet([{"table": '"nope"', "ids": [TID]}])
        self.assertIn("(unknown table nope)", out)


class TestPreciseLookupSurface(NormTableCase):
    def test_double_encoded_table_behaves_as_if_unquoted(self):
        out = sv.lookup(tables=['"tasks"'], filters=[], count=True)
        self.assertNotIn("not recognized", out)
        self.assertIn("PRECISE tasks", out)

    def test_unrecognized_table_names_the_received_value(self):
        out = sv.lookup(tables=['"nope"'], filters=[], count=True)
        self.assertIn("nope", out)
        self.assertNotIn('"nope"', out)


if __name__ == "__main__":
    unittest.main()
