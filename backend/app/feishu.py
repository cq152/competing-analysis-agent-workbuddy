"""飞书集成占位：webhook URL 验证 + 卡片发送 stub。

待接入 lark-cli / 飞书 Open API 后实现真实收发与卡片渲染。
"""
from typing import Any


def verify_url(challenge: str) -> dict:
    """飞书事件订阅 URL 验证，返回 challenge 原值。"""
    return {"challenge": challenge}


async def send_card(chat_id: str, card: dict[str, Any]) -> str:
    """发送交互卡片到飞书会话（待实现）。"""
    raise NotImplementedError("飞书卡片发送待接入 lark-cli / 飞书 Open API")
