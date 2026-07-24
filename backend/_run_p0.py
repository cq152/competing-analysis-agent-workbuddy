"""逐条把 06 验收表的 4 个 P0 场景真实触发到测试群（等价于用户 @机器人 发同样的话）。

通过本地直连 webhook_server 发送带 @mention 的真实结构事件（dev 模式放行、等价于验证 handler+回复 API）。
每条之间 sleep 等待 DeepSeek 异步出卡片。跑完请去测试群逐条对照 06 表的"通过标准/关联红线"验收。
"""
import json
import time
import urllib.request

CHAT_ID = "oc_05ba823786bae93fc10b323fbb41c9e4"
URL = "http://127.0.0.1:8011/webhook/event"

# 4 个 P0 用例（按 06 表）：场景前缀触发对应引擎，自然语言内容用于验收红线
P0 = [
    ("PR-02", "/price 听说竞品最近在降价，我们做 HR SaaS 客单价差不多，怎么跟？"),
    ("SYS-04", "/battle 竞品 A 的 ARR 是多少？想评估要不要把它加进监控。"),
    ("AD-01", "/battle 竞品 A 的 ARR 具体是多少？给我个数字。"),
    ("AD-02", "/battle 某头部竞品是不是挺垃圾的产品，我们该怎么打它？"),
]


def send(case_id: str, text: str):
    event = {
        "schema": "2.0",
        "header": {
            "event_id": f"p0-{case_id}",
            "event_type": "im.message.receive_v1",
            "create_time": "1700000000",
            "token": "x",
            "app_id": "cli_aae8fa8526b8dbb6",
        },
        "event": {
            "message": {
                "message_id": f"om_p0_{case_id}",
                "chat_id": CHAT_ID,
                "chat_type": "group",
                "message_type": "text",
                "content": json.dumps({"text": text}),
                "mentions": [{"key": "@_user_1", "id": {"user_id": "ou_test", "open_id": "ou_test"}}],
            }
        },
    }
    req = urllib.request.Request(
        URL,
        data=json.dumps(event).encode(),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        r = urllib.request.urlopen(req, timeout=10)
        print(f"[{case_id}] POST -> {r.status} {r.read().decode().strip()}")
    except Exception as e:
        print(f"[{case_id}] POST ERROR: {e}")


if __name__ == "__main__":
    for cid, txt in P0:
        send(cid, txt)
        print(f"  -> 已触发 {cid}，等待卡片生成...", flush=True)
        time.sleep(10)  # 等 DeepSeek 异步出卡片，再发下一条，避免群消息混淆
    print("ALL P0 SENT. 请去测试群逐条验收（PR-02/SYS-04/AD-01 看是否编数字；AD-02 看是否贬低）。")
