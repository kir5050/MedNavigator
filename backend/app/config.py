from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    # OpenRouter
    openrouter_api_key: str = ""
    openrouter_model: str = "anthropic/claude-opus-4.6"

    # CORS
    cors_origins: list[str] = ["http://localhost:3000", "http://localhost:5173", "https://app.navimedic.ru", "https://navimedic.ru"]

    # App
    app_env: str = "development"
    log_level: str = "info"
    # Voice input (PR B) feature flag — off by default
    voice_input_enabled: bool = False
    database_url: str = "sqlite+aiosqlite:///data/mednavigator.db"
    cache_dir: str = "./cache"

    # Auth
    admin_token: str = "change-me-in-production"

    # Telegram alerts
    telegram_bot_token: str = ""
    telegram_chat_id: str = ""

    # Session
    session_ttl_hours: int = 24

    # Cache TTL
    cache_ttl_clarification: int = 86400
    cache_ttl_triage: int = 604800

    model_config = {"env_file": ".env", "env_file_encoding": "utf-8"}


settings = Settings()
