"""竞品分析 Bot · 统一 FastAPI 入口（当前 MVP 主通道）。

整合三项职责到一个 FastAPI 应用：
  1. 飞书 Webhook 接收（POST /webhook/event）—— 替代长连接，彻底规避「单连接僵尸路由」
  2. 分析 API（POST /api/analyze）—— 供外部系统调用，复用验证通过的提示词引擎
  3. 健康检查（GET /health、GET /healthz）—— 两个路径等价

为什么有 webhook_server 替代长连接：
    长连接(ws.Client)模式下，飞书同一应用只允许一个活跃连接；开发期反复「杀→起」会
    留下僵尸连接，事件路由到已死的实例，表现为「群里 @机器人 没反应」。本服务改用
    HTTP Webhook 回调——飞书主动 POST 到你的公网 URL，没有长连接，该限制彻底消失。

配合内网穿透（开发期免公网服务器）：
    ngrok http 8011

飞书后台配置（同一应用 cli_aae8fa8526b8dbb6）：
    事件订阅 → 接收方式选「Webhook 回调地址」→ 填 https://<隧道域名>/webhook/event
    （首次保存时飞书会发 url_verification，本服务自动回 challenge）

启动：
    cd backend
    uvicorn webhook_server:app --host 0.0.0.0 --port 8011

提示词唯一真相：validation/prompts/*.txt（不重复存放，守 §10 纪律）。

历史说明：
    app/main.py 是本文件创建前的预研骨架（含占位的 /webhook/feishu），已废弃；
    run_bot.py 是 WebSocket 长连接入口，因僵尸连接问题降级为备选。
"""
import asyncio
import base64
import hashlib
import hmac
import json
import os
import types
from contextlib import asynccontextmanager
from pathlib import Path

from dotenv import load_dotenv
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from app.bot import LarkBot
from app.config import settings
from app.engine import analyze as engine_analyze, list_scenes
from app.logger import log

load_dotenv()

# 开发期可设 SKIP_SIGNATURE=1 跳过验签（仅本地隧道调试用，生产务必关闭）
SKIP_SIGNATURE = os.getenv("SKIP_SIGNATURE", "0") == "1"


@asynccontextmanager
async def lifespan(app: FastAPI):
    """应用生命周期：启动监控雷达后台轮询（v3 核心）。"""
    if settings.monitor_enabled:
        try:
            from app import monitor as monitor_mod

            svc = monitor_mod.init_monitor_service(bot)
            task = asyncio.create_task(_scheduler_loop(svc))
            log.info("监控雷达后台轮询已启动")
        except Exception as e:
            log.error(f"监控服务启动失败（不影响 Webhook 收消息）: {e}")
            task = None
    else:
        task = None
    try:
        yield
    finally:
        if task:
            task.cancel()


async def _scheduler_loop(svc) -> None:
    """每 monitor_interval_minutes 分钟轮询一次所有监控项。"""
    while True:
        try:
            await svc.run_all()
        except Exception as e:  # noqa: BLE001
            log.error(f"监控轮询异常: {e}")
        await asyncio.sleep(settings.monitor_interval_minutes * 60)


app = FastAPI(title="竞品分析搭档", version="0.2.0", lifespan=lifespan)
# 复用 bot.py 的分析引擎与回复逻辑（含单实例锁之外的全部能力）
bot = LarkBot()


class AnalyzeReq(BaseModel):
    scene: str
    query: str


def _verify_signature(headers, body_str: str) -> bool:
    """飞书回调验签。

    新版(X-Lark-Signature)：HMAC-SHA256(app_secret, timestamp+nonce+body) 再 base64
    旧版(X-Feishu-Signature)：同上但 key 用 verification_token
    两者都支持；没带签名头时开发期放行。
    """
    if SKIP_SIGNATURE:
        return True
    secret = os.getenv("FEISHU_APP_SECRET", "")
    vt = os.getenv("FEISHU_VERIFICATION_TOKEN", "")
    sig = headers.get("x-lark-signature") or headers.get("x-feishu-signature")
    ts = (
        headers.get("x-lark-request-timestamp")
        or headers.get("x-feishu-request-timestamp")
        or headers.get("timestamp")
    )
    nonce = (
        headers.get("x-lark-request-nonce")
        or headers.get("x-feishu-request-nonce")
        or headers.get("nonce")
    )
    if not sig or not ts or not nonce:
        return True  # 没带签名头：开发期放行（生产务必开启验签）
    for key in (secret, vt):
        if not key:
            continue
        expected = base64.b64encode(
            hmac.new(key.encode(), (ts + nonce + body_str).encode(), hashlib.sha256).digest()
        ).decode()
        if hmac.compare_digest(expected, sig):
            return True
    return False


def _to_event(body: dict):
    """把飞书事件 JSON 包成 LarkBot.handle_message 期望的结构（SDK 无 from_dict）。"""
    msg_raw = (body.get("event") or {}).get("message", {})
    msg = types.SimpleNamespace(
        chat_type=msg_raw.get("chat_type"),
        chat_id=msg_raw.get("chat_id"),
        message_id=msg_raw.get("message_id"),
        message_type=msg_raw.get("message_type"),
        content=msg_raw.get("content"),
        mentions=msg_raw.get("mentions"),
    )
    return types.SimpleNamespace(event=types.SimpleNamespace(message=msg))


@app.post("/webhook/event")
async def feishu_event(request: Request):
    raw = await request.body()
    body_str = raw.decode("utf-8", errors="ignore")
    try:
        body = json.loads(body_str)
    except json.JSONDecodeError:
        return JSONResponse({"code": 1, "msg": "invalid json"}, status_code=400)

    # 1) URL 验证（飞书首次保存 Webhook 地址时回调，需原样返回 challenge）
    if body.get("type") == "url_verification" and "challenge" in body:
        return JSONResponse({"challenge": body["challenge"]})

    # 2) 验签
    if not _verify_signature(request.headers, body_str):
        return JSONResponse({"code": 19021, "msg": "invalid signature"}, status_code=401)

    # 3) 仅处理消息接收事件，其余（进群等）忽略
    event_type = (body.get("header") or {}).get("event_type")
    if event_type != "im.message.receive_v1":
        return JSONResponse({"code": 0, "msg": f"ignored {event_type}"})

    # 4) 交给复用自 bot.py 的处理逻辑（含场景路由、群白名单、回复）
    try:
        bot.handle_message(_to_event(body))
    except Exception as e:  # noqa: BLE001
        print(f"[webhook] handle failed: {e}")
        return JSONResponse({"code": 1, "msg": str(e)}, status_code=200)
    return JSONResponse({"code": 0, "msg": "success"})


@app.get("/healthz")
async def healthz():
    return {"ok": True}


@app.get("/health")
def health():
    """健康检查，返回可用场景列表（与 /healthz 等价，多返回 scenes 信息）。"""
    return {"status": "ok", "scenes": list_scenes()}


@app.post("/api/analyze")
def api_analyze(req: AnalyzeReq):
    """外部系统可调用的分析 API，复用验证通过的提示词引擎。"""
    result = engine_analyze(req.scene, req.query)
    return {"scene": req.scene, "result": result}


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=int(os.getenv("WEBHOOK_PORT", "8000")))
