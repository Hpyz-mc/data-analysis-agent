from functools import lru_cache
from typing import Literal

from pydantic import Field, ValidationError
from pydantic_settings import BaseSettings, SettingsConfigDict


class ConfigurationError(RuntimeError):
    """Raised when required application settings are missing."""


class Settings(BaseSettings):
    llm_api_key: str
    llm_api_id: str = "deepseek-flash"
    llm_base_url: str = "https://api.deepseek.com"
    llm_thinking: Literal["enabled", "disabled"] = "disabled"
    llm_timeout_seconds: float = Field(default=60, ge=5, le=180)
    llm_request_deadline_seconds: float = Field(default=180, ge=10, le=600)
    llm_network_retries: int = Field(default=1, ge=0, le=2)
    llm_max_calls: int = Field(default=6, ge=2, le=12)
    llm_max_output_tokens: int = Field(default=1800, ge=512, le=8192)
    llm_prompt_token_budget: int = Field(default=24000, ge=4000, le=100000)
    llm_request_token_budget: int = Field(default=50000, ge=5000, le=300000)
    database_url: str
    session_database_url: str | None = None

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )


@lru_cache
def get_settings() -> Settings:
    try:
        return Settings()  # type: ignore[call-arg]
    except ValidationError as exc:
        raise ConfigurationError(
            "配置不完整，请检查 .env 中的 LLM_API_KEY、"
            "LLM_API_ID、LLM_BASE_URL 和 DATABASE_URL。"
        ) from exc
