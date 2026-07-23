"""会话状态：内存字典版，对应 02 文档的 Session（生产换 Redis/SQLite）。"""
from typing import Optional

_sessions: dict[str, dict] = {}


def set_current(session_id: str, scene: str, query: str) -> None:
    _sessions[session_id] = {"scene": scene, "query": query}


def get_current(session_id: str) -> Optional[dict]:
    return _sessions.get(session_id)
