"""飞书云文档客户端（W4.3，直接 Docx API 创建富文本报告）。

设计决策（已与用户确认）：**直接 Docx API 创建**，而非本地文件上传导入。
理由：同步、可聊天联动、易重生成；富文本块覆盖全（标题/列表/引用/代码/高亮 callout/
表格可合并/多栏/图片/彩色文字），并通过 /blocks/convert 自动把报告 markdown 转为富文本块。

能力：
- create_doc(title) -> (document_id, web_url)
- append_markdown(doc_id, md) -> 把 GFM 转为富文本块追加到文档末尾（含 callout 高亮块 patch）
- set_public_readable(doc_id) -> 文档链接组织内可读
- build_report_doc(title, md, make_public=True) -> web_url（编排上述三步）

依赖：飞书自建应用开通 `docx:document`（及公开的 drive 权限用于 set_public_readable）。

注意：本模块只负责「写文档」，不依赖飞书 SDK，可纯本地单测（无权限时 build_report_doc 抛
明确异常，调用方捕获后降级为「卡片不挂云文档链接」）。
"""
from __future__ import annotations

import os
import re
from typing import Optional

import httpx

from .config import settings

BASE = "https://open.feishu.cn/open-apis"
# callout 高亮块支持的表情前缀（把对应引用块升级为 callout）
_CALLOUT_MARKERS = ["💡", "⚠️", "📌", "✅", "🔴", "🟢", "🔵", "🟡", "❗", "🚀", "📊", "🛡️"]


def _tenant_token() -> str:
    """获取 tenant_access_token（应用级，文档操作用）。"""
    r = httpx.post(
        f"{BASE}/auth/v3/tenant_access_token/internal",
        json={"app_id": settings.feishu_app_id, "app_secret": settings.feishu_app_secret},
        timeout=10,
    )
    data = r.json()
    if data.get("code") != 0:
        raise RuntimeError(f"获取飞书 tenant_token 失败: {data}")
    return data["tenant_access_token"]


def _headers(token: str) -> dict:
    return {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}


def create_doc(title: str, folder_token: str = "") -> tuple[str, str]:
    """创建新版云文档，返回 (document_id, web_url)。"""
    token = _tenant_token()
    body: dict = {"title": title[:50]}
    if folder_token:
        body["folder_token"] = folder_token
    r = httpx.post(f"{BASE}/docx/v1/documents", headers=_headers(token), json=body, timeout=15)
    data = r.json()
    if data.get("code") != 0:
        raise RuntimeError(f"创建云文档失败: {data}")
    doc = data["data"]["document"]
    return doc["document_id"], doc.get("url", "")


def _convert_markdown(doc_id: str, token: str, md: str) -> tuple[list, list]:
    """调用 /blocks/convert 把 GFM 转为富文本块结构。

    返回 (first_level_block_ids, blocks)。blocks 为完整块树（含嵌套）。
    """
    r = httpx.post(
        f"{BASE}/docx/v1/documents/{doc_id}/blocks/convert",
        headers=_headers(token),
        json={"content_type": "markdown", "content": md},
        timeout=30,
    )
    data = r.json()
    if data.get("code") != 0:
        raise RuntimeError(f"markdown 转块失败: {data}")
    d = data["data"]
    return d.get("first_level_block_ids", []), d.get("blocks", [])


def _patch_callouts(blocks: list[dict]) -> None:
    """把以表情开头的引用块（block_type=15）就地升级为 callout 高亮块（block_type=19）。

    飞书 markdown 没有原生 callout 语法，用 `> 💡 文本` 约定表达，这里转成真正的 callout。
    解析失败时保持原样（try/except 兜底，不影响主流程）。
    """
    for blk in blocks:
        try:
            if blk.get("block_type") != 15:  # 15 = quote
                continue
            elems = (blk.get("text", {}) or {}).get("elements", [])
            if not elems:
                continue
            run = elems[0].get("text_run", {})
            content = run.get("content", "")
            m = re.match(r"^(\s*)([" + "".join(re.escape(x) for x in _CALLOUT_MARKERS) + r"])\s*(.*)$", content)
            if not m:
                continue
            emoji, rest = m.group(2), m.group(3)
            run["content"] = rest
            blk["block_type"] = 19  # callout
            blk["callout"] = {"emoji": emoji}
        except Exception:  # noqa: BLE001
            continue


def append_markdown(doc_id: str, md: str) -> None:
    """把 GFM 报告追加到文档末尾（含 callout patch）。"""
    token = _tenant_token()
    block_ids, blocks = _convert_markdown(doc_id, token, md)
    if not block_ids:
        return
    _patch_callouts(blocks)
    r = httpx.post(
        f"{BASE}/docx/v1/documents/{doc_id}/blocks/{doc_id}/descendant",
        headers=_headers(token),
        json={"children_id": block_ids, "descendants": blocks, "index": -1},
        timeout=30,
    )
    data = r.json()
    if data.get("code") != 0:
        raise RuntimeError(f"追加块失败: {data}")


def set_public_readable(doc_id: str) -> bool:
    """设置文档为「组织内获得链接的人可阅读」。失败返回 False（不影响已创建文档）。"""
    token = _tenant_token()
    r = httpx.post(
        f"{BASE}/drive/v1/permissions/{doc_id}/public",
        headers=_headers(token),
        json={
            "external_access_entity": "open_link_can_view",
            "security_entity": "anyone_can_view",
            "need_audit": False,
        },
        timeout=15,
    )
    data = r.json()
    if data.get("code") != 0:
        # 权限接口可能随版本变化，记录但不阻断主流程
        return False
    return True


def build_report_doc(title: str, md: str, make_public: bool = True) -> str:
    """编排：创建文档 → 追加富文本报告 → 设为公开可读 → 返回 web_url。

    Raises: RuntimeError（无 docx 权限 / 网络异常），由调用方捕获降级。
    """
    doc_id, web_url = create_doc(title)
    append_markdown(doc_id, md)
    if make_public:
        set_public_readable(doc_id)
    # 若 create_doc 未返回 url，用 document_id 拼出标准链接
    if not web_url:
        web_url = f"https://www.feishu.cn/docx/{doc_id}"
    return web_url


# ---------------------------------------------------------------------------
# 报告 markdown 构造（GFM，含 callout 约定语法，供 /blocks/convert 转富文本）
# ---------------------------------------------------------------------------

def build_report_markdown(res, folder_token: str = "") -> str:
    """把 AnalysisResult / CompareResult 渲染为富文本报告 markdown。

    包含：标题、元信息 callout、核心结论 callout、大纲、详细分析、来源表格、
    降级备注 callout。比纯文字卡片丰富：标题分级 + callout 高亮 + 表格 + 列表。
    """
    from .models import AnalysisResult, CompareResult

    if isinstance(res, CompareResult):
        return _compare_report_md(res)
    if isinstance(res, AnalysisResult):
        return _analysis_report_md(res)
    raise TypeError(f"unsupported result type: {type(res)}")


def _analysis_report_md(res: AnalysisResult) -> str:
    """分析类（battle_card/pricing/weekly/discovery）报告 markdown。"""
    ts = res.ts.strftime("%Y-%m-%d %H:%M") if hasattr(res.ts, "strftime") else str(res.ts)
    cov = res.coverage_summary or {}
    general_n = cov.get("general_web", len(res.sources))
    title = f"{_scene_cn(res.scene)}｜{res.query}" if res.query else _scene_cn(res.scene)

    lines: list[str] = [f"# {title}", ""]
    lines.append(f"> 🕒 生成时间：{ts}　｜　🎯 置信度：{res.confidence}　｜　🔎 通用源 {general_n} 条")
    lines.append("")
    if res.summary:
        lines.append(f"> 📌 **核心结论**：{res.summary}")
        lines.append("")
    if res.outline:
        lines.append("## 📑 报告大纲")
        for x in res.outline[:12]:
            lines.append(f"- {x}")
        lines.append("")
    lines.append("## 📝 详细分析")
    lines.append(res.analysis or "（分析内容为空）")
    lines.append("")
    lines.append(_sources_md(res.sources))
    if res.note:
        lines.append("")
        lines.append(f"> ⚠️ **数据说明**：{res.note}")
    return "\n".join(lines)


def _compare_report_md(res: CompareResult) -> str:
    """对比类报告 markdown。"""
    targets = res.targets or []
    title = f"⚖️ 竞品对比：{' vs '.join(targets[:3])}" if targets else "⚖️ 竞品对比"
    lines: list[str] = [f"# {title}", ""]
    lines.append("> 🛡️ 多竞品横向对比 · 维度矩阵 + 关键洞察")
    lines.append("")
    if res.summary:
        lines.append(f"> 📌 **核心结论**：{res.summary}")
        lines.append("")
    if res.outline:
        lines.append("## 📑 报告大纲")
        for x in res.outline[:12]:
            lines.append(f"- {x}")
        lines.append("")
    lines.append("## 🔍 对比分析")
    lines.append(res.analysis or "（未生成对比叙事）")
    lines.append("")
    if res.matrix:
        lines.append("## 📊 维度矩阵")
        header = ["维度"] + targets
        lines.append("| " + " | ".join(header) + " |")
        lines.append("| " + " | ".join(["---"] * len(header)) + " |")
        for dim, vals in res.matrix.items():
            row = [dim] + [str((vals or {}).get(t, "—")) for t in targets]
            lines.append("| " + " | ".join(row) + " |")
        lines.append("")
    if res.insights:
        lines.append("## 💡 关键洞察")
        for i, x in enumerate(res.insights, 1):
            lines.append(f"{i}. {x}")
        lines.append("")
    lines.append(_sources_md(res.sources))
    if res.note:
        lines.append("")
        lines.append(f"> ⚠️ **数据说明**：{res.note}")
    return "\n".join(lines)


def _sources_md(sources: list) -> str:
    if not sources:
        return "## 📚 参考来源\n\n（本次未检索到实时数据，基于模型知识，待核实）"
    lines = ["## 📚 参考来源", "", "| # | 来源 | 标题 | 链接 |", "|---|---|---|---|"]
    for i, s in enumerate(sources, 1):
        badge = "🔵 通用网页"
        title = (s.title or s.url or f"来源{i}").replace("|", "/")
        url = s.url or "#"
        lines.append(f"| {i} | {badge} | {title} | [打开]({url}) |")
    return "\n".join(lines)


def _scene_cn(scene: str) -> str:
    return {
        "battle_card": "🛡️ 销售应对卡",
        "pricing": "💰 定价策略",
        "discovery": "🔭 竞品发现",
        "weekly": "📊 竞品周报",
    }.get(scene, "💡 竞品分析")


def report_title(res) -> str:
    """生成云文档标题（与报告 H1 对齐）。"""
    from .models import AnalysisResult, CompareResult

    if isinstance(res, CompareResult):
        t = res.targets or []
        return f"⚖️ 对比：{' vs '.join(t[:3])}" if t else "⚖️ 竞品对比"
    if isinstance(res, AnalysisResult):
        return f"{_scene_cn(res.scene)}｜{res.query}" if res.query else _scene_cn(res.scene)
    return "竞品分析报告"
