"""[已废弃] 会话状态 —— 仅被 `app/main.py`（已废弃）引用。

本文件的会话管理功能仅被已废弃的 `app/main.py:/api/analyze` 使用。
当前 MVP 阶段无会话保持需求；若后续需要，应在 webhook_server.py 中重新实现。
"""
from typing import Optional

_sessions: dict[str, dict] = {}


def set_current(session_id: str, scene: str, query: str) -> None:
    _sessions[session_id] = {"scene": scene, "query": query}


def get_current(session_id: str) -> Optional[dict]:
    return _sessions.get(session_id)
