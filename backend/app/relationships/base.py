"""Shared logic of the reference collectors (step 1 of relationship extraction).

A collector visits the syntax tree of one file through `EntityExtractor.walk()`,
the same walk used by entity extraction. For every node, the walk gives the
entity the node belongs to, so a call written inside `User.login` is recorded with
`User.login` as its source, and broken code skipped by Phase 4 is skipped here too.

Each language collector implements one method, `visit()`, which looks at a single
syntax node and records what it finds (an import, a call, a base class...) in a
`FileReferences` through a `ReferenceRecorder`.

Collectors only read the syntax tree. They never re-parse the file and never run code.
"""

import re
from abc import ABC, abstractmethod
from collections.abc import Iterable

import tree_sitter

from app.extraction.models import Entity, EntityType
from app.ingestion.languages import Language
from app.relationships.models import RelationshipType
from app.relationships.references import (
    SELF,
    SUPER,
    DottedName,
    FileReferences,
    Import,
    Reference,
    VariableType,
)

# A name made of identifiers separated by dots, like "user.save" or "this.repo.find".
# Anything else (a call in the middle, an index, a string...) cannot be resolved by name.
_IDENTIFIER = re.compile(r"[A-Za-z_$][\w$]*")
_WHITESPACE = re.compile(r"\s+")
MAX_NAME_LENGTH = 200

TYPE_ENTITIES = frozenset({EntityType.CLASS, EntityType.INTERFACE})


def node_text(node: tree_sitter.Node | None) -> str:
    if node is None:
        return ""
    return (node.text or b"").decode("utf-8", errors="replace")


def split_dotted(text: str) -> DottedName | None:
    """("a", "b", "c") for "a.b.c", or None if `text` is not a plain dotted name."""
    compact = _WHITESPACE.sub("", text).replace("?.", ".")  # also accept `a?.b` (JavaScript)
    if not compact or len(compact) > MAX_NAME_LENGTH:
        return None
    parts = tuple(compact.split("."))
    if all(_IDENTIFIER.fullmatch(part) for part in parts):
        return parts
    return None


def is_broken(node: tree_sitter.Node) -> bool:
    """True if the node is missing or contains a syntax error: nothing is taken from it."""
    return node.is_missing or node.has_error


class ReferenceRecorder:
    """Collects the facts of one file, skipping anything built from broken syntax."""

    def __init__(self, file: Entity, language: Language) -> None:
        self.result = FileReferences(file=file, language=language)
        # Nodes already recorded as something more specific (for example the `User` of
        # `new User()` is a CALLS, not a USES): the node IDs of Tree-sitter are unique.
        self._consumed: set[int] = set()

    @property
    def file(self) -> Entity:
        return self.result.file

    def reference(
        self,
        type_: RelationshipType,
        source: Entity,
        node: tree_sitter.Node,
        parts: DottedName | None,
        text: str | None = None,
    ) -> None:
        if parts is None or is_broken(node):
            return
        self.result.references.append(
            Reference(
                type=type_,
                source_id=source.id,
                parts=parts,
                text=text if text is not None else _WHITESPACE.sub("", node_text(node)),
                line=node.start_point.row + 1,
                column=node.start_point.column + 1,
            )
        )

    def import_(
        self, node: tree_sitter.Node, module: str, name: str | None, alias: str | None
    ) -> None:
        if not module or is_broken(node):
            return
        self.result.imports.append(
            Import(
                module=module,
                name=name,
                alias=alias,
                line=node.start_point.row + 1,
                column=node.start_point.column + 1,
            )
        )

    def variable(self, scope_id: str | None, name: str, type_parts: DottedName | None) -> None:
        if scope_id is None or not name or type_parts is None:
            return
        self.result.variables.append(VariableType(scope_id, name, type_parts))

    def consume(self, node: tree_sitter.Node | None) -> None:
        """Mark `node` and everything inside it as already recorded."""
        stack = [node] if node is not None else []
        while stack:
            current = stack.pop()
            self._consumed.add(current.id)
            stack.extend(current.children)

    def is_consumed(self, node: tree_sitter.Node) -> bool:
        return node.id in self._consumed


class ReferenceCollector(ABC):
    language: Language

    # Receivers meaning "the current object" and the prefix of a call to the parent class.
    self_names: frozenset[str] = frozenset({"this"})
    super_prefix: str = "super."

    def collect(
        self, file: Entity, nodes: Iterable[tuple[tree_sitter.Node, Entity]]
    ) -> FileReferences:
        """Record the facts of one file. `nodes` comes from `EntityExtractor.walk()`."""
        recorder = ReferenceRecorder(file, self.language)
        for node, owner in nodes:
            self.visit(node, owner, recorder)
        return recorder.result

    @abstractmethod
    def visit(self, node: tree_sitter.Node, owner: Entity, recorder: ReferenceRecorder) -> None:
        """Record what `node` says about relationships. `owner` is the entity it belongs to."""

    def dotted(self, node: tree_sitter.Node | None) -> DottedName | None:
        """The dotted name written by `node`, with `this`/`self` and `super` normalized.

        `user.save` -> ("user", "save"), `this.save` -> (SELF, "save"),
        `super.save` -> (SUPER, "save"); `get_user().save` -> None.
        """
        if node is None:
            return None
        return self.dotted_text(node_text(node))

    def dotted_text(self, text: str) -> DottedName | None:
        """Like `dotted()`, for a name that is not a single node (Java `receiver.name`)."""
        text = _WHITESPACE.sub("", text)
        if text.startswith(self.super_prefix):
            rest = split_dotted(text[len(self.super_prefix) :])
            return (SUPER, *rest) if rest is not None else None
        parts = split_dotted(text)
        if parts is None:
            return None
        if parts[0] in self.self_names:
            return (SELF, *parts[1:])
        return parts


def class_scope(owner: Entity) -> str | None:
    """The scope where the fields assigned by a method live: the method's class."""
    if owner.type in TYPE_ENTITIES:
        return owner.id
    if owner.type == EntityType.METHOD:
        return owner.parent_id
    return None


def string_content(node: tree_sitter.Node | None) -> str:
    """The text of a string literal without its quotes, e.g. "./models/User".

    Tree-sitter splits a string into quotes and content nodes ("string_fragment" in
    JavaScript/TypeScript, "string_content" in Python).
    """
    if node is None:
        return ""
    return "".join(
        node_text(child)
        for child in node.named_children
        if child.type in ("string_fragment", "string_content")
    )
