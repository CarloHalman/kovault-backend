"""F1+F2: actionable near-duplicate group report (content overlap + name similarity), and the
capped orphan-task sample. Report only — no query here is anything but SELECT, so there is
structurally nothing to mutate. The heavy lifting (Jaccard, trigram similarity, the numbered-series
stem exclusion) runs in SQL and can't be exercised without a live DB; these tests pin the query
SHAPE (the same-type restriction, the stem-exclusion clause, the thresholds/caps as params) with a
fake DB that records what was asked, and the Python-side row -> report-dict transformation with
canned rows shaped like what Postgres would actually return.
"""
import unittest

from kovault_mcp import server as sv

GID_A = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
GID_B = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"
TID_A = "11111111-1111-1111-1111-111111111111"


class RecordingDB:
    """Records every query() call (sql, params) and returns a scripted result per call, matched
    by a substring of the SQL. Falls back to [] / {"n": 0} for anything unscripted."""

    def __init__(self, scripts: dict[str, list[dict]] | None = None):
        self.scripts = scripts or {}
        self.calls: list[tuple[str, object]] = []

    def query(self, sql, params=None):
        s = " ".join(str(sql).split())
        self.calls.append((s, params))
        for needle, rows in self.scripts.items():
            if needle in s:
                return rows
        return []

    def query_one(self, sql, params=None):
        rows = self.query(sql, params)
        return rows[0] if rows else {"n": 0}


class Case(unittest.TestCase):
    def setUp(self):
        self._db = sv._DB

    def tearDown(self):
        sv._DB = self._db


class TestContentOverlapQuery(Case):
    def test_shape_and_params(self):
        row = {"name_a": "wiki-database", "id_a": GID_A, "name_b": "old-llm-wiki-database",
               "id_b": GID_B, "size_a": 69, "size_b": 55, "shared": 40, "pct": 57.1}
        db = RecordingDB({"shared AS": [row]})
        sv._DB = db
        out = sv._dup_groups_by_content()
        self.assertEqual(out, [{"name_a": "wiki-database", "id_a": GID_A[:8], "size_a": 69,
                                "name_b": "old-llm-wiki-database", "id_b": GID_B[:8], "size_b": 55,
                                "shared": 40, "pct": 57.1}])
        sql, params = db.calls[0]
        self.assertIn("ga.type = gb.type", sql)              # same-type only: no area/topic nesting
        self.assertIn("s.n::numeric / (sa.n + sb.n - s.n) >= %(thresh)s", sql)
        # round() only accepts (numeric, int) in Postgres — similarity()/int division are real/
        # double precision otherwise, and that overload does not exist (caught against a real DB).
        self.assertIn("round(100.0 * s.n::numeric / (sa.n + sb.n - s.n), 1)", sql)
        self.assertEqual(params["thresh"], sv._GROUP_CONTENT_OVERLAP_THRESHOLD)
        self.assertEqual(params["cap"], sv._DUP_GROUP_PAIR_CAP)
        self.assertIn("trashed_at IS NULL", sql)
        self.assertIn("lifecycle <> 'archived'", sql)        # _live(): both trashed and archived excluded

    def test_empty_result_is_an_empty_list(self):
        sv._DB = RecordingDB({})
        self.assertEqual(sv._dup_groups_by_content(), [])

    def test_custom_cap_is_passed_through(self):
        db = RecordingDB({})
        sv._DB = db
        sv._dup_groups_by_content(cap=3)
        self.assertEqual(db.calls[0][1]["cap"], 3)


class TestNameSimilarityQuery(Case):
    def test_shape_and_params(self):
        row = {"name_a": "wiki-database", "id_a": GID_A, "size_a": 69,
               "name_b": "wiki-database (empty duplicate)", "id_b": GID_B, "size_b": 0, "pct": 64.0}
        db = RecordingDB({"similarity(a.name, b.name)": [row]})
        sv._DB = db
        out = sv._dup_groups_by_name()
        self.assertEqual(out, [{"name_a": "wiki-database", "id_a": GID_A[:8], "size_a": 69,
                                "name_b": "wiki-database (empty duplicate)", "id_b": GID_B[:8],
                                "size_b": 0, "pct": 64.0}])
        sql, params = db.calls[0]
        self.assertIn("regexp_replace(lower(a.name)", sql)   # numbered-series stem exclusion
        self.assertIn("[0-9]+$", sql)
        self.assertIn("similarity(a.name, b.name) >= %(thresh)s", sql)
        # similarity() returns real; round(double precision, int) does not exist in Postgres —
        # must cast to numeric right at the source, same house pattern as _similar_task_warn.
        self.assertIn("round(100.0 * similarity(a.name, b.name)::numeric, 1)", sql)
        self.assertEqual(params["thresh"], sv._GROUP_NAME_SIM_THRESHOLD)
        self.assertEqual(params["cap"], sv._DUP_GROUP_PAIR_CAP)

    def test_threshold_clears_the_real_pairs_with_margin(self):
        # decision 2's own measurement: real pairs sit at 0.64 and 0.48
        self.assertLess(sv._GROUP_NAME_SIM_THRESHOLD, 0.48)

    def test_empty_result_is_an_empty_list(self):
        sv._DB = RecordingDB({})
        self.assertEqual(sv._dup_groups_by_name(), [])


class TestJanitorDiagnose(Case):
    def setUp(self):
        super().setUp()
        self._content = sv._dup_groups_by_content
        self._name = sv._dup_groups_by_name

    def tearDown(self):
        sv._dup_groups_by_content = self._content
        sv._dup_groups_by_name = self._name
        super().tearDown()

    def test_returns_both_pair_lists_and_an_orphan_sample(self):
        sv._dup_groups_by_content = lambda: [{"name_a": "A", "id_a": "aaaaaaaa", "size_a": 3,
                                              "name_b": "B", "id_b": "bbbbbbbb", "size_b": 3,
                                              "shared": 3, "pct": 100.0}]
        sv._dup_groups_by_name = lambda: [{"name_a": "wiki-database", "id_a": "aaaaaaaa",
                                          "name_b": "old-llm-wiki-database", "id_b": "bbbbbbbb",
                                          "pct": 48.0}]
        rows_by_call = iter([
            [{"n": 0}], [{"n": 0}], [{"n": 0}], [{"n": 0}], [{"n": 0}],   # stale x4, trashed
            [{"n": 0}],                                                   # dangling
            [{"n": 0}],                                                   # redundant
            [{"n": 5}],                                                   # orphan_tasks count
            [{"id": TID_A, "title": "Do the thing"}],                     # orphan_task_sample
        ])

        class SeqDB:
            def query(self, sql, params=None):
                return next(rows_by_call, [])
            def query_one(self, sql, params=None):
                rows = self.query(sql, params)
                return rows[0] if rows else {"n": 0}
        sv._DB = SeqDB()
        diag = sv._janitor_diagnose()
        self.assertEqual(diag["duplicate_groups_content"][0]["pct"], 100.0)
        self.assertEqual(diag["duplicate_groups_name"][0]["name_b"], "old-llm-wiki-database")
        self.assertEqual(diag["orphan_tasks"], 5)
        self.assertEqual(diag["orphan_task_sample"], [{"id": TID_A[:8], "title": "Do the thing"}])


class TestJanitorReportRendering(Case):
    """The report a bare `janitor` call prints — the acceptance line checks this text directly."""

    def setUp(self):
        super().setUp()
        self._diag = sv._janitor_diagnose

    def tearDown(self):
        sv._janitor_diagnose = self._diag
        super().tearDown()

    def _run(self, diag):
        sv._janitor_diagnose = lambda: diag

        class DB:
            def connection(self):
                import contextlib

                @contextlib.contextmanager
                def cm():
                    class Cur:
                        rowcount = 1
                        def execute(self, sql, params=None):
                            pass
                        def fetchone(self):
                            return {"id": TID_A}
                    class Conn:
                        def cursor(self):
                            import contextlib as c2
                            @c2.contextmanager
                            def cur_cm():
                                yield Cur()
                            return cur_cm()
                        def commit(self):
                            pass
                    yield Conn()
                return cm()
        sv._DB = DB()
        return sv.janitor([])

    def test_names_the_real_duplicate_pair(self):
        out = self._run({
            "stale_embeddings": 0, "trashed_pages": 0, "dangling_header_links": 0,
            "redundant_blocks": 0, "orphan_tasks": 0, "orphan_task_sample": [],
            "duplicate_groups_content": [],
            "duplicate_groups_name": [{"name_a": "wiki-database", "id_a": "aaaaaaaa", "size_a": 69,
                                      "name_b": "old-llm-wiki-database", "id_b": "bbbbbbbb",
                                      "size_b": 55, "pct": 48.0}],
        })
        self.assertIn('"wiki-database"', out)
        self.assertIn('"old-llm-wiki-database"', out)
        self.assertIn("48.0%", out)

    def test_no_bench_pairs_leak_through_when_diagnose_returns_none(self):
        # the stem-exclusion lives in SQL (untestable here); this only pins that an empty list
        # renders no duplicate line at all, so nothing is silently synthesized in Python.
        out = self._run({
            "stale_embeddings": 0, "trashed_pages": 0, "dangling_header_links": 0,
            "redundant_blocks": 0, "orphan_tasks": 0, "orphan_task_sample": [],
            "duplicate_groups_content": [], "duplicate_groups_name": [],
        })
        self.assertNotIn("bench-", out)
        self.assertNotIn("Duplicate?", out)

    def test_content_overlap_pair_shows_sizes_and_percentage(self):
        out = self._run({
            "stale_embeddings": 0, "trashed_pages": 0, "dangling_header_links": 0,
            "redundant_blocks": 0, "orphan_tasks": 0, "orphan_task_sample": [],
            "duplicate_groups_content": [{"name_a": "A", "id_a": "aaaaaaaa", "size_a": 12,
                                         "name_b": "B", "id_b": "bbbbbbbb", "size_b": 10,
                                         "shared": 9, "pct": 69.2}],
            "duplicate_groups_name": [],
        })
        self.assertIn("69.2%", out)
        self.assertIn("12 members", out)
        self.assertIn("10 members", out)
        self.assertIn("9 shared", out)

    def test_orphan_sample_shows_capped_count_against_the_total(self):
        out = self._run({
            "stale_embeddings": 0, "trashed_pages": 0, "dangling_header_links": 0,
            "redundant_blocks": 0, "orphan_tasks": 137,
            "orphan_task_sample": [{"id": "11111111", "title": "Do the thing"}],
            "duplicate_groups_content": [], "duplicate_groups_name": [],
        })
        self.assertIn("showing 1 of 137", out)
        self.assertIn("Do the thing (11111111)", out)

    def test_diagnostics_summary_line_has_no_list_values(self):
        out = self._run({
            "stale_embeddings": 2, "trashed_pages": 0, "dangling_header_links": 0,
            "redundant_blocks": 0, "orphan_tasks": 0, "orphan_task_sample": [],
            "duplicate_groups_content": [{"name_a": "A", "id_a": "aaaaaaaa", "size_a": 1,
                                         "name_b": "B", "id_b": "bbbbbbbb", "size_b": 1,
                                         "shared": 1, "pct": 100.0}],
            "duplicate_groups_name": [],
        })
        diag_line = out.splitlines()[1]
        self.assertTrue(diag_line.startswith("Diagnostics: "))
        self.assertNotIn("duplicate_groups_content", diag_line)
        self.assertIn("stale_embeddings=2", diag_line)


if __name__ == "__main__":
    unittest.main()
