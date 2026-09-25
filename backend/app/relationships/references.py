"""Raw facts collected from one syntax tree, before any resolution.

Resolving a name needs the entities of the *whole* project, but a syntax tree is
released as soon as its file has been processed (so memory does not grow with the
project). Relationship extraction therefore works in two steps:

    1. while a file's tree is available, a collector records small facts about it:
       "login calls user.save at line 5", "auth.py imports User from models.user"...
    2. once every file is known, the resolver turns these facts into relationships.

These dataclasses are the facts of step 1. Names are kept as written in the
source, split on dots: `user.save()` gives ("user", "save").
"""

from dataclasses import dataclass, field

from app.extraction.models import Entity
from app.ingestion.languages import Language
from app.relationships.models import RelationshipType

# The receiver of `self.save()` (Python) or `this.save()` (Java, JavaScript, TypeScript),
# and of `super().save()` / `super.save()`. Written with "<>" so that they can never be
# confused with a real name.
SELF = "<self>"
SUPER = "<super>"

# Imported names with a special meaning (see Import.name).
WILDCARD = "*"
DEFAULT_EXPORT = "default"

DottedName = tuple[str, ...]


@dataclass(frozen=True)
class Reference:
    """`source_id` refers to something named `parts` (a call, a base class, a type...)."""

    type: RelationshipType  # CALLS, USES, INHERITS or IMPLEMENTS
    source_id: str  # the nearest enclosing entity (a method, a function, a class or the file)
    parts: DottedName  # e.g. ("User",), ("models", "User"), (SELF, "save"), ("user", "save")
    text: str  # as written, e.g. "this.save", for unresolved references
    line: int
    column: int


@dataclass(frozen=True)
class Import:
    """One imported name (or module) of a file.

    `module` is written in the language's own syntax: "..models.user" (Python),
    "./models/User" (JavaScript/TypeScript), "com.example" (Java).
    `name` is the imported symbol: None for the whole module, WILDCARD for
    `import *`, DEFAULT_EXPORT for a JavaScript default import.
    `alias` is the local name it is bound to (None when nothing is bound, e.g.
    `import "./styles.css"`). It can be dotted: Python `import a.b` binds "a.b".
    """

    module: str
    name: str | None
    alias: str | None
    line: int
    column: int


@dataclass(frozen=True)
class VariableType:
    """A variable whose type is known from the code, e.g. `user = User()` or `User user;`.

    It lets the resolver understand `user.save()` as a call to User.save.
    Fields live in the scope of their class (`self.repo = Repo()`, `private Repo repo;`).
    """

    scope_id: str  # the entity in which the variable is visible
    name: str
    type_parts: DottedName  # the type as written, resolved later like any other name


@dataclass
class FileReferences:
    """Everything a collector found in one file."""

    file: Entity  # the FILE entity
    language: Language
    package: str | None = None  # Java `package com.example;`
    default_export: str | None = None  # JavaScript/TypeScript `export default User`
    imports: list[Import] = field(default_factory=list)
    references: list[Reference] = field(default_factory=list)
    variables: list[VariableType] = field(default_factory=list)

    @property
    def path(self) -> str:
        return self.file.file_path
