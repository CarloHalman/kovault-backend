"""No-AI export: folder-tree layout, extension/week bucketing, and collision suffixes."""
import unittest
from datetime import datetime

from kovault_mcp import export as ex


class TestTaskTree(unittest.TestCase):
    """Native task-tree mode (folds scripts/task_page.py): blocker nesting, open-only default."""
    TASKS = {
        "Ship":   {"status": "todo", "description": "", "created": "2026-01-03", "blockers": ["Build, test"]},
        "Build, test": {"status": "todo", "description": "core", "created": "2026-01-02", "blockers": ["Design"]},
        "Design": {"status": "done", "description": "", "created": "2026-01-01", "blockers": []},
    }

    def test_open_only_nesting(self):
        children, roots = ex._build_tree(self.TASKS, include_done=False)
        self.assertEqual(roots, ["Ship"])                  # Ship blocked only by open tasks -> root
        self.assertEqual(children["Ship"], ["Build, test"])  # comma-in-title blocker kept intact
        self.assertNotIn("Design", children["Build, test"])  # done blocker dropped when open-only

    def test_render_checkbox_and_count(self):
        children, roots = ex._build_tree(self.TASKS, include_done=False)
        live = {t: v for t, v in self.TASKS.items() if v["status"] != "done"}
        md = ex._render_tree(live, children, roots, "Demo tasks")
        self.assertIn("- [ ] Ship", md)
        self.assertIn("  - [ ] Build, test", md)           # nested one level under Ship
        self.assertIn("2 tasks, 0 done, 2 open.", md)

    def test_include_done_shows_all(self):
        children, roots = ex._build_tree(self.TASKS, include_done=True)
        self.assertEqual(roots, ["Ship"])
        self.assertEqual(children["Build, test"], ["Design"])


class TestPathHelpers(unittest.TestCase):
    def test_seg_fallback_and_slug(self):
        self.assertEqual(ex._seg(None), "unknown")
        self.assertEqual(ex._seg("In Progress"), "in-progress")
        self.assertEqual(ex._seg(""), "unknown")

    def test_ext_seg(self):
        self.assertEqual(ex._ext_seg("/a/b/Notes.MD"), "md")
        self.assertEqual(ex._ext_seg("/a/b/script.py"), "py")
        self.assertEqual(ex._ext_seg("/a/b/README"), "no-ext")
        self.assertEqual(ex._ext_seg("https://x.com/f.docx?v=2"), "docx")
        self.assertEqual(ex._ext_seg(None), "no-ext")

    def test_isoweek(self):
        d = datetime(2026, 7, 6)
        self.assertEqual(ex._isoweek(d), f"{d.isocalendar()[0]}-W{d.isocalendar()[1]:02d}")
        self.assertEqual(ex._isoweek("2026-07-06T10:00:00"), ex._isoweek(d))
        self.assertEqual(ex._isoweek(None), "undated")

    def test_uniq_suffixes(self):
        used = set()
        self.assertEqual(ex._uniq(used, "a/b.md"), "a/b.md")
        self.assertEqual(ex._uniq(used, "a/b.md"), "a/b-01.md")
        self.assertEqual(ex._uniq(used, "a/b.md"), "a/b-02.md")


U = "2f0c4a1e-1111-4222-8333-444455556666"


class StubDB:
    """Minimal dbx: returns canned rows by matching the table in the SQL. Junction/edit queries
    return empty so build_bundle exercises pure pathing without a real database."""
    def __init__(self, rows):
        self.rows = rows

    def query(self, sql, params=None):
        s = sql.lower()
        if "from pages" in s:
            return self.rows["pages"]
        if "from headers" in s:
            return []
        if "from tasks" in s and "task_dependencies" not in s:
            return self.rows["tasks"]
        if "from decisions" in s:
            return self.rows["decisions"]
        if "from sources" in s:
            return self.rows["sources"]
        if "from groups" in s:
            return self.rows["groups"]
        return []  # links, task_dependencies, header_sources, group_links, edits

    def query_one(self, sql, params=None):
        return None


def _bundle_paths(rows):
    stub = StubDB(rows)
    files = ex.build_bundle(stub, list(ex.TABLES), None)
    return {rel for rel, _ in files}


class TestBundleLayout(unittest.TestCase):
    def _rows(self):
        return {
            "pages": [{"id": U, "title": "Alpha: one", "summary": "s", "type": "note",
                       "freshness": "hot", "created_at": None, "updated_at": None,
                       "contributors": []}],
            "tasks": [{"id": U, "title": "Do X", "description": "d", "status": "done",
                       "priority": "low", "scope": "days", "created_at": None,
                       "updated_at": None, "deadline": None, "responsible": []}],
            "decisions": [{"id": U, "title": "Pick Y", "description": "d",
                           "decided_at": datetime(2026, 7, 6), "created_at": None,
                           "updated_at": None, "decided_by": "alice"}],
            "sources": [
                {"id": U, "title": "My Notes", "type": "file", "reference": "/a/b/notes.md",
                 "summary": "s", "created_at": None, "updated_at": None, "sha256": ""},
                {"id": U, "title": "Site", "type": "website", "reference": "https://x.com",
                 "summary": "s", "created_at": None, "updated_at": None, "sha256": ""},
            ],
            "groups": [{"id": U, "name": "Infra", "type": "topic", "description": "d",
                        "participants": []}],
        }

    def test_folder_tree(self):
        paths = _bundle_paths(self._rows())
        d = datetime(2026, 7, 6)
        wk = f"{d.isocalendar()[0]}-W{d.isocalendar()[1]:02d}"
        self.assertIn("pages/note/alpha-one.md", paths)   # no freshness subfolder
        self.assertIn("tasks/done/do-x.md", paths)
        self.assertIn(f"decisions/{wk}/pick-y.md", paths)
        self.assertIn("sources/file/md/my-notes.md", paths)   # file source split by extension
        self.assertIn("sources/website/site.md", paths)       # non-file uses sourcetype only
        self.assertIn("groups/topic/infra.md", paths)
        self.assertIn("index.md", paths)
        self.assertIn("log.md", paths)

    def test_collision_suffix(self):
        rows = self._rows()
        rows["tasks"] = [
            {"id": U, "title": "Dup", "description": "", "status": "todo", "priority": "low",
             "scope": "days", "created_at": None, "updated_at": None, "deadline": None,
             "responsible": []},
            {"id": "9" + U[1:], "title": "Dup", "description": "", "status": "todo",
             "priority": "low", "scope": "days", "created_at": None, "updated_at": None,
             "deadline": None, "responsible": []},
        ]
        paths = _bundle_paths(rows)
        self.assertIn("tasks/todo/dup.md", paths)
        self.assertIn("tasks/todo/dup-01.md", paths)


if __name__ == "__main__":
    unittest.main()
