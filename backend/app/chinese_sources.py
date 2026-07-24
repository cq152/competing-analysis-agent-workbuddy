"""[v3 占位] 中文数据源层 —— 默认关闭，W4 按需实接。

v3（09 附录C.C.3 Task 2.8）将其从「必接」降级为「接口预留、默认关闭」：
- G6（中文优先）降级为目标非红线；
- 14 对比实验证明无中文源时裸 LLM 质量已够 SMB 用；
- 是否实接取决于 W5 用户验证是否在乎来源引用。

本文件仅定义接口与数据结构占位，不做真实 API 调用。
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Optional


class SourceType(str, Enum):
    """数据来源类型（v3 仅占位，W4 实接时扩写）。"""

    GENERAL_WEB = "general_web"
    CHINESE_SOCIAL = "chinese_social"   # 小红书 / 知乎 / 微博等（占位）
    TIANYANCHA = "tianyancha"           # 工商 / 融资（占位）
    THIRD_PARTY = "third_party"         # 七麦等第三方（占位）


@dataclass
class DataPoint:
    """统一数据点格式（与引擎层交换用）。"""

    title: str
    content: str = ""
    url: str = ""
    source_type: SourceType = SourceType.GENERAL_WEB
    source_name: str = ""


class ChineseSourceManager:
    """中文数据源管理器（v3 占位实现）。

    默认 enable=False：search() 返回空列表，引擎层据此走通用搜索 + 优雅降级。
    W4 实接天眼查 / site: 时，在此实现 search() 并读取 config 开关。
    """

    def __init__(self, enable: bool = False) -> None:
        self._enable = enable

    @property
    def enabled(self) -> bool:
        return self._enable

    def search(
        self, scene: str, query: str, target: Optional[str] = None
    ) -> list[DataPoint]:
        """中文数据源搜索（v3 占位）。

        Returns:
            默认关闭时返回 []；启用但未实现时抛 NotImplementedError 指回 W4。
        """
        if not self._enable:
            return []
        # TODO(W4): 实接天眼查 API + site: 搜索；当前为占位，启用后需补充实现
        raise NotImplementedError(
            "中文数据源 v3 为占位接口，默认关闭；实接请见 09 附录C.C.5（W4 Task 4.1）"
        )
