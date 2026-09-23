"""API models (request and response bodies) for projects."""

from datetime import datetime
from enum import StrEnum

from pydantic import BaseModel, Field

from app.ingestion.languages import Language


class SourceType(StrEnum):
    GITHUB = "github"
    ZIP = "zip"


class GitHubProjectCreate(BaseModel):
    url: str = Field(examples=["https://github.com/pallets/flask"])


class Project(BaseModel):
    id: str
    name: str
    source_type: SourceType
    source: str = Field(description="GitHub URL or uploaded file name")
    created_at: datetime
    file_count: int = Field(description="Number of supported source files")
    total_size_bytes: int = Field(description="Total size of the supported source files")
    languages: dict[str, int] = Field(description="Number of source files per language")


class SourceFile(BaseModel):
    path: str
    language: Language
    size_bytes: int


class ProjectFiles(BaseModel):
    project_id: str
    files: list[SourceFile]
