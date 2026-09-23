"""Java parser (.java)."""

import tree_sitter
import tree_sitter_java

from app.ingestion.languages import Language
from app.parsing.base import LanguageParser

JAVA_GRAMMAR = tree_sitter.Language(tree_sitter_java.language())


class JavaParser(LanguageParser):
    language = Language.JAVA

    def grammar_for(self, path: str) -> tree_sitter.Language:
        return JAVA_GRAMMAR
