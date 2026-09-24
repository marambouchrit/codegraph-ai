"""Python entities: classes, functions and methods.

    class_definition     -> CLASS
    function_definition  -> METHOD if its nearest enclosing entity is a class,
                            otherwise FUNCTION (module level or nested in a function)

`async def` is also a function_definition. A decorated definition is wrapped in a
decorated_definition node; its location starts at the first decorator, so the
reported lines include `@app.get("/users")` and similar.
"""

import tree_sitter

from app.extraction.base import Definition, EntityExtractor, is_type_entity
from app.extraction.models import Entity, EntityType
from app.ingestion.languages import Language


class PythonExtractor(EntityExtractor):
    language = Language.PYTHON

    def definition_for(self, node: tree_sitter.Node, parent: Entity) -> Definition | None:
        if node.type == "class_definition":
            return EntityType.CLASS, node.child_by_field_name("name")
        if node.type == "function_definition":
            kind = EntityType.METHOD if is_type_entity(parent) else EntityType.FUNCTION
            return kind, node.child_by_field_name("name")
        return None

    def location_node(self, node: tree_sitter.Node) -> tree_sitter.Node:
        if node.parent is not None and node.parent.type == "decorated_definition":
            return node.parent
        return node
