#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
竞品分析 Agent · 本地验证脚手架
================================
把 03 系统提示词 + 05 场景提示词跑起来，在本地验证分析质量，不需要飞书。

用法：
  python run.py --list                列出可用场景
  python run.py -s battle_card --dry-run   只组装并打印提示词，不调 API
  python run.py -s battle_card --case      用 cases.json 的默认问题调 LLM
  python run.py -s pricing -q "自定义问题"  自定义问题调 LLM

环境变量（放 .env，见 .env.example）：
  OPENAI_API_KEY   必填
  OPENAI_BASE_URL  可选，OpenAI 兼容端点（DeepSeek/Moonshot/通义/智谱 等）
  OPENAI_MODEL     模型名，默认 gpt-4o-mini
"""
import os
import sys
import json
import argparse
import glob

HERE = os.path.dirname(os.path.abspath(__file__))
PROMPTS_DIR = os.path.join(HERE, "prompts")
CASES_FILE = os.path.join(HERE, "cases.json")

# 自动加载 .env（不强制依赖 python-dotenv）
try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass


def load_prompt(name):
    path = os.path.join(PROMPTS_DIR, name)
    if not os.path.exists(path):
        sys.exit(f"[错误] 提示词文件不存在: {path}")
    with open(path, encoding="utf-8") as f:
        return f.read().strip()


def list_scenarios():
    files = glob.glob(os.path.join(PROMPTS_DIR, "*.txt"))
    return sorted(os.path.basename(p)[:-4] for p in files if os.path.basename(p) != "system.txt")


def load_cases():
    with open(CASES_FILE, encoding="utf-8") as f:
        return json.load(f)


def build_messages(scenario, user_query):
    system = load_prompt("system.txt")
    scenario_prompt = load_prompt(scenario + ".txt")
    system_full = system + "\n\n---\n\n# 当前场景指令\n" + scenario_prompt
    return [
        {"role": "system", "content": system_full},
        {"role": "user", "content": user_query},
    ]


def call_llm(messages):
    try:
        from openai import OpenAI
    except ImportError:
        sys.exit("[错误] 未安装 openai 库，请先: pip install -r requirements.txt")
    api_key = os.environ.get("OPENAI_API_KEY")
    if not api_key:
        sys.exit("[错误] 缺少 OPENAI_API_KEY，请在 .env 中配置（参考 .env.example）。")
    base_url = os.environ.get("OPENAI_BASE_URL")  # OpenAI 兼容端点，可为空
    model = os.environ.get("OPENAI_MODEL", "gpt-4o-mini")
    client = OpenAI(api_key=api_key, base_url=base_url) if base_url else OpenAI(api_key=api_key)
    resp = client.chat.completions.create(model=model, messages=messages, temperature=0.7)
    return resp.choices[0].message.content


def main():
    ap = argparse.ArgumentParser(description="竞品分析 Agent 本地验证脚手架")
    ap.add_argument("--list", action="store_true", help="列出可用场景")
    ap.add_argument("--scenario", "-s", help="场景名 (battle_card/pricing/weekly/discovery)")
    ap.add_argument("--query", "-q", help="自定义用户问题（覆盖 cases.json 默认）")
    ap.add_argument("--case", "-c", action="store_true", help="用 cases.json 中该场景的默认问题")
    ap.add_argument("--dry-run", action="store_true", help="只组装并打印提示词，不调用 API")
    args = ap.parse_args()

    if args.list:
        print("可用场景：")
        for s in list_scenarios():
            print("  -", s)
        return

    if not args.scenario:
        ap.error("请指定 --scenario (-s)，或用 --list 查看可用场景")

    cases = load_cases()
    if args.query:
        user_query = args.query
    else:
        user_query = cases.get(args.scenario, "")
        if not user_query and not args.dry_run:
            sys.exit(f"[错误] cases.json 没有场景 '{args.scenario}' 的默认问题，请用 --query 指定")

    messages = build_messages(args.scenario, user_query)

    if args.dry_run:
        print("=" * 64)
        print("SYSTEM（含场景指令）:")
        print("=" * 64)
        print(messages[0]["content"])
        print("\n" + "=" * 64)
        print("USER:")
        print("=" * 64)
        print(user_query)
        return

    print("⏳ 调用 LLM ...\n")
    out = call_llm(messages)
    print(out)


if __name__ == "__main__":
    main()
