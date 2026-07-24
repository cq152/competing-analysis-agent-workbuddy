"""飞书事件 Webhook 接收服务（方案 B：替代长连接，彻底规避「单连接僵尸路由」问题）。

为什么有这个文件：
    长连接(ws.Client)模式下，飞书同一应用只允许一个活跃连接；开发期反复「杀→起」会
    留下僵尸连接，事件路由到已死的实例，表现为「群里 @机器人 没反应」。本服务改用
    HTTP Webhook 回调——飞书主动 POST 到你的公网 URL，没有长连接，该限制彻底消失。

配合内网穿透（开发期免公网服务器）：
    cloudflared tunnel --url http://localhost:8000        # 免费免账号，给 https 临时域名
    # 或 ngrok http 8000

飞书后台配置（同一应用 cli_aae8fa8526b8dbb6）：
    事件订阅 → 接收方式选「Webhook 回调地址」→ 填 https://<隧道域名>/webhook/event
    （首次保存时飞书会发 url_verification，本服务自动回 challenge）

复用：本服务不重复实现业务逻辑，直接调 bot.py 的 LarkBot.handle_message（分析引擎 + 回复
逻辑全复用，提示词唯一真相仍是 validation/prompts/*.txt，不漂移）。

启动：
    cd backend
    uvicorn webhook_server:app --host 0.0.0.0 --port 8000
    # 或 python webhook_server.py
"""
import base64
import hashlib
import hmac
import json
import os
import types
from pathlib import Path

from dotenv import load_dotenv
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from app.bot import LarkBot

load_dotenv()

# 开发期可设 SKIP_SIGNATURE=1 跳过验签（仅本地隧道调试用，生产务必关闭）
SKIP_SIGNATURE = os.getenv("SKIP_SIGNATURE", "0") == "1"

app = FastAPI(title="竞品分析搭档 - 飞书 Webhook")
# 复用 bot.py 的分析引擎与回复逻辑（含单实例锁之外的全部能力）
bot = LarkBot()


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


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=int(os.getenv("WEBHOOK_PORT", "8000")))
