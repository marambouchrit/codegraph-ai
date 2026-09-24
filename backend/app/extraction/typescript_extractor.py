"""TypeScript entities (.ts and .tsx): everything JavaScript has, plus:

    abstract_class_declaration                 -> CLASS
    interface_declaration                      -> INTERFACE
    method_signature in an interface body      -> METHOD  (`login(): void;`)
    abstract_method_signature in a class body  -> METHOD  (`abstract run(): void;`)
    handle = () => {} in a class body          -> METHOD  (public_field_definition)

Not tracked: overload signatures (`function f(a: string): void;`, also inside
classes; the implementation is the entity), `declare function` in .d.ts files,
enums, type aliases and namespaces.

The TypeScript and TSX grammars (see typescript_parser.py) use the same node
types, so one extractor serves both.
"""

import tree_sitter

from app.extraction.base import Definition
from app.extraction.javascript_extractor import JavaScriptExtractor, has_function_value
from app.extraction.models import Entity, EntityType
from app.ingestion.languages import Language


class TypeScriptExtractor(JavaScriptExtractor):
    language = Language.TYPESCRIPT

    class_types = frozenset({"class_declaration", "abstract_class_declaration"})
    class_body_types = frozenset({"class_body", "interface_body"})

    def definition_for(self, node: tree_sitter.Node, parent: Entity) -> Definition | None:
        if node.type == "interface_declaration":
            return EntityType.INTERFACE, node.child_by_field_name("name")
        return super().definition_for(node, parent)

    def member_definition(self, node: tree_sitter.Node) -> Definition | None:
        if node.type == "method_signature" and parent_is_interface(node):
            return EntityType.METHOD, node.child_by_field_name("name")
        if node.type == "abstract_method_signature":
            return EntityType.METHOD, node.child_by_field_name("name")
        if node.type == "public_field_definition" and has_function_value(node):
            return EntityType.METHOD, node.child_by_field_name("name")
        return super().member_definition(node)


def parent_is_interface(node: tree_sitter.Node) -> bool:
    # In a class body, a method_signature is only an overload of a real method.
    return node.parent is not None and node.parent.type == "interface_body"
