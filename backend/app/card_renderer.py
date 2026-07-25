"""飞书卡片渲染器（W3.1）。

职责：把结构化分析结果（AnalysisResult / CompareResult）渲染成「飞书 Interactive Card」
JSON 体（dict），交给 bot.push_card 发送。本模块**不依赖飞书 SDK**，可纯本地单测。

v3 修正（来源分色）：
- 中文数据源（chinese_sources）当前为占位、未实接，故所有来源均标 🔵 通用网页，
  不写假数据（不因「想显得有中文源」而伪造 🟢 中文社媒 等标签）。
- 分色逻辑保留（_source_badge），待 W4 中文源实接后自动生效，无需改调用方。

卡片结构（飞书 interactive）：
{
  "config": {"wide_screen_mode": true},
  "header": {"title": {"tag": "plain_text", "content": "..."}, "template": "blue"},
  "elements": [ ... ]
}
注意：通过 im.v1.message.create 发送时，msg_type 单独设为 "interactive"，
content 即本模块返回的 dict 经 json.dumps 后的字符串（不含外层 msg_type）。
"""

from datetime import datetime
from typing import Optional

from .models import AnalysisResult, CompareResult, Source

# 场景 → (卡片标题前缀, 飞书 header 配色)
# 配色可选：blue / green / red / orange / grey / purple / indigo / teal
_SCENE_META: dict[str, tuple[str, str]] = {
    "battle_card": ("🛡️ 销售应对卡", "orange"),
    "pricing": ("💰 定价策略", "red"),
    "discovery": ("🔭 竞品发现", "green"),
    "weekly": ("📊 竞品周报", "blue"),
}

# 来源类型 → 展示徽标（v3 仅 general_web 真实出现；其余预留）
_SOURCE_BADGE: dict[str, str] = {
    "general_web": "🔵 通用网页",
    "chinese_social": "🟢 中文社媒",
    "tianyancha": "🟡 天眼查",
    "third_party": "🟣 第三方",
}

# 飞书卡片正文长度保护阈值（飞书 markdown 元素有上限，留余量）
_MAX_BODY_LEN = 18000
# header title 字符上限（飞书限制 50）
_MAX_HEADER_LEN = 50


def _scene_meta(scene: str) -> tuple[str, str]:
    return _SCENE_META.get(scene, ("💡 竞品分析", "blue"))


def _source_badge(source_type: str) -> str:
    """来源类型 → 分色徽标。v3 当前只会命中 general_web。"""
    return _SOURCE_BADGE.get(source_type, "🔵 通用网页")


def _fmt_ts(ts) -> str:
    if isinstance(ts, datetime):
        return ts.strftime("%Y-%m-%d %H:%M")
    return str(ts)


def render_analysis_card(res: AnalysisResult) -> dict:
    """把一次分析渲染成飞书卡片体 dict。

    不抛异常设计：即使 res 缺字段也尽量产出可读卡片（最坏降级为纯文本提示由调用方兜底）。
    """
    label, color = _scene_meta(res.scene)
    header_title = f"{label}｜{res.query}" if res.query else label
    header_title = header_title[:_MAX_HEADER_LEN]

    elements: list[dict] = []

    # 1) 分析正文
    body = res.analysis or "（分析内容为空）"
    if len(body) > _MAX_BODY_LEN:
        body = body[:_MAX_BODY_LEN] + "\n\n_…（内容过长已截断，完整版见对话）_"
    elements.append({"tag": "markdown", "content": body})
    elements.append({"tag": "hr"})

    # 2) 参考来源
    if res.sources:
        lines = ["**📚 参考来源**（实时检索）"]
        for i, s in enumerate(res.sources, 1):
            badge = _source_badge(s.source_type)
            title = s.title or s.url or f"来源{i}"
            url = s.url or "#"
            lines.append(f"{i}. {badge} [{title}]({url})")
        elements.append({"tag": "markdown", "content": "\n".join(lines)})
    else:
        elements.append(
            {"tag": "markdown", "content": "**📚 参考来源**：本次未检索到实时数据（基于模型知识，待核实）"}
        )

    # 3) 降级备注（note 非空 → 橙字提醒，不掩盖事实）
    if res.note:
        elements.append({"tag": "markdown", "content": f"⚠️ {res.note}"})

    # 4) 页脚：置信度 + 时间 + 覆盖率
    cov = res.coverage_summary or {}
    general_n = cov.get("general_web", len(res.sources))
    cov_txt = f"通用源 {general_n} 条"
    ts_txt = _fmt_ts(res.ts)
    elements.append({"tag": "hr"})
    elements.append(
        {
            "tag": "markdown",
            "content": f"🎯 置信度：{res.confidence}　｜　🕒 {ts_txt}　｜　🔎 {cov_txt}",
        }
    )

    return {
        "config": {"wide_screen_mode": True},
        "header": {
            "title": {"tag": "plain_text", "content": header_title},
            "template": color,
        },
        "elements": elements,
    }


def render_compare_card(res: CompareResult) -> dict:
    """竞品对比卡片（W3.2 启用）。

    顶部渲染 narrative 叙事（同 analysis 卡片正文+来源+降级+页脚），其下为维度矩阵表与关键洞察。
    """
    targets = res.targets or []
    header_title = "⚖️ 竞品对比"
    if targets:
        header_title = f"⚖️ 对比：{' vs '.join(targets[:3])}"

    elements: list[dict] = []

    # 1) 叙事正文
    if res.analysis:
        body = res.analysis
        if len(body) > _MAX_BODY_LEN:
            body = body[:_MAX_BODY_LEN] + "\n\n_…（内容过长已截断，完整版见对话）_"
        elements.append({"tag": "markdown", "content": body})
    else:
        elements.append({"tag": "markdown", "content": "（未生成对比叙事）"})
    elements.append({"tag": "hr"})

    # 2) 维度矩阵（markdown 表格）
    if res.matrix:
        header = ["维度"] + targets
        rows = [
            "| " + " | ".join(header) + " |",
            "| " + " | ".join(["---"] * len(header)) + " |",
        ]
        for dim, vals in res.matrix.items():
            row = [dim] + [str((vals or {}).get(t, "—")) for t in targets]
            rows.append("| " + " | ".join(row) + " |")
        elements.append({"tag": "markdown", "content": "\n".join(rows)})
    else:
        elements.append({"tag": "markdown", "content": "（暂无维度数据）"})
    elements.append({"tag": "hr"})

    # 3) 关键洞察
    if res.insights:
        ins = ["**💡 关键洞察**"] + [f"{i}. {x}" for i, x in enumerate(res.insights, 1)]
        elements.append({"tag": "markdown", "content": "\n".join(ins)})
    else:
        elements.append({"tag": "markdown", "content": "**💡 关键洞察**：（待生成）"})

    # 4) 参考来源
    if res.sources:
        src = ["**📚 参考来源**（实时检索）"] + [
            f"{i}. {_source_badge(s.source_type)} [{s.title or s.url}]({s.url or '#'})"
            for i, s in enumerate(res.sources, 1)
        ]
        elements.append({"tag": "markdown", "content": "\n".join(src)})

    # 5) 降级备注（note 非空 → 橙字提醒，不掩盖事实）
    if res.note:
        elements.append({"tag": "markdown", "content": f"⚠️ {res.note}"})

    return {
        "config": {"wide_screen_mode": True},
        "header": {
            "title": {"tag": "plain_text", "content": header_title[:_MAX_HEADER_LEN]},
            "template": "blue",
        },
        "elements": elements,
    }


def render_card(obj) -> Optional[dict]:
    """统一入口：按类型分发渲染。未知类型返回 None（调用方回退文本）。"""
    if isinstance(obj, AnalysisResult):
        return render_analysis_card(obj)
    if isinstance(obj, CompareResult):
        return render_compare_card(obj)
    return None
