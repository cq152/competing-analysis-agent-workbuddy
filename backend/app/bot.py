"""飞书长连接 Bot：基于 lark-coding-agent-bridge 思路的声明式飞书接入层。

借鉴该项目的三件套思路，但用 Python 实现、接我们已验证的分析引擎：
  1. 飞书开放平台「企业自建应用」（开发者后台创建，免企业开通「自定义智能体」权限）
  2. WebSocket 长连接（lark-oapi 的 lark.ws.Client，免公网域名/内网穿透）
  3. bot_config.json 声明式管理人设/场景路由/群权限

替代此前卡在 UI 的「Aily 平台自定义智能体」创建方式。
"""
import json
import os
import re
import threading
from pathlib import Path
from typing import Optional

import lark_oapi as lark
from lark_oapi.api.im.v1 import CreateMessageRequest, CreateMessageRequestBody

from .config import settings
from .engine import analyze, list_scenes

# 声明式配置（不含密钥，密钥走 .env）
CONFIG_PATH = Path(__file__).resolve().parent.parent / "bot_config.json"


def load_bot_config() -> dict:
    if CONFIG_PATH.exists():
        return json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    return {}


BOT_CONFIG = load_bot_config()

DEFAULT_SCENE = BOT_CONFIG.get("default_scene", "battle_card")
BOT_NAME = BOT_CONFIG.get("bot_name", "竞品分析搭档")
REQUIRE_MENTION_IN_GROUP = BOT_CONFIG.get("require_mention_in_group", True)
ALLOWED_CHATS = BOT_CONFIG.get("allowed_chats", [])  # 空=不限制

# 场景路由：命令前缀 -> scene key（可被 bot_config.json 的 scene_aliases 覆盖）
SCENE_ALIASES = {
    "/battle": "battle_card",
    "/price": "pricing",
    "/weekly": "weekly",
    "/discover": "discovery",
    "/report": "weekly",
}
if isinstance(BOT_CONFIG.get("scene_aliases"), dict):
    SCENE_ALIASES.update(BOT_CONFIG["scene_aliases"])


def _default_help() -> str:
    lines = [
        f"我是 {BOT_NAME}，竞品雷达 + 军师。用法：",
        "/battle 客户总拿 X 压我们，怎么回？",
        "/price 竞品降价了，我们怎么跟？",
        "/weekly 生成本周竞品周报",
        "/discover 我们做 XX 产品，帮我发现竞品",
        "不带前缀默认按 battle_card（销售应对卡）处理。",
        f"可用场景：{', '.join(list_scenes())}",
    ]
    return "\n".join(lines)


HELP_TEXT = BOT_CONFIG.get("help_text") or _default_help()

# 全局 bot 单例（在 main() 中创建，事件回调引用）
_bot: Optional["LarkBot"] = None


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
        text = text.strip()
        for alias, scene in SCENE_ALIASES.items():
            if text.startswith(alias):
                return scene, text[len(alias):].strip()
        return DEFAULT_SCENE, text

    def handle_message(self, data: lark.im.v1.P2ImMessageReceiveV1) -> None:
        msg = data.event.message
        # 群聊：只有被 @ 才响应（避免对群里每条消息都答）
        if msg.chat_type == "group" and REQUIRE_MENTION_IN_GROUP:
            if not getattr(msg, "mentions", None):
                return
        # 群权限白名单
        if ALLOWED_CHATS and msg.chat_id not in ALLOWED_CHATS:
            return
        if msg.message_type != "text":
            self._reply(msg, "目前只支持文本消息，请直接输入竞品分析问题。")
            return
        content = json.loads(msg.content)
        raw_text = content.get("text", "")
        # 去掉 @mention 占位（群聊 @机器人 时会有 @_user_x 或 <at> 标签）
        clean = re.sub(r"@_user_\d+", "", raw_text)
        clean = re.sub(r"<at[^>]*>.*?</at>", "", clean, flags=re.DOTALL)
        clean = clean.strip()
        if not clean or clean in ("/help", "帮助", "?"):
            self._reply(msg, HELP_TEXT)
            return
        scene, query = self.parse_command(clean)
        # 异步分析，避免阻塞回调触发超时重试
        threading.Thread(
            target=self._async_analyze, args=(msg, scene, query), daemon=True
        ).start()
        self._reply(msg, "🔍 分析中，稍候…")

    def _async_analyze(self, msg, scene: str, query: str) -> None:
        try:
            result = analyze(scene, query)
        except Exception as e:  # noqa: BLE001
            result = f"分析失败：{e}"
        self._reply(msg, result)

    def _reply(self, msg, text: str) -> None:
        content = json.dumps({"text": text}, ensure_ascii=False)
        req = (
            CreateMessageRequest.builder()
            .receive_id_type("message_id")
            .receive_id(msg.message_id)
            .msg_type("text")
            .content(content)
            .build()
        )
        resp = self.client.im.v1.message.create(req)
        if not resp.success():
            print(f"[lark] reply failed: code={resp.code} msg={resp.msg}")


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


def main():
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
