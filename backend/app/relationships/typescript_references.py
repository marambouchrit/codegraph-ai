"""TypeScript references (.ts and .tsx): everything JavaScript has, plus:

    class S extends Base<T> implements Auth    -> INHERITS Base, IMPLEMENTS Auth
    interface Auth extends Base                -> INHERITS Base
    repo: UserRepository / find(): Promise<User> -> USES UserRepository, Promise, User
                                                  (any type written in the code)

Type annotations also give variable types: `const u: User = ...`, `find(u: User)`,
`private repo: Repo;` and constructor parameter properties (`constructor(private r: Repo)`).

Not tracked: `import x = require("...")`, type-only constructs such as type aliases,
namespaces and enums (they are not entities in Phase 4).
"""

import tree_sitter

from app.extraction.models import Entity, EntityType
from app.ingestion.languages import Language
from app.relationships.base import ReferenceRecorder, class_scope, is_broken, node_text
from app.relationships.javascript_references import JavaScriptReferenceCollector
from app.relationships.models import RelationshipType
from app.relationships.references import DottedName

TYPE_NAMES = frozenset({"type_identifier", "nested_type_identifier"})
# Parents whose "name" field declares a new type instead of using one.
TYPE_DECLARATIONS = frozenset(
    {
        "class_declaration",
        "abstract_class_declaration",
        "class",
        "interface_declaration",
        "type_alias_declaration",
    }
)
PARAMETERS = frozenset({"required_parameter", "optional_parameter"})


class TypeScriptReferenceCollector(JavaScriptReferenceCollector):
    language = Language.TYPESCRIPT

    class_types = frozenset({"class_declaration", "abstract_class_declaration"})
    field_types = frozenset({"public_field_definition"})

    def visit(self, node: tree_sitter.Node, owner: Entity, recorder: ReferenceRecorder) -> None:
        if node.type == "interface_declaration":
            self._interface_bases(node, owner, recorder)
        elif node.type in TYPE_NAMES:
            self._type_use(node, owner, recorder)
        elif node.type in PARAMETERS:
            self._parameter(node, owner, recorder)
        else:
            super().visit(node, owner, recorder)

    def declared_type(self, node: tree_sitter.Node) -> DottedName | None:
        annotation = node.child_by_field_name("type")
        if annotation is not None:
            return self._annotation_type(annotation)
        return super().declared_type(node)

    def _interface_bases(
        self, node: tree_sitter.Node, owner: Entity, recorder: ReferenceRecorder
    ) -> None:
        clause = next((c for c in node.named_children if c.type == "extends_type_clause"), None)
        if clause is None or owner.type != EntityType.INTERFACE:
            return
        for type_node in clause.children_by_field_name("type"):
            head = self.type_head(type_node)
            recorder.consume(head)  # generic arguments stay USES
            recorder.reference(RelationshipType.INHERITS, owner, type_node, self.dotted(head))

    def _type_use(self, node: tree_sitter.Node, owner: Entity, recorder: ReferenceRecorder) -> None:
        parent = node.parent
        if recorder.is_consumed(node) or parent is None:
            return
        if parent.type in TYPE_DECLARATIONS and parent.child_by_field_name("name") == node:
            return  # `class User`, `interface Auth`: a declaration, not a use
        if parent.type in ("nested_type_identifier", "type_parameter"):
            return  # part of a longer name (handled with it), or a declaration like <T>
        recorder.consume(node)
        recorder.reference(RelationshipType.USES, owner, node, self.dotted(node))

    def _parameter(
        self, node: tree_sitter.Node, owner: Entity, recorder: ReferenceRecorder
    ) -> None:
        name = node.child_by_field_name("pattern")
        annotation = node.child_by_field_name("type")
        if name is None or name.type != "identifier" or annotation is None or is_broken(node):
            return
        type_parts = self._annotation_type(annotation)
        recorder.variable(owner.id, node_text(name), type_parts)
        if any(child.type in ("accessibility_modifier", "readonly") for child in node.children):
            # constructor(private repo: Repo) also declares the field this.repo.
            recorder.variable(class_scope(owner), node_text(name), type_parts)

    def _annotation_type(self, annotation: tree_sitter.Node) -> DottedName | None:
        """`: User` -> ("User",); `: Repository<User>` -> ("Repository",); `: string` -> None."""
        type_node = annotation.named_children[0] if annotation.named_children else None
        if type_node is None:
            return None
        head = self.type_head(type_node)
        return self.dotted(head) if head.type in TYPE_NAMES else None
