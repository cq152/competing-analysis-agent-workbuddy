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
from .engine import analyze, compare, list_scenes
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
            self._reply(msg, "目前只支持文本消息，请直接输入竞品分析问题。")
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
        if not clean or clean in ("/help", "帮助", "help", "?"):
            self._reply(msg, HELP_TEXT)
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
        # 异步分析，避免阻塞回调触发超时重试
        threading.Thread(
            target=self._async_analyze, args=(msg, scene, query), daemon=True
        ).start()
        self._reply(msg, "🔍 分析中，稍候…")

    def _async_analyze(self, msg, scene: str, query: str) -> None:
        try:
            res = analyze(scene, query)
        except Exception as e:  # noqa: BLE001
            self._reply(msg, f"分析失败：{e}")
            return
        # W3.3：分析完成后持久化历史（会话连续/回溯），摘要截取前 200 字
        try:
            get_session_store().save_analysis(
                msg.chat_id, scene, query, (res.analysis or "")[:200]
            )
        except Exception as e:  # noqa: BLE001
            print(f"[lark] save analysis history failed: {e}")
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

    def _remember_main(self, chat_id: str, target: str) -> None:
        """记录本群上次主竞品，供「和 XX 对比」自动补位（3.3 持久化到 SQLite）。"""
        get_session_store().set_primary(chat_id, target)

    def _handle_compare(self, msg, text: str) -> None:
        """处理「对比 A B [C]」或「/compare A B [C]」：显式多竞品对比。"""
        # 去掉命令前缀（"对比 " 或 "/compare " 或 "compare "）
        rest = re.sub(r"^(对比|/compare|compare)\s*", "", text).strip()
        targets = [t for t in rest.split() if t]
        if len(targets) < 2:
            self._reply(msg, "用法：对比 <竞品A> <竞品B> [竞品C]\n例如：对比 飞书 钉钉")
            return
        self._remember_main(msg.chat_id, targets[0])
        threading.Thread(target=self._async_compare, args=(msg, targets), daemon=True).start()
        self._reply(msg, "🔍 对比分析中，稍候…")

    def _handle_compare_phrase(self, msg, text: str) -> None:
        """处理「和 X 对比」/「对比 X」：提取 X，补上次主竞品成双目标。"""
        cleaned = re.sub(r"^(和|对比)\s*", "", text)
        cleaned = cleaned.replace("对比", "").strip()
        parts = cleaned.split()
        if not parts:
            self._reply(msg, "用法：和 <竞品> 对比 / 对比 <竞品>（需先「对比 A B」记录主竞品）")
            return
        other = parts[0]
        main = get_session_store().get_primary(msg.chat_id)
        if not main:
            self._reply(msg, "还没有记录主竞品，请先用「对比 A B」指定，例如：对比 飞书 钉钉")
            return
        self._remember_main(msg.chat_id, other)
        threading.Thread(target=self._async_compare, args=(msg, [main, other]), daemon=True).start()
        self._reply(msg, "🔍 对比分析中，稍候…")

    def _async_compare(self, msg, targets: list[str]) -> None:
        """异步跑对比引擎并卡片优先推送（复用 W3.1 push_card）。"""
        try:
            res = compare(targets)
        except Exception as e:  # noqa: BLE001
            self._reply(msg, f"对比分析失败：{e}")
            return
        # W3.3：对比也存历史（会话连续/回溯），摘要截取前 200 字
        try:
            get_session_store().save_analysis(
                msg.chat_id, "compare", " vs ".join(targets), (res.analysis or "")[:200]
            )
        except Exception as e:  # noqa: BLE001
            print(f"[lark] save compare history failed: {e}")
        try:
            from app.card_renderer import render_card

            card = render_card(res)
            if card and self.push_card(msg.chat_id, card):
                return
        except Exception as e:  # noqa: BLE001
            print(f"[lark] compare card render/send failed, fallback to text: {e}")
        text = res.analysis or str(res)
        self._reply(msg, text)

    def _reply(self, msg, text: str) -> None:
        # 飞书 `im.v1.message.create` 实测：receive_id_type="message_id"（引用回复）报
        # 99992402 field validation failed；改用 receive_id_type="chat_id" 发到群最稳
        # （群内以 bot 独立消息呈现，可见且不依赖消息 id 格式）。
        self.push(msg.chat_id, text)

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
                self._reply(msg, "本群暂无可监控竞品。用「监控 <竞品名>」添加。")
                return
            lines = ["📡 本群监控列表："]
            for m in items:
                lines.append(f"  #{m['id']} {m['competitor']}（场景：{m['scene']}）")
            self._reply(msg, "\n".join(lines))
            return

        if re.match(r"^(删除监控|monitor\s+remove|remove)\s*\d+", text):
            mid_str = re.sub(r"^(删除监控|monitor\s+remove|remove)\s*", "", text).strip()
            try:
                mid = int(mid_str)
            except ValueError:
                self._reply(msg, "请指定要删除的监控 ID，如：删除监控 1")
                return
            ok = svc.store.remove(mid, msg.chat_id)
            self._reply(msg, "✅ 已删除" if ok else "⚠️ 未找到该 ID 或无权删除")
            return

        # 「监控 <竞品>」「monitor add <竞品>」「添加监控 <竞品>」
        competitor = re.sub(
            r"^(监控|monitor\s+add|添加监控|添加)\s*", "", text
        ).strip()
        if not competitor:
            self._reply(msg, "用法：\n  监控 <竞品名>（添加监控）\n  监控列表\n  删除监控 <ID>")
            return
        mid, created = svc.store.add(competitor, msg.chat_id)
        if created:
            self._reply(msg, f"✅ 已加入监控 #{mid}：{competitor}（定时搜索，有变化推群）")
        else:
            self._reply(msg, f"⚠️ 本群已监控「{competitor}」（#{mid}），无需重复添加")


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
