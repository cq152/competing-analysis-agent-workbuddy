"""
变化检测（v3 最小版）—— 简单集合 diff，不做类型识别。

v3 定位：监控雷达的"变化"判定先用最简方案（标题/域名集合差异），
类型识别（定价/产品/融资/招聘）留到 W4 深化（见 09 附录C C.5）。
"""
from dataclasses import dataclass, field


@dataclass
class ChangeSet:
    added: list[str] = field(default_factory=list)    # 新增的标题/域名
    removed: list[str] = field(default_factory=list)  # 消失的标题/域名
    has_change: bool = False


def detect_changes(old_items: list[str], new_items: list[str]) -> ChangeSet:
    """对比新旧条目集合，返回新增/消失。

    Args:
        old_items: 上次检查时的条目列表（标题或 "标题 (域名)"）
        new_items: 本次检查时的条目列表

    Returns:
        ChangeSet（has_change 为是否有任何变化）
    """
    old_set = set(old_items)
    new_set = set(new_items)
    added = [x for x in new_items if x not in old_set]
    removed = [x for x in old_items if x not in new_set]
    return ChangeSet(added=added, removed=removed, has_change=bool(added or removed))
