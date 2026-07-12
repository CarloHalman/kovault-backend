"""Fetch/export render formats."""
import unittest
from datetime import datetime

from kovault_mcp import render as r

U = "2f0c4a1e-1111-4222-8333-444455556666"


class TestRender(unittest.TestCase):
    def test_page_frontmatter_and_lean_chunks(self):
        page = {"id": U, "title": "Deploy", "summary": "how to deploy", "type": "runbook",
                "created_at": datetime(2026, 1, 1), "updated_at": datetime(2026, 2, 1),
                "freshness": "hot", "contributors": ["carlo", "ana"]}
        headers = [
            {"id": "h1", "title": "(intro)", "blurb": "overview", "body": "text",
             "updated_at": datetime(2026, 2, 1), "index": 0},
            {"id": "h2", "title": "Steps", "blurb": "the steps",
             "body": f"1. do it, see [deploy task](task:{U})",  # navigation lives inline in the body
             "updated_at": datetime(2026, 2, 1), "index": 1},
        ]
        out = r.render_page(page, headers)
        self.assertIn("type: runbook", out)              # page frontmatter kept
        self.assertIn(f"id: {U}", out)
        self.assertIn("contributors: carlo, ana", out)
        self.assertIn("Steps", out)                      # chunk title kept
        self.assertIn(f"[deploy task](task:{U})", out)   # inline link preserved verbatim in body
        # lean chunks: no per-chunk callout / summary / related / header id
        self.assertNotIn("> [!info]", out)
        self.assertNotIn("> Summary:", out)
        self.assertNotIn("> Related:", out)
        self.assertNotIn("h2", out)                      # header id not shown

    def test_standalone_chunk_is_lean_with_page_locator(self):
        h = {"id": "h2", "title": "Steps", "blurb": "b", "body": "x",
             "updated_at": datetime(2026, 2, 1), "page_id": U, "index": 1}
        out = r.render_chunk(h, standalone=True)
        self.assertIn("Steps", out)
        self.assertIn(f"> page: {U} · index: 1", out)    # minimal locator only
        self.assertNotIn("Summary", out)
        self.assertNotIn("h2", out)                      # no header id
        self.assertNotIn("[!info]", out)

    def test_table_leading_body_gets_blank_line(self):
        # a body starting with a markdown table needs a blank line above the pipe row, or GFM
        # reads it as a paragraph and the table never renders (the Heimdall export bug)
        h = {"id": "h1", "title": "Hardware", "body": "| A | B |\n| --- | --- |\n| 1 | 2 |",
             "page_id": U, "index": 1}
        self.assertIn("\n\n| A | B |", r.render_chunk(h, standalone=False))
        self.assertIn("\n\n| A | B |", r.render_chunk(h, standalone=True))

    def test_task_frontmatter(self):
        t = {"id": U, "title": "ship", "description": "d", "status": "todo", "priority": "high",
             "scope": "days", "created_at": None, "updated_at": None, "deadline": None,
             "responsible": ["carlo"]}
        out = r.render_task(t, blockers=["design"], links=[("decision", U)])
        self.assertIn("type: task", out)
        self.assertIn("blockers: design", out)
        self.assertIn(f"related: decision:{U}", out)

    def test_source_and_group(self):
        s = {"id": U, "type": "file", "title": "notes", "reference": "/x", "summary": "s",
             "created_at": None, "updated_at": None, "sha256": "abc"}
        self.assertIn("sourcetype: file", r.render_source(s, ["h1"]))
        g = {"id": U, "type": "project", "name": "Migration", "description": "d",
             "participants": ["carlo"]}
        out = r.render_group(g, [("page", U, "Deploy")])
        self.assertIn("grouptype: project", out)
        # members line holds ': ' (colon-space) so it must be quoted or the YAML block breaks
        self.assertIn(f'members: "page: {U} — Deploy"', out)

    def test_colon_values_are_quoted(self):
        # a title/description with ': ' must be quoted so Obsidian doesn't read the tail as a nested key
        t = {"id": U, "title": "Plan: beat the old", "description": "two gaps: (1) a, (2) b",
             "status": "todo", "priority": "low", "scope": "days",
             "created_at": None, "updated_at": None, "deadline": None, "responsible": ["carlo"]}
        out = r.render_task(t)
        self.assertIn('title: "Plan: beat the old"', out)
        self.assertIn('description: "two gaps: (1) a, (2) b"', out)
        self.assertIn("status: todo", out)  # clean values stay plain


if __name__ == "__main__":
    unittest.main()
