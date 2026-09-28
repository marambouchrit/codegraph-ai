"""The shape of the knowledge graph in Neo4j: labels, relationship types, constraints.

    (:Entity:File {id: "p:src/auth.py"})
        -[:CONTAINS]->  (:Entity:Function {id: "p:src/auth.py:login"})
        -[:CALLS]->     (:Entity:Method   {id: "p:src/models/user.py:User.save"})

Every node has two labels: `Entity` (shared by all nodes, carries the uniqueness
constraint) and one label for its type, taken from the Phase 4 `EntityType`.
Relationship types are the Phase 5 `RelationshipType`s, plus CONTAINS, built from
the `parent_id` of entities.

Security: Cypher cannot take a label or a relationship type as a query parameter,
so they are written into the query text. They only ever come from the fixed
values below (never from analyzed code or user input); `node_label()` and
`relationship_type()` refuse anything else.
"""

from app.extraction.models import EntityType
from app.relationships.models import RelationshipType

ENTITY_LABEL = "Entity"

NODE_LABELS: dict[EntityType, str] = {
    EntityType.FILE: "File",
    EntityType.CLASS: "Class",
    EntityType.INTERFACE: "Interface",
    EntityType.FUNCTION: "Function",
    EntityType.METHOD: "Method",
}

# Structural edge from an entity to the entities defined inside it (File -> Class -> Method).
CONTAINS = "CONTAINS"

RELATIONSHIP_TYPES: frozenset[str] = frozenset(
    {relationship_type.value for relationship_type in RelationshipType} | {CONTAINS}
)

# Created once per database, with IF NOT EXISTS so running them again changes nothing.
SCHEMA_STATEMENTS: tuple[str, ...] = (
    # One node per entity ID. The constraint also creates the index used by every
    # MERGE and MATCH on `id`, so no separate index on `id` is needed. Entity IDs already
    # start with the project ID, so a (project_id, id) constraint would add nothing.
    f"CREATE CONSTRAINT entity_id IF NOT EXISTS FOR (n:{ENTITY_LABEL}) REQUIRE n.id IS UNIQUE",
    # Every project-level operation (statistics, deletion, stale cleanup) starts with
    # MATCH (n:Entity {project_id: $project_id}): this index avoids scanning all projects.
    f"CREATE INDEX entity_project_id IF NOT EXISTS FOR (n:{ENTITY_LABEL}) ON (n.project_id)",
)


def node_label(entity_type: EntityType) -> str:
    """The Neo4j label of an entity type ("Class" for EntityType.CLASS)."""
    try:
        return NODE_LABELS[EntityType(entity_type)]
    except (KeyError, ValueError):
        raise ValueError(f"Unknown entity type: {entity_type!r}") from None


def relationship_type(name: str) -> str:
    """Check that `name` is one of our relationship types before it enters a query."""
    if name not in RELATIONSHIP_TYPES:
        raise ValueError(f"Unknown relationship type: {name!r}")
    return name


def contains_id(parent_id: str, child_id: str) -> str:
    """Same format as Phase 5 relationship IDs: <TYPE>:<source_id>-><target_id>."""
    return f"{CONTAINS}:{parent_id}->{child_id}"
