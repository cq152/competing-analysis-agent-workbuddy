"""
网页内容提取层（v3 简化版）—— 从搜索结果 URL 抽取正文

v3 调整：砍掉定价页定向提取（移 W4），仅保留通用正文抽取 + 并发抓取 + 失败降级。
依赖：trafilatura 2.x（注意 API 漂移：output_format="txt"、extract_metadata 取代 extract_title/extract_date）、httpx
"""
import asyncio
from dataclasses import dataclass
from typing import Optional

import httpx
import trafilatura

from app.config import settings
from app.logger import log


@dataclass
class FetchedPage:
    url: str
    title: str
    text: str                       # 正文（trafilatura 提取）
    publish_date: Optional[str] = None
    word_count: int = 0


class Fetcher:
    """网页内容抓取器（v3 简化：无定价专项）"""

    HEADERS = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/120.0.0.0 Safari/537.36"
        ),
        "Accept": "text/html,application/xhtml+xml",
        "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
    }

    async def fetch_one(self, url: str) -> Optional[FetchedPage]:
        """抓取单个页面，失败返回 None（不抛异常）。"""
        try:
            async with httpx.AsyncClient(
                headers=self.HEADERS,
                timeout=settings.fetch_timeout,
                follow_redirects=True,
            ) as client:
                resp = await client.get(url)
                resp.raise_for_status()
                html = resp.text

            extracted = trafilatura.extract(
                html,
                include_comments=False,
                include_tables=True,
                favor_precision=True,
                output_format="txt",       # trafilatura 2.x 合法值：txt（非 "text"）
            )

            if not extracted or len(extracted.strip()) < settings.fetch_min_word_count:
                log.debug(f"页面内容过短或提取失败: {url}")
                return None

            # trafilatura 2.x：标题/日期需用 extract_metadata 获取
            title = ""
            publish_date = None
            try:
                meta = trafilatura.extract_metadata(html)
                if meta:
                    title = meta.title or ""
                    publish_date = str(meta.date) if meta.date else None
            except Exception:
                pass

            return FetchedPage(
                url=url,
                title=title,
                text=extracted,
                publish_date=publish_date,
                word_count=len(extracted),
            )

        except httpx.HTTPStatusError as e:
            log.warning(f"HTTP 错误 {e.response.status_code}: {url}")
            return None
        except Exception as e:
            log.error(f"抓取异常: {url} - {e}")
            return None

    async def fetch(self, urls: list[str], max_pages: int = 3) -> list[FetchedPage]:
        """并发抓取多个页面，返回成功的 FetchedPage 列表（顺序不限）。"""
        tasks = [self.fetch_one(url) for url in urls[:max_pages]]
        results = await asyncio.gather(*tasks, return_exceptions=True)
        pages = [r for r in results if isinstance(r, FetchedPage)]
        log.info(f"抓取完成: {len(pages)}/{len(urls[:max_pages])} 成功")
        return pages
