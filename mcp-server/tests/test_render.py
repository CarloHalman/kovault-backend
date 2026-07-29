"""Fetch/export render formats, and the reflective renderer that produces them.

Fixtures are shaped like the `SELECT *` rows every caller actually passes — including the
columns that must NEVER reach the output — because the row dict IS the reflection the renderer
reads.
"""
import unittest
from datetime import datetime

from kovault_mcp import blocks as bl
from kovault_mcp import render as r

U = "2f0c4a1e-1111-4222-8333-444455556666"

# columns present on every real row and never renderable: halfvec payloads, embed bookkeeping,
# and the GENERATED *_norm trigram columns
MACHINE = {"embedding": "[0.1,0.2]", "summary_embedding": "[0.3]", "embedded_at": datetime(2026, 1, 1),
           "title_norm": "deploy", "blurb_norm": "overview"}


def page_row(**kw):
    return {"id": U, "created_at": datetime(2026, 1, 1), "updated_at": datetime(2026, 2, 1),
            "trashed_at": None, "lifecycle": "live", "title": "Deploy", "summary": "how to deploy",
            "type": "runbook", "contributors": ["alice", "bob"], **kw}


def header_row(**kw):
    return {"id": "h2", "created_at": None, "updated_at": datetime(2026, 2, 1), "trashed_at": None,
            "lifecycle": "live", "page_id": U, "title": "Steps", "index": 1, "level": 2,
            "path": "Deploy > Steps", "blurb": "b", "body": "x", **MACHINE, **kw}


def task_row(**kw):
    return {"id": U, "created_at": None, "updated_at": None, "trashed_at": None,
            "lifecycle": "live", "title": "ship", "description": "d", "status": "todo",
            "priority": "high", "scope": "days", "deadline": None, "completed_at": None,
            "responsible": ["alice"], **MACHINE, **kw}


def group_row(**kw):
    return {"id": U, "created_at": None, "updated_at": None, "trashed_at": None,
            "lifecycle": "live", "name": "Migration", "type": "project", "description": "d",
            "participants": ["alice"], "status": "active", **kw}


class TestRender(unittest.TestCase):
    def test_page_frontmatter_and_lean_chunks(self):
        headers = [
            {"id": "h1", "title": "(intro)", "blurb": "overview", "body": "text",
             "updated_at": datetime(2026, 2, 1), "index": 0},
            {"id": "h2", "title": "Steps", "blurb": "the steps",
             "body": f"1. do it, see [deploy task](task:{U})",  # navigation lives inline in the body
             "updated_at": datetime(2026, 2, 1), "index": 1},
        ]
        out = r.render_page(page_row(), headers)
        self.assertIn("type: runbook", out)              # page frontmatter kept
        self.assertIn("lifecycle: live", out)            # E3 state, replaces freshness
        self.assertIn("trashed: \n", out)                # live page -> empty trash flag
        self.assertIn(f"id: {U}", out)
        self.assertIn("contributors: alice, bob", out)
        self.assertIn("Steps", out)                      # chunk title kept
        self.assertIn(f"[deploy task](task:{U})", out)   # inline link preserved verbatim in body
        # lean chunks: no per-chunk callout / summary / related / fence
        self.assertNotIn("> [!info]", out)
        self.assertNotIn("> Summary:", out)
        self.assertNotIn("> Related:", out)
        self.assertNotIn("type: header", out)            # chunks inside a page carry no frontmatter
        self.assertEqual(out.count("---"), 2)            # exactly one fenced block: the page's

    def test_standalone_chunk_carries_the_full_block(self):
        # A3: a chunk fetch must show its state, and the block has to be writable straight back
        out = r.render_chunk(header_row(), standalone=True)
        self.assertIn("type: header", out)
        self.assertIn("id: h2", out)
        self.assertIn(f"page_id: {U}", out)
        self.assertIn("index: 1", out)
        self.assertIn("trashed: \n", out)
        self.assertIn("title: Steps", out)               # a key, not a line above the body
        self.assertEqual(out.rsplit("---\n", 1)[1].strip(), "x")   # the body is only the body

    def test_standalone_chunk_shows_trashed_state(self):
        out = r.render_chunk(header_row(trashed_at=datetime(2026, 3, 1)), standalone=True)
        self.assertIn("trashed: true", out)

    def test_table_leading_body_gets_blank_line(self):
        # a body starting with a markdown table needs a blank line above the pipe row, or GFM
        # reads it as a paragraph and the table never renders (the Heimdall export bug)
        h = {"id": "h1", "title": "Hardware", "body": "| A | B |\n| --- | --- |\n| 1 | 2 |",
             "page_id": U, "index": 1}
        self.assertIn("\n\n| A | B |", r.render_chunk(h, standalone=False))
        self.assertIn("\n\n| A | B |", r.render_chunk(h, standalone=True))

    def test_task_frontmatter_both_dependency_directions(self):
        out = r.render_task(task_row(), blockers=[f"{U} — design"],
                            links=[("decision", U)], blocking=[f"{U} — ship it"])
        self.assertIn("type: task", out)
        self.assertIn(f"blockers: {U} — design", out)
        self.assertIn(f"blocking: {U} — ship it", out)     # I4: the reverse edge
        self.assertIn(f"related: decision:{U}", out)

    def test_source_and_group(self):
        s = {"id": U, "type": "file", "title": "notes", "reference": "/x", "summary": "s",
             "created_at": None, "updated_at": None, "trashed_at": None, "lifecycle": "live",
             "sha256": "abc", **MACHINE}
        self.assertIn("sourcetype: file", r.render_source(s, ["h1"]))
        g = group_row()
        out = r.render_group(g, [("page", U, "Deploy")])
        self.assertIn("grouptype: project", out)
        # members line holds ': ' (colon-space) so it must be quoted or the YAML block breaks
        self.assertIn(f'members: "page: {U} — Deploy"', out)
        self.assertIn("trashed: \n", out)                # live group -> empty trash flag
        self.assertIn("lifecycle: live", out)            # E3 state, replaces archived_at
        self.assertIn("status: active", out)             # E2: group workflow status
        out = r.render_group(group_row(lifecycle="archived", trashed_at=datetime(2026, 3, 1)))
        self.assertIn("lifecycle: archived", out)
        self.assertIn("trashed: true", out)              # a flag, not a date (the edits log has when)

    def test_colon_values_are_quoted(self):
        # a title/description with ': ' must be quoted so Obsidian doesn't read the tail as a nested key
        out = r.render_task(task_row(title="Plan: beat the old",
                                     description="two gaps: (1) a, (2) b", priority="low"))
        self.assertIn('title: "Plan: beat the old"', out)
        self.assertIn('description: "two gaps: (1) a, (2) b"', out)
        self.assertIn("status: todo", out)  # clean values stay plain


class TestReflective(unittest.TestCase):
    """The renderer reads the row, not a hand-maintained field list."""

    def test_machine_columns_never_render(self):
        for out in (r.render_page(page_row(**MACHINE), []),
                    r.render_task(task_row()),
                    r.render_chunk(header_row(), standalone=True)):
            for col in MACHINE:
                self.assertNotIn(col, out)
            self.assertNotIn("0.1", out)             # nor a vector's contents

    def test_timestamps_still_render(self):
        # created_at/updated_at are on the WRITE deny list and must still be SHOWN — the two
        # exclusion sets are not the same rule
        out = r.render_page(page_row(), [])
        self.assertIn("created: 2026-01-01T00:00:00", out)
        self.assertIn("updated: 2026-02-01T00:00:00", out)

    def test_an_unknown_column_renders_under_its_own_name(self):
        # the Koplan case: a column added to the table shows up with no code change
        out = r.render_task(task_row(planned_start=datetime(2026, 5, 1), effort_points=8))
        self.assertIn("planned_start: 2026-05-01T00:00:00", out)
        self.assertIn("effort_points: 8", out)

    def test_unknown_columns_come_after_the_known_block(self):
        lines = r.render_task(task_row(zz_extra="x")).splitlines()
        self.assertEqual(lines[-2], "zz_extra: x")       # last key before the closing fence
        self.assertLess(lines.index("type: task"), lines.index("zz_extra: x"))

    def test_key_order_is_stable(self):
        lines = r.render_page(page_row(), []).splitlines()
        keys = [ln.split(":")[0] for ln in lines[1:lines.index("---", 1)]]
        self.assertEqual(keys, ["type", "title", "id", "description", "created", "updated",
                                "trashed", "lifecycle", "contributors"])

    def test_template_keys_come_from_field_map(self):
        # description->summary, at->decided_at, by->decided_by: one mapping, not two
        out = r.render_decision({"id": U, "title": "Pick B", "description": "why",
                                 "decided_at": datetime(2026, 4, 1), "decided_by": "alice",
                                 "trashed_at": None, "lifecycle": "live"})
        self.assertIn("at: 2026-04-01T00:00:00", out)
        self.assertIn("by: alice", out)
        self.assertNotIn("decided_at", out)


class TestRoundTrip(unittest.TestCase):
    """Every key rendered must parse back without a warning, for all six kinds — the check that
    catches a renderer change that adds a key nothing on the parse side recognises."""

    def _clean(self, text, kind):
        p = bl.parse_block(text)
        self.assertEqual(p["kind"], kind)
        self.assertEqual(p["warnings"], [], f"{kind}: {p['warnings']}")
        return p

    def test_page(self):
        self._clean(r.render_page(page_row(), []), "page")

    def test_header(self):
        p = self._clean(r.render_chunk(header_row(), standalone=True), "header")
        self.assertEqual(p["fields"]["body"], "x")            # body only — the title is a key
        self.assertEqual(p["fields"]["title"], "Steps")       # ... so it round-trips as one

    def test_task(self):
        self._clean(r.render_task(task_row(), blockers=[f"{U} — d"], links=[("page", U)],
                                  blocking=[f"{U} — e"]), "task")

    def test_decision(self):
        self._clean(r.render_decision({"id": U, "title": "t", "description": "d",
                                       "decided_at": None, "decided_by": "alice",
                                       "created_at": None, "updated_at": None,
                                       "trashed_at": None, "lifecycle": "live"}), "decision")

    def test_source(self):
        self._clean(r.render_source({"id": U, "type": "file", "title": "n", "reference": "/x",
                                     "summary": "s", "sha256": "abc", "created_at": None,
                                     "updated_at": None, "trashed_at": None,
                                     "lifecycle": "live", **MACHINE}, ["h1"]), "source")

    def test_group(self):
        self._clean(r.render_group(group_row(), [("page", U, "Deploy")]), "group")

    def test_an_unknown_column_is_reported_not_silently_dropped(self):
        # a Koplan column renders (read-only) but core cannot write it — the warning is the
        # prompt to give it a FIELD_MAP entry, not a bug
        p = bl.parse_block(r.render_task(task_row(planned_start=None)))
        self.assertTrue(any("planned_start" in w for w in p["warnings"]))


if __name__ == "__main__":
    unittest.main()
