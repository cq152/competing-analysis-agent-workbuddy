"""[已废弃] FastAPI 预研骨架 —— 被 `webhook_server.py` 取代。

本文件是 2026-07-24 之前的预研阶段产物，用于验证 FastAPI + engine 可行性。
当前所有功能已迁移到项目根目录的 `webhook_server.py`（统一 FastAPI 入口）：

  - GET  /health       → webhook_server.py (新增，含 scenes 列表)
  - POST /api/analyze  → webhook_server.py (新增，复用 engine)
  - POST /webhook/feishu → 已删除（占位路由，无实际消息处理能力；由 /webhook/event 取代）

保留本文件仅为历史参考，所有新开发请走 `webhook_server.py`。
相关废弃文件：app/feishu.py（stub）、app/sessions.py（仅本文件引用）。
"""
# 以下代码已不再使用，保留仅供历史参考。
from fastapi import FastAPI, Request
from pydantic import BaseModel

from . import engine, sessions

app = FastAPI(title="竞品分析后端(预研骨架)", version="0.0.1")


class AnalyzeReq(BaseModel):
    scene: str
    query: str
    session_id: str | None = None


@app.get("/health")
def health():
    return {"status": "ok", "scenes": engine.list_scenes()}


@app.post("/api/analyze")
def api_analyze(req: AnalyzeReq):
    result = engine.analyze(req.scene, req.query)
    if req.session_id:
        sessions.set_current(req.session_id, req.scene, req.query)
    return {"scene": req.scene, "result": result}


@app.post("/webhook/feishu")
async def feishu_webhook(req: Request):
    """[已废弃] 无实际消息处理能力，仅做 URL 验证。请使用 webhook_server.py 的 /webhook/event。"""
    body = await req.json()
    if body.get("type") == "url_verification":
        from .feishu import verify_url
        return verify_url(body.get("challenge", ""))
    return {"ok": True, "warning": "此路由已废弃，请使用 webhook_server.py 的 /webhook/event"}
