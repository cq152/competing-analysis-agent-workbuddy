"""后端配置：优先 pydantic-settings 读取 .env。

设计要点：
- LLM 字段别名 OPENAI_* —— 兼容 W1 engine.py 对 `settings.api_key / base_url / model` 的引用，
  避免改动已通过 Gate 的引擎代码。
- 飞书字段别名 FEISHU_* —— 与 bot.py / webhook_server.py 现有 os.getenv 命名保持一致。
- W2 新增：搜索 / 抓取 / 中文数据源 / 日志存储 配置项。
"""
from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    # ===== LLM（兼容 W1 engine.py 的字段名）=====
    api_key: str = Field(default="", alias="OPENAI_API_KEY")
    base_url: str = Field(default="https://api.deepseek.com/v1", alias="OPENAI_BASE_URL")
    model: str = Field(default="deepseek-v4-pro", alias="OPENAI_MODEL")

    # ===== 飞书（与 bot.py / webhook_server.py 的 os.getenv 保持一致）=====
    feishu_app_id: str = Field(default="", alias="FEISHU_APP_ID")
    feishu_app_secret: str = Field(default="", alias="FEISHU_APP_SECRET")
    feishu_verification_token: str = Field(default="", alias="FEISHU_VERIFICATION_TOKEN")
    feishu_encrypt_key: str = Field(default="", alias="FEISHU_ENCRYPT_KEY")

    # ===== W2 新增：搜索配置 =====
    search_engine: str = "duckduckgo"          # 搜索引擎类型
    search_max_results: int = 5                # 每次搜索最大结果数

    # ===== W2 新增：抓取配置 =====
    fetch_timeout: int = 10                    # HTTP 抓取超时（秒）
    fetch_max_urls: int = 3                    # 每次最多抓取 URL 数
    fetch_min_word_count: int = 100            # 最小正文字数（低于此视为低质量）

    # ===== W2 新增：中文数据源配置 =====
    tianyancha_api_key: str = Field(default="", alias="TIANYANCHA_API_KEY")
    enable_third_party_api: bool = False       # 第三方数据 API 开关
    qimai_api_key: str = Field(default="", alias="QIMAI_API_KEY")

    # ===== W2 新增：日志与存储 =====
    log_level: str = "INFO"                    # 日志级别
    db_path: str = "data/bot.db"               # SQLite 路径

    # ===== W2 新增：监控雷达（v3 核心，提前到 W2）=====
    monitor_enabled: bool = True               # 监控轮询开关
    monitor_interval_minutes: int = 30         # 轮询间隔（分钟）


settings = Settings()
