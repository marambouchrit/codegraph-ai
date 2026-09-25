"""Python references.

    import a.b / import a.b as m               -> Import (binds "a.b" / "m")
    from ..models.user import User as U        -> Import (binds "U"); `import *` too
    class Admin(User, models.Base)             -> INHERITS User, models.Base
    user.save() / User() / self.save()         -> CALLS (User() creates an object: CALLS User)
    super().save()                             -> CALLS save in a parent class
    def f(a: User) -> Optional[Role]           -> USES User, Optional, Role (also `x: Role = ...`)

Variable types, so that `user.save()` can be resolved:

    user = User()        user: User = ...      def f(user: User)
    self.repo = Repo()   (a field of the enclosing class)

Not tracked: decorators without a call (`@login_required`), `isinstance()` checks,
imports inside functions (treated as imports of the whole file), `__all__`.
"""

import tree_sitter

from app.extraction.models import Entity
from app.ingestion.languages import Language
from app.relationships.base import (
    ReferenceCollector,
    ReferenceRecorder,
    class_scope,
    is_broken,
    node_text,
    split_dotted,
    string_content,
)
from app.relationships.models import RelationshipType
from app.relationships.references import SELF, WILDCARD, DottedName


class PythonReferenceCollector(ReferenceCollector):
    language = Language.PYTHON

    self_names = frozenset({"self", "cls"})
    super_prefix = "super()."

    def visit(self, node: tree_sitter.Node, owner: Entity, recorder: ReferenceRecorder) -> None:
        if node.type == "import_statement":
            self._import(node, recorder)
        elif node.type == "import_from_statement":
            self._import_from(node, recorder)
        elif node.type == "class_definition":
            self._base_classes(node, owner, recorder)
        elif node.type == "call":
            self._call(node, owner, recorder)
        elif node.type == "assignment":
            self._assignment(node, owner, recorder)
        elif node.type in ("typed_parameter", "typed_default_parameter"):
            self._parameter(node, owner, recorder)
        elif node.type == "type":
            # Every annotation is wrapped in a `type` node; a generic such as
            # Optional[User] contains nested `type` nodes, which are visited on their own.
            for name_node, parts in self._type_names(node.named_children[:1]):
                recorder.reference(RelationshipType.USES, owner, name_node, parts)

    # ----- Imports -----

    def _import(self, node: tree_sitter.Node, recorder: ReferenceRecorder) -> None:
        for name_node in node.children_by_field_name("name"):
            if name_node.type == "aliased_import":
                module = node_text(name_node.child_by_field_name("name"))
                alias = node_text(name_node.child_by_field_name("alias"))
            else:
                module = alias = node_text(name_node)  # `import a.b` binds the name "a.b"
            recorder.import_(name_node, module, None, alias)

    def _import_from(self, node: tree_sitter.Node, recorder: ReferenceRecorder) -> None:
        module_node = node.child_by_field_name("module_name")
        if module_node is None or is_broken(module_node):
            return
        module = "".join(node_text(module_node).split())  # e.g. "..models.user"
        if any(child.type == "wildcard_import" for child in node.children):
            recorder.import_(node, module, WILDCARD, None)
        for name_node in node.children_by_field_name("name"):
            if name_node.type == "aliased_import":
                name = node_text(name_node.child_by_field_name("name"))
                alias = node_text(name_node.child_by_field_name("alias"))
            else:
                name = alias = node_text(name_node)
            recorder.import_(name_node, module, name, alias)

    # ----- Classes and calls -----

    def _base_classes(
        self, node: tree_sitter.Node, owner: Entity, recorder: ReferenceRecorder
    ) -> None:
        bases = node.child_by_field_name("superclasses")
        if bases is None:
            return
        for base in bases.named_children:
            if base.type in ("identifier", "attribute"):  # not `metaclass=...`
                recorder.reference(RelationshipType.INHERITS, owner, base, self.dotted(base))

    def _call(self, node: tree_sitter.Node, owner: Entity, recorder: ReferenceRecorder) -> None:
        function = node.child_by_field_name("function")
        parts = self.dotted(function)
        if parts is None or parts == ("super",):  # super() alone only reaches the parent
            return
        recorder.reference(RelationshipType.CALLS, owner, node, parts, node_text(function))

    # ----- Variable types -----

    def _assignment(
        self, node: tree_sitter.Node, owner: Entity, recorder: ReferenceRecorder
    ) -> None:
        left = node.child_by_field_name("left")
        if left is None or is_broken(node):
            return
        if left.type == "identifier":
            scope_id, name = owner.id, node_text(left)
        else:
            target = self.dotted(left)
            if target is None or len(target) != 2 or target[0] != SELF:
                return  # only `name = ...` and `self.name = ...` are tracked
            scope_id, name = class_scope(owner), target[1]
        recorder.variable(scope_id, name, self._assigned_type(node))

    def _assigned_type(self, node: tree_sitter.Node) -> DottedName | None:
        annotation = node.child_by_field_name("type")
        if annotation is not None:
            return self._single_type(annotation)
        value = node.child_by_field_name("right")
        if value is not None and value.type == "call":
            # `user = User()`: if User turns out to be a class, user is a User.
            return self.dotted(value.child_by_field_name("function"))
        return None

    def _parameter(
        self, node: tree_sitter.Node, owner: Entity, recorder: ReferenceRecorder
    ) -> None:
        name_node = node.child_by_field_name("name")
        if name_node is None:  # typed_parameter has no "name" field
            name_node = next((c for c in node.named_children if c.type == "identifier"), None)
        annotation = node.child_by_field_name("type")
        if name_node is None or annotation is None or is_broken(node):
            return
        recorder.variable(owner.id, node_text(name_node), self._single_type(annotation))

    def _single_type(self, annotation: tree_sitter.Node) -> DottedName | None:
        """The type of `x: User` (a generic such as list[User] counts as `list`)."""
        names = self._type_names(annotation.named_children[:1])
        return names[0][1] if len(names) == 1 else None

    def _type_names(
        self, nodes: list[tree_sitter.Node]
    ) -> list[tuple[tree_sitter.Node, DottedName]]:
        """The names written directly in a type annotation (not in its generic arguments)."""
        names: list[tuple[tree_sitter.Node, DottedName]] = []
        for node in nodes:
            if node.type in ("identifier", "attribute"):
                parts = self.dotted(node)
            elif node.type == "string":  # forward reference: "User"
                parts = split_dotted(string_content(node))
            elif node.type == "generic_type":  # Optional[User] -> Optional
                names.extend(self._type_names(node.named_children[:1]))
                continue
            elif node.type == "binary_operator":  # User | None
                names.extend(self._type_names(node.named_children))
                continue
            else:
                continue
            if parts is not None:
                names.append((node, parts))
        return names
