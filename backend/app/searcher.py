"""
通用搜索层（v3 简化版）—— 基于 ddgs（DuckDuckGo 官方维护包）的网页搜索

v3 调整：砍掉 site: 中文社媒搜索与来源分类（中文数据源降为占位，见 chinese_sources.py 2.8）。
仅保留通用网页搜索，返回标准化 SearchResult 列表。所有方法不对外抛异常（失败返回 [] 并记日志）。

依赖：ddgs（原 duckduckgo-search 已废弃，8.x 实测默认 backend 被限流，须用 backend="lite"）
"""
from dataclasses import dataclass
from enum import Enum
from typing import Optional

from ddgs import DDGS

from app.config import settings
from app.logger import log


class SearchSource(Enum):
    GENERAL = "general_web"    # v3：仅通用网页（中文源分类移至 chinese_sources 占位层）


@dataclass
class SearchResult:
    url: str
    title: str
    snippet: str               # 搜索引擎返回的摘要
    domain: str                # 提取自 URL 的域名
    source: SearchSource = SearchSource.GENERAL
    relevance_score: float = 0.0  # 0-1 相关性评分


class Searcher:
    """DuckDuckGo 通用搜索封装。"""

    def __init__(self, timeout: int = 10, region: str = "wt-wt", backend: str = "lite"):
        self._timeout = timeout
        self._region = region
        self._backend = backend
        self._ddgs = DDGS(timeout=timeout)

    def search(
        self,
        query: str,
        max_results: Optional[int] = None,
        region: Optional[str] = None,
    ) -> list[SearchResult]:
        """通用搜索。

        Args:
            query: 搜索关键词
            max_results: 最大结果数（默认读 settings.search_max_results）
            region: 区域代码（wt-wt=无限制, cn-zh=中国）

        Returns:
            去重后的 SearchResult 列表；失败返回 []。
        """
        max_results = max_results or settings.search_max_results
        region = region or self._region
        try:
            raw = self._ddgs.text(
                query=query,
                max_results=max_results,
                region=region,
                backend=self._backend,
            )
        except Exception as e:  # 搜索服务抖动/限流时不应拖垮整个分析
            log.warning(f"通用搜索失败: query={query!r} - {e}")
            return []

        if not raw:
            return []

        results = [
            SearchResult(
                url=r.get("href", ""),
                title=r.get("title", ""),
                snippet=r.get("body", ""),
                domain=self._extract_domain(r.get("href", "")),
            )
            for r in raw
            if r.get("href")
        ]
        return self._dedup_and_sort(results)

    # ===== 内部方法 =====

    @staticmethod
    def _extract_domain(url: str) -> str:
        """从 URL 提取主域名（去掉协议与 www.）。"""
        import re
        m = re.match(r"https?://(?:www\.)?([^/]+)", url)
        return m.group(1) if m else url

    @staticmethod
    def _dedup_and_sort(results: list[SearchResult]) -> list[SearchResult]:
        """按 domain 去重（同一域名只保留摘要最长的），返回列表。"""
        seen: dict[str, SearchResult] = {}
        for r in results:
            if r.domain not in seen or len(r.snippet) > len(seen[r.domain].snippet):
                seen[r.domain] = r
        return list(seen.values())
