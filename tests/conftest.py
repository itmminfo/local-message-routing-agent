from collections.abc import Iterator

import pytest
from app.config import get_settings


@pytest.fixture(autouse=True)
def _reset_settings_cache() -> Iterator[None]:
    # get_settings() is lru_cached. Tests that monkeypatch env (e.g. APP_NAME /
    # API_PREFIX) and rely on call-time get_settings() reads must not leak a cached
    # Settings into later tests, so the cache is cleared around every test.
    # (MESSAGE_MAX_LENGTH is frozen at import in app.schemas.messages and is not
    # affected by cache_clear; changing it mid-suite would require reimporting it.)
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()
