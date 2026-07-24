# 竞品分析后端（预研骨架）

FastAPI 最小骨架，把 Aily 阶段验证通过的 4 个场景（battle_card / pricing / weekly / discovery）工程化。提示词**复用** `../validation/prompts/*.txt`（唯一真相），不重复存放。

## 运行方式一：飞书长连接 Bot（**当前 MVP 主通道**，免公网）

基于 [lark-coding-agent-bridge](https://github.com/zarazhangrui/lark-coding-agent-bridge) 思路，用代码声明式创建飞书接入层，替代此前卡在 UI 的 Aily 平台自定义智能体。

```bash
cd backend
python -m venv .venv
.venv\Scripts\activate        # Windows
pip install -r requirements.txt
cp .env.example .env          # 填 OPENAI_API_KEY + FEISHU_APP_ID/SECRET
python run_bot.py             # 启动 WebSocket 长连接，控制台出现 wss:// 即成功
```

- 对话：群里 @机器人 用 `/battle`、`/price`、`/weekly`、`/discover` 触发；私聊直接发。
- 声明式配置：`bot_config.json`（人设名/默认场景/群白名单/命令别名）。
- 详见 `../08-飞书自建Bot接入方案（基于bridge思路）.md`。

## 运行方式二：FastAPI（后续企业级扩展）

```bash
uvicorn app.main:app --reload --port 8000
```

- `GET /health` → `{"status":"ok","scenes":[...]}`
- `POST /api/analyze` → `{"scene": "...", "result": "..."}`
- `POST /webhook/feishu` → 飞书 URL 验证（challenge）/ 事件接收占位（Webhook 模式用）

## 状态

- ✅ 已实现：`/health`、`/api/analyze`（真调 LLM）、`/webhook/feishu`（验证）、`sessions` 内存、`engine` 复用 prompts。
- ✅ **新增**：`app/bot.py` + `run_bot.py` 飞书 WebSocket 长连接 Bot，接分析引擎，群聊 @/私聊触发，声明式场景路由。
- ⬜ 未实现：卡片渲染（当前用 text 回复）、搜索/抓取、监控雷达、SQLite。

> 正式开发待 `06` §4.5 验收 Gate 全过（详见 `07-自建后端技术方案（预研骨架）.md`）。
