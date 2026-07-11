"""RRF fusion, cutoff ladder, page aggregation, hop points — pure math."""
import math
import unittest

from kovault_mcp import search as se


class TestRRF(unittest.TestCase):
    def test_fuse_sums_reciprocal_ranks(self):
        k = 60
        s1 = se.dense_ranks([("a", 0.9), ("b", 0.8), ("c", 0.7)])   # ranks a=1, b=2, c=3
        s2 = se.dense_ranks([("b", 0.5), ("a", 0.4)])               # ranks b=1, a=2
        out = se.rrf_fuse([s1, s2], k)
        self.assertAlmostEqual(out["a"], 1 / (k + 1) + 1 / (k + 2))
        self.assertAlmostEqual(out["b"], 1 / (k + 2) + 1 / (k + 1))
        self.assertAlmostEqual(out["c"], 1 / (k + 3))
        self.assertAlmostEqual(out["a"], out["b"])

    def test_dense_ranks_ties_share_rank(self):
        # option B: equal (graph) scores get the SAME dense rank, no gaps -> equal RRF share
        r = se.dense_ranks([("a", 4), ("b", 4), ("c", 2), ("d", 1)])
        self.assertEqual((r["a"], r["b"], r["c"], r["d"]), (1, 1, 2, 3))

    def test_dense_rank_ties_contribute_equally_in_rrf(self):
        k = 60
        ranks = se.dense_ranks([("a", 4), ("b", 4), ("c", 4)])       # three-way tie -> all rank 1
        out = se.rrf_fuse([ranks], k)
        self.assertAlmostEqual(out["a"], out["b"])
        self.assertAlmostEqual(out["b"], out["c"])
        self.assertAlmostEqual(out["a"], 1 / (k + 1))                # not arbitrary 1,2,3

    def test_order_by_score_desc_deterministic(self):
        ordered = se.order_by_score({"x": 0.1, "y": 0.3, "z": 0.3})
        self.assertEqual(ordered[0][0], "y")           # highest
        self.assertEqual([i for i, _ in ordered[1:]], ["z", "x"])  # tie y/z? no: y,z=0.3 -> id order


class TestLadder(unittest.TestCase):
    def L(self, scores, r, floor, cap):
        ranked = [(str(i), s) for i, s in enumerate(scores)]
        return [s for _, s in se.apply_ladder(ranked, r, floor, cap)]

    def test_empty_stays_empty(self):
        self.assertEqual(se.apply_ladder([], 0.7, 3, 9), [])

    def test_floor_applies_only_up_to_length(self):
        self.assertEqual(self.L([10, 9], 0.7, 3, 9), [10, 9])

    def test_floor_then_threshold_cut(self):
        # floor=3 kept; rank4 = 1 < 0.7*10 -> stop
        self.assertEqual(self.L([10, 9, 8, 1, 0.5], 0.7, 3, 9), [10, 9, 8])

    def test_threshold_keeps_strong_tail(self):
        self.assertEqual(self.L([10, 9, 8, 7.5, 7.1, 1], 0.7, 3, 9), [10, 9, 8, 7.5, 7.1])

    def test_cap_bounds(self):
        self.assertEqual(len(self.L([9] * 20, 0.7, 3, 9)), 9)

    def test_pages_ladder_floor1_cap6(self):
        self.assertEqual(self.L([5, 0.1, 0.1], 0.75, 1, 6), [5])


class TestPageAggregation(unittest.TestCase):
    def test_topk_formula(self):
        self.assertEqual(se.page_topk(0), 0)
        self.assertEqual(se.page_topk(1), 1)
        self.assertEqual(se.page_topk(2), 1)   # ceil(1.0)
        self.assertEqual(se.page_topk(3), 2)   # ceil(1.5)
        self.assertEqual(se.page_topk(10), 5)  # ceil(5) capped at 5
        self.assertEqual(se.page_topk(20), 5)  # capped

    def test_mean_of_topk_with_zero_pad(self):
        agg = se.aggregate_page_signal({"p": [0.9, 0.1]}, {"p": 2})   # k=1
        self.assertAlmostEqual(agg["p"], 0.9)
        agg2 = se.aggregate_page_signal({"p": [0.9]}, {"p": 4})       # k=2, one header -> pad 0
        self.assertAlmostEqual(agg2["p"], 0.45)

    def test_long_page_cannot_win_by_volume(self):
        one_great = se.aggregate_page_signal({"a": [0.95]}, {"a": 1})["a"]
        many_mediocre = se.aggregate_page_signal({"b": [0.5] * 10}, {"b": 10})["b"]
        self.assertGreater(one_great, many_mediocre)


class TestGraph(unittest.TestCase):
    def test_hop_points(self):
        self.assertEqual([se.hop_points(h) for h in range(6)], [4, 3, 2, 1, 0, 0])

    def test_bfs_sql_shape(self):
        # sanity: the recursive CTE names the pieces the server relies on
        self.assertIn("WITH RECURSIVE", se.GRAPH_BFS_SQL)
        self.assertIn("edges", se.GRAPH_BFS_SQL)
        self.assertIn("anchors", se.GRAPH_BFS_SQL)
        self.assertIn("bfs.hops < 3", se.GRAPH_BFS_SQL)


if __name__ == "__main__":
    unittest.main()
