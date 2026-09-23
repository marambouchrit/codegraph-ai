"""JavaScript parser (.js, .jsx, .mjs, .cjs).

The JavaScript grammar understands JSX, so React files need no special handling.
"""

import tree_sitter
import tree_sitter_javascript

from app.ingestion.languages import Language
from app.parsing.base import LanguageParser

JAVASCRIPT_GRAMMAR = tree_sitter.Language(tree_sitter_javascript.language())


class JavaScriptParser(LanguageParser):
    language = Language.JAVASCRIPT

    def grammar_for(self, path: str) -> tree_sitter.Language:
        return JAVASCRIPT_GRAMMAR
