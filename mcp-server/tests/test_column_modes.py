"""I1: three column modes on `fetch` and precise `lookup`.

The mode is applied by filtering the row dict BEFORE it reaches the renderer — the renderer is
driven by row.keys(), so a filtered row already IS a column mode. These tests drive the real
resolver and the real render path; only the DB read is faked.
"""
import unittest
from datetime import datetime

from kovault_mcp import blocks as bl
from kovault_mcp import render as rnd
from kovault_mcp import server as sv

TID = "11111111-1111-1111-1111-111111111111"
PID = "22222222-2222-2222-2222-222222222222"


def _col(**kw):
    d = {"type": "text", "udt": "text", "max_len": None, "is_array": False, "is_generated": False}
    return {**d, **kw}


SCHEMA = {
    "tasks": {c: _col() for c in ("id", "created_at", "updated_at", "trashed_at", "lifecycle",
                                  "title", "description", "status", "priority", "scope",
                                  "deadline", "completed_at", "responsible", "embedding",
                                  "embedded_at", "title_norm", "koplan_planned_start")},
    "pages": {c: _col() for c in ("id", "created_at", "updated_at", "trashed_at", "lifecycle",
                                  "title", "summary", "type", "contributors")},
    "headers": {c: _col() for c in ("id", "created_at", "updated_at", "trashed_at", "lifecycle",
                                    "page_id", "title", "index", "level", "path", "blurb", "body",
                                    "title_norm", "blurb_norm", "embedding", "embedded_at")},
}

TASK_ROW = {"id": TID, "created_at": None, "updated_at": None, "trashed_at": None,
            "lifecycle": "live", "title": "ship", "description": "d", "status": "todo",
            "priority": "high", "scope": "2 weeks", "deadline": None, "completed_at": None,
            "responsible": ["alice"], "embedding": "[0.1]", "embedded_at": None,
            "title_norm": "ship", "koplan_planned_start": datetime(2026, 5, 1)}


class FakeDB:
    """Returns one canned row for any read; records the SQL precise mode builds."""

    def __init__(self, rows):
        self.rows = rows
        self.sql = ""

    JUNCTIONS = ("links", "task_dependencies", "header_sources", "group_links")

    def query(self, sql, params=None):
        self.sql = " ".join(sql.split())
        if any(f"FROM {j}" in self.sql or f"JOIN {j}" in self.sql for j in self.JUNCTIONS):
            return []                       # rosters are not what these tests are about
        return self.rows

    def query_one(self, sql, params=None):
        self.sql = " ".join(sql.split())
        return {"n": len(self.rows)} if "count(*)" in sql else (self.rows[0] if self.rows else None)


class Case(unittest.TestCase):
    def setUp(self):
        sv._COLS_CACHE.update({t: dict(c) for t, c in SCHEMA.items()})
        self._db = sv._DB

    def tearDown(self):
        for t in SCHEMA:
            sv._COLS_CACHE.pop(t, None)
        sv._DB = self._db

    def resolve(self, kind="task", table="tasks", default=None, columns=None, always=None):
        avail = SCHEMA[table]
        return sv._resolve_columns(
            kind, avail, default if default is not None else [c for c in avail],
            columns, always if always is not None else sv._always_cols(kind))


class TestResolver(Case):
    def test_absent_returns_the_default_untouched(self):
        sel, err = self.resolve(columns=None)
        self.assertIsNone(err)
        self.assertEqual(sel, list(SCHEMA["tasks"]))

    def test_signed_adjusts_the_default(self):
        sel, err = self.resolve(default=["title", "status"], columns=["+priority", "-status"])
        self.assertIsNone(err)
        self.assertEqual(sel, ["title", "priority", "id"])   # id always survives

    def test_bare_list_replaces_the_default(self):
        sel, err = self.resolve(default=["title", "status", "priority"],
                                columns=["status", "title"])
        self.assertIsNone(err)
        self.assertEqual(sel, ["status", "title", "id"])

    def test_mixing_the_two_forms_is_an_error(self):
        _, err = self.resolve(columns=["+priority", "title"])
        self.assertIn("mixed column syntax", err)

    def test_unknown_column_is_named(self):
        _, err = self.resolve(columns=["nope"])
        self.assertEqual(err, "unknown column 'nope' on task")

    def test_machine_columns_are_refused_in_both_modes(self):
        for entry in ("embedding", "+embedding", "title_norm", "+title_norm", "+embedded_at"):
            _, err = self.resolve(columns=[entry])
            self.assertIn("never rendered", err or "", entry)

    def test_dropping_id_is_refused(self):
        _, err = self.resolve(columns=["-id"])
        self.assertIn("cannot be dropped", err)

    def test_dropping_a_pages_type_marker_is_refused(self):
        _, err = self.resolve(kind="page", table="pages", columns=["-type"])
        self.assertIn("cannot be dropped", err)

    def test_names_resolve_through_the_rendered_key(self):
        # `description` is what a page fetch shows; `summary` is the column. Both must work.
        for name in ("description", "summary"):
            sel, err = self.resolve(kind="page", table="pages",
                                    default=["title", "summary"], columns=[f"-{name}"])
            self.assertIsNone(err)
            self.assertNotIn("summary", sel)
        sel, _ = self.resolve(default=["title"], columns=["+created"])
        self.assertIn("created_at", sel)                 # meta key -> its column

    def test_an_extension_column_needs_no_code_change(self):
        sel, err = self.resolve(default=["title"], columns=["+koplan_planned_start"])
        self.assertIsNone(err)
        self.assertIn("koplan_planned_start", sel)

    def test_blank_entries_are_ignored(self):
        sel, err = self.resolve(default=["title"], columns=["+status", "", "  "])
        self.assertIsNone(err)
        self.assertEqual(sel, ["title", "status", "id"])


class TestFetch(Case):
    def fetch(self, **kw):
        sv._DB = FakeDB([TASK_ROW])
        return sv.fetch(tasks=[TID], **kw)

    def test_default_is_unchanged(self):
        self.assertEqual(self.fetch(), self.fetch(columns=None))
        self.assertIn("priority: high", self.fetch())

    def test_bare_list_returns_exactly_those(self):
        out = self.fetch(columns=["title", "status"])
        keys = [ln.split(":")[0] for ln in out.splitlines()[1:-1]]
        self.assertEqual(sorted(keys), ["id", "status", "title", "type"])   # type: the marker

    def test_minus_drops_a_field(self):
        self.assertNotIn("priority:", self.fetch(columns=["-priority", "-scope"]))
        self.assertIn("title: ship", self.fetch(columns=["-priority", "-scope"]))

    def test_plus_pulls_in_an_extension_column(self):
        self.assertIn("koplan_planned_start: 2026-05-01T00:00:00",
                      self.fetch(columns=["+koplan_planned_start"]))

    def test_machine_columns_never_leak_even_by_default(self):
        out = self.fetch()
        for col in ("embedding", "title_norm", "embedded_at"):
            self.assertNotIn(col, out)

    def test_a_refusal_names_the_column(self):
        self.assertIn("(fetch columns: unknown column 'nope' on task)", self.fetch(columns=["nope"]))
        self.assertIn("never rendered", self.fetch(columns=["embedding"]))

    def test_a_column_mode_still_round_trips(self):
        for cols in (["title", "status"], ["-priority"], ["+koplan_planned_start", "-scope"]):
            p = bl.parse_block(self.fetch(columns=cols))
            self.assertEqual(p["kind"], "task", cols)
            extra = [w for w in p["warnings"] if "koplan" not in w]
            self.assertEqual(extra, [], cols)            # only the unwired extension column warns


class TestPrecise(Case):
    def precise(self, rows, columns=None, table="tasks"):
        sv._DB = FakeDB(rows)
        return sv._precise_lookup([table], [], False, 50, 0, columns), sv._DB

    def test_default_output_is_untouched(self):
        out, _ = self.precise([{"id": TID, "label": "ship", "disp": "d", "status": "todo"}])
        self.assertIn("label | summary | status | id", out)

    def test_bare_list_selects_exactly_those(self):
        out, db = self.precise([{"title": "ship", "status": "todo", "id": TID}],
                               columns=["title", "status"])
        self.assertIn("SELECT title, status, id FROM tasks", db.sql)
        self.assertIn("title | status | id", out)
        self.assertIn(f"ship | todo | {TID[:8]}", out)     # id stays short in an index (C1)

    def test_signed_adjusts_the_default(self):
        _, db = self.precise([], columns=["+koplan_planned_start", "-description"])
        self.assertIn("SELECT title, status, id, koplan_planned_start FROM tasks", db.sql)

    def test_same_refusals_as_fetch(self):
        out, _ = self.precise([], columns=["embedding"])
        self.assertIn("(precise columns: ", out)
        self.assertIn("never rendered", out)
        out, _ = self.precise([], columns=["+title", "status"])
        self.assertIn("mixed column syntax", out)

    def test_cells_are_clipped_and_arrays_joined(self):
        out, _ = self.precise([{"responsible": ["alice", "bob"], "id": TID}],
                              columns=["responsible"])
        self.assertIn("alice, bob", out)


class TestRenderStaysPure(unittest.TestCase):
    def test_render_knows_nothing_about_modes(self):
        import pathlib
        src = pathlib.Path(rnd.__file__).read_text()
        for word in ("columns", "_resolve", "mode"):
            self.assertNotIn(f"def {word}", src)
        self.assertNotIn("from .db", src)                 # still no DB handle: export.py drives it

    def test_column_of_is_the_shared_mapping(self):
        self.assertEqual(rnd.column_of("page", "description"), "summary")
        self.assertEqual(rnd.column_of("decision", "at"), "decided_at")
        self.assertEqual(rnd.column_of("task", "created"), "created_at")
        self.assertEqual(rnd.column_of("task", "anything_else"), "anything_else")


if __name__ == "__main__":
    unittest.main()
