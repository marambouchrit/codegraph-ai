"""Resolve references to entities of the project (step 2 of relationship extraction).

The resolver answers one question: "which entity does this name refer to, here?"

    reference ("user", "save") in auth.py:login
        -> is "user" a variable of known type?     user = User()  -> the class User
        -> which User? follow the imports          from models.user import User
        -> member "save" of that class             -> Method User.save (or a parent's save)

A name is looked up where the language would look for it, in this order:

    1. the enclosing entities, innermost first (nested functions, then the file).
       In Java, the members of the enclosing classes too (`save()` means `this.save()`).
    2. the names bound by the file's imports, following re-exports.
    3. Java only: the types of the same package, then `import pkg.*` packages.
       Python/JavaScript: `from x import *` / `export * from` modules.

Only explicit evidence is used: a definition or an import. There is no guessing from a
name found somewhere else in the project. When nothing matches, or several entities
match equally well, the result is an `UnresolvedReason` instead of an entity.
"""

from collections import defaultdict
from collections.abc import Iterable, Sequence

from app.extraction.models import Entity, EntityType, FileEntities
from app.ingestion.languages import Language
from app.relationships.models import RelationshipType, UnresolvedReason
from app.relationships.modules import ModuleIndex
from app.relationships.references import (
    DEFAULT_EXPORT,
    SELF,
    SUPER,
    WILDCARD,
    DottedName,
    FileReferences,
    Import,
    Reference,
)

Resolution = Entity | UnresolvedReason
Kinds = frozenset[EntityType] | None  # None: any kind of entity

TYPES = frozenset({EntityType.CLASS, EntityType.INTERFACE})
# What the left part of `a.b` can be: something that has members.
CONTAINERS = frozenset({EntityType.FILE, EntityType.CLASS, EntityType.INTERFACE})
TARGET_KINDS: dict[RelationshipType, frozenset[EntityType]] = {
    RelationshipType.CALLS: frozenset({EntityType.FUNCTION, EntityType.METHOD, EntityType.CLASS}),
    # A JSX component (<UserCard />) is often a function.
    RelationshipType.USES: frozenset({EntityType.CLASS, EntityType.INTERFACE, EntityType.FUNCTION}),
    RelationshipType.INHERITS: TYPES,
    RelationshipType.IMPLEMENTS: TYPES,
}

# Limits that protect against cycles in the analyzed code (a = b.x(); b = a.y(), or two
# modules re-exporting each other). Real code never needs more.
MAX_DEPTH = 8

# Marks a variable assigned values of different types: its type is unknown.
CONFLICTING_TYPES: DottedName = ()


class ReferenceResolver:
    def __init__(self, files: Sequence[FileEntities], references: Iterable[FileReferences]) -> None:
        self._files: dict[str, Entity] = {file.path: file.file for file in files}
        self._entities: dict[str, Entity] = {e.id: e for file in files for e in file.entities}
        self._references: dict[str, FileReferences] = {refs.path: refs for refs in references}
        for file in files:  # a file whose references could not be collected has none
            self._references.setdefault(
                file.path, FileReferences(file=file.file, language=file.language)
            )

        # parent ID -> name -> entities with that name (several: overloads, redefinitions)
        self._children: dict[str, dict[str, list[Entity]]] = defaultdict(lambda: defaultdict(list))
        for entity in self._entities.values():
            if entity.parent_id is not None:
                self._children[entity.parent_id][entity.name].append(entity)

        # scope ID -> variable name -> type
        self._variables: dict[str, dict[str, DottedName]] = defaultdict(dict)
        # file path -> alias -> Import, and the `import *` of each file
        self._bindings: dict[str, dict[str, Import]] = defaultdict(dict)
        self._wildcards: dict[str, list[Import]] = defaultdict(list)
        for refs in self._references.values():
            for variable in refs.variables:
                known = self._variables[variable.scope_id]
                if known.get(variable.name, variable.type_parts) != variable.type_parts:
                    known[variable.name] = CONFLICTING_TYPES
                else:
                    known[variable.name] = variable.type_parts
            for imp in refs.imports:
                if imp.name == WILDCARD:
                    self._wildcards[refs.path].append(imp)
                elif imp.alias:
                    self._bindings[refs.path].setdefault(imp.alias, imp)

        # class ID -> resolved parent classes and interfaces, filled by add_base()
        self._bases: dict[str, list[Entity]] = defaultdict(list)
        self._modules = ModuleIndex(
            ((file.path, file.language) for file in files),
            {path: refs.package for path, refs in self._references.items()},
        )
        self._import_cache: dict[tuple[str, Import], Resolution] = {}

    # ----- Public API -----

    def entity(self, entity_id: str) -> Entity:
        return self._entities[entity_id]

    def file_of(self, entity: Entity) -> Entity:
        return self._files[entity.file_path]

    def add_base(self, entity: Entity, base: Entity) -> None:
        """Record that `entity` inherits from or implements `base` (for member lookups)."""
        if base not in self._bases[entity.id]:
            self._bases[entity.id].append(base)

    def resolve(self, refs: FileReferences, reference: Reference) -> Resolution:
        source = self._entities[reference.source_id]
        kinds = TARGET_KINDS[reference.type]
        result = self._resolve_parts(reference.parts, source, refs, kinds, depth=0)
        return _filter(result, kinds)

    def resolve_import(self, refs: FileReferences, imp: Import) -> Resolution | None:
        """The FILE an import names, or None for a Java package (`import pkg.*`).

        It is the module written in the import, even when the imported name is only
        re-exported there (`import { User } from "./models"` imports models/index.ts).
        """
        if refs.language == Language.JAVA:
            if imp.name == WILDCARD:
                return None  # a package is not a file: its types are linked when used
            target = self._import_target(refs, imp)  # the file that declares the type
            return self.file_of(target) if isinstance(target, Entity) else target
        if refs.language == Language.PYTHON and imp.name not in (None, WILDCARD):
            target = self._import_target(refs, imp)
            if isinstance(target, Entity) and target.type == EntityType.FILE:
                return target  # from . import helpers: the module helpers.py
        return self._import_module(refs, imp)

    # ----- Dotted names -----

    def _resolve_parts(
        self, parts: DottedName, source: Entity, refs: FileReferences, kinds: Kinds, depth: int
    ) -> Resolution:
        if depth > MAX_DEPTH:
            return UnresolvedReason.NOT_FOUND
        head, rest = parts[0], parts[1:]

        if head in (SELF, SUPER):
            owner = self._enclosing_type(source)
            if owner is None or not rest:
                return UnresolvedReason.NOT_FOUND
            if head == SELF:
                return self._members_path(owner, rest, kinds, depth)
            for base in self._bases[owner.id]:  # super.save(): start in the parents
                result = self._members_path(base, rest, kinds, depth)
                if isinstance(result, Entity):
                    return result
            return UnresolvedReason.NOT_FOUND

        variable_type = self._variable(head, source, refs.language) if rest else None
        if variable_type is not None:
            # user.save() with `user = User()`: continue from the class User.
            if variable_type == CONFLICTING_TYPES:
                return UnresolvedReason.UNKNOWN_RECEIVER
            current = self._resolve_parts(variable_type, source, refs, TYPES, depth + 1)
            if not isinstance(current, Entity):
                return _receiver_reason(current)
            return self._members_path(current, rest, kinds, depth)

        current, rest = self._resolve_head(parts, source, refs, kinds if not rest else CONTAINERS)
        if not isinstance(current, Entity):
            return _receiver_reason(current) if rest else current
        return self._members_path(current, rest, kinds, depth)

    def _resolve_head(
        self, parts: DottedName, source: Entity, refs: FileReferences, kinds: Kinds
    ) -> tuple[Resolution, DottedName]:
        """Resolve the first name of `parts`; returns it with the parts left to resolve."""
        found = self._lookup_scopes(parts[0], source, refs.language, kinds)
        if found is not None:
            return found, parts[1:]
        # Python `import a.b` binds the dotted name "a.b": try the longest bound prefix.
        bindings = self._bindings[refs.path]
        for length in range(len(parts), 1, -1):
            imp = bindings.get(".".join(parts[:length]))
            if imp is not None:
                return _filter(self._import_target(refs, imp), kinds), parts[length:]
        return self._lookup_imported(parts[0], refs, kinds), parts[1:]

    def _members_path(
        self, current: Entity, rest: DottedName, kinds: Kinds, depth: int
    ) -> Resolution:
        """Follow `rest` through members: User -> save, or module -> User -> save."""
        for index, part in enumerate(rest):
            last = index == len(rest) - 1
            if not last and current.type in TYPES:
                field_type = self._variables.get(current.id, {}).get(part)
                if field_type is not None:  # self.repo.find(): continue from repo's type
                    refs = self._references[current.file_path]
                    if field_type == CONFLICTING_TYPES:
                        return UnresolvedReason.UNKNOWN_RECEIVER
                    result = self._resolve_parts(field_type, current, refs, TYPES, depth + 1)
                    if not isinstance(result, Entity):
                        return _receiver_reason(result)
                    current = result
                    continue
            result = self._member(current, part, kinds if last else CONTAINERS, depth=0)
            if not isinstance(result, Entity):
                return result
            current = result
        return current

    def _member(self, owner: Entity, name: str, kinds: Kinds, depth: int) -> Resolution:
        if owner.type == EntityType.FILE:
            return self._exported(owner, name, kinds, depth)
        if owner.type in TYPES:
            return _single(_keep(self._members(owner, name), kinds))
        return UnresolvedReason.NOT_FOUND  # functions have no members we track

    # ----- Names -----

    def _lookup_scopes(
        self, name: str, source: Entity, language: Language, kinds: Kinds
    ) -> Resolution | None:
        """Look `name` up in the enclosing entities, innermost first. None: not defined here."""
        scope: Entity | None = source
        while scope is not None:
            if scope.type in TYPES:
                # Class members are visible without `this.` in Java only; in Python and
                # JavaScript, `save()` inside a method never means `self.save()`.
                candidates = self._members(scope, name) if language == Language.JAVA else []
            else:
                candidates = self._children[scope.id].get(name, [])
            candidates = _keep(candidates, kinds)
            if candidates:
                return _single(candidates)
            scope = self._parent(scope)
        return None

    def _lookup_imported(self, name: str, refs: FileReferences, kinds: Kinds) -> Resolution:
        imp = self._bindings[refs.path].get(name)
        if imp is not None:
            return _filter(self._import_target(refs, imp), kinds)
        if refs.language == Language.JAVA:
            # Types of the same package are visible without an import.
            found = self._package_member(refs.package or "", name, kinds)
            if found is not None:
                return found
        for wildcard in self._wildcards[refs.path]:
            found = self._wildcard_member(refs, wildcard, name, kinds, depth=0)
            if found is not None:
                return found
        return UnresolvedReason.NOT_FOUND

    def _variable(self, name: str, source: Entity, language: Language) -> DottedName | None:
        scope: Entity | None = source
        while scope is not None:
            # Same visibility rule as names: fields are reachable without `this.` in Java only.
            if scope.type not in TYPES or language == Language.JAVA:
                variable_type = self._variables.get(scope.id, {}).get(name)
                if variable_type is not None:
                    return variable_type
            scope = self._parent(scope)
        return None

    def _members(self, owner: Entity, name: str) -> list[Entity]:
        """Members of a class named `name`: its own first, then its parents' (breadth first)."""
        queue, seen = [owner], {owner.id}
        while queue:
            current = queue.pop(0)
            found = self._children[current.id].get(name)
            if found:
                return found
            for base in self._bases[current.id]:
                if base.id not in seen:
                    seen.add(base.id)
                    queue.append(base)
        return []

    def _exported(self, file: Entity, name: str, kinds: Kinds, depth: int) -> Resolution:
        """What `name` means when imported from `file`: a definition, or a re-export."""
        candidates = _keep(self._children[file.id].get(name, []), kinds)
        if candidates:
            return _single(candidates)
        if depth > MAX_DEPTH:
            return UnresolvedReason.NOT_FOUND
        refs = self._references[file.file_path]
        imp = self._bindings[file.file_path].get(name)
        if imp is not None:  # from .user import User (in __init__.py), export { User } from
            return _filter(self._import_target(refs, imp), kinds)
        for wildcard in self._wildcards[file.file_path]:
            found = self._wildcard_member(refs, wildcard, name, kinds, depth + 1)
            if found is not None:
                return found
        if refs.language == Language.PYTHON and kinds is not None and EntityType.FILE in kinds:
            # `import pkg` then `pkg.sub.f()`: sub is a module of the package.
            return self._python_submodule(file.file_path, ".", name)
        return UnresolvedReason.NOT_FOUND

    # ----- Imports -----

    def _import_target(self, refs: FileReferences, imp: Import) -> Resolution:
        """The entity an import binds: a module (FILE) or a definition inside it. Cached."""
        key = (refs.path, imp)
        if key not in self._import_cache:
            # Guard against import cycles (a re-exports b, b re-exports a).
            self._import_cache[key] = UnresolvedReason.NOT_FOUND
            self._import_cache[key] = self._find_import_target(refs, imp)
        return self._import_cache[key]

    def _find_import_target(self, refs: FileReferences, imp: Import) -> Resolution:
        if refs.language == Language.JAVA:
            return self._java_qualified(f"{imp.module}.{imp.name}".split("."))
        module = self._import_module(refs, imp)
        name = imp.name
        if name is None or name == WILDCARD:
            return module
        if not isinstance(module, Entity):
            if refs.language == Language.PYTHON:  # from . import helpers (no __init__.py)
                submodule = self._python_submodule(refs.path, imp.module, name)
                return submodule if isinstance(submodule, Entity) else module
            return module
        if name == DEFAULT_EXPORT:
            name = self._references[module.file_path].default_export
            if name is None:
                return UnresolvedReason.NOT_FOUND
        result = self._exported(module, name, None, depth=0)
        if not isinstance(result, Entity) and refs.language == Language.PYTHON:
            submodule = self._python_submodule(refs.path, imp.module, name)
            if isinstance(submodule, Entity):
                return submodule
        return result

    def _import_module(self, refs: FileReferences, imp: Import) -> Resolution:
        if refs.language == Language.PYTHON:
            found = self._modules.python_module(refs.path, imp.module)
        else:
            found = self._modules.script_module(refs.path, imp.module)
        return found if isinstance(found, UnresolvedReason) else self._files[found]

    def _python_submodule(self, importer: str, package: str, name: str) -> Resolution:
        module = f"{package}{name}" if package.endswith(".") else f"{package}.{name}"
        found = self._modules.python_module(importer, module)
        return found if isinstance(found, UnresolvedReason) else self._files[found]

    def _wildcard_member(
        self, refs: FileReferences, imp: Import, name: str, kinds: Kinds, depth: int
    ) -> Entity | None:
        if refs.language == Language.JAVA:
            return self._package_member(imp.module, name, kinds)
        module = self._import_target(refs, imp)
        if not isinstance(module, Entity):
            return None
        found = self._exported(module, name, kinds, depth)
        return found if isinstance(found, Entity) else None

    # ----- Java packages -----

    def _package_member(self, package: str, name: str, kinds: Kinds) -> Resolution | None:
        """A top-level type of a Java package, or None if the package has no such name."""
        candidates = [
            entity
            for path in self._modules.java_package(package)
            for entity in _keep(self._children[self._files[path].id].get(name, []), kinds)
        ]
        return _single(candidates) if candidates else None

    def _java_qualified(self, parts: list[str]) -> Resolution:
        """`com.example.User` or `com.example.Outer.Inner` or `com.example.Util.max`."""
        for split in range(len(parts) - 1, 0, -1):  # the longest known package first
            package = ".".join(parts[:split])
            if not self._modules.is_java_package(package):
                continue
            current = self._package_member(package, parts[split], TYPES)
            if not isinstance(current, Entity):
                return current or UnresolvedReason.NOT_FOUND
            return self._members_path(current, tuple(parts[split + 1 :]), None, depth=0)
        return UnresolvedReason.EXTERNAL  # java.util.List, org.springframework...

    # ----- Helpers -----

    def _parent(self, entity: Entity) -> Entity | None:
        return self._entities.get(entity.parent_id) if entity.parent_id else None

    def _enclosing_type(self, entity: Entity) -> Entity | None:
        current: Entity | None = entity
        while current is not None and current.type not in TYPES:
            current = self._parent(current)
        return current


def _keep(candidates: list[Entity], kinds: Kinds) -> list[Entity]:
    if kinds is None:
        return candidates
    return [entity for entity in candidates if entity.type in kinds]


def _filter(result: Resolution, kinds: Kinds) -> Resolution:
    if isinstance(result, Entity) and kinds is not None and result.type not in kinds:
        return UnresolvedReason.NOT_FOUND
    return result


def _single(candidates: list[Entity]) -> Resolution:
    """The only candidate, or AMBIGUOUS (Java overloads, a Python function defined twice...)."""
    unique = {entity.id: entity for entity in candidates}
    if len(unique) == 1:
        return next(iter(unique.values()))
    return UnresolvedReason.AMBIGUOUS if unique else UnresolvedReason.NOT_FOUND


def _receiver_reason(reason: UnresolvedReason) -> UnresolvedReason:
    """Why `x.method()` failed when `x` itself could not be resolved."""
    if reason == UnresolvedReason.NOT_FOUND:
        return UnresolvedReason.UNKNOWN_RECEIVER  # e.g. an untyped parameter
    return reason  # EXTERNAL (x comes from a library) or AMBIGUOUS
