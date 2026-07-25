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

import json
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

    # 0) 核心结论 + 报告大纲（W4 预览）
    if res.summary:
        elements.append({"tag": "markdown", "content": f"**📌 核心结论**\n\n{res.summary}"})
    if res.outline:
        ol = "\n".join(f"• {x}" for x in res.outline[:10])
        elements.append({"tag": "markdown", "content": f"**📑 报告大纲**\n{ol}"})
    if res.summary or res.outline:
        elements.append({"tag": "hr"})

    # 1) 分析正文（有云文档则截断预览，否则全文）
    body = res.analysis or "（分析内容为空）"
    if res.doc_url and len(body) > 1500:
        body = body[:1500] + "\n\n_…（以下为摘要，完整图文报告见云文档链接）_"
    elif len(body) > _MAX_BODY_LEN:
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

    # 4.5) 云文档入口（W4）
    if res.doc_url:
        elements.append({"tag": "markdown", "content": f"📄 [查看完整图文报告]({res.doc_url})"})

    # 5) 操作按钮（P1）：再分析 / 监控此竞品
    elements.append(_action_buttons(res.scene, res.query))

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

    # 0) 核心结论 + 报告大纲（W4 预览）
    if res.summary:
        elements.append({"tag": "markdown", "content": f"**📌 核心结论**\n\n{res.summary}"})
    if res.outline:
        ol = "\n".join(f"• {x}" for x in res.outline[:10])
        elements.append({"tag": "markdown", "content": f"**📑 报告大纲**\n{ol}"})
    if res.summary or res.outline:
        elements.append({"tag": "hr"})

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

    # 6) 操作按钮（P1）：再对比 / 监控
    targets = res.targets or []
    compare_query = " vs ".join(targets) if targets else ""
    if res.doc_url:
        elements.append({"tag": "markdown", "content": f"📄 [查看完整图文报告]({res.doc_url})"})
    elements.append(_action_buttons("compare", compare_query, competitors=targets))

    return {
        "config": {"wide_screen_mode": True},
        "header": {
            "title": {"tag": "plain_text", "content": header_title[:_MAX_HEADER_LEN]},
            "template": "blue",
        },
        "elements": elements,
    }


def _action_buttons(scene: str, query: str, competitors: list[str] | None = None) -> dict:
    """生成卡片底部操作按钮（P1）：再分析 / 监控竞品。

    Feishu callback button value 必须为字符串，用 JSON 编码传递 cmd+参数。
    card.action.trigger 事件到达 webhook 后解析并路由到 bot.handle_card_action。

    注意：对比卡片的「再分析」走 re_compare（用 targets 重新调用 engine.compare），
    不能走 re_analyze（analyze 不认 scene="compare"，会报 unknown scene）。
    """
    # 对比卡片：再分析按钮走 re_compare 专用命令，传干净 targets 列表
    if scene == "compare":
        actions = [{
            "tag": "button",
            "text": {"tag": "plain_text", "content": "🔄 再对比一次"},
            "type": "primary",
            "value": json.dumps({"cmd": "re_compare", "targets": competitors or []}),
        }]
    else:
        actions = [{
            "tag": "button",
            "text": {"tag": "plain_text", "content": "🔄 再分析一次"},
            "type": "primary",
            "value": json.dumps({"cmd": "re_analyze", "scene": scene, "query": query}),
        }]
    # 监控按钮：对比卡片给每个竞品一个按钮，分析卡片给单个按钮
    comps = competitors or [_extract_main_competitor(query)] if query else []
    comps = [c for c in comps if c]
    if len(comps) <= 2:
        for c in comps:
            actions.append({
                "tag": "button",
                "text": {"tag": "plain_text", "content": f"📡 监控 {c}"},
                "type": "default",
                "value": json.dumps({"cmd": "quick_monitor", "competitor": c}),
            })
    else:
        actions.append({
            "tag": "button",
            "text": {"tag": "plain_text", "content": "📡 一键监控"},
            "type": "default",
            "value": json.dumps({"cmd": "quick_monitor", "competitor": comps[0]}),
        })
    # 换场景按钮
    actions.append({
        "tag": "button",
        "text": {"tag": "plain_text", "content": "📊 切换为周报"},
        "type": "default",
        "value": json.dumps({"cmd": "re_analyze", "scene": "weekly", "query": query}),
    })
    return {"tag": "action", "actions": actions}


def _extract_main_competitor(query: str) -> str:
    """从 query 中提取主要竞品名（取第一个非空词）。"""
    words = query.strip().split()
    return words[0] if words else ""


def render_clarify_card() -> dict:
    """无关消息的友好澄清卡（不套用任何分析场景 chrome，避免误导成分析卡）。"""
    elements = [
        {
            "tag": "markdown",
            "content": "👋 我是 **竞品雷达 + 军师**，专注帮你分析竞品动态。\n你刚才说的好像不是竞品分析问题，我就不硬凑分析卡啦～",
        },
        {"tag": "hr"},
        {"tag": "markdown", "content": "**可以这样问我：**"},
        {
            "tag": "markdown",
            "content": (
                "- 🛡️ 「客户总拿飞书压我们，怎么应对？」→ 销售应对卡\n"
                "- 💰 「钉钉降价了，我们怎么跟？」→ 定价分析\n"
                "- ⚔️ 「对比 飞书 钉钉 企业微信」→ 多竞品对比\n"
                "- 📊 「本周竞品周报」→ 周报生成\n"
                "- 🔭 「我们做协同办公，帮我发现竞品」→ 竞品发现\n"
                "- 📡 「监控 飞书」→ 添加竞品监控（有变化自动推群）"
            ),
        },
        {
            "tag": "markdown",
            "content": "_如果在反馈机器人本身的 bug（如按钮点不开），把现象告诉我即可，我会转交开发同学排查。_",
        },
    ]
    return {
        "config": {"wide_screen_mode": True},
        "header": {
            "title": {"tag": "plain_text", "content": "💡 我没理解为竞品分析请求"},
            "template": "grey",
        },
        "elements": elements,
    }


def render_monitor_alert_card(competitor: str, added: list[str], removed: list[str]) -> dict:
    """渲染竞品雷达告警卡片（P1：取代纯文本推送）。

    橙色 header + 变更明细 + 时间，比纯文本更醒目易读。
    """
    ts = datetime.now().strftime("%Y-%m-%d %H:%M")
    elements: list[dict] = []

    # 简介行
    total_changes = len(added) + len(removed)
    elements.append(
        {"tag": "markdown", "content": f"**{competitor}** 检测到 {total_changes} 项变化，于 {ts}"}
    )
    elements.append({"tag": "hr"})

    # 新增
    if added:
        lines = ["**➕ 新增**"] + [f"{i}. {x[:100]}" for i, x in enumerate(added[:5], 1)]
        elements.append({"tag": "markdown", "content": "\n".join(lines)})

    # 消失
    if removed:
        lines = ["**➖ 消失**"] + [f"{i}. {x[:100]}" for i, x in enumerate(removed[:5], 1)]
        elements.append({"tag": "markdown", "content": "\n".join(lines)})

    elements.append({"tag": "hr"})
    elements.append(
        {"tag": "markdown", "content": "_基于公开网络搜索，建议进一步核实_"}
    )

    # 操作按钮
    elements.append({
        "tag": "action",
        "actions": [
            {
                "tag": "button",
                "text": {"tag": "plain_text", "content": "🔍 查看详情"},
                "type": "primary",
                "value": json.dumps({"cmd": "re_analyze", "scene": "discovery", "query": competitor}),
            },
            {
                "tag": "button",
                "text": {"tag": "plain_text", "content": "🗑 删除监控"},
                "type": "default",
                "value": json.dumps({"cmd": "quick_unmonitor", "competitor": competitor}),
            },
        ],
    })

    return {
        "config": {"wide_screen_mode": True},
        "header": {
            "title": {"tag": "plain_text", "content": f"📡 竞品雷达｜{competitor}"},
            "template": "orange",
        },
        "elements": elements,
    }


def render_help_card(bot_name: str = "竞品分析搭档", scenes: list[str] | None = None) -> dict:
    """渲染帮助卡片：分「我能做什么 / 命令示例 / 使用说明」三栏，视觉分层。

    取代旧版纯文本 HELP_TEXT，让新用户扫一眼就知道怎么用。
    """
    scenes = scenes or ["battle_card", "pricing", "weekly", "discovery"]
    elements: list[dict] = [
        # 一句话定位
        {"tag": "markdown", "content": f"我是 **{bot_name}**，你的竞品雷达 + 军师。\n实时检索 + 模型分析，把竞品动态变成可执行的应对策略。"},
        {"tag": "hr"},
        # 我能做什么
        {"tag": "markdown", "content": (
            "**🎯 我能做什么**\n"
            "• 🛡️ **销售应对卡** — 客户拿竞品压价，给你话术 + 差异化卖点\n"
            "• 💰 **定价策略** — 竞品调价了，分析意图 + 推荐跟进方案\n"
            "• 📊 **竞品周报** — 一键生成本周竞品动态摘要\n"
            "• 🔭 **竞品发现** — 给定赛道，帮你找出主要玩家\n"
            "• ⚖️ **多竞品对比** — 横向对比 2~3 个竞品的维度矩阵\n"
            "• 📡 **竞品监控** — 添加监控，有变化自动推群"
        )},
        {"tag": "hr"},
        # 命令示例
        {"tag": "markdown", "content": (
            "**💬 直接对我说（不用加 /）**\n"
            "```\n"
            "客户总拿飞书压我们，怎么应对？\n"
            "钉钉降价了，我们怎么跟？\n"
            "本周竞品周报\n"
            "我们做协同办公，帮我发现竞品\n"
            "对比 飞书 钉钉 企业微信\n"
            "监控 飞书\n"
            "监控列表 / 删除监控 1\n"
            "帮助\n"
            "```"
        )},
        {"tag": "hr"},
        # 使用说明
        {"tag": "markdown", "content": (
            "**⚠️ 使用说明**\n"
            "• 群里需要 @我 才会响应；私聊直接发即可\n"
            "• 分析基于实时网络检索 + 模型推理，结果仅供参考\n"
            "• 中文数据源（天眼查/小红书等）暂未接入，W4 补全\n"
            f"• 可用场景：{', '.join(scenes)}"
        )},
        {"tag": "hr"},
        {"tag": "markdown", "content": "💡 随时发「帮助」重新查看本说明。"},
    ]
    return {
        "config": {"wide_screen_mode": True},
        "header": {
            "title": {"tag": "plain_text", "content": f"🤖 {bot_name}｜使用指南"},
            "template": "indigo",
        },
        "elements": elements,
    }


def render_onboarding_card(bot_name: str = "竞品分析搭档") -> dict:
    """首次@欢迎卡片：简洁有力，引导第一次使用。

    设计原则：不要又是长 help，只给「我是谁 + 3 个最常用命令 + 怎么开始」。
    新用户扫一眼就能动手，详细说明走「帮助」。
    """
    elements: list[dict] = [
        {"tag": "markdown", "content": f"👋 你好！我是 **{bot_name}**。"},
        {"tag": "markdown", "content": (
            "我能帮你 **盯竞品 + 出对策**：实时检索网络情报，把竞品动态变成销售话术、定价方案、周报。"
        )},
        {"tag": "hr"},
        {"tag": "markdown", "content": (
            "**🚀 试试这三句**\n"
            "```\n"
            "客户总拿飞书压我们，怎么应对？\n"
            "对比 飞书 钉钉 企业微信\n"
            "监控 飞书\n"
            "```"
        )},
        {"tag": "hr"},
        {"tag": "markdown", "content": "📖 发「帮助」查看完整功能说明。"},
    ]
    return {
        "config": {"wide_screen_mode": True},
        "header": {
            "title": {"tag": "plain_text", "content": f"👋 欢迎使用 {bot_name}"},
            "template": "green",
        },
        "elements": elements,
    }


def render_tip_card(
    title: str,
    content: str = "",
    template: str = "blue",
) -> dict:
    """通用提示卡片（P2）：用于错误/成功/等待/警告等轻量提示。

    template 配色：
    - blue: 信息/等待（🔍 分析中…）
    - green: 成功（✅ 已添加 / 已删除）
    - red: 错误（⚠️ 失败 / 不支持）
    - orange: 警告（用法提示 / 限制说明）
    """
    elements: list[dict] = []
    if content:
        elements.append({"tag": "markdown", "content": content})
    elements.append({"tag": "hr"})
    elements.append({"tag": "markdown", "content": "竞品分析搭档 · 即时提示"})
    return {
        "config": {"wide_screen_mode": True},
        "header": {
            "title": {"tag": "plain_text", "content": title},
            "template": template,
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
