from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    database_url: str = "postgresql+psycopg://athena:athena@localhost:5433/athena"
    anthropic_api_key: str = ""
    sec_edgar_user_agent: str = ""
    voyage_api_key: str = ""


@lru_cache
def get_settings() -> Settings:
    return Settings()
