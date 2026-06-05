import os
from collections.abc import Iterator
from contextlib import contextmanager

from app.config import Settings, get_settings

SETTING_ENV_KEYS = (
    "APP_NAME",
    "API_PREFIX",
    "OLLAMA_HOST",
    "OLLAMA_MODEL",
    "OLLAMA_NUM_PREDICT",
    "SMTP_HOST",
    "SMTP_PORT",
    "MAIL_FROM",
    "REQUEST_TIMEOUT_SECONDS",
    "MESSAGE_MAX_LENGTH",
    "SMTP_TIMEOUT_SECONDS",
    "MAX_CONCURRENT_ROUTE_REQUESTS",
    "MAX_REQUEST_BODY_BYTES",
)


@contextmanager
def clean_settings_environment() -> Iterator[None]:
    original_values = {key: os.environ.get(key) for key in SETTING_ENV_KEYS}
    try:
        for key in SETTING_ENV_KEYS:
            os.environ.pop(key, None)
        yield
    finally:
        for key, value in original_values.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


def test_settings_defaults_use_container_hosts() -> None:
    with clean_settings_environment():
        settings = Settings(_env_file=None)

    assert settings.APP_NAME == "Local Message Routing Agent"
    assert settings.API_PREFIX == "/api/v1"
    assert settings.OLLAMA_HOST == "http://ollama:11434"
    assert settings.OLLAMA_MODEL == "qwen2.5:3b"
    assert settings.OLLAMA_NUM_PREDICT == 512
    assert settings.SMTP_HOST == "mailpit"
    assert settings.SMTP_PORT == 1025
    assert settings.MAIL_FROM == "no-reply@local-message-routing-agent.test"
    assert settings.REQUEST_TIMEOUT_SECONDS == 120
    assert settings.MESSAGE_MAX_LENGTH == 5000
    assert settings.SMTP_TIMEOUT_SECONDS == 10
    assert settings.MAX_CONCURRENT_ROUTE_REQUESTS == 8
    assert settings.MAX_REQUEST_BODY_BYTES == 65536
    assert "localhost" not in settings.OLLAMA_HOST
    assert "localhost" not in settings.SMTP_HOST


def test_get_settings_reads_environment_once() -> None:
    with clean_settings_environment():
        get_settings.cache_clear()
        os.environ["APP_NAME"] = "Overridden App"

        settings = get_settings()

        assert settings.APP_NAME == "Overridden App"
        assert get_settings() is settings
        get_settings.cache_clear()
