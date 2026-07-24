r"""启动飞书长连接 Bot 的入口。

用法：
    cd backend
    cp .env.example .env   # 填 FEISHU_APP_ID / FEISHU_APP_SECRET / OPENAI_API_KEY
    .venv\Scripts\activate
    python run_bot.py
"""
from app.bot import main

if __name__ == "__main__":
    main()
