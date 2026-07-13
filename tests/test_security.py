import pytest
from fastapi import HTTPException

from app.config import get_settings
from app.security import require_api_key


@pytest.fixture(autouse=True)
def _clear_settings_cache():
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.mark.asyncio
async def test_require_api_key_disabled_when_unset(monkeypatch):
    monkeypatch.delenv("API_KEY", raising=False)
    await require_api_key(authorization=None)  # must not raise


@pytest.mark.asyncio
async def test_require_api_key_rejects_missing_header(monkeypatch):
    monkeypatch.setenv("API_KEY", "s3cret-key-value")
    with pytest.raises(HTTPException) as exc_info:
        await require_api_key(authorization=None)
    assert exc_info.value.status_code == 401


@pytest.mark.asyncio
async def test_require_api_key_rejects_wrong_key(monkeypatch):
    monkeypatch.setenv("API_KEY", "s3cret-key-value")
    with pytest.raises(HTTPException) as exc_info:
        await require_api_key(authorization="Bearer wrong-key")
    assert exc_info.value.status_code == 401


@pytest.mark.asyncio
async def test_require_api_key_accepts_correct_key(monkeypatch):
    monkeypatch.setenv("API_KEY", "s3cret-key-value")
    await require_api_key(authorization="Bearer s3cret-key-value")  # must not raise
