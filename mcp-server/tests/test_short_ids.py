"""Phase 3: short ids everywhere (F3 extended) + C1's index reorder.

Half 1 — `_full_id` (single) already resolved a unique prefix; `_full_ids` is the batch version
(one query for many short ids, used by the junction rosters) and `_resolve_roster_ids` is the
glue that turns that into "resolve, drop missing with a warning, raise on ambiguity" for
`_sync_block_junctions`/`_write_group`. Integration tests drive `write()`/`snippet()` end to end
with a fake DB that answers the id-prefix LIKE lookups.

Half 2 — C1's CHUNKS/PAGES/precise-mode column reorder (title first, id last) and the 8-char
`_short_id` truncation used everywhere on the READ path (never in render.py/export.py).
"""
import unittest
from contextlib import contextmanager

from kovault_mcp import server as sv
from kovault_mcp import blocks as bl
from tests.test_write_batch import SCHEMA as WB_SCHEMA, FakeCursor, FakeDB

TASK_A = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
TASK_B = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"
TASK_C = "cccccccc-cccc-4ccc-8ccc-cccccccccccc"
PAGE_A = "22222222-2222-2222-2222-222222222222"


# ---- id-resolving fake DB: answers db().query() for the id::text LIKE lookups too -------------

class IdDB(FakeDB):
    """Scripted per table: {table: [full_id, ...]} — the rows a prefix search over that table
    would find. Falls back to the parent's empty-list behaviour for every other query."""

    def __init__(self, cur, id_rows: dict[str, list[str]] | None = None, settings=None):
        super().__init__(cur)
        self.id_rows = id_rows or {}
        self._settings = settings or {}

    def query(self, sql, params=None):
        s = " ".join(str(sql).split())
        if "id::text LIKE" in s:
            for table, ids in self.id_rows.items():
                if f"FROM {table} " in s:
                    pats = params if isinstance(params, (list, tuple)) else [params]
                    prefixes = [p.rstrip("%") for p in pats]
                    return [{"id": i} for i in ids if any(i.startswith(p) for p in prefixes)]
            return []
        return super().query(sql, params)

    def settings(self):
        return self._settings


class Case(unittest.TestCase):
    def setUp(self):
        schema = dict(WB_SCHEMA)
        schema["groups"] = {
            "id": {"type": "uuid", "max_len": None, "is_array": False, "is_generated": False},
            "name": {"type": "character varying", "max_len": 64, "is_array": False, "is_generated": False},
        }
        sv._COLS_CACHE.update({t: dict(c) for t, c in schema.items()})
        self._db = sv._DB

    def tearDown(self):
        sv._COLS_CACHE.pop("groups", None)
        for t in WB_SCHEMA:
            sv._COLS_CACHE.pop(t, None)
        sv._DB = self._db


# ============================== Half 1: batch resolver + wiring ===============================

class TestFullIdsBatch(unittest.TestCase):
    def setUp(self):
        self._db = sv._DB

    def tearDown(self):
        sv._DB = self._db

    def test_full_id_passes_through_with_no_query(self):
        sv._DB = IdDB(None, {})   # any query() call would return [] and fail the test's intent
        resolved, missing, err = sv._full_ids("tasks", [TASK_A])
        self.assertEqual(resolved, {TASK_A: TASK_A})
        self.assertEqual(missing, [])
        self.assertIsNone(err)

    def test_short_prefix_resolves_uniquely(self):
        sv._DB = IdDB(None, {"tasks": [TASK_A, TASK_B]})
        resolved, missing, err = sv._full_ids("tasks", [TASK_A[:8]])
        self.assertEqual(resolved, {TASK_A[:8]: TASK_A})
        self.assertIsNone(err)

    def test_ambiguous_prefix_names_itself(self):
        sv._DB = IdDB(None, {"tasks": ["aaaaaaaa-1111-1111-1111-111111111111",
                                       "aaaaaaaa-2222-2222-2222-222222222222"]})
        resolved, missing, err = sv._full_ids("tasks", ["aaaaaaaa"])
        self.assertEqual(resolved, {})
        self.assertIn("ambiguous", err)
        self.assertIn("aaaaaaaa", err)

    def test_missing_prefix_is_reported_not_erred(self):
        sv._DB = IdDB(None, {"tasks": [TASK_A]})
        resolved, missing, err = sv._full_ids("tasks", ["deadbeef"])
        self.assertIsNone(err)
        self.assertEqual(missing, ["deadbeef"])

    def test_mixed_batch_one_query_for_all_short_ids(self):
        cur = None
        db = IdDB(cur, {"tasks": [TASK_A, TASK_B]})
        calls = []
        orig_query = db.query
        db.query = lambda sql, params=None: (calls.append(sql) or orig_query(sql, params))
        sv._DB = db
        resolved, missing, err = sv._full_ids("tasks", [TASK_A, TASK_B[:8], "deadbeef"])
        self.assertEqual(len(calls), 1)                 # ONE query for every short id, not N
        self.assertEqual(resolved[TASK_A], TASK_A)       # full id needed no query
        self.assertEqual(resolved[TASK_B[:8]], TASK_B)
        self.assertEqual(missing, ["deadbeef"])


class TestResolveRosterIds(unittest.TestCase):
    def setUp(self):
        self._db = sv._DB

    def tearDown(self):
        sv._DB = self._db

    def test_short_id_resolved(self):
        sv._DB = IdDB(None, {"tasks": [TASK_A]})
        ids, warns = sv._resolve_roster_ids("tasks", [TASK_A[:8]])
        self.assertEqual(ids, [TASK_A])
        self.assertEqual(warns, [])

    def test_missing_is_dropped_with_a_warning_not_an_error(self):
        sv._DB = IdDB(None, {"tasks": [TASK_A]})
        ids, warns = sv._resolve_roster_ids("tasks", ["deadbeef"])
        self.assertEqual(ids, [])
        self.assertTrue(any("not found" in w for w in warns))

    def test_ambiguous_raises(self):
        sv._DB = IdDB(None, {"tasks": ["aaaaaaaa-1111-1111-1111-111111111111",
                                       "aaaaaaaa-2222-2222-2222-222222222222"]})
        with self.assertRaises(ValueError):
            sv._resolve_roster_ids("tasks", ["aaaaaaaa"])

    def test_no_table_is_a_no_op(self):
        self.assertEqual(sv._resolve_roster_ids(None, [TASK_A]), ([TASK_A], []))


class TestShortIdHelper(unittest.TestCase):
    def test_truncates_to_eight(self):
        self.assertEqual(sv._short_id(TASK_A), TASK_A[:8])
        self.assertEqual(len(sv._short_id(TASK_A)), 8)


class TestGroupEntitySets(unittest.TestCase):
    def setUp(self):
        self._db = sv._DB

    def tearDown(self):
        sv._DB = self._db

    def test_full_id_and_name_both_work(self):
        db = IdDB(None, {})

        def query(sql, params=None):
            s = " ".join(str(sql).split())
            if s.startswith("SELECT id FROM groups"):
                return [{"id": "gid-1"}]
            if s.startswith("SELECT entity_id"):
                return [{"entity_id": TASK_A}]
            return []
        db.query = query
        sv._DB = db
        ids, err = sv._group_entity_sets([PAGE_A, "some name"])
        self.assertIsNone(err)
        self.assertEqual(ids, {TASK_A})

    def test_ambiguous_group_prefix_errors(self):
        sv._DB = IdDB(None, {"groups": ["aaaaaaaa-1111-1111-1111-111111111111",
                                        "aaaaaaaa-2222-2222-2222-222222222222"]})
        ids, err = sv._group_entity_sets(["aaaaaaaa"])
        self.assertEqual(ids, set())
        self.assertIn("ambiguous", err)

    def test_looks_id_like(self):
        self.assertTrue(sv._looks_id_like("8f54528f"))
        self.assertTrue(sv._looks_id_like(TASK_A))
        self.assertFalse(sv._looks_id_like("marketing team"))
        self.assertFalse(sv._looks_id_like("kovault"))   # contains 'k' — not hex


class TestBlocksShortIdParsing(unittest.TestCase):
    """blocks.py must recognize a short id token too, or fetch's own short-id rosters would
    silently fail to round-trip through write (_id_list would find nothing to extract)."""

    def test_looks_uuid_accepts_short_hex_and_full(self):
        self.assertTrue(bl._looks_uuid(TASK_A))
        self.assertTrue(bl._looks_uuid(TASK_A[:8]))
        self.assertFalse(bl._looks_uuid("task"))      # kind-prefix sugar, not hex
        self.assertFalse(bl._looks_uuid("ship"))       # an ordinary title word

    def test_id_list_extracts_short_blocker_ids(self):
        self.assertEqual(bl._id_list(f"{TASK_A[:8]} — design, {TASK_B[:8]} — spec"),
                         ([TASK_A[:8], TASK_B[:8]], []))

    def test_id_list_extracts_short_member_ids_with_kind_prefix(self):
        self.assertEqual(bl._id_list(f"task: {TASK_A[:8]} — Some Task"), ([TASK_A[:8]], []))

    def test_id_list_strips_flow_style_brackets(self):
        """The bracketed form is what the tool docs show, and it used to lose its FIRST and LAST
        entry: split on ',' left '[a' and 'c]', neither matched _looks_uuid, both were dropped —
        and the write still reported success."""
        self.assertEqual(bl._id_list(f"[{TASK_A}, {TASK_B}]"), ([TASK_A, TASK_B], []))
        self.assertEqual(bl._id_list(f"[{TASK_A}]"), ([TASK_A], []))          # single: was empty
        self.assertEqual(bl._id_list(f"{TASK_A}, {TASK_B}"), ([TASK_A, TASK_B], []))   # unbracketed still fine

    def test_id_list_reports_an_entry_it_cannot_read(self):
        ids, warns = bl._id_list(f"{TASK_A}, not-an-id-at-all")
        self.assertEqual(ids, [TASK_A])
        self.assertEqual(len(warns), 1)
        self.assertIn("not-an-id-at-all", warns[0])

    def test_array_columns_strip_flow_style_brackets(self):
        """Same root cause as the roster case, worse symptom: the people arrays split on ',' too,
        so `participants: [alice, bob]` STORED '[alice' and 'bob]' as names — silent corruption
        rather than a dropped value."""
        p = bl.parse_block("---\ntype: group\nname: g\nparticipants: [alice, bob]\n---")
        self.assertEqual(p["fields"]["participants"], ["alice", "bob"])
        p = bl.parse_block("---\ntype: task\ntitle: t\nresponsible: [ada, grace]\n---")
        self.assertEqual(p["fields"]["responsible"], ["ada", "grace"])
        p = bl.parse_block("---\ntype: group\nname: g\nparticipants: alice, bob\n---")
        self.assertEqual(p["fields"]["participants"], ["alice", "bob"])   # unbracketed unchanged

    def test_body_on_a_task_is_reported_not_swallowed(self):
        """A task carries its prose in `description:`; text after the fence was dropped in silence,
        so a task written that way inserted with description NULL and still said `inserted`."""
        p = bl.parse_block("---\ntype: task\ntitle: t\n---\ndetail that goes nowhere")
        self.assertTrue(any("dropped" in w for w in p["warnings"]), p["warnings"])
        # a page renders its document after the fence and must stay quiet
        p = bl.parse_block("---\ntype: note\ntitle: t\n---\nPage document")
        self.assertEqual(p["warnings"], [])


class TestDispatchBlockIdResolution(Case):
    """write()'s block-level id: and header page_id: route through _full_id (F3/C1)."""

    def test_short_id_resolves_and_updates(self):
        sv._DB = IdDB(FakeCursor(), {"tasks": [TASK_A]})
        # ROW (the FakeCursor's canned row) already carries this exact id, so the update path works
        import tests.test_write_batch as wb
        wb.ROW = {**wb.ROW, "id": TASK_A}
        try:
            out = sv.write([f"---\ntype: task\nid: {TASK_A[:8]}\ntitle: renamed\n---"])
        finally:
            wb.ROW = {**wb.ROW, "id": wb.TID}
        self.assertTrue(out.startswith("updated tasks"), out)

    def test_ambiguous_short_id_errors_and_names_itself(self):
        sv._DB = IdDB(FakeCursor(), {"tasks": ["aaaaaaaa-1111-1111-1111-111111111111",
                                               "aaaaaaaa-2222-2222-2222-222222222222"]})
        out = sv.write([f"---\ntype: task\nid: aaaaaaaa\ntitle: x\n---"])
        self.assertTrue(out.startswith("(error:"), out)
        self.assertIn("ambiguous", out)
        self.assertIn("aaaaaaaa", out)

    def test_header_page_id_short_prefix_resolves_on_insert(self):
        sv._DB = IdDB(FakeCursor(), {"pages": [PAGE_A]})
        out = sv.write([f"---\ntype: header\npage_id: {PAGE_A[:8]}\nindex: 0\ntitle: T\n---\nbody"])
        self.assertTrue(out.startswith("inserted headers"), out)


class TestJunctionRosterShortIds(Case):
    """A short id in blockers:/members: resolves and reconciles (D1 + F3/C1 together)."""

    def test_task_blockers_add_with_a_short_id(self):
        sv._DB = IdDB(FakeCursor(), {"tasks": [TASK_A, TASK_B]})
        import tests.test_write_batch as wb
        wb.ROW = {**wb.ROW, "id": TASK_A}
        try:
            out = sv.write([f"---\ntype: task\nid: {TASK_A[:8]}\nblockers_add: {TASK_B[:8]}\n---"])
        finally:
            wb.ROW = {**wb.ROW, "id": wb.TID}
        self.assertFalse(sv._failed(out), out)

    def test_group_members_with_a_short_id(self):
        cur = FakeCursor()
        db = IdDB(cur, {"entities": [TASK_A]})
        sv._DB = db
        out = sv.write([f"---\ntype: group\nname: G\nmembers: {TASK_A[:8]}\n---"])
        self.assertFalse(sv._failed(out), out)
        self.assertTrue(any(s.startswith("INSERT INTO group_links") for s in cur.sql), cur.sql)


class TestSnippetShortIdsAndCrashFix(unittest.TestCase):
    def setUp(self):
        self._db = sv._DB

    def tearDown(self):
        sv._DB = self._db

    def test_malformed_id_returns_a_message_not_a_crash(self):
        sv._DB = IdDB(None, {"tasks": []})
        out = sv.snippet([{"table": "tasks", "ids": ["not-a-uuid-at-all"]}])
        self.assertEqual(out, "(no snippet matches)")   # no match, no raise

    def test_short_id_resolves(self):
        db = IdDB(None, {"tasks": [TASK_A]})

        def query(sql, params=None):
            s = " ".join(str(sql).split())
            if "id::text LIKE" in s:
                return IdDB.query(db, sql, params)
            if s.startswith("SELECT id, title AS title"):
                return [{"id": TASK_A, "title": "Ship it", "summary": "desc"}]
            return []
        db.query = query
        sv._DB = db
        out = sv.snippet([{"table": "tasks", "ids": [TASK_A[:8]]}])
        self.assertIn("Ship it", out)
        self.assertIn(TASK_A, out)

    def test_ambiguous_id_names_itself_and_does_not_crash(self):
        sv._DB = IdDB(None, {"tasks": ["aaaaaaaa-1111-1111-1111-111111111111",
                                       "aaaaaaaa-2222-2222-2222-222222222222"]})
        out = sv.snippet([{"table": "tasks", "ids": ["aaaaaaaa"]}])
        self.assertIn("ambiguous", out)


# ============================== Half 2: C1 reorder + short display ids ==========================

class TestChunksColumnOrder(unittest.TestCase):
    """CHUNKS leads with title, ends with a short id, after rrf (C1)."""

    def setUp(self):
        self._db = sv._DB
        self._kw = sv._keyword_hits
        self._emb = sv._embedder

        class FakeEmbedder:
            def embed(self, text):
                return "[0]"
        sv._embedder = lambda: FakeEmbedder()

        def fake_keyword_hits(table, inc, exc):
            return {TASK_A: {"id": TASK_A, "page_id": None, "title": "Similar and alternatives",
                             "disp": "Carries both an absolute ISO date and a relative one.",
                             "keyword": 1.0}}
        sv._keyword_hits = fake_keyword_hits

        class DB:
            def query(self, sql, params=None):
                return []
            def query_one(self, sql, params=None):
                return {"n": 0}
            def settings(self):
                return {"rrf_k": 60, "ladder_chunks": {"r": 0.0, "floor": 5, "cap": 10},
                        "ladder_pages": {"r": 0.0, "floor": 5, "cap": 10}}
        sv._DB = DB()

    def tearDown(self):
        sv._DB = self._db
        sv._keyword_hits = self._kw
        sv._embedder = self._emb

    def test_header_is_title_first_id_last(self):
        out = sv.lookup(tables=["tasks"], include=["x"])
        lines = out.splitlines()
        self.assertEqual(lines[0], "CHUNKS")
        self.assertEqual(lines[1], "title | kind | blurb/summary | rrf | id")

    def test_row_leads_with_title_and_ends_with_a_short_id(self):
        out = sv.lookup(tables=["tasks"], include=["x"])
        lines = out.splitlines()
        row = lines[2]
        cols = [c.strip() for c in row.split("|")]
        self.assertEqual(cols[0], "Similar and alternatives")
        self.assertEqual(cols[1], "task")
        self.assertEqual(cols[-1], TASK_A[:8])
        self.assertEqual(len(cols[-1]), 8)


class TestPagesIndexColumnOrder(unittest.TestCase):
    """_pages_index (the PAGES block lookup builds) leads with title, ends with a short id (C1)."""

    def setUp(self):
        self._db = sv._DB

        class DB:
            def query(self, sql, params=None):
                s = " ".join(str(sql).split())
                if s.startswith("SELECT page_id AS id"):
                    return [{"id": PAGE_A, "n": 1}]
                if s.startswith("SELECT id, title, summary FROM pages"):
                    return [{"id": PAGE_A, "title": "Deploy Guide", "summary": "how to deploy"}]
                return []
        sv._DB = DB()

    def tearDown(self):
        sv._DB = self._db

    def test_header_and_row_shape(self):
        cand = [{"id": TASK_A, "kind": "header", "page_id": PAGE_A, "title": "Deploy Guide",
                 "disp": "step one", "vector": 0.5, "keyword": 0.5, "graph": 0, "rrf": 1.0}]
        lines = sv._pages_index(cand, 60, {"ladder_pages": {"r": 0.0, "floor": 5, "cap": 10}})
        self.assertEqual(lines[1], "PAGES")
        self.assertEqual(lines[2], "title | summary | rrf | top chunk | id")
        cols = [c.strip() for c in lines[3].split("|")]
        self.assertEqual(cols[0], "Deploy Guide")
        self.assertEqual(cols[-1], PAGE_A[:8])


class TestPreciseModeColumnOrder(unittest.TestCase):
    """Covered directly in test_reflection.py's TestPreciseStatusColumn; this pins the short id too."""

    def test_precise_row_ends_with_a_short_id(self):
        class DB:
            def query_one(self, sql, params=None):
                return {"n": 1}
            def query(self, sql, params=None):
                return [{"id": TASK_A, "label": "Ship it", "disp": "d"}]
        old, sv._DB = sv._DB, DB()
        sv._COLS_CACHE["tasks"] = {"title": {"type": "text", "max_len": 64, "is_array": False, "is_generated": False}}
        try:
            out = sv._precise_lookup(["tasks"], [], False, 50, 0)
        finally:
            sv._DB = old
            sv._COLS_CACHE.pop("tasks", None)
        self.assertIn("label | summary | id", out)
        self.assertIn(f"Ship it | d | {TASK_A[:8]}", out)


if __name__ == "__main__":
    unittest.main()
