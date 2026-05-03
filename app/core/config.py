from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    database_url: str = "postgresql+asyncpg://pigugu:pigugu@localhost:5432/pigugu"
    redis_url: str = "redis://localhost:6379/0"

    jwt_secret_key: str = ""
    jwt_algorithm: str = "HS256"
    jwt_access_token_expire_minutes: int = 60
    jwt_refresh_token_expire_minutes: int = 60 * 24 * 30  # 30 days

    livekit_url: str = ""
    livekit_api_key: str = ""
    livekit_api_secret: str = ""

    anthropic_api_key: str = ""

    firebase_credentials_path: str = "./firebase-credentials.json"

    aws_region: str = "us-west-1"
    aws_access_key_id: str = ""
    aws_secret_access_key: str = ""
    aws_iot_endpoint: str = "agktqmnw53qnj-ats.iot.us-west-1.amazonaws.com"
    aws_iot_webhook_secret: str = ""

    # MQTT / Device Token (separate from user auth JWT)
    mqtt_jwt_secret_key: str = ""
    mqtt_jwt_algorithm: str = "HS256"
    mqtt_jwt_expire_minutes: int = 1440  # 24 hours
    mqtt_broker_uri: str = "mqtts://agktqmnw53qnj-ats.iot.us-west-1.amazonaws.com:8883"

    app_env: str = "development"
    log_level: str = "INFO"


settings = Settings()
