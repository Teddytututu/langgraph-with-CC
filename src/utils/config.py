"""src/utils/config.py — 配置加载"""
import os
from dataclasses import dataclass
from dotenv import load_dotenv

load_dotenv()


@dataclass
class Config:
    api_key: str = ""
    model: str = "glm-5"
    max_retries: int = 3
    default_timeout: int = 60


def get_config() -> Config:
    return Config(
        api_key=os.getenv("ANTHROPIC_API_KEY", ""),
        model=os.getenv("DEFAULT_MODEL", "glm-5"),
        max_retries=int(os.getenv("MAX_RETRIES", "3")),
        default_timeout=int(os.getenv("DEFAULT_TIMEOUT_MINUTES", "60")),
    )
