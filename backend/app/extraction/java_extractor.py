"""Java entities: classes, interfaces and methods.

    class_declaration, enum_declaration, record_declaration  -> CLASS
    interface_declaration, annotation_type_declaration       -> INTERFACE
    method_declaration, constructor_declaration,
    compact_constructor_declaration,
    annotation_type_element_declaration                      -> METHOD

Enums and records are special kinds of classes in Java, and annotation types are
special interfaces, so they reuse those entity types. Java has no functions
outside classes.

Methods of anonymous classes (`new Runnable() { public void run() {} }`) are
skipped: the anonymous class has no name, so their nearest enclosing entity is
a method, not a class.
"""

import tree_sitter

from app.extraction.base import Definition, EntityExtractor, is_type_entity
from app.extraction.models import Entity, EntityType
from app.ingestion.languages import Language

CLASS_TYPES = frozenset({"class_declaration", "enum_declaration", "record_declaration"})
INTERFACE_TYPES = frozenset({"interface_declaration", "annotation_type_declaration"})
METHOD_TYPES = frozenset(
    {
        "method_declaration",
        "constructor_declaration",
        "compact_constructor_declaration",
        "annotation_type_element_declaration",
    }
)


class JavaExtractor(EntityExtractor):
    language = Language.JAVA

    def definition_for(self, node: tree_sitter.Node, parent: Entity) -> Definition | None:
        if node.type in CLASS_TYPES:
            return EntityType.CLASS, node.child_by_field_name("name")
        if node.type in INTERFACE_TYPES:
            return EntityType.INTERFACE, node.child_by_field_name("name")
        if node.type in METHOD_TYPES and is_type_entity(parent):
            return EntityType.METHOD, node.child_by_field_name("name")
        return None
