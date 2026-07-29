"""C2: fetch(members=...) forces a group's roster shape instead of the size threshold deciding it.

'full' = labels, 'ids' = ids only, 'count' = just the number (no roster at all), absent = today's
_GROUP_IDS_ONLY_MAX threshold, byte-identical. The trap: `members:` is a reconciled junction
(blocks._JUNCTION_KEYS) — present-and-empty means "clear every member" on write-back, so count-only
must never render that key at all.
"""
import unittest

from kovault_mcp import blocks as bl
from kovault_mcp import server as sv

GID = "11111111-1111-1111-1111-111111111111"
TID = "22222222-2222-2222-2222-222222222222"


def _col(**kw):
    d = {"type": "text", "udt": "text", "max_len": None, "is_array": False, "is_generated": False}
    return {**d, **kw}


SCHEMA = {
    "groups": {c: _col() for c in ("id", "created_at", "updated_at", "trashed_at", "lifecycle",
                                   "name", "type", "description", "participants", "status")},
}

GROUP_ROW = {"id": GID, "created_at": None, "updated_at": None, "trashed_at": None,
            "lifecycle": "live", "name": "Big Group", "type": "project", "description": "d",
            "participants": None, "status": None}


def _members(n: int) -> list[dict]:
    return [{"kind": "task", "id": f"{i:08x}-0000-0000-0000-000000000000", "label": f"task {i}"}
            for i in range(n)]


class FakeGroupDB:
    def __init__(self, member_rows):
        self.member_rows = member_rows

    def query_one(self, sql, params=None):
        return dict(GROUP_ROW)

    def query(self, sql, params=None):
        return self.member_rows if "FROM group_links" in " ".join(sql.split()) else []


class Case(unittest.TestCase):
    def setUp(self):
        sv._COLS_CACHE.update({t: dict(c) for t, c in SCHEMA.items()})
        self._db = sv._DB

    def tearDown(self):
        for t in SCHEMA:
            sv._COLS_CACHE.pop(t, None)
        sv._DB = self._db

    def fetch(self, n, **kw):
        sv._DB = FakeGroupDB(_members(n))
        return sv.fetch(groups=[GID], **kw)


class TestDefaultThresholdUnchanged(Case):
    def test_below_threshold_still_shows_labels_by_default(self):
        out = self.fetch(3)
        self.assertIn('members: "task:', out)
        self.assertIn("task 0", out)          # labels present
        self.assertNotIn("ids only", out)

    def test_above_threshold_still_goes_ids_only_by_default(self):
        out = self.fetch(30)
        self.assertIn('members: "task:', out)
        self.assertNotIn("task 0", out)       # no label
        self.assertIn("30 members, ids only", out)

    def test_no_param_reproduces_todays_output_byte_for_byte(self):
        # same call twice, with and without the new kwarg at its default (None) — must be identical
        sv._DB = FakeGroupDB(_members(30))
        a = sv.fetch(groups=[GID])
        sv._DB = FakeGroupDB(_members(30))
        b = sv.fetch(groups=[GID], members=None)
        self.assertEqual(a, b)


class TestExplicitModes(Case):
    def test_count_returns_the_number_and_no_ids(self):
        out = self.fetch(142, members="count")
        self.assertIn("142 members", out)
        self.assertNotIn("members:", out)     # the roster KEY itself must not appear
        self.assertNotIn("00000000", out)     # no member id leaked in either

    def test_ids_forces_ids_only_below_the_threshold(self):
        out = self.fetch(3, members="ids")
        self.assertIn('members: "task:', out)
        self.assertNotIn("task 0", out)       # forced ids-only: no label even though small
        self.assertIn("3 members, ids only", out)

    def test_full_forces_labels_above_the_threshold(self):
        out = self.fetch(30, members="full")
        self.assertIn("task 0", out)          # forced labels even though over the threshold
        self.assertNotIn("ids only", out)

    def test_bad_value_is_refused(self):
        out = self.fetch(3, members="bogus")
        self.assertIn("(fetch: members must be", out)
        self.assertIn("bogus", out)


class TestRoundTripNeverTouchesGroupLinks(Case):
    """The acceptance line Alice checks by counting group_links rows before/after."""

    def _no_junction_write(self, n, **kw):
        out = self.fetch(n, **kw)
        p = bl.parse_block(out)
        self.assertEqual(p["kind"], "group")
        return p

    def test_count_mode_has_no_members_key_at_all(self):
        p = self._no_junction_write(142, members="count")
        self.assertNotIn("members", p)         # absent -> write leaves group_links untouched

    def test_ids_mode_carries_the_roster_key_but_parses_to_the_real_ids(self):
        p = self._no_junction_write(3, members="ids")
        self.assertIn("members", p)
        self.assertEqual(len(p["members"]), 3)

    def test_full_mode_carries_the_roster_key_and_parses_to_the_real_ids(self):
        p = self._no_junction_write(3, members="full")
        self.assertIn("members", p)
        self.assertEqual(len(p["members"]), 3)


class TestComposesWithColumnsI1(Case):
    def test_columns_drops_members_count_then_has_nothing_to_act_on(self):
        out = self.fetch(142, columns=["-members"], members="count")
        self.assertNotIn("members:", out)
        self.assertNotIn("142 members", out)   # not resurrected as a note either

    def test_columns_drops_members_ids_mode_also_shows_nothing(self):
        out = self.fetch(30, columns=["-members"], members="ids")
        self.assertNotIn("members:", out)
        self.assertNotIn("ids only", out)

    def test_columns_without_dropping_members_plus_count_still_summarises(self):
        out = self.fetch(142, columns=["-description"], members="count")
        self.assertIn("142 members", out)
        self.assertNotIn("members:", out)


if __name__ == "__main__":
    unittest.main()
