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
    last_push_at: Optional[str] = None  # 3.4 频率上限：上次推群时间


class MonitorStore:
    """SQLite 持久化监控项。"""

    def __init__(self, db_path: str = settings.db_path):
        self.db_path = db_path
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(db_path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._init_schema()                 # 建表（不建唯一索引）
        self._migrate_add_last_push_at()    # 3.4 迁移列
        self.dedupe_existing()              # 3.4 先去重（老 db 可能有重复）
        self._add_unique_index()            # 3.4 再建唯一索引（去重后安全）

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
                last_signature TEXT DEFAULT '',
                last_push_at TEXT
            )
            """
        )
        self._conn.commit()

    def _add_unique_index(self) -> None:
        """3.4 去重：唯一索引 (chat_id, competitor)，同群同竞品不可重复。
        独立方法，在 dedupe_existing 之后调用，避免老 db 重复数据冲突。"""
        self._conn.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_monitors_chat_comp "
            "ON monitors(chat_id, competitor)"
        )
        self._conn.commit()

    def _migrate_add_last_push_at(self) -> None:
        """3.4 迁移：旧表可能无 last_push_at 列，启动时补。"""
        cols = {r[1] for r in self._conn.execute("PRAGMA table_info(monitors)")}
        if "last_push_at" not in cols:
            self._conn.execute("ALTER TABLE monitors ADD COLUMN last_push_at TEXT")
            self._conn.commit()

    def dedupe_existing(self) -> int:
        """3.4 启动清理：同 (chat_id, competitor) 保留最早一条，删除其余。

        返回删除条数。迁移老 db（3.4 前无唯一索引，可能已存重复项）。
        注意：唯一索引已在 _init_schema 建好，但老 db 可能先有重复再建索引——
        这里扫描时使用子查询绕过唯一索引限制（不依赖唯一索引去重，直接 SQL 删）。
        """
        # 找所有重复组中要保留的 id（最早一条）
        rows = self._conn.execute(
            "SELECT chat_id, competitor, MIN(id) AS keep_id "
            "FROM monitors GROUP BY chat_id, competitor HAVING COUNT(*) > 1"
        ).fetchall()
        if not rows:
            return 0
        deleted = 0
        for r in rows:
            # 删掉非最早的同群同竞品记录
            cur = self._conn.execute(
                "DELETE FROM monitors WHERE chat_id=? AND competitor=? AND id != ?",
                (r["chat_id"], r["competitor"], r["keep_id"]),
            )
            deleted += cur.rowcount
        if deleted:
            self._conn.commit()
            log.info(f"监控去重清理：删除 {deleted} 条重复项")
        return deleted

    def add(self, competitor: str, chat_id: str, scene: str = "discovery") -> tuple[int, bool]:
        """添加监控项。3.4 去重：同 (chat_id, competitor) 已存在则返回已有 ID + created=False。

        Returns:
            (id, created) —— created=False 表示已存在未新增。
        """
        existing = self._conn.execute(
            "SELECT id FROM monitors WHERE chat_id=? AND competitor=?",
            (chat_id, competitor),
        ).fetchone()
        if existing:
            return existing["id"], False
        cur = self._conn.execute(
            "INSERT INTO monitors (competitor, scene, chat_id, created_at) VALUES (?,?,?,?)",
            (competitor, scene, chat_id, _now()),
        )
        self._conn.commit()
        return cur.lastrowid, True

    def list(self, chat_id: str) -> list[dict]:
        rows = self._conn.execute(
            "SELECT id, competitor, scene, chat_id, created_at, last_check_at, last_signature "
            "FROM monitors WHERE chat_id=?",
            (chat_id,),
        ).fetchall()
        return [dict(r) for r in rows]

    def get_all(self) -> list[Monitor]:
        rows = self._conn.execute(
            "SELECT id, competitor, scene, chat_id, created_at, last_check_at, last_signature, last_push_at "
            "FROM monitors"
        ).fetchall()
        return [Monitor(**dict(r)) for r in rows]

    def remove(self, mid: int, chat_id: str) -> bool:
        cur = self._conn.execute(
            "DELETE FROM monitors WHERE id=? AND chat_id=?", (mid, chat_id)
        )
        self._conn.commit()
        return cur.rowcount > 0

    def remove_by_competitor(self, chat_id: str, competitor: str) -> bool:
        """P1：按竞品名删除监控项（供卡片按钮 quick_unmonitor 使用）。"""
        cur = self._conn.execute(
            "DELETE FROM monitors WHERE chat_id=? AND competitor=?", (chat_id, competitor)
        )
        self._conn.commit()
        return cur.rowcount > 0

    def update_check(self, mid: int, signature: str) -> None:
        self._conn.execute(
            "UPDATE monitors SET last_check_at=?, last_signature=? WHERE id=?",
            (_now(), signature, mid),
        )
        self._conn.commit()

    def update_push(self, mid: int) -> None:
        """3.4 频率上限：记录推送时间。"""
        self._conn.execute(
            "UPDATE monitors SET last_push_at=? WHERE id=?", (_now(), mid)
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
        """对单个监控项执行一次检查。有变化则推群，返回是否有变化。

        3.4 体验修复：
        - 首次静默期：last_signature 为空时只写 baseline，不推群（避免新监控项立即灌一条"全部新增"）。
        - 频率上限：距上次推送 < 24h 则跳过（记 log，不推）。
        """
        from app.detector import detect_changes

        # Searcher.search 是同步网络调用，用 to_thread 避免阻塞事件循环
        results = await asyncio.to_thread(
            self.searcher.search, m.competitor, settings.search_max_results
        )
        items = [f"{r.title} ({r.domain})" for r in results]
        old_items = _deserialize(m.last_signature)

        # ① 首次静默期：空 baseline 只记录，不推
        if not old_items and not m.last_signature:
            self.store.update_check(m.id, _serialize(items))
            log.info(f"监控首次检查(静默期): {m.competitor} baseline 已记录，不推群")
            return False

        changes = detect_changes(old_items, items)
        if not changes.has_change:
            # 无变化也更新 last_check_at，保持活跃
            self.store.update_check(m.id, m.last_signature)
            return False

        # ② 频率上限：距上次推送 < 24h 则跳过
        if m.last_push_at:
            try:
                last = datetime.fromisoformat(m.last_push_at)
                elapsed_hours = (datetime.now(timezone.utc) - last).total_seconds() / 3600
                if elapsed_hours < 24:
                    log.info(
                        f"监控频率限制: {m.competitor} 距上次推送 {elapsed_hours:.1f}h < 24h，跳过"
                    )
                    self.store.update_check(m.id, _serialize(items))
                    return False
            except (ValueError, TypeError):
                pass  # 时间解析失败则正常推送

        # P1：推卡片（取代纯文本）；检查返回值避免日志误报
        push_ok = self._push_alert_card(m.competitor, m.chat_id, changes)
        self.store.update_push(m.id)
        self.store.update_check(m.id, _serialize(items))
        if push_ok:
            log.info(f"监控告警已推送: {m.competitor} (chat={m.chat_id})")
        else:
            log.warning(f"监控告警推送失败: {m.competitor} (chat={m.chat_id})")
        return True

    def _push_alert_card(self, competitor: str, chat_id: str, changes) -> bool:
        """P1：用飞书卡片推送监控告警（取代纯文本 _format_change）。返回是否成功。"""
        from app.card_renderer import render_monitor_alert_card

        card = render_monitor_alert_card(
            competitor, changes.added or [], changes.removed or []
        )
        ok = self.bot.push_card(chat_id, card)
        if not ok:
            # 卡片推送失败回退纯文本（不应常见，但兜底）
            fallback = self._format_change_text(competitor, changes)
            ok = self.bot.push(chat_id, fallback)
        return ok

    @staticmethod
    def _format_change_text(competitor: str, changes) -> str:
        """卡片失败时的纯文本兜底（保留原 _format_change 逻辑）。"""
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
