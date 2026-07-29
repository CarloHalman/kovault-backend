"""Column reflection and its three consumers — A4 length validation, A5 array-aware filters,
I1 deny list. The reflection itself is a cached DB read, so it is stubbed; everything downstream
of it is the real code path.
"""
import unittest

from kovault_mcp import server as sv

PID = "22222222-2222-2222-2222-222222222222"


def _col(type_="character varying", max_len=None, is_array=False, is_generated=False, udt=None):
    return {"type": type_, "udt": udt, "max_len": max_len, "is_array": is_array,
            "is_generated": is_generated}


# What _cols() returns for the real schema, trimmed to the columns these tests touch.
SCHEMA = {
    "pages": {
        "id": _col("uuid"), "title": _col(max_len=64), "summary": _col(max_len=512),
        "type": _col(max_len=32), "contributors": _col("ARRAY", max_len=64, is_array=True),
        "created_at": _col("timestamp with time zone"),
        "updated_at": _col("timestamp with time zone"),
    },
    "tasks": {
        "id": _col("uuid"), "title": _col(max_len=64), "description": _col(max_len=1024),
        "scope": _col(max_len=16), "responsible": _col("ARRAY", max_len=64, is_array=True),
        # both are data_type 'USER-DEFINED'; only udt tells the enum from the vector (A6)
        "status": _col("USER-DEFINED", udt="task_status"),
        "embedding": _col("USER-DEFINED", udt="halfvec"),
        "title_norm": _col("text", is_generated=True),
        "embedded_at": _col("timestamp with time zone"),
    },
    "headers": {"title": _col(max_len=64), "title_norm": _col("text", is_generated=True)},
    "groups": {"name": _col(max_len=64), "description": _col(max_len=512),
               "status": _col("USER-DEFINED"), "trashed_at": _col("timestamp with time zone"),
               "participants": _col("ARRAY", max_len=64, is_array=True)},
    "janitor_reports": {"flags": _col("ARRAY", max_len=16, is_array=True), "report": _col("text")},
}


class ReflectionCase(unittest.TestCase):
    def setUp(self):
        sv._COLS_CACHE.update({t: dict(c) for t, c in SCHEMA.items()})

    def tearDown(self):
        for t in SCHEMA:
            sv._COLS_CACHE.pop(t, None)


class TestLengthAndDeny(ReflectionCase):
    def test_clean_row_reports_nothing(self):
        self.assertEqual(sv._check_columns("tasks", {"title": "ship it", "scope": "2 weeks"}), [])

    def test_over_length_names_field_actual_and_limit(self):
        msgs = sv._check_columns("pages", {"summary": "x" * 604}, "page")
        self.assertEqual(msgs, ["description is 604 chars, limit is 512"])  # template key, not column

    def test_every_over_length_field_at_once(self):
        msgs = sv._check_columns("tasks", {"title": "t" * 70, "description": "d" * 2000,
                                           "scope": "ok"})
        self.assertEqual(msgs, ["title is 70 chars, limit is 64",
                                "description is 2000 chars, limit is 1024"])

    def test_array_is_checked_per_element(self):
        # varchar(64)[] caps each element at 64 — the limit comes from element_types
        msgs = sv._check_columns("tasks", {"responsible": ["alice", "n" * 70]})
        self.assertEqual(msgs, ["responsible is 70 chars, limit is 64"])

    def test_exactly_at_the_limit_is_fine(self):
        self.assertEqual(sv._check_columns("tasks", {"title": "t" * 64}), [])

    def test_deny_listed_column_is_refused(self):
        for col in ("created_at", "updated_at", "embedded_at", "embedding"):
            table = "pages" if col in ("created_at", "updated_at") else "tasks"
            msgs = sv._check_columns(table, {col: "whatever"})
            self.assertEqual(msgs, [f"{col} is set by the server and cannot be written"])

    def test_generated_column_is_refused(self):
        # writing a GENERATED column is a hard Postgres error, so it must never reach the DB
        self.assertEqual(sv._check_columns("headers", {"title_norm": "x"}),
                         ["title_norm is set by the server and cannot be written"])

    def test_unknown_column_is_left_to_the_parser(self):
        self.assertEqual(sv._check_columns("tasks", {"nope": "x" * 999}), [])

    def test_null_and_unbounded_values_pass(self):
        self.assertEqual(sv._check_columns("tasks", {"title": None, "status": "todo"}), [])


class TestWriteBoundary(ReflectionCase):
    """The acceptance message, end to end through `write`. Every block here is invalid, so phase 1
    reports them all without opening a connection (a valid sibling WOULD be written — see
    test_write_batch, A1)."""

    def setUp(self):
        super().setUp()
        self._db, sv._DB = sv._DB, None      # any DB touch would assert

    def tearDown(self):
        super().tearDown()
        sv._DB = self._db

    def _page(self, desc):
        return f"---\ntype: note\nid: {PID}\ntitle: Home\ndescription: {desc}\n---"

    def test_message_shape(self):
        out = sv.write([self._page("x" * 604)])
        self.assertIn("block 0 (page): description is 604 chars, limit is 512", out)

    def test_every_block_reported_in_one_response(self):
        out = sv.write([self._page("x" * 604), f"---\ntype: task\ntitle: {'t' * 70}\n---"])
        self.assertIn("block 0 (page): description is 604 chars, limit is 512", out)
        self.assertIn("block 1 (task): title is 70 chars, limit is 64", out)
        self.assertEqual(out.splitlines()[-1], "0 committed, 2 failed")

    def test_several_problems_on_one_block_are_one_line(self):
        out = sv.write([f"---\ntype: task\ntitle: {'t' * 70}\ndescription: {'d' * 2000}\n---"])
        self.assertEqual(out.splitlines()[0],
                         "(error: block 0 (task): title is 70 chars, limit is 64; "
                         "description is 2000 chars, limit is 1024)")


class TestPreciseStatusColumn(ReflectionCase):
    """E2: a group in `lookup` shows its status. Driven off the reflection, so any table with a
    status column gets the column — no per-table list to keep in step."""

    class DB:
        def __init__(self, rows):
            self.rows = rows

        def query_one(self, sql, params=None):
            return {"n": len(self.rows)}

        def query(self, sql, params=None):
            self.sql = " ".join(sql.split())
            return self.rows

    def _precise(self, table, rows):
        old, sv._DB = sv._DB, self.DB(rows)
        try:
            return sv._precise_lookup([table], [], False, 50, 0), sv._DB
        finally:
            sv._DB = old

    def test_groups_show_status(self):
        out, db = self._precise("groups", [{"id": PID, "label": "Migration", "disp": "d",
                                            "status": "active"}])
        self.assertIn("label | summary | status | id", out)   # title first, id last (C1)
        self.assertIn("Migration | d | active |", out)
        self.assertIn("SELECT id, name AS label, description AS disp, status", db.sql)

    def test_a_table_without_status_is_unchanged(self):
        out, _ = self._precise("pages", [{"id": PID, "label": "Home", "disp": "hub"}])
        self.assertIn("label | summary | id", out)
        self.assertNotIn("status", out)


class TestArrayFilters(ReflectionCase):
    def test_array_equality_is_containment(self):
        self.assertEqual(sv._filter_clause("tasks", "responsible", "=", "alice"),
                         ("%s = ANY(responsible)", "alice"))

    def test_array_ilike_unnests(self):
        clause, param = sv._filter_clause("groups", "participants", "ilike", "%carl%")
        self.assertEqual(clause, "EXISTS (SELECT 1 FROM unnest(participants) x WHERE x ILIKE %s)")
        self.assertEqual(param, "%carl%")

    def test_janitor_reports_flags_is_array_aware_too(self):
        # reachable only through `rows`, and it shares the same helper now
        self.assertEqual(sv._filter_clause("janitor_reports", "flags", "=", "embed"),
                         ("%s = ANY(flags)", "embed"))

    def test_scalar_columns_are_unchanged(self):
        self.assertEqual(sv._filter_clause("tasks", "title", "=", "x"), ("title = %s", "x"))
        self.assertEqual(sv._filter_clause("tasks", "title", "ilike", "%x%"),
                         ("title ILIKE %s", "%x%"))
        self.assertEqual(sv._filter_clause("tasks", "title", ">=", 3), ("title >= %s", 3))

    def test_in_wraps_a_scalar_into_a_list(self):
        self.assertEqual(sv._filter_clause("tasks", "title", "in", "x"), ("title = ANY(%s)", ["x"]))
        self.assertEqual(sv._filter_clause("tasks", "title", "in", ["x", "y"]),
                         ("title = ANY(%s)", ["x", "y"]))

    def test_unknown_column_falls_through_to_scalar(self):
        # `rows` / precise mode reject unknown columns before they get here; never raise
        self.assertEqual(sv._filter_clause("tasks", "nope", "=", 1), ("nope = %s", 1))


if __name__ == "__main__":
    unittest.main()


class TestRowsExcludesVectors(ReflectionCase):
    """A6: `rows` must never SELECT *. One embedded chunk serialises its halfvec(4000) to ~13k
    tokens and the default limit is 50, so a single call could return ~650k tokens."""

    class DB:
        def query(self, sql, params=None):
            self.sql = " ".join(sql.split())
            return []

    def _rows(self, table="tasks"):
        old, sv._DB = sv._DB, self.DB()
        try:
            sv.rows(table=table, limit=1)
            return sv._DB.sql
        finally:
            sv._DB = old

    def test_vector_column_is_never_selected(self):
        self.assertNotIn("embedding", self._rows())

    def test_select_star_is_gone(self):
        self.assertNotIn("SELECT *", self._rows())

    def test_enum_and_generated_columns_survive(self):
        sql = self._rows()
        self.assertIn("status", sql)        # USER-DEFINED too, but an enum, not a vector
        self.assertIn("title_norm", sql)    # cheap text; `rows` is the raw read path
