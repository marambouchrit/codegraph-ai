"""Application settings loaded from environment variables and the `.env` file."""

from functools import lru_cache
from pathlib import Path

from pydantic import SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    app_name: str = "CodeGraph AI"
    app_version: str = "0.1.0"
    environment: str = "development"
    log_level: str = "INFO"

    # Stored as a comma-separated string so it is easy to set in `.env`.
    cors_origins: str = "http://localhost:5173"

    # --- Repository ingestion ---
    # Imported projects are stored here (relative paths start from where the server is launched).
    workspace_dir: Path = Path("workspace")
    max_upload_size_mb: int = 50
    max_extracted_size_mb: int = 500
    max_archive_files: int = 20_000
    max_repository_size_mb: int = 500
    max_source_file_kb: int = 1024
    git_clone_timeout_seconds: int = 120

    # --- Knowledge graph (Neo4j) ---
    neo4j_uri: str = "bolt://localhost:7687"
    neo4j_username: str = "neo4j"
    # SecretStr hides the value in logs and error messages (it prints as '**********').
    neo4j_password: SecretStr = SecretStr("")
    neo4j_database: str = "neo4j"
    # Number of nodes or relationships sent to Neo4j in one query (see app/graph/repository.py).
    graph_batch_size: int = 1000

    @property
    def cors_origin_list(self) -> list[str]:
        return [origin.strip() for origin in self.cors_origins.split(",") if origin.strip()]


@lru_cache
def get_settings() -> Settings:
    """Return a cached Settings instance so `.env` is read only once."""
    return Settings()
