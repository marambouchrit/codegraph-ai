"""Shared entity-extraction logic.

Every language extractor walks the syntax tree the same way (see `extract()`).
The only thing that differs between languages is which syntax nodes define an
entity, so each subclass implements a single method: `definition_for()`.

Extraction only reads the syntax tree built in Phase 3. It never re-parses the
file and never runs any code.
"""

from abc import ABC, abstractmethod
from collections.abc import Iterator
from pathlib import PurePosixPath

import tree_sitter

from app.extraction.models import Entity, EntityType
from app.ingestion.languages import Language
from app.parsing.base import ParseResult

# What a language extractor recognized in one syntax node:
# the entity type, and the node holding the entity's name (e.g. the identifier "User").
Definition = tuple[EntityType, tree_sitter.Node | None]

TYPE_ENTITIES = frozenset({EntityType.CLASS, EntityType.INTERFACE})


class EntityExtractor(ABC):
    language: Language

    @abstractmethod
    def definition_for(self, node: tree_sitter.Node, parent: Entity) -> Definition | None:
        """Return what `node` defines, or None if it is not an entity we track.

        `parent` is the nearest enclosing entity (the file, a class, a function...).
        It tells a function defined in a class (a method) apart from a plain function.
        """

    def location_node(self, node: tree_sitter.Node) -> tree_sitter.Node:
        """The node whose position is reported for the entity defined by `node`."""
        return node

    def extract(self, parse_result: ParseResult, project_id: str) -> list[Entity]:
        """Return the FILE entity followed by every entity found in the tree, in source order."""
        # A new entity is first met at its own definition node, and walk() visits nodes in
        # source order, so the dict keeps the entities in source order too.
        entities: dict[str, Entity] = {}
        for _node, entity in self.walk(parse_result, project_id):
            entities.setdefault(entity.id, entity)
        return list(entities.values())

    def walk(
        self, parse_result: ParseResult, project_id: str
    ) -> Iterator[tuple[tree_sitter.Node, Entity]]:
        """Yield every trusted syntax node with the entity it belongs to, in source order.

        The root node comes first, with the FILE entity. A definition node (a class, a
        function...) belongs to the entity it defines, and so does everything inside it.
        Relationship extraction (Phase 5) uses this walk, so it attributes every call or
        import to exactly the entity found here, and skips exactly the same broken code.
        """
        ids = _IdGenerator(project_id, parse_result.path)
        file_entity = self._file_entity(parse_result, ids.file_id())
        yield parse_result.root_node, file_entity

        # Walk the tree with an explicit stack (no recursion, like find_syntax_errors).
        # Each item is (syntax node, nearest enclosing entity).
        stack = [(child, file_entity) for child in reversed(parse_result.root_node.children)]
        while stack:
            node, parent = stack.pop()
            if node.is_error:
                # Tree-sitter wraps code it could not understand in an ERROR node. What is
                # inside has lost its context (a method may look like a module-level
                # function), so nothing in it is trusted.
                continue
            definition = self.definition_for(node, parent)
            if definition is not None:
                entity = self._make_entity(node, definition, parent, ids)
                if entity is None:
                    # A definition whose name is missing or broken (bad syntax): skip it and
                    # everything inside, rather than attach its children to the wrong parent.
                    continue
                parent = entity
            yield node, parent
            stack.extend((child, parent) for child in reversed(node.children))

    def _file_entity(self, parse_result: ParseResult, entity_id: str) -> Entity:
        end = parse_result.root_node.end_point
        return Entity(
            id=entity_id,
            type=EntityType.FILE,
            name=PurePosixPath(parse_result.path).name,
            qualified_name=parse_result.path,
            file_path=parse_result.path,
            language=parse_result.language,
            start_line=1,
            start_column=1,
            end_line=end.row + 1,
            end_column=end.column + 1,
        )

    def _make_entity(
        self, node: tree_sitter.Node, definition: Definition, parent: Entity, ids: "_IdGenerator"
    ) -> Entity | None:
        entity_type, name_node = definition
        name = node_name(name_node)
        if name is None:
            return None
        if parent.type == EntityType.FILE:
            qualified_name = name
        else:
            qualified_name = f"{parent.qualified_name}.{name}"
        location = self.location_node(node)
        return Entity(
            id=ids.entity_id(qualified_name),
            type=entity_type,
            name=name,
            qualified_name=qualified_name,
            file_path=parent.file_path,
            language=parent.language,
            start_line=location.start_point.row + 1,
            start_column=location.start_point.column + 1,
            end_line=location.end_point.row + 1,
            end_column=location.end_point.column + 1,
            parent_id=parent.id,
        )


def node_name(name_node: tree_sitter.Node | None) -> str | None:
    """The text of a name node, or None if the name is absent or broken by a syntax error."""
    if name_node is None or name_node.is_missing or name_node.has_error:
        return None
    name = (name_node.text or b"").decode("utf-8", errors="replace").strip()
    return name or None


def is_type_entity(entity: Entity) -> bool:
    """True for classes and interfaces, the only entities that contain methods."""
    return entity.type in TYPE_ENTITIES


class _IdGenerator:
    """Builds deterministic, readable entity IDs.

        file:   <project_id>:<file_path>
        other:  <project_id>:<file_path>:<qualified_name>

    Two entities of one file can share a qualified name (Java overloads such as
    login() and login(String), or a Python function defined twice). The second one
    gets "#2", the third "#3", and so on, in source order.
    """

    def __init__(self, project_id: str, file_path: str) -> None:
        self._prefix = f"{project_id}:{file_path}"
        self._seen: dict[str, int] = {}

    def file_id(self) -> str:
        return self._prefix

    def entity_id(self, qualified_name: str) -> str:
        base_id = f"{self._prefix}:{qualified_name}"
        count = self._seen.get(base_id, 0) + 1
        self._seen[base_id] = count
        return base_id if count == 1 else f"{base_id}#{count}"
