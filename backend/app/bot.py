"""飞书长连接 Bot：基于 lark-coding-agent-bridge 思路的声明式飞书接入层。

借鉴该项目的三件套思路，但用 Python 实现、接我们已验证的分析引擎：
  1. 飞书开放平台「企业自建应用」（开发者后台创建，免企业开通「自定义智能体」权限）
  2. WebSocket 长连接（lark-oapi 的 lark.ws.Client，免公网域名/内网穿透）
  3. bot_config.json 声明式管理人设/场景路由/群权限

替代此前卡在 UI 的「Aily 平台自定义智能体」创建方式。
"""
import atexit
import json
import os
import re
import subprocess
import sys
import threading
from pathlib import Path
from typing import Optional

import lark_oapi as lark
from lark_oapi.api.im.v1 import CreateMessageRequest, CreateMessageRequestBody

from .config import settings
from .engine import analyze, compare, list_scenes, classify_intent, general_qa
from .sessions import get_session_store

# 声明式配置（不含密钥，密钥走 .env）
CONFIG_PATH = Path(__file__).resolve().parent.parent / "bot_config.json"
# 单实例锁：飞书长连接同一应用只允许一个活跃连接，多开会路由到僵尸旧连接
LOCK_PATH = Path(__file__).resolve().parent.parent / ".bot.lock"


def load_bot_config() -> dict:
    if CONFIG_PATH.exists():
        return json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    return {}


BOT_CONFIG = load_bot_config()

DEFAULT_SCENE = BOT_CONFIG.get("default_scene", "battle_card")
BOT_NAME = BOT_CONFIG.get("bot_name", "竞品分析搭档")
REQUIRE_MENTION_IN_GROUP = BOT_CONFIG.get("require_mention_in_group", True)
ALLOWED_CHATS = BOT_CONFIG.get("allowed_chats", [])  # 空=不限制
REPLY_IF_UNAUTHORIZED = BOT_CONFIG.get("reply_if_unauthorized", False)  # 非白名单群是否回提示

# 场景路由：命令前缀 -> scene key（无 / 自然语言为主，兼容旧 / 前缀）
SCENE_ALIASES = {
    "battle": "battle_card",    "/battle": "battle_card",
    "price": "pricing",         "/price": "pricing",
    "weekly": "weekly",         "/weekly": "weekly",
    "discover": "discovery",    "/discover": "discovery",
    "report": "weekly",         "/report": "weekly",
    "周报": "weekly",
    "发现": "discovery",
    "发现竞品": "discovery",
    "定价": "pricing",
    "价格": "pricing",
    "应对": "battle_card",
    "battle card": "battle_card",
    "销售应对": "battle_card",
    "帮助": None,  # 走 help 分支
    "help": None,
}
if isinstance(BOT_CONFIG.get("scene_aliases"), dict):
    SCENE_ALIASES.update(BOT_CONFIG["scene_aliases"])

# 明显非竞品分析信号（UI 报错 / 闲聊中的故障描述）：命中直接澄清，
# 不调 LLM、不生成销售应对卡（修复「查看详情按钮没效果」被误判为 battle_card 的 bug）
_OFF_TOPIC_RE = re.compile(
    r"(按钮|没反应|没效果|无反应|无效果|点击|单击|点不|报错|打不开|崩溃|"
    r"加载|卡住|转圈|弹窗|白屏|404|查看详情)",
    re.IGNORECASE,
)


def _default_help() -> str:
    lines = [
        f"我是 {BOT_NAME}，竞品雷达 + 军师。直接对我说：",
        "「客户总拿飞书压我们，怎么应对？」（销售应对卡）",
        "「钉钉降价了，我们怎么跟？」（定价分析）",
        "「本周竞品周报」（周报生成）",
        "「我们做协同办公，帮我发现竞品」（竞品发现）",
        "「对比 飞书 钉钉 企业微信」（多竞品对比）",
        "「监控 飞书」（添加竞品监控，有变化自动推群）",
        "「监控列表 / 删除监控 1」（管理监控项）",
        "「帮助」重新显示本说明",
        f"可用场景：{', '.join(list_scenes())}",
    ]
    return "\n".join(lines)


HELP_TEXT = BOT_CONFIG.get("help_text") or _default_help()

# 全局 bot 单例（在 main() 中创建，事件回调引用）
_bot: Optional["LarkBot"] = None

# W3.2 的「和 XX 对比」主竞品记忆已持久化到 SQLite（见 app/sessions.py）；
# bot 不再持有 in-memory 状态，跨重启不丢。


class LarkBot:
    def __init__(self):
        self.app_id = os.getenv("FEISHU_APP_ID", "")
        self.app_secret = os.getenv("FEISHU_APP_SECRET", "")
        if not self.app_id or not self.app_secret:
            raise RuntimeError(
                "缺少 FEISHU_APP_ID / FEISHU_APP_SECRET，请在 backend/.env 配置"
            )
        # API client 用于发消息（长连接本身只收事件）
        self.client = (
            lark.Client.builder()
            .app_id(self.app_id)
            .app_secret(self.app_secret)
            .build()
        )

    @staticmethod
    def parse_command(text: str) -> tuple[str, str]:
        """从文本中匹配场景别名（最长匹配优先，避免"发现竞品"被"发现"误截）。
        返回 (scene, 去掉别名后的剩余 query)。"""
        text = text.strip()
        # 按别名长度降序排列，确保"发现竞品"优先于"发现"
        candidates = sorted(
            [(a, s) for a, s in SCENE_ALIASES.items() if s is not None],
            key=lambda x: -len(x[0]),
        )
        for alias, scene in candidates:
            if text.startswith(alias):
                rest = text[len(alias):].strip()
                return scene, rest
        return DEFAULT_SCENE, text

    def handle_message(self, data: lark.im.v1.P2ImMessageReceiveV1) -> None:
        msg = data.event.message
        # 群聊：只有被 @ 才响应（避免对群里每条消息都答）
        if msg.chat_type == "group" and REQUIRE_MENTION_IN_GROUP:
            if not getattr(msg, "mentions", None):
                return
        # 打印来源群 ID，便于把测试群填进 allowed_chats 白名单
        print(f"[lark] chat_id={msg.chat_id} chat_type={msg.chat_type}")
        # 群权限白名单
        if ALLOWED_CHATS and msg.chat_id not in ALLOWED_CHATS:
            if REPLY_IF_UNAUTHORIZED:
                self._reply(msg, "本群未在白名单内，请联系管理员配置 allowed_chats。")
            return
        if msg.message_type != "text":
            print(f"[lark] non-text message: type={msg.message_type}")
            self._push_tip(msg.chat_id, "⚠️ 暂不支持", f"目前只支持**文本消息**。\n\n请直接输入竞品分析问题，例如：\n`客户总拿飞书压我们，怎么应对？`", "orange")
            return
        content = json.loads(msg.content)
        raw_text = content.get("text", "")
        # 去掉 @mention 占位：优先按事件自带的 mentions 数组精确剥离（key 即飞书下发的
        # "@_user_ou_xxxx" 完整 token，最稳），再兜底正则（<at> 标签 + @_user_<任意非空白>）。
        # 注：飞书真实 open_id 形如 ou_xxxx（含字母），旧正则 @_user_\d+ 只认数字会漏剥，
        # 导致命令（/monitor、/weekly 等）被误判为默认 battle_card —— 已修复。
        # 注 2：webhook 传来的 mentions 是 dict 而非 SimpleNamespace，getattr 对 dict
        # 会返回 None，导致 key 永远提不到 —— 须同时兼容 dict 和 namespace。
        for m in (getattr(msg, "mentions", None) or []):
            key = None
            if isinstance(m, dict):
                key = m.get("key")
            else:
                key = getattr(m, "key", None)
            if key:
                raw_text = raw_text.replace(key, "")
        clean = re.sub(r"@_user_[^\s@]+", "", raw_text)
        clean = re.sub(r"<at[^>]*>.*?</at>", "", clean, flags=re.DOTALL)
        # 兜底：飞书新版自定义机器人 @ 格式可能直接是 "@机器人名" 而非 "@_user_xxx"
        # （mentions 数组可能为空或 key 不匹配），去掉行首 "@xxx" 前缀避免命令路由失效。
        # 先试带空格的 "@机器人名 " → 再试无空格的 "@机器人名"（如 "@机器人帮助"）
        clean = re.sub(r"^@[^\s]+[\s　]+", "", clean)
        clean = re.sub(r"^@[^\s]+", "", clean)
        clean = clean.strip()
        # 去掉句首问候语（避免「你好，对比飞书钉钉」被误判；纯问候走帮助卡分支）
        clean = re.sub(r"^(你好|您好|hi|hello|hey|嗨|哈喽)[,，。.\s]*", "", clean, flags=re.IGNORECASE).strip()
        print(f"[lark] clean={clean!r}")
        # W3.5：首次@欢迎（chat_id 维度，每群只推一次），不阻塞当前命令
        self._maybe_onboard(msg)
        # 空 @mention 或显式"帮助" → 推使用指南卡片（用户期望：@了不说话也给引导）
        if not clean or clean in ("/help", "帮助", "help", "?"):
            self._push_help_card(msg)
            return
        # 命令路由：优先自然语言（无 /），同时兼容旧 / 前缀
        # 注意顺序：子命令（列表/删除）必须优先于「监控 <竞品>」，避免"监控列表"被误判为监控"列表"
        if re.match(r"^(监控列表|删除监控|monitor\s+list|monitor\s+remove)", clean):
            self._handle_monitor(msg, clean)
            return
        if re.match(r"^/?(监控|monitor|添加监控|添加)", clean):
            self._handle_monitor(msg, clean)
            return
        # 「和 X 对比」/「对比 X」（单参数=补主竞品）先于「对比 A B」（多参数）
        if re.match(r"^(和\s+\S+\s*对比|对比\s+\S+)$", clean):
            self._handle_compare_phrase(msg, clean)
            return
        # 「对比 A B」：多竞品显式对比（"对比"后至少两个词）
        if re.match(r"^/?(对比|compare)\s*\S+\s+\S+", clean):
            self._handle_compare(msg, clean)
            return
        scene, query = self.parse_command(clean)
        # 无命令前缀命中（parse_command 退回默认场景且未剥离任何别名）→ 先过意图鉴别，
        # 避免「查看详情按钮没效果」这类无关消息被强转为销售应对卡。
        if scene == DEFAULT_SCENE and query == clean:
            threading.Thread(target=self._async_route, args=(msg, clean), daemon=True).start()
        else:
            threading.Thread(
                target=self._async_analyze, args=(msg, scene, query), daemon=True
            ).start()
            self._push_tip(msg.chat_id, "🔍 分析中", f"正在检索网络情报并生成**{scene}**分析…\n稍候片刻，结果即将送达。", "blue")

    def _async_analyze(self, msg, scene: str, query: str) -> None:
        try:
            res = analyze(scene, query)
        except Exception as e:  # noqa: BLE001
            self._push_tip(msg.chat_id, "⚠️ 分析失败", f"错误信息：{e}\n\n请稍后重试，或换个问法。", "red")
            return
        # W3.3：分析完成后持久化历史（会话连续/回溯），摘要截取前 200 字
        try:
            get_session_store().save_analysis(
                msg.chat_id, scene, query, (res.analysis or "")[:200]
            )
        except Exception as e:  # noqa: BLE001
            print(f"[lark] save analysis history failed: {e}")
        # W4.3：尝试生成飞书云文档（完整图文报告），失败静默降级（doc_url 留空=卡片不挂链接）
        self._attach_doc(res)
        # W3.1：富卡片优先，失败回退纯文本（不破坏 W1 已验证文本链路）
        try:
            from app.card_renderer import render_card

            card = render_card(res)
            if card and self.push_card(msg.chat_id, card):
                return
        except Exception as e:  # noqa: BLE001
            print(f"[lark] card render/send failed, fallback to text: {e}")
        # 回退：纯文本
        text = res.analysis if hasattr(res, "analysis") else str(res)
        self._reply(msg, text)

    def _async_route(self, msg, clean: str) -> None:
        """无命令前缀时的意图鉴别（异步，避免阻塞事件回调）。

        - 命中明显非竞品分析信号（按钮报错等）→ 友好澄清，不生成分析卡；
        - 其余交给 LLM 分类，判为 unknown（非竞品分析意图）→ 同样澄清；
        - 判到具体场景（battle_card/pricing/weekly/discovery/compare）→ 正常分析。
        """
        if _OFF_TOPIC_RE.search(clean):
            self._push_clarify_card(msg)
            return
        intent = classify_intent(clean)
        if intent == "unknown":
            self._push_clarify_card(msg)
            return
        if intent == "chat":
            self._push_tip(
                msg.chat_id,
                "💬 通用问答",
                "正在处理你的请求（可联网检索）…\n稍候片刻，结果即将送达。",
                "blue",
            )
            threading.Thread(target=self._async_general_qa, args=(msg, clean), daemon=True).start()
            return
        if intent == "compare":
            self._handle_compare(msg, clean)
            return
        # battle_card / pricing / weekly / discovery
        self._push_tip(msg.chat_id, "🔍 分析中", f"正在检索网络情报并生成**{intent}**分析…\n稍候片刻，结果即将送达。", "blue")
        self._async_analyze(msg, intent, clean)

    def _async_general_qa(self, msg, text: str) -> None:
        """通用问答兜底（异步）：与竞品分析无关但属助手能帮忙的请求。"""
        try:
            answer = general_qa(text, use_web=True)
        except Exception as e:  # noqa: BLE001
            self._push_tip(msg.chat_id, "⚠️ 通用问答失败", f"错误信息：{e}\n\n请稍后重试。", "red")
            return
        # 卡片 markdown 有长度限制：短答案用卡片，长答案直接走文本（上限更高）
        if len(answer) <= 1800:
            try:
                from app.card_renderer import render_qa_card

                card = render_qa_card(text, answer)
                if card and self.push_card(msg.chat_id, card):
                    return
            except Exception as e:  # noqa: BLE001
                print(f"[lark] qa card render/send failed, fallback to text: {e}")
        self._reply(msg, f"💬 通用问答：\n\n{answer}")

    def _push_clarify_card(self, msg) -> None:
        """无关 / 无法识别为竞品分析意图时，友好澄清（不生成销售应对卡等分析卡）。"""
        try:
            from app.card_renderer import render_clarify_card

            card = render_clarify_card()
            if self.push_card(msg.chat_id, card):
                return
        except Exception as e:  # noqa: BLE001
            print(f"[lark] clarify card failed, fallback to text: {e}")
        self._reply(
            msg,
            "我是竞品雷达 + 军师，专注竞品分析。你可以这样问我：\n"
            "「客户总拿飞书压我们，怎么应对？」（销售应对卡）\n"
            "「钉钉降价了，我们怎么跟？」（定价分析）\n"
            "「对比 飞书 钉钉 企业微信」（多竞品对比）\n"
            "「监控 飞书」（添加竞品监控）",
        )

    def _remember_main(self, chat_id: str, target: str) -> None:
        """记录本群上次主竞品，供「和 XX 对比」自动补位（3.3 持久化到 SQLite）。"""
        get_session_store().set_primary(chat_id, target)

    def _handle_compare(self, msg, text: str) -> None:
        """处理「对比 A B [C]」或「/compare A B [C]」：显式多竞品对比。"""
        # 去掉命令前缀（"对比 " / "/compare " / "compare "），并剔除 vs/和/对比 等填充词，
        # 使「对比 飞书 vs 钉钉」也能正确解析为 ["飞书","钉钉"]
        rest = re.sub(r"^(对比|/compare|compare)\s*", "", text).strip()
        FILLER = {"vs", "VS", "和", "对比", "compare", "/compare"}
        targets = [t for t in rest.split() if t and t not in FILLER]
        if len(targets) < 2:
            self._push_tip(msg.chat_id, "📖 用法提示", "对比需要至少 2 个竞品。\n\n**示例：**\n`对比 飞书 钉钉`\n`对比 飞书 钉钉 企业微信`", "orange")
            return
        self._remember_main(msg.chat_id, targets[0])
        threading.Thread(target=self._async_compare, args=(msg.chat_id, targets), daemon=True).start()
        self._push_tip(msg.chat_id, "🔍 对比分析中", f"正在检索并横向对比 **{' / '.join(targets)}**…\n稍候片刻，对比卡片即将送达。", "blue")

    def _handle_compare_phrase(self, msg, text: str) -> None:
        """处理「和 X 对比」/「对比 X」：提取 X，补上次主竞品成双目标。"""
        cleaned = re.sub(r"^(和|对比)\s*", "", text)
        cleaned = cleaned.replace("对比", "").strip()
        parts = cleaned.split()
        if not parts:
            self._push_tip(msg.chat_id, "📖 用法提示", "和 <竞品> 对比 / 对比 <竞品>\n（需先「对比 A B」记录主竞品）", "orange")
            return
        other = parts[0]
        main = get_session_store().get_primary(msg.chat_id)
        if not main:
            self._push_tip(msg.chat_id, "📖 用法提示", "还没有记录主竞品，请先用 `对比 A B` 指定。\n\n**示例：** `对比 飞书 钉钉`", "orange")
            return
        # 注意：不更新主竞品——"和X对比"语义下 X 是对比对象，主竞品保持不变。
        # 只有显式「对比 A B」(_handle_compare) 才 remember_main(targets[0])。
        # 此前这里误写 _remember_main(chat_id, other) 导致连续对比时主竞品被覆盖（X vs X bug）。
        threading.Thread(target=self._async_compare, args=(msg.chat_id, [main, other]), daemon=True).start()
        self._push_tip(msg.chat_id, "🔍 对比分析中", f"正在检索并对比 **{main} / {other}**…\n稍候片刻，对比卡片即将送达。", "blue")

    def _async_compare(self, chat_id: str, targets: list[str]) -> None:
        """异步跑对比引擎并卡片优先推送（复用 W3.1 push_card）。

        与 _async_analyze 对齐：直接收 chat_id（卡片按钮回调也用 chat_id 调用）。
        """
        try:
            res = compare(targets)
        except Exception as e:  # noqa: BLE001
            self._push_tip(chat_id, "⚠️ 对比分析失败", f"错误信息：{e}\n\n请稍后重试，或换个竞品组合。", "red")
            return
        # W3.3：对比也存历史（会话连续/回溯），摘要截取前 200 字
        try:
            get_session_store().save_analysis(
                chat_id, "compare", " vs ".join(targets), (res.analysis or "")[:200]
            )
        except Exception as e:  # noqa: BLE001
            print(f"[lark] save compare history failed: {e}")
        # W4.3：尝试生成飞书云文档（完整图文报告），失败静默降级
        self._attach_doc(res)
        try:
            from app.card_renderer import render_card

            card = render_card(res)
            if card and self.push_card(chat_id, card):
                return
        except Exception as e:  # noqa: BLE001
            print(f"[lark] compare card render/send failed, fallback to text: {e}")
        text = res.analysis or str(res)
        self.push(chat_id, text)

    def _attach_doc(self, res) -> None:
        """W4.3：把分析结果生成飞书云文档（完整图文报告），并把链接挂到 res.doc_url。

        失败静默降级（权限未开 / 网络异常）→ doc_url 留空，卡片退回全文展示，不影响主链路。
        """
        if not settings.feishu_docx_enabled:
            return
        try:
            from app.docx_client import build_report_doc, build_report_markdown, report_title

            md = build_report_markdown(res)
            url = build_report_doc(report_title(res), md)
            res.doc_url = url
        except Exception as e:  # noqa: BLE001
            print(f"[lark] cloud doc build skipped (docx permission?): {e}")

    def _reply(self, msg, text: str) -> None:
        # 飞书 `im.v1.message.create` 实测：receive_id_type="message_id"（引用回复）报
        # 99992402 field validation failed；改用 receive_id_type="chat_id" 发到群最稳
        # （群内以 bot 独立消息呈现，可见且不依赖消息 id 格式）。
        self.push(msg.chat_id, text)

    def _push_tip(self, chat_id: str, title: str, content: str = "", template: str = "blue") -> None:
        """P2：推送轻量提示卡片（等待/错误/成功/警告）。失败回退纯文本。"""
        try:
            from app.card_renderer import render_tip_card

            card = render_tip_card(title, content, template)
            if self.push_card(chat_id, card):
                return
        except Exception as e:  # noqa: BLE001
            print(f"[lark] tip card failed, fallback to text: {e}")
        text = title
        if content:
            text += f"\n{content}"
        self.push(chat_id, text)

    def _maybe_onboard(self, msg) -> None:
        """首次@欢迎：chat_id 维度每群只推一次 onboarding 卡片，不阻塞当前命令。

        用 sessions 表的 onboarded 标记（W3.5 新增列）。失败静默，不影响主流程。
        """
        try:
            if not get_session_store().mark_onboarded(msg.chat_id):
                return  # 已欢迎过
            from app.card_renderer import render_onboarding_card

            card = render_onboarding_card(BOT_NAME)
            self.push_card(msg.chat_id, card)
        except Exception as e:  # noqa: BLE001
            print(f"[lark] onboard failed (skip): {e}")

    def _push_help_card(self, msg) -> None:
        """推送帮助卡片（W3.5 取代纯文本 HELP_TEXT）。卡片发送失败回退纯文本。"""
        try:
            from app.card_renderer import render_help_card

            card = render_help_card(BOT_NAME, list_scenes())
            if self.push_card(msg.chat_id, card):
                return
        except Exception as e:  # noqa: BLE001
            print(f"[lark] help card failed, fallback to text: {e}")
        self._reply(msg, HELP_TEXT)

    def handle_card_action(self, chat_id: str, value: dict) -> None:
        """处理卡片按钮回调（P1）。由 webhook_server 的 card.action.trigger 路由调用。

        value 来自按钮 JSON value 字段（已由 webhook 反序列化为 dict）。
        支持命令：re_analyze（重新分析）、quick_monitor（快速添加监控）。
        """
        if not isinstance(value, dict):
            print(f"[lark] card action value not dict: {type(value).__name__} {str(value)[:120]}")
            self.push(chat_id, "⚠️ 按钮数据格式异常，请重试，或直接在群里发消息给我。")
            return
        cmd = value.get("cmd", "")
        if cmd == "re_analyze":
            scene = value.get("scene", DEFAULT_SCENE)
            query = value.get("query", "")
            if not query:
                self.push(chat_id, "⚠️ 按钮信息不完整，请直接发消息给我。")
                return
            # 兼容旧版对比卡片：结构上误用 scene="compare"（正确应是 re_compare）。
            # 从 query 中解析竞品（剔除 vs/和/对比 等填充词）后重新跑对比。
            if scene == "compare":
                parts = [t for t in re.split(r"\s+", query) if t and t not in ("vs", "VS", "和", "对比", "compare")]
                if len(parts) >= 2:
                    self.push(chat_id, f"🔍 重新对比 {' / '.join(parts)} 中…")
                    threading.Thread(target=self._async_compare, args=(chat_id, parts), daemon=True).start()
                else:
                    self.push(chat_id, "⚠️ 这张卡片较旧，请重新发「对比 A B」给我。")
                return
            self.push(chat_id, f"🔍 {scene} 重新分析中…")
            threading.Thread(
                target=self._async_card_re_analyze, args=(chat_id, scene, query), daemon=True
            ).start()
        elif cmd == "re_compare":
            targets = value.get("targets", [])
            if not isinstance(targets, list) or len(targets) < 2:
                self.push(chat_id, "⚠️ 按钮信息不完整，请直接发「对比 A B」给我。")
                return
            self.push(chat_id, f"🔍 重新对比 {' / '.join(targets)} 中…")
            threading.Thread(
                target=self._async_compare, args=(chat_id, targets), daemon=True
            ).start()
        elif cmd == "quick_monitor":
            competitor = value.get("competitor", "")
            if not competitor:
                self.push(chat_id, "⚠️ 按钮信息不完整，请直接发「监控 <竞品>」给我。")
                return
            try:
                from app.monitor import get_monitor_service

                svc = get_monitor_service(self)
                rowid, created = svc.store.add(chat_id, competitor)
                if created:
                    self.push(chat_id, f"📡 已添加监控：{competitor}（有变化自动推群）")
                else:
                    self.push(chat_id, f"📡 {competitor} 已在监控中，无需重复添加。")
            except Exception as e:  # noqa: BLE001
                print(f"[lark] quick_monitor failed: {e}")
                self.push(chat_id, f"⚠️ 添加监控失败：{e}")
        elif cmd == "quick_unmonitor":
            competitor = value.get("competitor", "")
            if not competitor:
                self.push(chat_id, "⚠️ 按钮信息不完整。")
                return
            try:
                from app.monitor import get_monitor_service

                svc = get_monitor_service(self)
                ok = svc.store.remove_by_competitor(chat_id, competitor)
                if ok:
                    self.push(chat_id, f"🗑 已删除监控：{competitor}")
                else:
                    self.push(chat_id, f"⚠️ 未找到 {competitor} 的监控记录。")
            except Exception as e:  # noqa: BLE001
                print(f"[lark] quick_unmonitor failed: {e}")
                self.push(chat_id, f"⚠️ 删除监控失败：{e}")

    def _async_card_re_analyze(self, chat_id: str, scene: str, query: str) -> None:
        """异步重新分析并推送卡片（卡片按钮「再分析一次」专用）。"""
        try:
            from app.card_renderer import render_card

            res = analyze(scene, query)
            # 存历史
            try:
                get_session_store().save_analysis(
                    chat_id, scene, query, (res.analysis or "")[:200]
                )
            except Exception as e:  # noqa: BLE001
                print(f"[lark] card re-analyze save history failed: {e}")
            # 推卡片
            card = render_card(res)
            if card and self.push_card(chat_id, card):
                return
            # 回退文本
            self.push(chat_id, res.analysis or str(res))
        except Exception as e:  # noqa: BLE001
            self.push(chat_id, f"⚠️ 重新分析失败：{e}")

    def push(self, chat_id: str, text: str) -> bool:
        """主动向指定群推送消息（监控雷达用）。返回是否发送成功。"""
        content = json.dumps({"text": text}, ensure_ascii=False)
        req = (
            CreateMessageRequest.builder()
            .receive_id_type("chat_id")
            .request_body(
                CreateMessageRequestBody.builder()
                .receive_id(chat_id)
                .msg_type("text")
                .content(content)
                .build()
            )
            .build()
        )
        resp = self.client.im.v1.message.create(req)
        if not resp.success():
            print(f"[lark] push failed: code={resp.code} msg={resp.msg}")
            return False
        return True

    def push_card(self, chat_id: str, card: dict) -> bool:
        """主动向指定群推送飞书 Interactive Card（W3.1 富交互消息）。返回是否发送成功。"""
        content = json.dumps(card, ensure_ascii=False)
        req = (
            CreateMessageRequest.builder()
            .receive_id_type("chat_id")
            .request_body(
                CreateMessageRequestBody.builder()
                .receive_id(chat_id)
                .msg_type("interactive")
                .content(content)
                .build()
            )
            .build()
        )
        resp = self.client.im.v1.message.create(req)
        if not resp.success():
            print(f"[lark] push_card failed: code={resp.code} msg={resp.msg}")
            return False
        return True

    def _handle_monitor(self, msg, text: str) -> None:
        """处理监控命令：「监控 飞书」「监控列表」「删除监控 1」等（兼容 /monitor 前缀）。"""
        from app.monitor import get_monitor_service

        # 标准化：去掉 / 前缀
        text = re.sub(r"^/", "", text).strip()
        svc = get_monitor_service(self)

        # 子命令判断：优先精确匹配
        if re.match(r"^(监控列表|monitor\s+list|list)$", text):
            items = svc.store.list(msg.chat_id)
            if not items:
                self._push_tip(msg.chat_id, "📡 监控列表", "本群暂无监控项。\n\n用 `监控 <竞品名>` 添加，例如：\n`监控 飞书`", "blue")
                return
            lines = ["📡 本群监控列表：", ""]
            for m in items:
                lines.append(f"  **#{m['id']}** {m['competitor']}（场景：{m['scene']}）")
            lines.append("")
            lines.append(f"共 {len(items)} 项 · 有变化自动推群")
            self._push_tip(msg.chat_id, "📡 监控列表", "\n".join(lines), "blue")
            return

        if re.match(r"^(删除监控|monitor\s+remove|remove)\s*\d+", text):
            mid_str = re.sub(r"^(删除监控|monitor\s+remove|remove)\s*", "", text).strip()
            try:
                mid = int(mid_str)
            except ValueError:
                self._push_tip(msg.chat_id, "📖 用法提示", "请指定要删除的监控 ID，如：`删除监控 1`", "orange")
                return
            ok = svc.store.remove(mid, msg.chat_id)
            if ok:
                self._push_tip(msg.chat_id, "✅ 已删除", f"监控 #{mid} 已移除。", "green")
            else:
                self._push_tip(msg.chat_id, "⚠️ 未找到", f"未找到监控 #{mid}，或无权删除。\n\n发 `监控列表` 查看当前监控项。", "red")
            return

        # 「监控 <竞品>」「monitor add <竞品>」「添加监控 <竞品>」
        competitor = re.sub(
            r"^(监控|monitor\s+add|添加监控|添加)\s*", "", text
        ).strip()
        if not competitor:
            self._push_tip(msg.chat_id, "📖 用法提示", "**监控命令用法：**\n\n`监控 <竞品名>` — 添加监控\n`监控列表` — 查看监控项\n`删除监控 <ID>` — 移除监控", "orange")
            return
        mid, created = svc.store.add(competitor, msg.chat_id)
        if created:
            self._push_tip(msg.chat_id, "✅ 监控已添加", f"**{competitor}**（#{mid}）\n\n定时搜索公开网络，有变化自动推群。", "green")
        else:
            self._push_tip(msg.chat_id, "📡 无需重复添加", f"**{competitor}**（#{mid}）已在监控中。", "orange")


def do_p2_im_message_receive_v1(data: lark.im.v1.P2ImMessageReceiveV1) -> None:
    if _bot is None:
        return
    _bot.handle_message(data)


def build_event_handler():
    return (
        lark.EventDispatcherHandler.builder("", "")
        .register_p2_im_message_receive_v1(do_p2_im_message_receive_v1)
        .build()
    )


def _is_pid_alive(pid: int) -> bool:
    if os.name == "nt":
        # 中文 Windows 下 tasklist 输出为 GBK，需按字节读再解码，避免 UTF-8 解码报错
        proc = subprocess.run(
            ["tasklist", "/FI", f"PID eq {pid}"],
            capture_output=True,
        )
        out = proc.stdout.decode("gbk", errors="ignore")
        return str(pid) in out
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def _acquire_single_instance() -> None:
    """确保全局只有一个 Bot 长连接。

    飞书长连接同一应用只允许一个活跃连接；多开会因为旧连接僵尸（被 kill 后飞书靠心跳
    才发现死掉）导致事件路由到已死的实例，表现为『群里 @机器人 没反应』。
    用 PID 锁 + 原子 O_EXCL 创建保证单实例（杜绝竞态），并清理残留的过期锁。
    """
    for _ in range(5):
        if LOCK_PATH.exists():
            try:
                old_pid = int(LOCK_PATH.read_text(encoding="utf-8").strip())
            except (ValueError, OSError):
                old_pid = None
            if old_pid and _is_pid_alive(old_pid):
                print(f"[lark] 已有实例在运行（PID={old_pid}），本进程退出以避免重复长连接。")
                sys.exit(0)
            # 旧锁已失效（进程不在），删掉重试
            LOCK_PATH.unlink(missing_ok=True)
        try:
            fd = os.open(str(LOCK_PATH), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError:
            continue  # 被别的实例抢先创建，下一轮循环再判断
        os.write(fd, str(os.getpid()).encode("utf-8"))
        os.close(fd)
        atexit.register(lambda: LOCK_PATH.unlink(missing_ok=True))
        return
    print("[lark] 无法获取单实例锁，可能已有实例在运行，本进程退出。")
    sys.exit(1)


def main():
    _acquire_single_instance()
    global _bot
    _bot = LarkBot()
    handler = build_event_handler()
    cli = lark.ws.Client(
        _bot.app_id, _bot.app_secret, event_handler=handler, log_level=lark.LogLevel.INFO
    )
    print(f"[{BOT_NAME}] 飞书长连接 Bot 启动中（免公网，等待事件）…")
    cli.start()


if __name__ == "__main__":
    main()
