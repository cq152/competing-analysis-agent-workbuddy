"""数据模型：结构化分析结果（v3 最小集）。

v3 仅要求 analyze() 返回带来源的结果；中文数据源（chinese_sources）为占位、默认关闭，
故本模块不依赖 chinese_sources，保持自包含，避免循环导入。
"""
from datetime import datetime
from typing import Optional

from pydantic import BaseModel, Field


class Source(BaseModel):
    """单条引用来源。"""

    url: str = ""
    title: str = ""
    snippet: str = ""                 # 引用的原文片段
    source_type: str = "general_web"  # general_web / chinese_social / tianyancha / third_party
    source_display: str = ""          # 展示名：「网页搜索」「天眼查」等
    relevance: str = "high"           # high / medium / low


class AnalysisResult(BaseModel):
    """一次分析的结构化结果（v3 引擎返回类型）。"""

    scene: str = ""
    query: str = ""
    analysis: str                     # LLM 生成的分析文本
    sources: list[Source] = Field(default_factory=list)
    confidence: str = "medium"        # high / medium / low
    ts: datetime = Field(default_factory=datetime.now)
    # 覆盖率指标：{"general_web": 3, "chinese_social": 0, "tianyancha": 0, "total": 3}
    coverage_summary: dict = Field(default_factory=dict)
    note: str = ""                    # 优雅降级备注（如「未能获取实时数据，待核实」）


class CompareResult(BaseModel):
    """竞品对比结果（预留，v3 暂未启用）。"""

    targets: list[str] = Field(default_factory=list)
    matrix: dict = Field(default_factory=dict)       # 维度 → {竞品: 值}
    insights: list[str] = Field(default_factory=list)
    sources: list[Source] = Field(default_factory=list)
