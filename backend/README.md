# 竞品分析后端（预研骨架）

FastAPI 最小骨架，把 Aily 阶段验证通过的 4 个场景（battle_card / pricing / weekly / discovery）工程化。提示词**复用** `../validation/prompts/*.txt`（唯一真相），不重复存放。

## 运行

```bash
cd backend
python -m venv .venv
.venv\Scripts\activate        # Windows
pip install -r requirements.txt
cp .env.example .env          # 填入 OPENAI_API_KEY（可用 DeepSeek/Moonshot 等兼容端点）
uvicorn app.main:app --reload --port 8000
```

## 接口

- `GET /health` → `{"status":"ok","scenes":[...]}`
- `POST /api/analyze` → `{"scene": "...", "result": "..."}`
  ```bash
  curl -X POST http://localhost:8000/api/analyze \
    -H "Content-Type: application/json" \
    -d '{"scene":"battle_card","query":"我做 AI 笔记产品，客户总拿 Notion 压我们，怎么回？","session_id":"demo"}'
  ```
- `POST /webhook/feishu` → 飞书 URL 验证（challenge）/ 事件接收占位

## 状态

- ✅ 已实现：`/health`、`/api/analyze`（真调 LLM）、`/webhook/feishu`（验证）、`sessions` 内存、`engine` 复用 prompts。
- ⬜ 未实现：飞书真实收发、卡片渲染、搜索/抓取、监控雷达、SQLite。

> 正式开发待 `06` §4.5 验收 Gate 全过（详见 `07-自建后端技术方案（预研骨架）.md`）。
