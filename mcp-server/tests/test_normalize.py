"""search.normalize_term — mirrors the SQL normalized column (F2). Pure, no DB."""
import unittest

from kovault_mcp import search as se


class TestNormalizeTerm(unittest.TestCase):
    def test_strips_hyphens_spaces_case(self):
        self.assertEqual(se.normalize_term("E-drawing"), "edrawing")
        self.assertEqual(se.normalize_term("e drawing"), "edrawing")
        self.assertEqual(se.normalize_term("Edrawing"), "edrawing")
        self.assertEqual(se.normalize_term("Emp-Viewer"), "empviewer")

    def test_strips_accents(self):
        self.assertEqual(se.normalize_term("café"), "cafe")
        self.assertEqual(se.normalize_term("naïve"), "naive")

    def test_empty(self):
        self.assertEqual(se.normalize_term(None), "")
        self.assertEqual(se.normalize_term("   "), "")


if __name__ == "__main__":
    unittest.main()
