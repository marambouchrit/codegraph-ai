"""TypeScript parser (.ts, .mts, .cts, .tsx).

The TypeScript package ships two grammars. JSX is only allowed in .tsx files, and
the plain TypeScript grammar reports JSX as a syntax error, so .tsx files need the
TSX grammar. (The reverse is not safe either: `<Type>value` casts are valid in .ts
but mean JSX in .tsx.)
"""

from pathlib import PurePath

import tree_sitter
import tree_sitter_typescript

from app.ingestion.languages import Language
from app.parsing.base import LanguageParser

TYPESCRIPT_GRAMMAR = tree_sitter.Language(tree_sitter_typescript.language_typescript())
TSX_GRAMMAR = tree_sitter.Language(tree_sitter_typescript.language_tsx())


class TypeScriptParser(LanguageParser):
    language = Language.TYPESCRIPT

    def grammar_for(self, path: str) -> tree_sitter.Language:
        if PurePath(path).suffix.lower() == ".tsx":
            return TSX_GRAMMAR
        return TYPESCRIPT_GRAMMAR
