"""[已废弃] 飞书集成占位 —— 被 `app/bot.py` + `webhook_server.py` 取代。

本文件的 verify_url / send_card stub 仅被 `app/main.py` 的 `/webhook/feishu`
（已废弃）调用。实际飞书集成逻辑已迁移到：
  - app/bot.py：LarkBot 类（API 客户端、消息收发、场景路由）
  - webhook_server.py：/webhook/event（url_verification + 事件接收全链路）
"""
from typing import Any


def verify_url(challenge: str) -> dict:
    """飞书事件订阅 URL 验证，返回 challenge 原值。"""
    return {"challenge": challenge}


async def send_card(chat_id: str, card: dict[str, Any]) -> str:
    """发送交互卡片到飞书会话（待实现）。"""
    raise NotImplementedError("飞书卡片发送待接入 lark-cli / 飞书 Open API")
