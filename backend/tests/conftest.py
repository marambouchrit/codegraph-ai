"""Shared test fixtures."""

import io
import zipfile
from collections.abc import Callable
from pathlib import Path

import pytest

from app.core.config import Settings

MakeZip = Callable[[dict[str, str | bytes]], bytes]


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    """Settings that store projects in a temporary folder deleted after each test."""
    return Settings(workspace_dir=tmp_path / "workspace")


@pytest.fixture
def make_zip() -> MakeZip:
    """Build an in-memory ZIP archive from {"path/in/archive": content}."""

    def build(entries: dict[str, str | bytes]) -> bytes:
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
            for name, content in entries.items():
                archive.writestr(name, content)
        return buffer.getvalue()

    return build
