"""JavaScript references (TypeScriptReferenceCollector extends this collector).

    import Def, { User as U } from "./models/User"   -> Imports (binds "Def" and "U")
    import * as utils from "../utils"                -> Import of the whole module
    import "./styles.css"                            -> Import (binds nothing)
    const lib = require("./lib") / import("./lazy")  -> Import
    export { User } from "./User" / export * from    -> Import (re-export, used by "barrel"
                                                        index files)
    export default User                              -> the file's default export
    class Admin extends User                         -> INHERITS User
    new User() / helper() / user.save() / this.save()-> CALLS (new User(): CALLS User)
    super.save()                                     -> CALLS save in the parent class
    <UserCard />                                     -> USES UserCard (JSX component)

Variable types, so that `user.save()` can be resolved:

    const user = new User()     this.repo = new Repo()     repo = new Repo()  (class field)

Not tracked: `module.exports = ...`, calls through computed properties (`obj[name]()`),
methods of object literals.
"""

import tree_sitter

from app.extraction.models import Entity, EntityType
from app.ingestion.languages import Language
from app.relationships.base import (
    ReferenceCollector,
    ReferenceRecorder,
    class_scope,
    is_broken,
    node_text,
    string_content,
)
from app.relationships.models import RelationshipType
from app.relationships.references import DEFAULT_EXPORT, SELF, WILDCARD, DottedName

JSX_ELEMENTS = frozenset({"jsx_opening_element", "jsx_self_closing_element"})


class JavaScriptReferenceCollector(ReferenceCollector):
    language = Language.JAVASCRIPT

    # Same meaning as in JavaScriptExtractor: TypeScript adds abstract classes.
    class_types = frozenset({"class_declaration"})
    field_types = frozenset({"field_definition"})

    def visit(self, node: tree_sitter.Node, owner: Entity, recorder: ReferenceRecorder) -> None:
        if node.type == "import_statement":
            self._import(node, recorder)
        elif node.type == "export_statement":
            self._export(node, recorder)
        elif node.type == "call_expression":
            self._call(node, owner, recorder)
        elif node.type == "new_expression":
            constructor = node.child_by_field_name("constructor")
            recorder.consume(constructor)
            parts = self.dotted(constructor)
            recorder.reference(RelationshipType.CALLS, owner, node, parts, node_text(constructor))
        elif node.type == "class_heritage" and self._is_named_class(node.parent, owner):
            self._heritage(node, owner, recorder)
        elif node.type == "variable_declarator":
            name = node.child_by_field_name("name")
            if name is not None and name.type == "identifier" and not is_broken(node):
                recorder.variable(owner.id, node_text(name), self.declared_type(node))
        elif node.type in self.field_types and owner.type == EntityType.CLASS:
            name = node.child_by_field_name("property") or node.child_by_field_name("name")
            if name is not None and not is_broken(node):
                recorder.variable(owner.id, node_text(name), self.declared_type(node))
        elif node.type == "assignment_expression":
            self._field_assignment(node, owner, recorder)
        elif node.type in JSX_ELEMENTS:
            self._jsx_component(node, owner, recorder)

    # ----- Imports and exports -----

    def _import(self, node: tree_sitter.Node, recorder: ReferenceRecorder) -> None:
        module = string_content(node.child_by_field_name("source"))
        clause = next((c for c in node.named_children if c.type == "import_clause"), None)
        if clause is None:
            recorder.import_(node, module, None, None)  # import "./side-effects"
            return
        for child in clause.named_children:
            if child.type == "identifier":  # import User from "./User"
                recorder.import_(child, module, DEFAULT_EXPORT, node_text(child))
            elif child.type == "namespace_import":  # import * as utils from "./utils"
                alias = next((c for c in child.named_children if c.type == "identifier"), None)
                recorder.import_(child, module, None, node_text(alias))
            elif child.type == "named_imports":  # import { User as U } from "./User"
                for specifier in child.named_children:
                    self._specifier(specifier, module, recorder)

    def _export(self, node: tree_sitter.Node, recorder: ReferenceRecorder) -> None:
        source = node.child_by_field_name("source")
        if source is not None:
            # A re-export also imports: `export { User } from "./User"` binds User here,
            # so `import { User } from "./models"` (an index file) can be followed.
            module = string_content(source)
            clause = next((c for c in node.named_children if c.type == "export_clause"), None)
            namespace = next((c for c in node.named_children if c.type == "namespace_export"), None)
            if clause is not None:
                for specifier in clause.named_children:
                    self._specifier(specifier, module, recorder)
            elif namespace is not None:  # export * as utils from "./utils"
                alias = namespace.named_children[-1] if namespace.named_children else None
                recorder.import_(node, module, None, node_text(alias))
            else:  # export * from "./utils"
                recorder.import_(node, module, WILDCARD, None)
        elif any(child.type == "default" for child in node.children) and not is_broken(node):
            recorder.result.default_export = _default_export_name(node)

    def _specifier(
        self, specifier: tree_sitter.Node, module: str, recorder: ReferenceRecorder
    ) -> None:
        name = node_text(specifier.child_by_field_name("name"))
        alias = node_text(specifier.child_by_field_name("alias")) or name
        recorder.import_(specifier, module, name, alias)

    # ----- Calls and classes -----

    def _call(self, node: tree_sitter.Node, owner: Entity, recorder: ReferenceRecorder) -> None:
        function = node.child_by_field_name("function")
        if function is None or function.type == "super":  # super(...) in a constructor
            return
        if function.type == "import" or node_text(function) == "require":
            self._dynamic_import(node, recorder)
            return
        parts = self.dotted(function)
        recorder.reference(RelationshipType.CALLS, owner, node, parts, node_text(function))

    def _dynamic_import(self, node: tree_sitter.Node, recorder: ReferenceRecorder) -> None:
        """`require("./lib")` or `import("./lib")`, with a literal module name only."""
        arguments = node.child_by_field_name("arguments")
        first = arguments.named_children[0] if arguments and arguments.named_children else None
        if first is None or first.type != "string":
            return
        alias = None
        parent = node.parent
        if parent is not None and parent.type == "variable_declarator":
            name = parent.child_by_field_name("name")
            if name is not None and name.type == "identifier":
                alias = node_text(name)  # const lib = require("./lib")
        recorder.import_(node, string_content(first), None, alias)

    def _is_named_class(self, node: tree_sitter.Node | None, owner: Entity) -> bool:
        """True if `node` is the class `owner` (not an anonymous class inside it)."""
        if node is None or owner.type != EntityType.CLASS:
            return False
        if node.type in self.class_types:
            return True
        # const Admin = class extends User {}
        return node.type == "class" and node.parent is not None and (
            node.parent.type == "variable_declarator"
        )

    def _heritage(self, node: tree_sitter.Node, owner: Entity, recorder: ReferenceRecorder) -> None:
        for child in node.named_children:
            if child.type == "extends_clause":  # TypeScript: extends Base<T>
                for value in child.children_by_field_name("value"):
                    recorder.reference(RelationshipType.INHERITS, owner, value, self.dotted(value))
            elif child.type == "implements_clause":  # TypeScript: implements A, B<T>
                for type_node in child.named_children:
                    head = self.type_head(type_node)
                    recorder.consume(head)
                    recorder.reference(
                        RelationshipType.IMPLEMENTS, owner, type_node, self.dotted(head)
                    )
            else:  # JavaScript: extends Base / extends models.Base
                recorder.reference(RelationshipType.INHERITS, owner, child, self.dotted(child))

    # ----- Variable types -----

    def declared_type(self, node: tree_sitter.Node) -> DottedName | None:
        """The type of a variable or field: `new User()` in JavaScript (TypeScript adds
        type annotations)."""
        value = node.child_by_field_name("value")
        if value is not None and value.type == "new_expression":
            return self.dotted(value.child_by_field_name("constructor"))
        return None

    def type_head(self, node: tree_sitter.Node) -> tree_sitter.Node:
        """`Base<T>` -> `Base` (TypeScript generics)."""
        if node.type == "generic_type":
            return node.child_by_field_name("name") or node
        return node

    def _field_assignment(
        self, node: tree_sitter.Node, owner: Entity, recorder: ReferenceRecorder
    ) -> None:
        """`this.repo = new Repo()` makes `repo` a field of the enclosing class."""
        target = self.dotted(node.child_by_field_name("left"))
        value = node.child_by_field_name("right")
        if target is None or len(target) != 2 or target[0] != SELF or value is None:
            return
        if value.type == "new_expression" and not is_broken(node):
            recorder.variable(
                class_scope(owner), target[1], self.dotted(value.child_by_field_name("constructor"))
            )

    # ----- JSX -----

    def _jsx_component(
        self, node: tree_sitter.Node, owner: Entity, recorder: ReferenceRecorder
    ) -> None:
        name = node.child_by_field_name("name")
        parts = self.dotted(name)
        # <div> is an HTML tag; <UserCard> and <ui.Card> are components.
        if parts is not None and (len(parts) > 1 or parts[0][:1].isupper()):
            recorder.reference(RelationshipType.USES, owner, name, parts)


def _default_export_name(node: tree_sitter.Node) -> str | None:
    """`export default class User {}`, `export default function f() {}`, `export default User`."""
    declaration = node.child_by_field_name("declaration")
    if declaration is not None:
        name = declaration.child_by_field_name("name")
        return node_text(name) or None
    value = node.child_by_field_name("value")
    if value is not None and value.type == "identifier":
        return node_text(value)
    return None
