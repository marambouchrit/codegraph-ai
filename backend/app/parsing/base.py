"""Shared parser abstraction and the data returned by every parser.

Each supported language has a small subclass of `LanguageParser` (see the
`*_parser.py` modules). A subclass only has to say which Tree-sitter grammar to
use; parsing and syntax-error detection are the same for every language.

Parsing only builds a syntax tree from the file's bytes. It never runs the code.
"""

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from enum import StrEnum

import tree_sitter

from app.ingestion.languages import Language

# A file full of mistakes could produce thousands of error nodes; a few are enough to report.
MAX_REPORTED_SYNTAX_ERRORS = 50


class SyntaxErrorKind(StrEnum):
    ERROR = "error"  # code the grammar could not understand (an ERROR node)
    MISSING = "missing"  # a token the parser had to invent, e.g. a missing ")"


@dataclass(frozen=True)
class SyntaxErrorInfo:
    kind: SyntaxErrorKind
    node_type: str  # for MISSING nodes, the missing token, e.g. ")"
    # Human-friendly positions: lines and columns start at 1 (Tree-sitter starts at 0).
    start_line: int
    start_column: int
    end_line: int
    end_column: int

    @property
    def message(self) -> str:
        if self.kind == SyntaxErrorKind.MISSING:
            return f"Missing '{self.node_type}' at line {self.start_line}"
        return f"Invalid syntax at line {self.start_line}"


@dataclass(frozen=True)
class RootNodeInfo:
    type: str  # e.g. "module" (Python) or "program" (Java, JavaScript, TypeScript)
    child_count: int  # number of top-level syntax nodes
    start_line: int
    end_line: int
    end_byte: int


@dataclass
class ParseResult:
    """The syntax tree of one source file, plus metadata about it."""

    path: str  # relative to the project root, with "/" separators
    language: Language
    source: bytes
    tree: tree_sitter.Tree
    syntax_errors: list[SyntaxErrorInfo] = field(default_factory=list)

    @property
    def root_node(self) -> tree_sitter.Node:
        return self.tree.root_node

    @property
    def has_syntax_errors(self) -> bool:
        # Tree-sitter flags the root node when any node below it is an ERROR or MISSING node.
        return self.root_node.has_error

    @property
    def root_info(self) -> RootNodeInfo:
        root = self.root_node
        return RootNodeInfo(
            type=root.type,
            child_count=root.child_count,
            start_line=root.start_point.row + 1,
            end_line=root.end_point.row + 1,
            end_byte=root.end_byte,
        )


class LanguageParser(ABC):
    """Turns the bytes of a source file into a Tree-sitter syntax tree."""

    language: Language

    @abstractmethod
    def grammar_for(self, path: str) -> tree_sitter.Language:
        """Return the Tree-sitter grammar to use for `path`.

        Most languages have one grammar. The path is passed so that a language can
        pick a variant (TypeScript uses a different grammar for .tsx files).
        """

    def parse(self, source: bytes, path: str) -> ParseResult:
        # Tree-sitter works on raw bytes, so the file never has to be decoded. Invalid
        # UTF-8 or a byte-order mark does not break parsing.
        # A new Parser is created for every file: it is cheap and keeps this thread-safe.
        parser = tree_sitter.Parser(self.grammar_for(path))
        tree = parser.parse(source)
        return ParseResult(
            path=path,
            language=self.language,
            source=source,
            tree=tree,
            syntax_errors=find_syntax_errors(tree.root_node),
        )


def find_syntax_errors(
    root: tree_sitter.Node, limit: int = MAX_REPORTED_SYNTAX_ERRORS
) -> list[SyntaxErrorInfo]:
    """Collect up to `limit` ERROR and MISSING nodes, in source order.

    Tree-sitter never gives up on bad code: it builds a tree anyway and marks the
    parts it could not understand. `node.has_error` is true when a node or any node
    below it is marked, so subtrees without errors are skipped entirely.
    A stack is used instead of recursion so deeply nested code cannot hit
    Python's recursion limit.
    """
    errors: list[SyntaxErrorInfo] = []
    stack = [root]
    while stack and len(errors) < limit:
        node = stack.pop()
        if node.is_missing:
            errors.append(_to_error_info(node, SyntaxErrorKind.MISSING))
        elif node.is_error:
            # One entry per ERROR node is enough; errors nested inside it add nothing useful.
            errors.append(_to_error_info(node, SyntaxErrorKind.ERROR))
        elif node.has_error:
            # Reversed, so the first child is popped first and errors stay in source order.
            stack.extend(reversed(node.children))
    return errors


def _to_error_info(node: tree_sitter.Node, kind: SyntaxErrorKind) -> SyntaxErrorInfo:
    return SyntaxErrorInfo(
        kind=kind,
        node_type=node.type,
        start_line=node.start_point.row + 1,
        start_column=node.start_point.column + 1,
        end_line=node.end_point.row + 1,
        end_column=node.end_point.column + 1,
    )
