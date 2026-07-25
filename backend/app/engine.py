"""分析引擎：加载 validation/prompts/*.txt（唯一真相）并调用 LLM。

v3 轻改造（09 附录C Task 2.7）：在保留 W1 已验证的「prompt 加载 + LLM 调用」前提下，
追加「通用搜索 → 抓取 → 上下文组装 → 注入」并封装为结构化 AnalysisResult。

设计约束（保护 W1 已验证核心）：
- 仍只读 validation/prompts/*.txt，不重复存放（避免与 Aily 漂移）；
- 不注入 v2.1 的 G6/G1 硬性指令（v3 已降级为目标，避免改变红线立场）；
- 搜索/抓取失败优雅降级为纯 LLM + 标注「待核实」，不整体失败。
"""
from __future__ import annotations

import asyncio
import json
import re
from pathlib import Path

from openai import OpenAI

from .config import settings
from .models import AnalysisResult, CompareResult, Source

# validation/prompts 相对本文件的路径：backend/app/engine.py -> ../../../validation/prompts
PROMPTS_DIR = Path(__file__).resolve().parent.parent.parent / "validation" / "prompts"

SCENES = {
    "battle_card": "battle_card.txt",
    "pricing": "pricing.txt",
    "weekly": "weekly.txt",
    "discovery": "discovery.txt",
}


def list_scenes() -> list[str]:
    return list(SCENES.keys())


def _read(name: str) -> str:
    return (PROMPTS_DIR / name).read_text(encoding="utf-8")


def _gather_context(scene: str, query: str) -> tuple[list[Source], str]:
    """通用搜索 + 抓取，返回 (来源列表, 上下文文本)。

    失败时返回空列表 + 降级备注（以 ⚠️ 开头）。不向外抛异常。
    """
    from . import fetcher
    from .searcher import Searcher  # search 是 Searcher 实例方法，需先实例化

    sources: list[Source] = []
    try:
        results = Searcher().search(query, max_results=settings.search_max_results)
    except Exception:  # noqa: BLE001
        return sources, "⚠️ 未能获取实时数据，以下基于模型知识（待核实）"

    if not results:
        return sources, "⚠️ 实时搜索无结果，以下基于模型知识（待核实）"

    # 抓取 Top N URL 正文（fetcher.fetch 为异步，引擎在同步上下文用 asyncio.run 驱动）
    urls = [r.url for r in results if r.url][: settings.fetch_max_urls]
    pages: list = []
    if urls:
        try:
            pages = asyncio.run(fetcher.fetch(urls))
        except Exception:  # noqa: BLE001
            pages = []

    lines: list[str] = []
    for i, r in enumerate(results, 1):
        sources.append(
            Source(
                url=r.url,
                title=r.title,
                snippet=r.snippet,
                source_type="general_web",
                source_display="网页搜索",
                relevance="high" if i <= 2 else "medium",
            )
        )
        lines.append(f"### {i}. [{r.title}]({r.url})\n> {(r.snippet or '')[:300]}")

    for p in pages:
        if p and getattr(p, "text", ""):
            lines.append(f"\n正文节选（{p.title}）：\n{p.text[:800]}")

    context = f"## 参考资料来源（{len(sources)} 条）\n" + "\n".join(lines)
    return sources, context


def _extract_outline(analysis: str) -> list[str]:
    """从 markdown 分析文本中抽取大纲（##/### 标题），免费、零 LLM 调用。"""
    out: list[str] = []
    for ln in (analysis or "").splitlines():
        m = re.match(r"^#{1,3}\s+(.+?)\s*$", ln)
        if m:
            out.append(m.group(1).strip())
    return out[:12]


def _summarize(scene: str, query: str, analysis: str) -> str:
    """生成一句话核心结论（卡片顶部展示）。

    用一次轻量 LLM 调用（小 max_tokens、低 temperature），不改动主分析 prompt，
    保护 W1 已验证的分析质量。调用失败则回退到分析首句。
    """
    fallback = ""
    first = re.split(r"[。\n！\?]", (analysis or "").strip())
    if first:
        fallback = first[0].strip()[:60]

    client = OpenAI(api_key=settings.api_key, base_url=settings.base_url)
    try:
        resp = client.chat.completions.create(
            model=settings.model,
            temperature=0.2,
            max_tokens=120,
            messages=[
                {
                    "role": "system",
                    "content": "你是竞品分析助手。请用一句话（不超过40个汉字）概括分析的核心结论，"
                    "中文，直接给结论、不要寒暄、不要使用markdown。",
                },
                {"role": "user", "content": f"主题：{query}\n场景：{scene}\n分析：\n{analysis[:2500]}"},
            ],
        )
        s = (resp.choices[0].message.content or "").strip()
        s = s.strip("。").strip()
        return s[:60] if s else fallback
    except Exception:  # noqa: BLE001
        return fallback


def analyze(scene: str, query: str, target: str | None = None) -> AnalysisResult:
    """按场景加载提示词、注入实时搜索上下文并调用 LLM，返回结构化 AnalysisResult。

    Args:
        scene: 场景键（battle_card / pricing / weekly / discovery）
        query: 用户查询
        target: 目标竞品（v3 预留，当前通用搜索未强制使用）
    """
    if scene not in SCENES:
        raise ValueError(f"unknown scene: {scene}，可选: {list_scenes()}")

    # —— 以下 prompt 加载与 LLM 调用保持 W1 已验证逻辑不变 ——
    system_prompt = _read("system.txt")
    scene_prompt = _read(SCENES[scene])

    sources, context = _gather_context(scene, query)

    note = ""
    if context.startswith("⚠️"):
        note = context
        user_content = query
    else:
        user_content = (
            f"{query}\n\n{context}\n\n"
            "（请基于以上参考资料分析，并标注信息来源；"
            "无法验证的信息明确标注「待核实」。）"
        )

    client = OpenAI(api_key=settings.api_key, base_url=settings.base_url)
    messages = [
        {"role": "system", "content": system_prompt + "\n\n# 当前场景指令\n" + scene_prompt},
        {"role": "user", "content": user_content},
    ]
    analysis = ""
    for _ in range(2):  # 偶发空 completion 时重试一次（模型冷启动等）
        resp = client.chat.completions.create(
            model=settings.model,
            temperature=0.4,
            messages=messages,
        )
        analysis = resp.choices[0].message.content or ""
        if analysis.strip():
            break

    coverage = {"general_web": len(sources), "total": len(sources)}
    return AnalysisResult(
        scene=scene,
        query=query,
        analysis=analysis,
        sources=sources,
        confidence="medium",
        coverage_summary=coverage,
        note=note,
        summary=_summarize(scene, query, analysis),
        outline=_extract_outline(analysis),
    )


# 从 LLM 输出中提取 ```json ... ``` 围栏块（容错：没有则返回 None）
_JSON_BLOCK_RE = re.compile(r"```json\s*(.*?)\s*```", re.DOTALL)


def _extract_json_block(text: str) -> str | None:
    m = _JSON_BLOCK_RE.search(text or "")
    return m.group(1) if m else None


def compare(targets: list[str], query: str = "") -> CompareResult:
    """多竞品横向对比（W3.2）。

    合并 targets 做通用搜索取上下文，用 compare.txt 场景指令调 LLM，
    解析其输出的 JSON 块为结构化 CompareResult（targets/analysis/matrix/insights/sources/note）。
    """
    targets = [t.strip() for t in (targets or []) if t.strip()]
    if len(targets) < 2:
        raise ValueError("compare 至少需要 2 个竞品")

    system_prompt = _read("system.txt")
    scene_prompt = _read("compare.txt")

    combined_query = query or " vs ".join(targets)
    sources, context = _gather_context("compare", combined_query)

    note = ""
    if context.startswith("⚠️"):
        note = context
        user_content = combined_query
    else:
        user_content = (
            f"请对比以下竞品：{', '.join(targets)}\n\n"
            f"{context}\n\n"
            "（请基于以上参考资料分析，并标注信息来源；无法验证的信息明确标注「待核实」。）"
        )

    client = OpenAI(api_key=settings.api_key, base_url=settings.base_url)
    messages = [
        {"role": "system", "content": system_prompt + "\n\n# 当前场景指令\n" + scene_prompt},
        {"role": "user", "content": user_content},
    ]
    raw = ""
    for _ in range(2):  # 偶发空 completion 时重试一次
        resp = client.chat.completions.create(
            model=settings.model,
            temperature=0.4,
            messages=messages,
        )
        raw = resp.choices[0].message.content or ""
        if raw.strip():
            break

    narrative, matrix, insights = "", {}, []
    json_str = _extract_json_block(raw)
    if json_str:
        try:
            data = json.loads(json_str)
            narrative = data.get("narrative", "")
            matrix = data.get("matrix", {}) or {}
            insights = data.get("insights", []) or []
        except Exception:  # noqa: BLE001
            narrative = raw  # JSON 解析失败则整段作为叙事兜底
    else:
        narrative = raw

    return CompareResult(
        targets=targets,
        analysis=narrative,
        matrix=matrix,
        insights=insights,
        sources=sources,
        note=note,
        summary=_summarize("compare", combined_query, narrative or raw),
        outline=_extract_outline(narrative or raw),
    )


def classify_intent(text: str) -> str:
    """意图鉴别：判断用户消息是否属于竞品分析类请求。

    返回场景键（battle_card / pricing / weekly / discovery / compare）或 "unknown"。
    - 用于修复「无关消息被默认强转为销售应对卡」的问题；unknown 时 bot 应澄清。
    - LLM 调用失败时保守返回 "unknown"（避免把无关消息当分析处理而硬凑分析卡）。
    """
    known = {"battle_card", "pricing", "weekly", "discovery", "compare"}
    client = OpenAI(api_key=settings.api_key, base_url=settings.base_url)
    system = (
        "你是竞品情报助手的意图分类器。判断用户消息是否属于竞品分析类请求。"
        "只输出以下之一（不要任何解释或多余标点）：\n"
        "battle_card —— 销售应对/话术/客户拿竞品压我们/怎么回竞品\n"
        "pricing —— 价格/定价/报价/降价/收费\n"
        "weekly —— 竞品周报/本周动态汇总\n"
        "discovery —— 发现竞品/调研某赛道竞品/竞品名单\n"
        "compare —— 明确对比两个及以上竞品（即使没写“对比”二字）\n"
        "unknown —— 与竞品分析无关：闲聊、打招呼、UI/按钮报错、技术问题、非商业话题"
    )
    try:
        resp = client.chat.completions.create(
            model=settings.model,
            temperature=0.0,
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": text},
            ],
        )
        label = (resp.choices[0].message.content or "").strip().lower()
        label = label.strip(" .,;:()[]{}")
        # 容错：若模型附带了说明文字，提取首个命中标签
        if label not in known:
            for k in known:
                if k in label:
                    label = k
                    break
        if label in known:
            return label
        return "unknown"
    except Exception:  # noqa: BLE001
        return "unknown"
