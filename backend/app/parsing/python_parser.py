"""Python parser (.py, .pyi)."""

import tree_sitter
import tree_sitter_python

from app.ingestion.languages import Language
from app.parsing.base import LanguageParser

PYTHON_GRAMMAR = tree_sitter.Language(tree_sitter_python.language())


class PythonParser(LanguageParser):
    language = Language.PYTHON

    def grammar_for(self, path: str) -> tree_sitter.Language:
        return PYTHON_GRAMMAR
