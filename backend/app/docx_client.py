"""飞书云文档客户端（W4.3，直接 Docx API 创建富文本报告）。

设计决策（已与用户确认）：**直接 Docx API 创建**，而非本地文件上传导入。
理由：同步、可聊天联动、易重生成；富文本块覆盖全（标题/列表/引用/代码/高亮 callout/
分割线/加粗彩色文字），满足「丰富表现形式，不仅仅只是文字」。

能力：
- create_doc(title) -> (document_id, web_url)
- append_markdown(doc_id, md) -> 把 GFM 转为富文本块追加到文档末尾（手写解析器）
- set_public_readable(doc_id) -> 文档链接组织内可读
- build_report_doc(title, md, make_public=True) -> web_url（编排上述三步）

关于表格：飞书公开 Docx 块 API 对 table 的 cells schema 极不友好（/children 不支持建表，
/descendant 建表必须有 cells 但 cell 对象 schema 反复报「类型不匹配」，16 种变体均失败）。
因此对比矩阵改用「高亮 callout 块 + 加粗文字」呈现，规避原生表格接入摩擦，仍属丰富表现。

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
_CALLOUT_MARKERS = ["💡", "⚠️", "📌", "✅", "🔴", "🟢", "🔵", "🟡", "❗", "🚀", "📊", "🛡️", "🔗", "🕒"]


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


# ---------------------------------------------------------------------------
# markdown -> 飞书富文本块 解析器
# ---------------------------------------------------------------------------

def _parse_inline(text: str) -> list[dict]:
    """把行内 `**加粗**` 解析为带 bold 样式的 text_run 元素列表。"""
    elements: list[dict] = []
    parts = re.split(r"(\*\*.+?\*\*)", text)
    for p in parts:
        if not p:
            continue
        if p.startswith("**") and p.endswith("**") and len(p) >= 4:
            elements.append({"text_run": {"content": p[2:-2], "text_element_style": {"bold": True}}})
        else:
            elements.append({"text_run": {"content": p}})
    if not elements:
        elements.append({"text_run": {"content": text}})
    return elements


def _extract_callout_emoji(content: str) -> tuple[str, str]:
    """从 `💡 文本` 形式提取表情；非已知表情则默认 📌。"""
    m = re.match(r"^\s*([" + "".join(re.escape(x) for x in _CALLOUT_MARKERS) + r"])\s*(.*)$", content)
    if m:
        return m.group(1), m.group(2)
    return "📌", content


def _is_special(s: str) -> bool:
    """判断一行是否应独立成块（标题/列表/引用/代码/分割线）。"""
    if not s:
        return True
    if s.startswith(("```", "#", ">", "-", "*")):
        return True
    if re.match(r"^\d+\.\s+", s):
        return True
    if s in ("---", "***", "___"):
        return True
    return False


def _md_to_blocks(md: str) -> list[dict]:
    """把 GFM 报告解析为飞书富文本块列表（标题/段落/列表/callout/代码/分割线/加粗）。"""
    lines = md.split("\n")
    blocks: list[dict] = []
    n = len(lines)
    i = 0
    while i < n:
        line = lines[i]
        s = line.strip()
        if not s:
            i += 1
            continue
        # 代码块：飞书 code 块 elements schema 校验不同（报类型不匹配），转 callout 高亮块呈现
        if s.startswith("```"):
            lang = s[3:].strip()
            code_lines: list[str] = []
            i += 1
            while i < n and not lines[i].strip().startswith("```"):
                code_lines.append(lines[i])
                i += 1
            i += 1  # 跳过结尾 ```
            blocks.append({
                "block_type": 19,
                "callout": {"emoji": "💻", "text": {"elements": [{"text_run": {"content": "\n".join(code_lines)}}]}},
            })
            continue
        # 分割线：飞书 divider 块创建报 invalid param，跳过（用前后空行自然分隔）
        if s in ("---", "***", "___"):
            i += 1
            continue
        # 标题
        m = re.match(r"^(#{1,3})\s+(.*)$", s)
        if m:
            level = len(m.group(1))
            bt = {1: 3, 2: 4, 3: 5}[level]
            key = {3: "heading1", 4: "heading2", 5: "heading3"}[bt]
            blocks.append({"block_type": bt, key: {"elements": _parse_inline(m.group(2))}})
            i += 1
            continue
        # 无序列表：飞书 bullet 块 elements schema 与 heading 校验不同（反复报类型不匹配），
        # 改用「• 」前缀段落块（block_type 2，稳定）呈现，保留加粗等行内样式。
        if re.match(r"^[-*]\s+", s):
            blocks.append({"block_type": 2, "text": {"elements": _parse_inline("• " + s[2:].strip())}})
            i += 1
            continue
        # 有序列表：同上，保留原序号前缀
        m_ord = re.match(r"^(\d+)\.\s+(.*)$", s)
        if m_ord:
            blocks.append({"block_type": 2, "text": {"elements": _parse_inline(f"{m_ord.group(1)}. " + m_ord.group(2))}})
            i += 1
            continue
        # 引用/callout
        if s.startswith(">"):
            content = s[1:].strip()
            emoji, rest = _extract_callout_emoji(content)
            blocks.append({"block_type": 19, "callout": {"emoji": emoji, "text": {"elements": _parse_inline(rest)}}})
            i += 1
            continue
        # 表格行（兼容旧 markdown 表格语法，转 callout 呈现）
        if s.startswith("|") and s.endswith("|"):
            cells = [c.strip() for c in s.strip("|").split("|")]
            blocks.append({"block_type": 19, "callout": {"emoji": "📊", "text": {"elements": _parse_inline(" ｜ ".join(cells))}}})
            i += 1
            continue
        # 段落：合并连续普通行
        para = [s]
        i += 1
        while i < n and lines[i].strip() and not _is_special(lines[i].strip()):
            para.append(lines[i].strip())
            i += 1
        blocks.append({"block_type": 2, "text": {"elements": _parse_inline(" ".join(para))}})
    return blocks


def _code_lang(lang: str) -> int:
    """飞书代码块语言枚举（1=纯文本，其余常见值）。"""
    table = {
        "": 1, "text": 1, "plaintext": 1, "bash": 13, "sh": 13, "shell": 13,
        "python": 3, "py": 3, "json": 5, "javascript": 14, "js": 14,
        "html": 10, "css": 11, "sql": 8, "java": 2, "go": 7, "c": 4, "cpp": 6,
    }
    return table.get((lang or "").lower(), 1)


def append_markdown(doc_id: str, md: str) -> None:
    """把 GFM 报告追加到文档末尾（手写解析 + /children 分块，规避原生表格摩擦）。"""
    token = _tenant_token()
    blocks = _md_to_blocks(md)
    if not blocks:
        return
    # 单请求最多 20 块，避免触达上限；尊重 3 req/s 限频
    chunk_size = 20
    for ci in range(0, len(blocks), chunk_size):
        chunk = blocks[ci : ci + chunk_size]
        r = httpx.post(
            f"{BASE}/docx/v1/documents/{doc_id}/blocks/{doc_id}/children",
            headers=_headers(token),
            json={"children": chunk, "index": -1},
            timeout=30,
        )
        data = r.json()
        if data.get("code") != 0:
            raise RuntimeError(f"追加块失败: {data}")


def set_public_readable(doc_id: str) -> bool:
    """设置文档为「组织内获得链接的人可阅读」。失败返回 False（不影响已创建文档）。

    依赖 drive 类权限（如 drive:drive）。未开通时接口返回非 JSON/错误码，此处安全吞掉。
    """
    try:
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
    except Exception:  # noqa: BLE001
        # 接口不可达 / 返回非 JSON / 无 drive 权限 —— 不影响已创建文档
        return False
    if data.get("code") != 0:
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
# 报告 markdown 构造（GFM，含 callout 约定语法 + 对比矩阵 callout 化）
# ---------------------------------------------------------------------------

def build_report_markdown(res, folder_token: str = "") -> str:
    """把 AnalysisResult / CompareResult 渲染为富文本报告 markdown。

    包含：标题、元信息 callout、核心结论 callout、大纲、详细分析、参考来源、
    降级备注 callout。比纯文字卡片丰富：标题分级 + callout 高亮 + 列表 + 加粗。
    对比维度矩阵用 callout 高亮块呈现（规避原生表格接入摩擦）。
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
    """对比类报告 markdown（维度矩阵用 callout 呈现）。"""
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
        for dim, vals in res.matrix.items():
            cells = "　｜　".join(f"{t}：{str((vals or {}).get(t, '—'))}" for t in targets)
            lines.append(f"> 📊 **{dim}**　{cells}")
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
        return "## 📚 参考来源\n\n> 🔗 本次未检索到实时数据，基于模型知识，待核实"
    lines = ["## 📚 参考来源", ""]
    for i, s in enumerate(sources, 1):
        title = (s.title or s.url or f"来源{i}").replace("|", "/")
        url = s.url or "#"
        lines.append(f"> 🔗 **来源{i}**：{title} — [打开]({url})")
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
