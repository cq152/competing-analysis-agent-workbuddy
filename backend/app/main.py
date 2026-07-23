"""FastAPI 入口：健康检查、飞书 webhook、分析接口。"""
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
    body = await req.json()
    # 飞书事件订阅 URL 验证
    if body.get("type") == "url_verification":
        from .feishu import verify_url
        return verify_url(body.get("challenge", ""))
    # TODO: 解析 im.message.receive_v1 → 意图识别 → 调 engine → 卡片回复
    return {"ok": True}
