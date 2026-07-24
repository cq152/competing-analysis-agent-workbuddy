# 08 - 飞书自建 Bot 接入方案（基于 lark-coding-agent-bridge 思路）

> 本方案**替换**此前卡在 UI 的「Aily 平台自定义智能体」创建方式（`aily/` 目录保留作参考，但主推路径改为代码创建）。
> 思路来源：[lark-coding-agent-bridge](https://github.com/zarazhangrui/lark-coding-agent-bridge) —— 它用「扫码建 PersonalAgent + WebSocket 长连接 + 声明式配置」接飞书。我们借鉴其三件套本质，但用 Python 实现、接已验证的 `backend/` 分析引擎。

---

## 1. 为什么换方式

之前在 Aily 平台手动创建自定义智能体，卡在两点：
1. **企业是否开通「自定义智能体」权限不确定** —— 在「智能伙伴（小满）」页找不到人设/发布模式，根本原因是页面走错，但深层是企业权限不可控。
2. **UI 迷宫** —— 不同租户/版本界面差异大，文档给功能名不够，必须对照用户实际截图校准。

bridge 项目揭示了一条更稳的路：**用代码声明式创建飞书接入层**，而不是在 UI 点。它解决的就是"免企业审批 + 免公网 + 配置可控"。

## 2. 三件套思路（借鉴点）

| 借鉴自 bridge | 本方案落地 |
|---|---|
| PersonalAgent 扫码建（免企业审批） | 飞书开放平台**企业自建应用**（开发者后台创建，同样免「自定义智能体」权限；普通成员通常可建） |
| WebSocket 长连接收消息（免公网） | `lark-oapi` 的 `lark.ws.Client` 长连接，**不需要公网域名/内网穿透**，本地进程即可收事件 |
| `config.json` 声明式管理 | `backend/bot_config.json` 声明式管理人设名/默认场景/场景路由/群白名单 |

> 说明：标准长连接模式仅支持**企业自建应用**（不支持商店应用），与 bridge 的 PersonalAgent 具体应用类型略有不同，但「免公网 + 代码配置」的体验一致。PersonalAgent 是飞书 `lark-cli` 私有能力，Python 标准 SDK 不暴露，故用自建应用替代，更通用、文档更全。

## 3. 架构

```
飞书群/私聊 @机器人
      │  im.message.receive_v1 事件
      ▼
backend/run_bot.py  ── lark.ws.Client（WebSocket 长连接，免公网）
      │  do_p2_im_message_receive_v1
      ▼
app/bot.py  LarkBot
      │  解析 @mention、路由 /场景
      ▼
app/engine.py  analyze(scene, query)  ── 复用 validation/prompts/*.txt（唯一真相）
      │  调 DeepSeek（OpenAI 兼容）
      ▼
回复消息（reply 到原 message_id）
```

优势：**完全复用** `validation/` 已验证提示词 + `backend/app/engine.py` 引擎，飞书只是接入层，不重复任何业务逻辑。

## 4. 创建飞书应用的步骤（一次性）

1. 飞书开发者后台 `open.feishu.cn` → **创建企业自建应用**（取 App ID / App Secret）。
2. **权限**：应用功能 → 机器人 → 启用；权限管理加 `im:message`、`im:message:send_as_bot`、`im:chat` 等。
3. **事件订阅**：改成「**长连接**」接收方式（不是 Webhook URL）；添加事件 `接收消息 v2.0`（`im.message.receive_v1`）。
4. 把 App ID / App Secret 填进 `backend/.env` 的 `FEISHU_APP_ID` / `FEISHU_APP_SECRET`。
5. **发布应用**到飞书（企业内可见），把机器人加进测试群。

## 5. 运行

```bash
cd backend
cp .env.example .env        # 填 FEISHU_APP_ID / FEISHU_APP_SECRET / OPENAI_API_KEY
.venv\Scripts\activate
pip install -r requirements.txt   # 已含 lark-oapi
python run_bot.py
```

控制台出现 `connected to wss://...` 即长连接成功，在群里 @机器人 即可对话。

## 6. 对话用法（声明式场景路由）

| 输入 | 场景 |
|---|---|
| `/battle 客户总拿 X 压我们，怎么回？` | battle_card（销售应对卡） |
| `/price 竞品降价了，我们怎么跟？` | pricing |
| `/weekly 生成本周竞品周报` | weekly |
| `/discover 我们做 XX 产品，帮我发现竞品` | discovery |
| 不带前缀 | 默认 battle_card |

群聊需 @机器人；私聊直接发。配置见 `backend/bot_config.json`（人设名/默认场景/群白名单/命令别名均可改）。

## 7. 与现有路线的关系

- **替代** Aily 平台的「手动创建自定义智能体」（`aily/` 留作参考，不再主推）。
- **复用** `validation/prompts/*.txt`（提示词唯一真相）、`backend/app/engine.py`（分析引擎）。
- 原 FastAPI 路线（`backend/app/main.py` + `/webhook/feishu`）保留作后续「企业级 Webhook / 多租户」扩展；长连接 Bot 是当前 MVP 验证主通道。

## 8. 已知限制 / 下一步

- 回复用 text 消息；后续可升级 interactive 卡片（富文本/展开收起），复用原 `feishu.py` 卡片逻辑。
- 监控雷达 + 定时周报：长连接模式可加定时器进程触发 `engine.analyze` 后主动推群（待实现）。
- 多模态（截图/白皮书）：`im.message.receive_v1` 含 file/image 事件，后续接 engine 多模态。
- 企业权限：若企业禁止成员建自建应用，仍需管理员开通（这是唯一外部依赖）。

---

## 附录 A · 权限清单（scope，建应用时逐项核对）

| 类别 | 项目 | 说明 |
|---|---|---|
| 应用能力 | **机器人** | 应用功能 → 机器人 → 启用（不启用无法进群/收发消息） |
| 权限 scope | `im:message` | 读取用户发给机器人的消息 |
| 权限 scope | `im:message:send_as_bot` | 以机器人身份发送消息（回复必需） |
| 权限 scope | `im:message.group_at_msg` | 接收群聊中 @机器人 的消息（群场景必需） |
| 事件订阅 | `im.message.receive_v1`（接收消息 v2.0） | 订阅后才有消息事件；接收方式必须选「**长连接**」而非 Webhook URL |
| 发布 | 创建版本并发布（企业内可用） | 未发布时权限不生效；发布后把机器人拉进测试群 |

> 权限变更后需**重新发布版本**才生效；若群里 @机器人 无反应，先查「事件订阅是否为长连接」和「版本是否已发布」。

## 附录 B · 指定测试群配置（allowed_chats 白名单）

1. 先保持 `backend/bot_config.json` 的 `"allowed_chats": []`（空 = 不限群）启动 Bot。
2. 在目标测试群 @机器人 发任意消息，控制台会打印：
   ```
   [lark] chat_id=oc_xxxxxxxx chat_type=group
   ```
3. 把该 `oc_` 开头的 chat_id 填进白名单，并确认静默策略：
   ```json
   "allowed_chats": ["oc_xxxxxxxx"],
   "reply_if_unauthorized": false
   ```
   - `reply_if_unauthorized: false`（默认）：非白名单群**静默不回**，机器人存在感为零；
   - 设为 `true`：非白名单群回一句"本群未在白名单内"提示。
4. 重启 `run_bot.py` 生效。之后机器人只响应白名单群 + 私聊。
