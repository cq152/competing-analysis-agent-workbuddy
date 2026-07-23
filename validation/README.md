# 本地验证脚手架（validation/）

在把提示词粘进飞书 Aily 之前，先在本地用真实 LLM 跑一遍，验证「分析质量够不够深」。
不依赖飞书账号，改提示词 → 跑 → 看输出 → 调，循环极快。

## 目录结构
```
validation/
├── prompts/            # 从 03/05 抽出的可版本化提示词（单一事实来源）
│   ├── system.txt      # 03 系统提示词（人设 + 红线 + 工作方法）
│   ├── battle_card.txt # 05 场景1：销售应对卡
│   ├── pricing.txt     # 05 场景2：定价参考
│   ├── weekly.txt      # 05 场景3：周报
│   └── discovery.txt   # 05 场景4：竞品发现
├── cases.json          # 各场景的默认测试问题
├── run.py              # CLI
├── requirements.txt
├── .env.example        # 配置模板（复制为 .env 填密钥）
└── README.md
```

## 快速开始
```bash
cd validation
python -m venv .venv && source .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -r requirements.txt
cp .env.example .env      # 填入 OPENAI_API_KEY（及可选 BASE_URL/MODEL）
```

## 用法
```bash
python run.py --list                       # 列出场景
python run.py -s battle_card --dry-run     # 只打印组装后的提示词，不花钱
python run.py -s battle_card --case        # 用 cases.json 默认问题调 LLM
python run.py -s pricing -q "自定义问题"    # 自定义问题调 LLM
```

## 验证方法
对照 `03` 的 10 项验证清单 + `05` 第 6 节调参锦囊：
1. 先 `--dry-run` 确认提示词组装正确、场景指令就位。
2. 再 `--case` 跑真实输出，看是否满足：有因果链 / 不编造 / 收尾有动作 / 不贬对手 / 周报≤3条 / 竞品发现含间接降维跨界。
3. 不过 → 改对应 `prompts/*.txt` → 再跑。改完提交 git，提示词演进全程可追溯。

## 接飞书 Aily 时
`prompts/system.txt` 整段 = Aily「人设/指令」框内容；
`prompts/battle_card.txt` 等 = 各「指令/快捷指令」内容（含 few-shot 示例）。
本地验证通过的版本，直接复制进 Aily 即可。
