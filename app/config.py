from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    APP_NAME: str = "Local Message Routing Agent"
    API_PREFIX: str = "/api/v1"
    OLLAMA_HOST: str = "http://ollama:11434"
    OLLAMA_MODEL: str = "qwen2.5:3b"
    # Cap on tokens generated per Ollama call. The routing response is a single
    # short tool call, so 512 bounds runaway generation without truncating it.
    OLLAMA_NUM_PREDICT: int = 512
    # Optional Ollama `think` request field for thinking-capable models. None omits
    # the field, so the model's own default applies and existing profiles are
    # unchanged. False/True are sent explicitly to every chat call.
    OLLAMA_THINK: bool | None = None
    SMTP_HOST: str = "mailpit"
    SMTP_PORT: int = 1025
    MAIL_FROM: str = "no-reply@local-message-routing-agent.test"
    REQUEST_TIMEOUT_SECONDS: int = 120
    MESSAGE_MAX_LENGTH: int = 5000
    SMTP_TIMEOUT_SECONDS: int = 10
    # Max concurrent /messages/route requests admitted before returning 429. Sync
    # route handlers run in the anyio threadpool; this bounds how many of those
    # threads routing can occupy, leaving headroom for liveness/readiness probes.
    MAX_CONCURRENT_ROUTE_REQUESTS: int = 8
    # Reject requests whose declared body exceeds this many bytes before buffering,
    # so an oversized payload cannot be read into memory just to fail validation.
    MAX_REQUEST_BODY_BYTES: int = 65536
    REVIEW_DATABASE_PATH: str = "/data/review.sqlite3"
    REVIEW_ALLOWED_ORIGINS: list[str] = ["http://localhost:8000", "http://127.0.0.1:8000"]
    REVIEW_SESSION_TTL_SECONDS: int = 28800
    REVIEW_PREVIEW_TTL_SECONDS: int = 1800

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )


@lru_cache
def get_settings() -> Settings:
    return Settings()
