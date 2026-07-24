# 竞品分析后端（MVP）

把 Aily 阶段验证通过的 4 个场景（battle_card / pricing / weekly / discovery）工程化。提示词**复用** `../validation/prompts/*.txt`（唯一真相），不重复存放。

## 运行方式一：HTTP Webhook Bot（**当前 MVP 主通道**）

飞书事件通过 HTTP Webhook 回调接收，配合 ngrok 内网穿透免公网服务器。**已替代长连接**（WebSocket 模式下同一应用仅允许一个活跃连接，开发期反复重启会僵尸路由）。

```bash
cd backend
python -m venv .venv
.venv\Scripts\activate        # Windows
pip install -r requirements.txt
cp .env.example .env          # 填 OPENAI_API_KEY + FEISHU_APP_ID/SECRET
# 终端 1：启动 Webhook 服务
uvicorn webhook_server:app --host 0.0.0.0 --port 8011
# 终端 2：启动内网穿透
ngrok http 8011
# 将 ngrok 提供的 https://xxx.ngrok-free.dev/webhook/event 填入飞书后台事件订阅
```

- 统一 FastAPI 入口：`webhook_server.py`（飞书 Webhook + 分析 API + 健康检查）
- 对话：群里 @机器人 用 `/battle`、`/price`、`/weekly`、`/discover` 触发；私聊直接发。
- 声明式配置：`bot_config.json`（人设名/默认场景/群白名单/命令别名）。
- 详见 `../08-飞书自建Bot接入方案（基于bridge思路）.md`。

## 运行方式二：WebSocket 长连接 Bot（备选，免公网但不稳定）

```bash
python run_bot.py             # 启动 WebSocket 长连接，控制台出现 wss:// 即成功
```

> ⚠️ 开发期反复重启会触发飞书「同应用仅一个活跃连接」限制，旧连接僵尸导致消息路由丢失。**推荐用方式一。**

## API 路由

| 路由 | 方法 | 说明 |
|------|------|------|
| `/webhook/event` | POST | 飞书事件接收（url_verification + im.message.receive_v1） |
| `/api/analyze` | POST | 外部系统调用分析引擎 `{"scene":"battle_card","query":"..."}` |
| `/health` | GET | 健康检查，含可用场景列表 |
| `/healthz` | GET | 健康检查（精简版） |

## 状态

- ✅ 已实现：`/webhook/event`（全链路通）、`/api/analyze`（真调 LLM）、`/health`、`app/bot.py`（消息处理+场景路由）、`app/engine.py`（复用 prompts）
- ✅ Webhook 全链路验证通过（接收→解析→场景路由→调 DeepSeek→回复到群）
- ⬜ 未实现：卡片渲染（当前用 text 回复）、搜索/抓取、监控雷达、SQLite

## 废弃文件

以下文件为预研阶段产物，已被 `webhook_server.py` 取代，仅保留历史参考：
- `app/main.py`（预研骨架 FastAPI，`/webhook/feishu` 为死占位）
- `app/feishu.py`（verify_url / send_card stub）
- `app/sessions.py`（仅被 app/main.py 引用）

> 正式开发待 `06` §4.5 验收 Gate 全过（详见 `07-自建后端技术方案（预研骨架）.md`）。
