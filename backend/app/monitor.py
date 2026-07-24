"""
监控雷达（v3 最小版）—— SQLite 存储 + 定时搜索 + 变化检测 + 推群

v3 定位：这是 W2 的**核心差异化**（裸大模型做不到持久化/定时/diff）。
最小可用：加竞品 → 定时用 Searcher 搜索 → Detector 比变化 → 有变化 bot.push 推群。
LLM 分析不在本模块（属于按需 /analyze；监控只做"变化告警"）。

依赖：app.searcher / app.fetcher / app.detector / app.bot.LarkBot.push
为避免循环导入：monitor 不在顶层 import bot；bot 侧用懒加载 get_monitor_service。
"""
from __future__ import annotations

import asyncio
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from app.config import settings
from app.logger import log


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


@dataclass
class Monitor:
    id: int
    competitor: str
    scene: str
    chat_id: str
    created_at: str
    last_check_at: Optional[str] = None
    last_signature: str = ""


class MonitorStore:
    """SQLite 持久化监控项。"""

    def __init__(self, db_path: str = settings.db_path):
        self.db_path = db_path
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(db_path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._init_schema()

    def _init_schema(self) -> None:
        self._conn.execute(
            """
            CREATE TABLE IF NOT EXISTS monitors (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                competitor TEXT NOT NULL,
                scene TEXT NOT NULL DEFAULT 'discovery',
                chat_id TEXT NOT NULL,
                created_at TEXT NOT NULL,
                last_check_at TEXT,
                last_signature TEXT DEFAULT ''
            )
            """
        )
        self._conn.commit()

    def add(self, competitor: str, chat_id: str, scene: str = "discovery") -> int:
        cur = self._conn.execute(
            "INSERT INTO monitors (competitor, scene, chat_id, created_at) VALUES (?,?,?,?)",
            (competitor, scene, chat_id, _now()),
        )
        self._conn.commit()
        return cur.lastrowid

    def list(self, chat_id: str) -> list[dict]:
        rows = self._conn.execute(
            "SELECT id, competitor, scene, chat_id, created_at, last_check_at, last_signature "
            "FROM monitors WHERE chat_id=?",
            (chat_id,),
        ).fetchall()
        return [dict(r) for r in rows]

    def get_all(self) -> list[Monitor]:
        rows = self._conn.execute(
            "SELECT id, competitor, scene, chat_id, created_at, last_check_at, last_signature "
            "FROM monitors"
        ).fetchall()
        return [Monitor(**dict(r)) for r in rows]

    def remove(self, mid: int, chat_id: str) -> bool:
        cur = self._conn.execute(
            "DELETE FROM monitors WHERE id=? AND chat_id=?", (mid, chat_id)
        )
        self._conn.commit()
        return cur.rowcount > 0

    def update_check(self, mid: int, signature: str) -> None:
        self._conn.execute(
            "UPDATE monitors SET last_check_at=?, last_signature=? WHERE id=?",
            (_now(), signature, mid),
        )
        self._conn.commit()


class MonitorService:
    """监控业务逻辑：定时检查 + 变化推群。"""

    def __init__(self, store: MonitorStore, searcher, fetcher, bot):
        self.store = store
        self.searcher = searcher
        self.fetcher = fetcher
        self.bot = bot

    async def check_one(self, m: Monitor) -> bool:
        """对单个监控项执行一次检查。有变化则推群，返回是否有变化。"""
        from app.detector import detect_changes

        # Searcher.search 是同步网络调用，用 to_thread 避免阻塞事件循环
        results = await asyncio.to_thread(
            self.searcher.search, m.competitor, settings.search_max_results
        )
        items = [f"{r.title} ({r.domain})" for r in results]
        old_items = _deserialize(m.last_signature)
        changes = detect_changes(old_items, items)

        if changes.has_change:
            msg = self._format_change(m.competitor, changes)
            self.bot.push(m.chat_id, msg)
            self.store.update_check(m.id, _serialize(items))
            log.info(f"监控告警已推送: {m.competitor} (chat={m.chat_id})")
            return True

        # 无变化也更新 last_check_at，保持活跃
        self.store.update_check(m.id, m.last_signature)
        return False

    @staticmethod
    def _format_change(competitor: str, changes) -> str:
        lines = [f"📡 竞品雷达：{competitor} 有变化"]
        if changes.added:
            lines.append("➕ 新增：")
            lines.extend(f"  · {x}" for x in changes.added[:5])
        if changes.removed:
            lines.append("➖ 消失：")
            lines.extend(f"  · {x}" for x in changes.removed[:5])
        lines.append("\n（基于公开网络搜索，建议进一步核实）")
        return "\n".join(lines)

    async def run_all(self) -> int:
        """轮询所有监控项，返回有变化的项数。"""
        monitors = self.store.get_all()
        changed = 0
        for m in monitors:
            try:
                if await self.check_one(m):
                    changed += 1
            except Exception as e:  # noqa: BLE001
                log.error(f"监控检查失败 {m.competitor}: {e}")
        log.info(f"监控轮询完成：{len(monitors)} 项，{changed} 项有变化")
        return changed


def _serialize(items: list[str]) -> str:
    return "\n".join(items)


def _deserialize(sig: str) -> list[str]:
    return [x for x in sig.split("\n") if x] if sig else []


# ===== 模块级单例（由 webhook_server 启动时 init，bot 命令时懒取）=====
_svc: Optional[MonitorService] = None


def init_monitor_service(bot) -> MonitorService:
    """应用启动时调用，注入 bot 实例并建单例。"""
    global _svc
    if _svc is None:
        from app.searcher import Searcher
        from app.fetcher import Fetcher

        store = MonitorStore()
        _svc = MonitorService(store, Searcher(), Fetcher(), bot)
    return _svc


def get_monitor_service(bot=None) -> MonitorService:
    """懒取单例；若未 init 且未传 bot 则报错。"""
    global _svc
    if _svc is None:
        if bot is None:
            raise RuntimeError("monitor service 未初始化，请先 init_monitor_service(bot)")
        return init_monitor_service(bot)
    return _svc
