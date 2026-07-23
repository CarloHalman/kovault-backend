"""Deterministic embedding-text composition."""
import unittest
from datetime import datetime

from kovault_mcp import embedding_text as et


class TestCompose(unittest.TestCase):
    def test_header_joins_path_blurb_body_skipping_nulls(self):
        row = {"path": "Kovault > Deploy", "blurb": None, "body": "run the script"}
        self.assertEqual(et.compose_header_text(row), "Kovault > Deploy\nrun the script")

    def test_source_prefixes_type(self):
        text = et.compose_source_text({"type": "file", "title": "notes", "summary": "a file"})
        self.assertTrue(text.startswith("source: file"))
        self.assertIn("notes", text)

    def test_task_prefix_and_worded_deadline(self):
        text = et.compose_task_text({
            "title": "ship", "status": "todo", "priority": "high", "scope": "days",
            "deadline": datetime(2020, 12, 10, 21, 20), "description": None,
        })
        self.assertTrue(text.startswith("task:"))
        self.assertIn("10th of December 2020 21:20", text)
        self.assertNotIn("None", text)          # null description skipped

    def test_decision_prefix_and_worded_date(self):
        text = et.compose_decision_text({
            "title": "use CTE", "decided_by": "Alice",
            "decided_at": datetime(2026, 7, 9), "description": "graph engine",
        })
        self.assertTrue(text.startswith("decision:"))
        self.assertIn("9th of July 2026", text)

    def test_compose_dispatch(self):
        self.assertEqual(et.compose("headers", {"path": "P", "body": "B"}), "P\nB")
        self.assertEqual(et.COMPOSERS["sources"][1], "summary_embedding")
        self.assertEqual(et.COMPOSERS["headers"][1], "embedding")


if __name__ == "__main__":
    unittest.main()
