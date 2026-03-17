from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    # LLM
    llm_primary_provider: str = "openrouter"

    # OpenRouter
    openrouter_api_key: str = ""
    openrouter_model: str = "anthropic/claude-opus-4.6"

    # YandexGPT
    yandex_api_key: str = ""
    yandex_folder_id: str = ""
    yandex_model: str = "yandexgpt-lite"

    # GigaChat
    gigachat_client_id: str = ""
    gigachat_client_secret: str = ""

    # App
    app_env: str = "development"
    log_level: str = "info"
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
