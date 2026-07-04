import pytest

from apps.api.config import Settings


def test_settings_read_from_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DATABASE_URL", "postgresql+psycopg://u:p@h:5/db")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    monkeypatch.setenv("SEC_EDGAR_USER_AGENT", "Test test@example.com")
    settings = Settings()
    assert settings.database_url == "postgresql+psycopg://u:p@h:5/db"
    assert settings.anthropic_api_key == "test-key"
    assert settings.sec_edgar_user_agent == "Test test@example.com"


def test_settings_have_local_defaults(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("DATABASE_URL", raising=False)
    settings = Settings(_env_file=None)
    assert settings.database_url.endswith("localhost:5433/athena")
