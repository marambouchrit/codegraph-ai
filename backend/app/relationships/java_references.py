"""Java references.

    package com.example.app;                   -> the file's package
    import com.example.User;                   -> Import (binds "User"); also `.*` and static
    class A extends B implements I, J          -> INHERITS B, IMPLEMENTS I and J
    interface I extends J                      -> INHERITS J
    new User()                                 -> CALLS User (object creation)
    user.save() / save() / this.save()         -> CALLS
    super.save()                               -> CALLS save in the parent class
    private User user; / User find(Role r)     -> USES User, Role (any type written in code)

Variable types, so that `user.save()` can be resolved: fields, parameters, local
variables (including `var u = new User()`) and enhanced for loops.

Not tracked: `super(...)` / `this(...)` constructor calls, method references
(`User::save`), lambdas' parameter types, annotations.
"""

import tree_sitter

from app.extraction.models import Entity
from app.ingestion.languages import Language
from app.relationships.base import (
    TYPE_ENTITIES,
    ReferenceCollector,
    ReferenceRecorder,
    is_broken,
    node_text,
    split_dotted,
)
from app.relationships.models import RelationshipType
from app.relationships.references import WILDCARD, DottedName

JAVA_TYPE_DECLARATIONS = frozenset(
    {"class_declaration", "enum_declaration", "record_declaration", "interface_declaration"}
)
TYPED_VARIABLES = frozenset({"local_variable_declaration", "field_declaration"})


class JavaReferenceCollector(ReferenceCollector):
    language = Language.JAVA

    def visit(self, node: tree_sitter.Node, owner: Entity, recorder: ReferenceRecorder) -> None:
        if node.type == "package_declaration":
            name = next((c for c in node.named_children if "identifier" in c.type), None)
            if name is not None and not is_broken(node):
                recorder.result.package = "".join(node_text(name).split())
        elif node.type == "import_declaration":
            self._import(node, recorder)
        elif node.type in JAVA_TYPE_DECLARATIONS and owner.type in TYPE_ENTITIES:
            self._supertypes(node, owner, recorder)
        elif node.type == "object_creation_expression":
            type_node = _type_head(node.child_by_field_name("type"))
            recorder.consume(type_node)  # `new User()` is a CALLS, not a USES
            recorder.reference(
                RelationshipType.CALLS, owner, node, _type_name(type_node), node_text(type_node)
            )
        elif node.type == "method_invocation":
            self._method_call(node, owner, recorder)
        elif node.type in TYPED_VARIABLES:
            self._variables(node, owner, recorder)
        elif node.type in ("formal_parameter", "enhanced_for_statement"):
            name = node.child_by_field_name("name")
            if name is not None and not is_broken(node):
                recorder.variable(
                    owner.id, node_text(name), _type_name(node.child_by_field_name("type"))
                )
        elif node.type in ("type_identifier", "scoped_type_identifier"):
            self._type_use(node, owner, recorder)

    def _import(self, node: tree_sitter.Node, recorder: ReferenceRecorder) -> None:
        name_node = next((c for c in node.named_children if "identifier" in c.type), None)
        parts = split_dotted(node_text(name_node)) if name_node is not None else None
        if parts is None:
            return
        if any(child.type == "asterisk" for child in node.children):
            # `import com.example.*;` (every type of a package)
            recorder.import_(node, ".".join(parts), WILDCARD, None)
        elif len(parts) > 1:
            # `import com.example.User;` or `import static com.example.Util.max;`
            recorder.import_(node, ".".join(parts[:-1]), parts[-1], parts[-1])

    def _supertypes(
        self, node: tree_sitter.Node, owner: Entity, recorder: ReferenceRecorder
    ) -> None:
        # class A extends B            -> superclass
        # class A implements I, J      -> super_interfaces (also enums and records)
        # interface I extends J, K     -> extends_interfaces
        for child in node.named_children:
            if child.type == "superclass":
                relationship, types = RelationshipType.INHERITS, child.named_children
            elif child.type == "super_interfaces":
                relationship, types = RelationshipType.IMPLEMENTS, _type_list(child)
            elif child.type == "extends_interfaces":
                relationship, types = RelationshipType.INHERITS, _type_list(child)
            else:
                continue
            for type_node in types:
                head = _type_head(type_node)
                recorder.consume(head)  # generic arguments (B<User>) stay USES
                recorder.reference(relationship, owner, type_node, _type_name(head))

    def _method_call(
        self, node: tree_sitter.Node, owner: Entity, recorder: ReferenceRecorder
    ) -> None:
        name = node.child_by_field_name("name")
        receiver = node.child_by_field_name("object")
        if name is None:
            return
        # save(), user.save(), this.save(), super.save(), a.b.save()
        text = node_text(name) if receiver is None else f"{node_text(receiver)}.{node_text(name)}"
        recorder.reference(RelationshipType.CALLS, owner, node, self.dotted_text(text), text)

    def _variables(
        self, node: tree_sitter.Node, owner: Entity, recorder: ReferenceRecorder
    ) -> None:
        if is_broken(node):
            return
        declared_type = node.child_by_field_name("type")
        for declarator in node.children_by_field_name("declarator"):
            name = node_text(declarator.child_by_field_name("name"))
            type_parts = _type_name(declared_type)
            value = declarator.child_by_field_name("value")
            if type_parts == ("var",) and value is not None:
                # `var user = new User();`: the type comes from the created object.
                type_parts = None
                if value.type == "object_creation_expression":
                    type_parts = _type_name(_type_head(value.child_by_field_name("type")))
            # Fields are declared in the class body, so their scope is the class itself.
            recorder.variable(owner.id, name, type_parts)

    def _type_use(self, node: tree_sitter.Node, owner: Entity, recorder: ReferenceRecorder) -> None:
        if recorder.is_consumed(node):
            return
        parent_type = node.parent.type if node.parent is not None else ""
        if parent_type in ("scoped_type_identifier", "type_parameter"):
            return  # part of a longer name (handled with it), or a declaration like <T>
        recorder.consume(node)
        parts = _type_name(node)
        if parts != ("var",):
            recorder.reference(RelationshipType.USES, owner, node, parts)


def _type_list(node: tree_sitter.Node) -> list[tree_sitter.Node]:
    type_list = next((c for c in node.named_children if c.type == "type_list"), None)
    return type_list.named_children if type_list is not None else []


def _type_head(node: tree_sitter.Node | None) -> tree_sitter.Node | None:
    """`List<User>` -> `List`: the generic arguments are not part of the type's name."""
    if node is not None and node.type == "generic_type" and node.named_children:
        return node.named_children[0]
    return node


def _type_name(node: tree_sitter.Node | None) -> DottedName | None:
    head = _type_head(node)
    if head is None or head.type not in ("type_identifier", "scoped_type_identifier"):
        return None  # primitive types (int, boolean...), arrays, wildcards
    return split_dotted(node_text(head))
