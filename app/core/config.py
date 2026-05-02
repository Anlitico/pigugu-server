from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    database_url: str = "postgresql+asyncpg://pigugu:pigugu@localhost:5432/pigugu"
    redis_url: str = "redis://localhost:6379/0"

    jwt_secret_key: str = "change-me"
    jwt_algorithm: str = "HS256"
    jwt_access_token_expire_minutes: int = 60
    jwt_refresh_token_expire_minutes: int = 60 * 24 * 30  # 30 days

    livekit_url: str = ""
    livekit_api_key: str = ""
    livekit_api_secret: str = ""

    anthropic_api_key: str = ""

    firebase_credentials_path: str = "./firebase-credentials.json"

    app_env: str = "development"
    log_level: str = "INFO"


settings = Settings()
