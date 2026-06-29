"""Crawler agent configuration — loaded from env / .env."""

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=(".env", "../.env"), extra="ignore")

    # Anthropic API key (used by anthropic SDK)
    anthropic_api_key: str = ""

    # Database (shared with pigugu-server)
    database_url: str = "postgresql+asyncpg://pigugu:pigugu@localhost:5432/pigugu"

    # Agent behaviour
    agent_model: str = "claude-haiku-4-5-20251001"  # low-cost, fast, ideal for daily cron
    agent_max_turns: int = 15


settings = Settings()
