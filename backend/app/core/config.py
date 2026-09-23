"""Application settings loaded from environment variables and the `.env` file."""

from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    app_name: str = "CodeGraph AI"
    app_version: str = "0.1.0"
    environment: str = "development"
    log_level: str = "INFO"

    # Stored as a comma-separated string so it is easy to set in `.env`.
    cors_origins: str = "http://localhost:5173"

    @property
    def cors_origin_list(self) -> list[str]:
        return [origin.strip() for origin in self.cors_origins.split(",") if origin.strip()]


@lru_cache
def get_settings() -> Settings:
    """Return a cached Settings instance so `.env` is read only once."""
    return Settings()
