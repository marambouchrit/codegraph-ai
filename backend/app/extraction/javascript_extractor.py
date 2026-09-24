"""JavaScript entities: classes, functions and methods.

    class_declaration                          -> CLASS
    function_declaration,
    generator_function_declaration             -> FUNCTION
    const f = () => {} / function () {} /
              function* () {}                  -> FUNCTION named after the variable
    const User = class {}                      -> CLASS named after the variable
    method_definition in a class body          -> METHOD (also getters, setters, #private)
    handle = () => {} in a class body          -> METHOD (arrow-function class field)

Anonymous functions (callbacks, `export default function () {}`) have no name
and are not entities; functions declared inside them still are. Methods of
object literals (`{ run() {} }`) are not tracked.
"""

import tree_sitter

from app.extraction.base import Definition, EntityExtractor, is_type_entity
from app.extraction.models import Entity, EntityType
from app.ingestion.languages import Language

FUNCTION_VALUE_TYPES = frozenset({"arrow_function", "function_expression", "generator_function"})


class JavaScriptExtractor(EntityExtractor):
    language = Language.JAVASCRIPT

    # TypeScriptExtractor extends these: the TypeScript grammar builds on the JavaScript one.
    class_types = frozenset({"class_declaration"})
    class_body_types = frozenset({"class_body"})

    def definition_for(self, node: tree_sitter.Node, parent: Entity) -> Definition | None:
        if node.type in self.class_types:
            return EntityType.CLASS, node.child_by_field_name("name")
        if node.type in ("function_declaration", "generator_function_declaration"):
            return EntityType.FUNCTION, node.child_by_field_name("name")
        if node.type == "variable_declarator":
            return variable_definition(node)
        # Checking the syntactic parent too avoids taking methods of an object literal
        # stored in a class field (`config = { run() {} }`) for methods of the class.
        if is_type_entity(parent) and _parent_type(node) in self.class_body_types:
            return self.member_definition(node)
        return None

    def member_definition(self, node: tree_sitter.Node) -> Definition | None:
        """What a node placed directly in a class body defines."""
        if node.type == "method_definition":
            return EntityType.METHOD, node.child_by_field_name("name")
        if node.type == "field_definition" and has_function_value(node):
            return EntityType.METHOD, node.child_by_field_name("property")
        return None


def variable_definition(node: tree_sitter.Node) -> Definition | None:
    """`const name = <function or class>`: the variable gives its name to the value."""
    name_node = node.child_by_field_name("name")
    value = node.child_by_field_name("value")
    if name_node is None or name_node.type != "identifier" or value is None:
        return None  # destructuring (`const { a } = b`) or no value
    if value.type in FUNCTION_VALUE_TYPES:
        return EntityType.FUNCTION, name_node
    if value.type == "class":
        return EntityType.CLASS, name_node
    return None


def has_function_value(node: tree_sitter.Node) -> bool:
    value = node.child_by_field_name("value")
    return value is not None and value.type in FUNCTION_VALUE_TYPES


def _parent_type(node: tree_sitter.Node) -> str | None:
    return node.parent.type if node.parent is not None else None
