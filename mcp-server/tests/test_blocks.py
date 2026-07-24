"""blocks.parse_block — the inverse of render.py. Pure round-trip tests, no DB."""
import unittest

from kovault_mcp import blocks as bl
from kovault_mcp import render as rnd

TID = "11111111-1111-1111-1111-111111111111"
PID = "22222222-2222-2222-2222-222222222222"
HID = "33333333-3333-3333-3333-333333333333"


class TestRoundTrip(unittest.TestCase):
    def test_task_round_trip(self):
        row = {"title": "Do X: now", "id": TID, "description": "desc",
               "created_at": None, "updated_at": None, "status": "todo",
               "priority": "high", "scope": "hours", "deadline": None,
               "responsible": ["alice", "bob"]}
        p = bl.parse_block(rnd.render_task(row, blockers=[], links=[]))
        self.assertEqual((p["kind"], p["table"], p["id"]), ("task", "tasks", TID))
        self.assertEqual(p["fields"], {
            "title": "Do X: now", "description": "desc", "status": "todo",
            "priority": "high", "scope": "hours", "deadline": None,
            "responsible": ["alice", "bob"]})
        # read-only/computed keys never become writable fields
        for k in ("created", "updated", "blockers", "related", "id", "type"):
            self.assertNotIn(k, p["fields"])

    def test_decision_round_trip_at_by_mapping(self):
        row = {"title": "Pick B", "id": TID, "description": "why",
               "created_at": None, "updated_at": None,
               "decided_at": None, "decided_by": "alice"}
        p = bl.parse_block(rnd.render_decision(row, links=[]))
        self.assertEqual(p["kind"], "decision")
        self.assertEqual(p["fields"], {"title": "Pick B", "description": "why",
                                       "decided_at": None, "decided_by": "alice"})

    def test_source_round_trip_sourcetype_and_summary(self):
        row = {"type": "website", "title": "Docs", "reference": "http://x/y",
               "id": TID, "summary": "a ref", "created_at": None, "updated_at": None,
               "sha256": None}
        p = bl.parse_block(rnd.render_source(row, referenced_by=[]))
        self.assertEqual(p["kind"], "source")
        # sourcetype -> type column, description -> summary column
        self.assertEqual(p["fields"]["type"], "website")
        self.assertEqual(p["fields"]["summary"], "a ref")
        self.assertEqual(p["fields"]["reference"], "http://x/y")

    def test_group_round_trip_grouptype(self):
        row = {"type": "project", "name": "Kovault", "id": TID,
               "description": "the vault", "participants": ["alice"]}
        p = bl.parse_block(rnd.render_group(row, members=[("task", TID, "x")]))
        self.assertEqual(p["kind"], "group")
        self.assertEqual(p["fields"]["type"], "project")
        self.assertEqual(p["fields"]["name"], "Kovault")
        self.assertEqual(p["fields"]["participants"], ["alice"])
        # members roster is read-only on write
        self.assertNotIn("members", p["fields"])

    def test_page_detected_by_exclusion(self):
        page = {"type": "note", "title": "Home", "id": PID, "summary": "hub",
                "created_at": None, "updated_at": None, "freshness": "hot",
                "contributors": ["alice"]}
        headers = [{"title": "Intro", "body": "hello", "page_id": PID, "index": 0}]
        p = bl.parse_block(rnd.render_page(page, headers))
        self.assertEqual(p["kind"], "page")
        self.assertEqual(p["fields"]["type"], "note")       # page type preserved
        self.assertEqual(p["fields"]["summary"], "hub")     # description -> summary
        self.assertEqual(p["fields"]["freshness"], "hot")
        self.assertEqual(p["fields"]["contributors"], ["alice"])  # now rewritable (A2)
        self.assertEqual(p["warnings"], [])                  # clean round-trip => no anomalies


class TestAnomalies(unittest.TestCase):
    """No silent failures (A1): write reports keys it would otherwise drop."""

    def test_bogus_key_warns(self):
        p = bl.parse_block(f"---\ntype: task\nid: {TID}\ntitle: X\nbogus: 1\n---")
        self.assertTrue(any("bogus" in w for w in p["warnings"]))

    def test_page_summary_alias_hint(self):
        # old insert/update used the `summary` column; the write template key is `description`
        p = bl.parse_block(f"---\ntype: note\nid: {PID}\nsummary: hub\n---")
        self.assertTrue(any("summary" in w and "description" in w for w in p["warnings"]))
        self.assertNotIn("summary", p["fields"])             # dropped, but now reported

    def test_blockers_are_a_write_field_now(self):
        # blockers reconcile via write (task_dependencies) — a recognized field, no "use the link
        # tool" warning; the value parses to ids and drops the ` — title` label sugar
        p = bl.parse_block(f"---\ntype: task\nid: {TID}\ntitle: X\nblockers: {PID} — some task\n---")
        self.assertEqual(p["warnings"], [])
        self.assertEqual(p["blockers"], [PID])

    def test_empty_junction_key_is_quiet_and_clears(self):
        # a clean round-trip echoes `blockers:` empty — no warn, and it means "clear all" ([])
        p = bl.parse_block(f"---\ntype: task\nid: {TID}\ntitle: X\nblockers: \n---")
        self.assertEqual(p["warnings"], [])
        self.assertEqual(p["blockers"], [])              # present-but-empty -> clear the junction

    def test_absent_junction_key_is_left_unchanged(self):
        p = bl.parse_block(f"---\ntype: task\nid: {TID}\ntitle: X\n---")
        self.assertNotIn("blockers", p)                  # omitted -> leave the junction untouched


class TestJunctionRoundTrips(unittest.TestCase):
    """The four fields that let `write` fold group/link: members, blockers, sources, archived."""

    def test_group_members_round_trip_ids_only(self):
        row = {"type": "project", "name": "Kovault", "id": TID, "description": "d",
               "participants": ["alice"], "archived_at": None}
        # rendered with labels and (ids_only) both — parse must keep just the entity ids
        p = bl.parse_block(rnd.render_group(row, members=[("page", PID, "Home"), ("task", HID, "Do")]))
        self.assertEqual(p["members"], [PID, HID])
        p2 = bl.parse_block(rnd.render_group(row, members=[("page", PID, "Home")], ids_only=True))
        self.assertEqual(p2["members"], [PID])

    def test_task_blockers_round_trip(self):
        row = {"title": "ship", "id": TID, "description": "", "status": "todo",
               "priority": None, "scope": None, "deadline": None, "responsible": []}
        p = bl.parse_block(rnd.render_task(row, blockers=[f"{PID} — design", f"{HID} — spec"]))
        self.assertEqual(p["blockers"], [PID, HID])

    def test_header_sources_parsed(self):
        text = (f"---\ntype: header\nid: {HID}\npage_id: {PID}\nindex: 0\n"
                f"title: T\nsources: {TID}, {PID}\n---\nbody")
        p = bl.parse_block(text)
        self.assertEqual(p["sources"], [TID, PID])
        self.assertEqual(p["warnings"], [])              # `sources:` is recognized, not "unknown key"

    def test_group_archived_round_trip(self):
        live = {"type": "topic", "name": "G", "id": TID, "archived_at": None}
        self.assertEqual(bl.parse_block(rnd.render_group(live))["archived"], "")   # live -> clear
        arch = {**live, "archived_at": "2026-03-01T00:00:00+00:00"}
        self.assertEqual(bl.parse_block(rnd.render_group(arch))["archived"], "2026-03-01T00:00:00+00:00")


class TestHeaderBlock(unittest.TestCase):
    def test_header_body_keeps_horizontal_rules(self):
        text = (
            "---\n"
            "type: header\n"
            f"id: {HID}\n"
            f"page_id: {PID}\n"
            "index: 2\n"
            "title: Intro\n"
            "blurb: the intro\n"
            "---\n"
            "Body line 1\n\n---\n\nAfter a rule")
        p = bl.parse_block(text)
        self.assertEqual((p["kind"], p["id"]), ("header", HID))
        self.assertEqual(p["fields"]["page_id"], PID)
        self.assertEqual(p["fields"]["index"], "2")
        self.assertEqual(p["fields"]["title"], "Intro")
        self.assertIn("---", p["fields"]["body"])           # a --- inside the body survives
        self.assertIn("After a rule", p["fields"]["body"])


class TestClassifyAndValues(unittest.TestCase):
    def test_type_variants(self):
        self.assertEqual(bl.classify({"type": "task"}), "task")
        self.assertEqual(bl.classify({"type": "report"}), "page")   # not a marker -> page
        self.assertEqual(bl.classify({"type": ""}), "page")
        self.assertEqual(bl.classify({}), "page")

    def test_empty_value_becomes_none(self):
        p = bl.parse_block(f"---\ntype: task\nid: {TID}\ntitle: \nstatus: todo\n---")
        self.assertIsNone(p["fields"]["title"])             # present-but-empty -> None (clear)
        self.assertEqual(p["fields"]["status"], "todo")

    def test_quoted_value_with_colon_and_escapes(self):
        self.assertEqual(bl._unquote(r'"a: b"'), "a: b")
        self.assertEqual(bl._unquote(r'"line1\nline2"'), "line1\nline2")
        self.assertEqual(bl._unquote(r'"a \"q\" b"'), 'a "q" b')
        self.assertEqual(bl._unquote("plain"), "plain")

    def test_trashed_flags(self):
        self.assertTrue(bl.parse_block(f"---\ntype: task\nid: {TID}\ntrashed: true\n---")["trashed"])
        self.assertTrue(bl.parse_block(f"---\ntype: note\nid: {PID}\nfreshness: trashed\n---")["trashed"])
        self.assertFalse(bl.parse_block(f"---\ntype: task\nid: {TID}\nstatus: done\n---")["trashed"])

    def test_edit_kind_delete(self):
        p = bl.parse_block(f"---\ntype: edit\nid: {TID}\ntrashed: true\n---")
        self.assertEqual((p["kind"], p["table"]), ("edit", "edits"))
        self.assertTrue(p["trashed"])
        self.assertEqual(p["warnings"], [])                 # id/type/trashed are all recognized

    def test_malformed_raises(self):
        with self.assertRaises(bl.BlockError):
            bl.parse_block("no fence here")
        with self.assertRaises(bl.BlockError):
            bl.parse_block("---\ntype: task\nno closing fence")

    def test_duplicate_key_raises(self):
        # a stray second `type:` (grouptype was meant) must not silently overwrite the marker
        with self.assertRaises(bl.BlockError):
            bl.parse_block("---\ntype: group\nname: x\ntype: topic\n---")


if __name__ == "__main__":
    unittest.main()
