"""Same-activity matching across upload sources (watch vs phone).

决策顺序：
1. 来源ID（source external id / 同一原始文件指纹）相同且时间窗高度重叠 -> 自动判重。
2. 否则必须 *同时* 满足时间相似与空间相似才判重；相邻两段真实训练即使
   首尾时间相接、起终点很近，因时间窗不重叠也不会被合并。
3. 人工决定（confirmed_duplicate / split）优先于一切自动判断；split 永久保留，
   恢复（merge）只能再次人工显式操作。
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import timezone

from .geo import Point, haversine_m
from .rules import Rules


@dataclass
class TimeSim:
    overlap_ratio: float  # 交集 / 较短时长
    start_gap_s: float


def time_similarity(a_start, a_end, b_start, b_end) -> TimeSim:
    a_start, a_end = min(a_start, a_end), max(a_start, a_end)
    b_start, b_end = min(b_start, b_end), max(b_start, b_end)
    inter = max(0.0, (min(a_end, b_end) - max(a_start, b_start)).total_seconds())
    short = max(1.0, min((a_end - a_start).total_seconds(), (b_end - b_start).total_seconds()))
    return TimeSim(inter / short, abs((a_start - b_start).total_seconds()))


def endpoint_distance_m(pa: list[Point], pb: list[Point]) -> float:
    """Mean of start-start and end-end distances (robust to opposite recording order)."""
    if not pa or not pb:
        return float("inf")
    d1 = haversine_m(pa[0].lat, pa[0].lon, pb[0].lat, pb[0].lon)
    d2 = haversine_m(pa[-1].lat, pa[-1].lon, pb[-1].lat, pb[-1].lon)
    return (d1 + d2) / 2


def hausdorff_m(pa: list[Point], pb: list[Point], max_samples: int = 80) -> float:
    """Symmetric discrete Hausdorff distance, sampled for performance."""
    if not pa or not pb:
        return float("inf")

    def sample(pts: list[Point]) -> list[Point]:
        if len(pts) <= max_samples:
            return pts
        step = len(pts) / max_samples
        return [pts[int(i * step)] for i in range(max_samples)]

    a, b = sample(pa), sample(pb)

    def directed(x: list[Point], y: list[Point]) -> float:
        worst = 0.0
        for p in x:
            best = min(haversine_m(p.lat, p.lon, q.lat, q.lon) for q in y)
            worst = max(worst, best)
        return worst

    return max(directed(a, b), directed(b, a))


@dataclass
class MatchResult:
    is_duplicate: bool
    reason: str
    same_source: bool
    time_overlap: float
    start_gap_s: float
    endpoint_m: float
    hausdorff_m: float


def evaluate(
    *,
    source_a: str,
    source_b: str,
    external_id_a: str | None,
    external_id_b: str | None,
    a_start, a_end,
    b_start, b_end,
    pa: list[Point],
    pb: list[Point],
    rules: Rules,
) -> MatchResult:
    same_source = bool(
        source_a
        and source_a == source_b
        and external_id_a
        and external_id_a == external_id_b
    )
    tsim = time_similarity(a_start, a_end, b_start, b_end)
    ep = endpoint_distance_m(pa, pb)
    hd = hausdorff_m(pa, pb)

    time_ok = (
        tsim.overlap_ratio >= rules.dedup_overlap_ratio
        and tsim.start_gap_s <= rules.dedup_time_gap_s
    )
    space_ok = ep <= rules.dedup_endpoint_m and hd <= rules.dedup_hausdorff_m

    if same_source and tsim.overlap_ratio >= rules.dedup_overlap_ratio:
        return MatchResult(True, "same_source_id+time_overlap", True,
                           tsim.overlap_ratio, tsim.start_gap_s, ep, hd)
    # 关键：时间 AND 空间同时相似，绝不只凭时间接近合并
    if time_ok and space_ok:
        return MatchResult(True, "time+space_similar", False,
                           tsim.overlap_ratio, tsim.start_gap_s, ep, hd)
    if time_ok and not space_ok:
        reason = "time_close_but_space_differs->kept_separate"
    elif space_ok and not time_ok:
        reason = "space_close_but_time_disjoint->kept_separate"
    else:
        reason = "distinct_activities"
    return MatchResult(False, reason, False,
                       tsim.overlap_ratio, tsim.start_gap_s, ep, hd)
