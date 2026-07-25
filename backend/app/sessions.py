"""会话持久化（W3.3 重建）—— SQLite 存储「主竞品记忆」+「分析历史」。

重建原因：W3.2 的「和 X 对比」主竞品记忆用进程内 dict（`bot._last_compare`），
进程重启即丢。本模块接手持久化，让主竞品记忆 + 分析历史跨重启不丢。

设计：沿用 monitor.py 的 SQLite 封装风格（check_same_thread=False 适配线程回调）。
避免循环导入：bot 侧懒加载 get_session_store()，本模块只依赖 app.config。
"""
from __future__ import annotations

import sqlite3
from pathlib import Path

from app.config import settings


def _now() -> str:
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class SessionStore:
    """SQLite 持久化：主竞品记忆 + 分析历史。

    表 sessions：每个 chat 一行，记录「上次主竞品」（供「和 X 对比」自动补位）。
    表 analysis_history：每次分析存一条摘要，供后续会话连续/回溯。
    """

    def __init__(self, db_path: str = settings.db_path):
        self.db_path = db_path
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(db_path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._init_schema()

    def _init_schema(self) -> None:
        self._conn.execute(
            """
            CREATE TABLE IF NOT EXISTS sessions (
                chat_id TEXT PRIMARY KEY,
                primary_competitor TEXT NOT NULL DEFAULT '',
                updated_at TEXT
            )
            """
        )
        self._conn.execute(
            """
            CREATE TABLE IF NOT EXISTS analysis_history (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                chat_id TEXT NOT NULL,
                scene TEXT NOT NULL,
                query TEXT NOT NULL,
                summary TEXT NOT NULL DEFAULT '',
                ts TEXT NOT NULL
            )
            """
        )
        self._conn.commit()

    # ===== 主竞品记忆（接替 W3.2 的 _last_compare in-memory）=====
    def get_primary(self, chat_id: str) -> str:
        """返回该群上次记录的主竞品；未记录则返回空串。"""
        row = self._conn.execute(
            "SELECT primary_competitor FROM sessions WHERE chat_id=?", (chat_id,)
        ).fetchone()
        return row["primary_competitor"] if row else ""

    def set_primary(self, chat_id: str, competitor: str) -> None:
        """记录/更新该群主竞品（INSERT OR REPLACE 兼容无 ON CONFLICT 的旧 SQLite）。"""
        self._conn.execute(
            "INSERT OR REPLACE INTO sessions (chat_id, primary_competitor, updated_at) "
            "VALUES (?,?,?)",
            (chat_id, competitor, _now()),
        )
        self._conn.commit()

    # ===== 分析历史 =====
    def save_analysis(
        self, chat_id: str, scene: str, query: str, summary: str = ""
    ) -> None:
        """追加一条分析历史（摘要）。"""
        self._conn.execute(
            "INSERT INTO analysis_history (chat_id, scene, query, summary, ts) "
            "VALUES (?,?,?,?,?)",
            (chat_id, scene, query, summary, _now()),
        )
        self._conn.commit()

    def get_history(self, chat_id: str, limit: int = 20) -> list[dict]:
        """按时间倒序取该群最近 limit 条分析历史。"""
        rows = self._conn.execute(
            "SELECT id, chat_id, scene, query, summary, ts FROM analysis_history "
            "WHERE chat_id=? ORDER BY id DESC LIMIT ?",
            (chat_id, limit),
        ).fetchall()
        return [dict(r) for r in rows]


# ===== 模块级单例（懒加载，首次调用时建；与 monitor 一致）=====
_svc: "SessionStore | None" = None


def get_session_store() -> SessionStore:
    """懒取全局 SessionStore 单例。"""
    global _svc
    if _svc is None:
        _svc = SessionStore()
    return _svc
