"""后端配置：从 .env 读取 LLM 连接信息（OpenAI 兼容协议）。"""
import os
from dotenv import load_dotenv

load_dotenv()


class Settings:
    api_key: str = os.getenv("OPENAI_API_KEY", "")
    base_url: str = os.getenv("OPENAI_BASE_URL", "https://api.deepseek.com/v1")
    model: str = os.getenv("OPENAI_MODEL", "deepseek-chat")


settings = Settings()
