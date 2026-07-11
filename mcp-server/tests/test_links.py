"""Markdown-link parsing for the graph."""
import unittest

from kovault_mcp import links

U1 = "2f0c4a1e-1111-4222-8333-444455556666"
U2 = "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee"


class TestParse(unittest.TestCase):
    def test_parses_kind_uuid_links(self):
        text = f"see [deploy guide](header:{U1}) and [task](task:{U2})."
        self.assertEqual(links.parse_links(text), {("header", U1), ("task", U2)})

    def test_all_kinds(self):
        for kind in ("page", "header", "task", "decision", "source"):
            self.assertEqual(links.parse_links(f"[x]({kind}:{U1})"), {(kind, U1)})

    def test_ignores_http_links(self):
        self.assertEqual(links.parse_links("[site](https://example.com) [x](http://a.b)"), set())

    def test_ignores_bad_uuid_and_unknown_kind(self):
        self.assertEqual(links.parse_links("[x](header:not-a-uuid)"), set())
        self.assertEqual(links.parse_links(f"[x](widget:{U1})"), set())

    def test_dedup_and_case_insensitive(self):
        text = f"[a](Header:{U1.upper()}) [b](header:{U1})"
        self.assertEqual(links.parse_links(text), {("header", U1)})

    def test_none_and_empty(self):
        self.assertEqual(links.parse_links(None), set())
        self.assertEqual(links.parse_links(""), set())


class TestObsidian(unittest.TestCase):
    def test_parses_plain_alias_and_heading(self):
        self.assertEqual(links.parse_obsidian_links("see [[Deploy Guide]]"),
                         [("[[Deploy Guide]]", "Deploy Guide", None)])
        self.assertEqual(links.parse_obsidian_links("[[Deploy Guide|the guide]]"),
                         [("[[Deploy Guide|the guide]]", "Deploy Guide", "the guide")])
        # #heading is dropped from the resolution target
        self.assertEqual(links.parse_obsidian_links("[[Deploy Guide#Steps]]")[0][1], "Deploy Guide")
        self.assertEqual(links.parse_obsidian_links("[[Deploy Guide#Steps|steps]]")[0],
                         ("[[Deploy Guide#Steps|steps]]", "Deploy Guide", "steps"))

    def test_none_and_empty(self):
        self.assertEqual(links.parse_obsidian_links(None), [])
        self.assertEqual(links.parse_obsidian_links("no links here"), [])

    def test_ratio(self):
        self.assertEqual(links.obsidian_ratio(""), 0.0)
        self.assertEqual(links.obsidian_ratio("plain text"), 0.0)
        self.assertEqual(links.obsidian_ratio("[[A]] [[B]]"), 1.0)                 # pure obsidian
        self.assertAlmostEqual(links.obsidian_ratio(f"[x](page:{U1}) [[A]]"), 0.5)  # 1 base + 1 obs
        # 4 base + 1 obsidian = 0.2 exactly -> NOT above the 0.20 convert threshold
        base = " ".join(f"[x](task:{U2})" for _ in range(4))
        self.assertAlmostEqual(links.obsidian_ratio(base + " [[A]]"), 0.2)

    def test_base_link_count_counts_instances(self):
        self.assertEqual(links.base_link_count(f"[a](page:{U1}) [b](page:{U1})"), 2)


class TestDiff(unittest.TestCase):
    def test_diff(self):
        old = {("header", U1)}
        new = {("header", U1), ("task", U2)}
        add, remove = links.diff_links(old, new)
        self.assertEqual(add, {("task", U2)})
        self.assertEqual(remove, set())

    def test_diff_removes(self):
        add, remove = links.diff_links({("task", U2)}, set())
        self.assertEqual((add, remove), (set(), {("task", U2)}))


if __name__ == "__main__":
    unittest.main()
