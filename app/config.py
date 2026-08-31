"""配置 —— 环境变量驱动。"""
from __future__ import annotations

import os


class Settings:
    # P0 密钥契约（2026-08-31）：不烘焙任何凭据默认，生产必须显式注入 DATABASE_URL
    DATABASE_URL: str = os.environ.get("DATABASE_URL", "")
    OBJECT_STORE_URL: str = os.environ.get("OBJECT_STORE_URL", "")
    OPENCLAW_BIN: str = os.environ.get("OPENCLAW_BIN", "openclaw")
    OPENCLAW_SHARED_DIR: str = os.environ.get(
        "OPENCLAW_SHARED_DIR", os.path.expanduser("~/.openclaw/shared/messages"))
    # 底层算力接入（架构文档十二）：只要用户有 API 就接得进，key 走 env（P0 不烘焙）
    LLM_PROVIDER: str = os.environ.get("LLM_PROVIDER", "")       # anthropic|openai|openai_compatible|auto
    LLM_MODEL: str = os.environ.get("LLM_MODEL", "")             # 未设按 provider 默认
    LLM_BASE_URL: str = os.environ.get("LLM_BASE_URL", "")       # openai_compatible 端点（DeepSeek/vLLM/Groq 等）
    LLM_API_KEY: str = os.environ.get("LLM_API_KEY", "")         # 或 ANTHROPIC_API_KEY / OPENAI_API_KEY
    # 主动推进调度（Worker）
    HEARTBEAT_TIMEOUT_S: int = int(os.environ.get("HEARTBEAT_TIMEOUT_S", "300"))
    BLOCKER_TIMEOUT_S: int = int(os.environ.get("BLOCKER_TIMEOUT_S", "600"))
    # 权限
    DEFAULT_PERMISSION_LEVEL: str = os.environ.get("DEFAULT_PERMISSION_LEVEL", "L1")


settings = Settings()
