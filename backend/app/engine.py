"""分析引擎：加载 validation/prompts/*.txt（唯一真相）并调用 LLM。

提示词源文件只存在于 validation/，后端不重复存放，避免与 Aily 漂移。
"""
from pathlib import Path

from openai import OpenAI

from .config import settings

# validation/prompts 相对本文件的路径：backend/app/engine.py -> ../../../validation/prompts
PROMPTS_DIR = Path(__file__).resolve().parent.parent.parent / "validation" / "prompts"

SCENES = {
    "battle_card": "battle_card.txt",
    "pricing": "pricing.txt",
    "weekly": "weekly.txt",
    "discovery": "discovery.txt",
}


def list_scenes() -> list[str]:
    return list(SCENES.keys())


def _read(name: str) -> str:
    return (PROMPTS_DIR / name).read_text(encoding="utf-8")


def analyze(scene: str, query: str) -> str:
    """按场景加载提示词并调用 LLM，返回分析结果文本。"""
    if scene not in SCENES:
        raise ValueError(f"unknown scene: {scene}，可选: {list_scenes()}")

    system_prompt = _read("system.txt")
    scene_prompt = _read(SCENES[scene])

    client = OpenAI(api_key=settings.api_key, base_url=settings.base_url)
    resp = client.chat.completions.create(
        model=settings.model,
        temperature=0.4,
        messages=[
            {"role": "system", "content": system_prompt + "\n\n# 当前场景指令\n" + scene_prompt},
            {"role": "user", "content": query},
        ],
    )
    return resp.choices[0].message.content
