"""飞书群 E2E 测试：模拟飞书 webhook 推送，bot 真调飞书 API 回复到群。

用法：
  cd backend && python test_feishu_e2e.py

原理：
  不通过 ngrok（走公网多一跳），直接 POST 到本地 localhost:8011/webhook/event，
  模拟飞书真实请求体（mentions 为 dict 而非 SimpleNamespace）。
  bot 收到后会调飞书 Open API 把消息发到测试群。
"""

import json
import time
import urllib.request

WEBHOOK_URL = "http://localhost:8011/webhook/event"
CHAT_ID = "oc_05ba823786bae93fc10b323fbb41c9e4"  # 真实测试群

# 测试用例定义：(描述, content_text, 期望路由)
# content_text 即飞书消息 JSON 中 content.text 的值
TESTS = [
    # ===== 1. 帮助 =====
    ("帮助（@ 有空格）", "@竞品分析助手-workbuddy 帮助", "help"),
    # ===== 2. 场景命令（自然语言） =====
    ("周报", "@竞品分析助手-workbuddy 周报", "weekly"),
    ("竞品发现", "@竞品分析助手-workbuddy 发现竞品 协同办公", "discovery"),
    ("定价分析", "@竞品分析助手-workbuddy 定价 飞书", "pricing"),
    ("销售应对", "@竞品分析助手-workbuddy 应对 飞书", "battle_card"),
    ("默认场景（无命令前缀）", "@竞品分析助手-workbuddy 飞书最近有什么新功能", "battle_card"),
    # ===== 3. 多竞品对比 =====
    ("对比 飞书 钉钉 企业微信", "@竞品分析助手-workbuddy 对比 飞书 钉钉 企业微信", "compare"),
    # ===== 4. 监控 =====
    ("监控列表", "@竞品分析助手-workbuddy 监控列表", "monitor_list"),
    ("添加监控", "@竞品分析助手-workbuddy 监控 飞书", "monitor_add"),
    ("删除监控", "@竞品分析助手-workbuddy 删除监控 1", "monitor_remove"),
    # ===== 5. 边界情况 =====
    ("@ 无空格 + 帮助", "@竞品分析助手-workbuddy帮助", "help"),
]


def make_body(chat_id: str, content_text: str) -> dict:
    """构造飞书 webhook 事件体（mentions 用 dict 模拟真实格式）。"""
    return {
        "header": {"event_type": "im.message.receive_v1", "event_id": f"e2e_{int(time.time()*1000)}"},
        "event": {
            "message": {
                "chat_id": chat_id,
                "chat_type": "group",
                "message_type": "text",
                "message_id": f"msg_{int(time.time()*1000)}",
                "mentions": [{"key": "@竞品分析助手-workbuddy"}],
                "content": json.dumps({"text": content_text}, ensure_ascii=False),
            }
        },
    }


def send_one(desc: str, content_text: str, expected_route: str):
    body = make_body(CHAT_ID, content_text)
    data = json.dumps(body, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(WEBHOOK_URL, data=data, headers={"Content-Type": "application/json"})
    try:
        resp = urllib.request.urlopen(req, timeout=30)
        result = json.loads(resp.read().decode())
        status = "✅" if result.get("code") == 0 else "❌"
        print(f"  {status} {desc}")
        print(f"      期望路由: {expected_route} | 响应: {result}")
    except Exception as e:
        print(f"  ❌ {desc} → 请求失败: {e}")
    time.sleep(2)  # 避免限流


def main():
    print(f"飞书 E2E 测试 — 测试群: {CHAT_ID}")
    print(f"共 {len(TESTS)} 个用例，每个间隔 2s\n")

    for desc, content_text, expected_route in TESTS:
        send_one(desc, content_text, expected_route)

    print("\n全部用例已发送。请在飞书群查看回复，并用以下命令检查日志：")
    print("  tail -30 backend/logs/webhook.log")


if __name__ == "__main__":
    main()
