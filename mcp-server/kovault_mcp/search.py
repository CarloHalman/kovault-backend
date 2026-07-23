"""Search fusion + scoring.

Three signals — vector (pgvector cosine), keyword (pg_search BM25), graph (recursive CTE
over `links`) — fused with pure unweighted RRF. RRF consumes RANKS, not raw scores, so no
normalization. The cutoff ladder then trims weak tails. Page scores aggregate their headers.

The math here is pure and unit-tested. The graph traversal SQL constant lives here too but
is executed by the server (db.py), not this module.
"""
from __future__ import annotations

import math
import re
import unicodedata
from collections import defaultdict


def normalize_term(s: str | None) -> str:
    r"""Normalize a title/query for the trigram arm (F2): strip diacritics, drop hyphens/whitespace,
    lowercase. Mirrors the SQL generated column
    `lower(f_unaccent(regexp_replace(col,'[-\s]+','','g')))` so a query term matches the stored
    normalized form (E-drawing / e drawing / Edrawing -> edrawing; Emp-Viewer -> empviewer)."""
    s = unicodedata.normalize("NFKD", s or "")
    s = "".join(c for c in s if not unicodedata.combining(c))
    return re.sub(r"[-\s]+", "", s).lower()

# ---------------------------------------------------------------------------------------
# RRF fusion
# ---------------------------------------------------------------------------------------

def dense_ranks(scored) -> dict:
    """Assign 1-based DENSE ranks to (id, score) items: equal scores share a rank, no gaps
    (scores [4,4,2,1] -> ranks 1,1,2,3). This is the tie policy for RRF — tied rows (very
    common for the integer graph score) contribute equally instead of getting arbitrary
    distinct ranks from the sort. Returns {id: rank}. Determinism: the sort breaks score ties
    by str(id), but tied scores get the SAME rank regardless of that order.
    """
    ranks: dict = {}
    rank = 0
    prev = None
    for rid, score in sorted(scored, key=lambda kv: (-kv[1], str(kv[0]))):
        if prev is None or score != prev:
            rank += 1
            prev = score
        ranks[rid] = rank
    return ranks


def rrf_fuse(rank_maps: list[dict], k: int = 60) -> dict:
    """Reciprocal Rank Fusion over per-signal rank maps ({id: rank}, e.g. from dense_ranks).

    Each map contributes 1/(k + rank) to the ids it contains. Ids absent from a signal simply
    get nothing from it. RRF consumes ranks, not raw scores — no normalization. Returns
    {id: fused_score}.
    """
    scores: dict = defaultdict(float)
    for rmap in rank_maps:
        for rid, rank in rmap.items():
            scores[rid] += 1.0 / (k + rank)
    return dict(scores)


def order_by_score(score_map: dict) -> list[tuple]:
    """Sort {id: score} into [(id, score)] descending; ties broken by str(id) for determinism."""
    return sorted(score_map.items(), key=lambda kv: (-kv[1], str(kv[0])))


# ---------------------------------------------------------------------------------------
# Cutoff ladder (relative-threshold). All constants come from the settings table.
# ---------------------------------------------------------------------------------------

def apply_ladder(ranked: list[tuple], r: float, floor: int, cap: int) -> list[tuple]:
    """Trim a descending [(id, score), ...] list.

    Keep ranks 1..floor unconditionally (floor only bites when matches exist), then keep
    each further rank i (floor+1..cap) while score_i >= r * score_1, and hard-cap at `cap`.
    An empty input stays empty.
    """
    if not ranked:
        return []
    top = ranked[0][1]
    n = len(ranked)
    kept = list(ranked[: min(floor, n)])
    for i in range(floor, min(cap, n)):
        if top > 0 and ranked[i][1] >= r * top:
            kept.append(ranked[i])
        else:
            break
    return kept


# ---------------------------------------------------------------------------------------
# Page score aggregation.
# A page's score per signal = mean of its top min(ceil(50% of live headers), 5) headers by
# that signal. Headers that didn't score for a signal count as 0. Pages then rank by RRF
# over those aggregated per-signal ranks.
# ---------------------------------------------------------------------------------------

def page_topk(live_header_count: int) -> int:
    """min(ceil(50% of live headers), 5), at least 1 when the page has any live header."""
    if live_header_count <= 0:
        return 0
    return max(1, min(math.ceil(0.5 * live_header_count), 5))


def aggregate_page_signal(
    header_scores_by_page: dict, live_counts: dict
) -> dict:
    """{page_id: [header scores for one signal]} + {page_id: live_header_count}
    -> {page_id: aggregated score for that signal}.

    Zero-pads to k so a page with one great chunk isn't unfairly averaged against absent
    headers, while long pages can't win by sheer volume.
    """
    out: dict = {}
    for page_id, scores in header_scores_by_page.items():
        k = page_topk(live_counts.get(page_id, len(scores)))
        if k == 0:
            continue
        top = sorted(scores, reverse=True)[:k]
        top += [0.0] * (k - len(top))          # zero-pad missing headers
        out[page_id] = sum(top) / k
    return out


# ---------------------------------------------------------------------------------------
# Graph traversal — recursive CTE over `links`, undirected, <= 3 hops (BUILD.md B5).
#
# `edges` symmetrises `links` (a non-recursive CTE) so the recursive term references the
# working table exactly once — the form Postgres allows. `anchors` = headers whose title
# ILIKEs the topic term. Returns each reached node
# with its shortest hop distance; the caller turns hops into points max(0, 4 - hops) and
# sums good topics minus bad topics.
# ---------------------------------------------------------------------------------------

GRAPH_BFS_SQL = """
WITH RECURSIVE
edges(a_kind, a_id, b_kind, b_id) AS (
    SELECT from_kind, from_id, to_kind, to_id FROM links
    UNION ALL
    SELECT to_kind, to_id, from_kind, from_id FROM links
),
anchors(kind, id) AS (
    SELECT 'header'::link_kind, h.id
    FROM headers h
    WHERE h.trashed_at IS NULL AND h.title ILIKE %(pat)s
),
bfs(kind, id, hops) AS (
    SELECT kind, id, 0 FROM anchors
    UNION
    SELECT e.b_kind, e.b_id, bfs.hops + 1
    FROM bfs
    JOIN edges e ON e.a_kind = bfs.kind AND e.a_id = bfs.id
    WHERE bfs.hops < 3
)
SELECT kind::text AS kind, id, min(hops) AS hops
FROM bfs
GROUP BY kind, id
"""


def hop_points(hops: int) -> int:
    """Direct hit (0 hops) = 4, then minus 1 per hop; 3 hops = 1, further = 0."""
    return max(0, 4 - hops)
