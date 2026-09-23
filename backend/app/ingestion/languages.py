"""Detect the programming language of a file from its extension."""

from enum import StrEnum
from pathlib import PurePath


class Language(StrEnum):
    PYTHON = "python"
    JAVA = "java"
    JAVASCRIPT = "javascript"
    TYPESCRIPT = "typescript"


EXTENSION_TO_LANGUAGE: dict[str, Language] = {
    ".py": Language.PYTHON,
    ".pyi": Language.PYTHON,
    ".java": Language.JAVA,
    ".js": Language.JAVASCRIPT,
    ".jsx": Language.JAVASCRIPT,
    ".mjs": Language.JAVASCRIPT,
    ".cjs": Language.JAVASCRIPT,
    ".ts": Language.TYPESCRIPT,
    ".tsx": Language.TYPESCRIPT,
    ".mts": Language.TYPESCRIPT,
    ".cts": Language.TYPESCRIPT,
}


def detect_language(path: str | PurePath) -> Language | None:
    """Return the language of `path`, or None if it is not a supported source file."""
    return EXTENSION_TO_LANGUAGE.get(PurePath(path).suffix.lower())
