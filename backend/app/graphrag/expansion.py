"""Which graph questions to ask about a seed entity, depending on its type.

Asking every question about every seed would bury the relevant code under
unrelated neighbors ("vector search + dump the graph"). Each entity type gets the
few one-hop relationships that explain what it is and how it is used:

    method / function   its class or file, what it calls, who calls it
    class               its file, its methods, its base classes, its subclasses,
                        the interfaces it implements, who creates it (CALLS -> class)
    interface           its file, its methods, the interfaces it extends, the
                        interfaces extending it, the classes implementing it
    file                what it defines, the files it depends on, the files
                        depending on it (DEPENDS_ON already covers its imports)

The order of each tuple is the order neighbors are listed in the context: first
what the seed belongs to, then what it does, then who uses it.
"""

from enum import StrEnum


class Expansion(StrEnum):
    CONTAINER = "container"  # the class or file defining the seed (CONTAINS, incoming)
    MEMBERS = "members"  # what the seed defines (CONTAINS, outgoing)
    CALLEES = "callees"  # CALLS, outgoing
    CALLERS = "callers"  # CALLS, incoming (for a class: who creates instances)
    PARENTS = "parents"  # INHERITS, outgoing
    SUBCLASSES = "subclasses"  # INHERITS, incoming
    INTERFACES = "interfaces"  # IMPLEMENTS, outgoing
    IMPLEMENTATIONS = "implementations"  # IMPLEMENTS, incoming
    DEPENDENCIES = "dependencies"  # DEPENDS_ON, outgoing (files)
    DEPENDENTS = "dependents"  # DEPENDS_ON, incoming (files)


_CALLABLE = (Expansion.CONTAINER, Expansion.CALLEES, Expansion.CALLERS)

STRATEGY: dict[str, tuple[Expansion, ...]] = {
    "method": _CALLABLE,
    "function": _CALLABLE,
    "class": (
        Expansion.CONTAINER,
        Expansion.MEMBERS,
        Expansion.PARENTS,
        Expansion.SUBCLASSES,
        Expansion.INTERFACES,
        Expansion.CALLERS,
    ),
    "interface": (
        Expansion.CONTAINER,
        Expansion.MEMBERS,
        Expansion.PARENTS,
        Expansion.SUBCLASSES,
        Expansion.IMPLEMENTATIONS,
    ),
    "file": (Expansion.MEMBERS, Expansion.DEPENDENCIES, Expansion.DEPENDENTS),
}


def expansions_for(entity_type: str) -> tuple[Expansion, ...]:
    """The expansions of an entity type (none for an unknown type)."""
    return STRATEGY.get(entity_type, ())
