"""Statistics methodology versions.

年报生成时把规则版本冻结进快照；历史年报可用其原始规则重新生成。
"""
from __future__ import annotations

from dataclasses import dataclass, asdict

from .geo import SPEED_SPIKE_MPS, STOP_SPEED_MPS


@dataclass(frozen=True)
class Rules:
    version: str
    label: str
    # 跨午夜里程归属："start_utc" = 开始时刻的 UTC 日期（v1）；
    # "start_local" = 活动时区的开始当地日期（v2，当前规则）
    date_attribution: str
    spike_speed_mps: float
    stop_speed_mps: float
    # 去重：来源ID相同且时间重叠 -> 自动重复；
    # 否则要求时间 AND 空间都相似，绝不只凭时间接近合并
    dedup_overlap_ratio: float
    dedup_endpoint_m: float
    dedup_hausdorff_m: float
    dedup_time_gap_s: float
    # 成绩与训练分开统计
    races_excluded_from_training: bool

    def to_dict(self) -> dict:
        return asdict(self)


RULES: dict[str, Rules] = {
    "v1-2023": Rules(
        version="v1-2023",
        label="2023 年报口径（UTC 归属日）",
        date_attribution="start_utc",
        spike_speed_mps=SPEED_SPIKE_MPS,
        stop_speed_mps=STOP_SPEED_MPS,
        dedup_overlap_ratio=0.8,
        dedup_endpoint_m=80.0,
        dedup_hausdorff_m=120.0,
        dedup_time_gap_s=3600.0,
        races_excluded_from_training=True,
    ),
    "v2-2024": Rules(
        version="v2-2024",
        label="2024 起口径（当地时区归属日）",
        date_attribution="start_local",
        spike_speed_mps=SPEED_SPIKE_MPS,
        stop_speed_mps=STOP_SPEED_MPS,
        dedup_overlap_ratio=0.8,
        dedup_endpoint_m=80.0,
        dedup_hausdorff_m=120.0,
        dedup_time_gap_s=3600.0,
        races_excluded_from_training=True,
    ),
}

CURRENT_RULES = "v2-2024"


def get_rules(version: str | None = None) -> Rules:
    return RULES[version or CURRENT_RULES]
