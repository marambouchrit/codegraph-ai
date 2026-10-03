"""Graph retrieval: answer structural questions about a project from its Neo4j graph.

    GraphRetrievalService   validation, defaults, "not found" errors     (Phase 7)
            ↓
    GraphRepository         the Cypher: fixed read queries per question  (Phases 6-7)
            ↓
    Neo4jClient.read()      read transactions, driver error translation  (Phase 6)

This service contains no Cypher. It checks every argument before anything reaches
Neo4j (project ID format, entity ID of the same project, depth, limit, search
text), then asks the repository. Results are the typed models of
`app/graph/models.py`, never raw Neo4j records.

Usage (the client owns the Neo4j connection, so close it when done):

    with Neo4jClient.from_settings(settings) as client:
        retrieval = GraphRetrievalService(GraphRepository(client))
        [user] = retrieval.find_entities(project_id, "User", entity_types=[EntityType.CLASS])
        methods = retrieval.get_contained_entities(project_id, user.id)
        callers = retrieval.get_callers(project_id, methods[0].entity.id)

"Nothing found" and "unknown entity" are different answers: a query about an
existing entity with no result returns an empty list, while an entity ID that is
not in the project's graph raises EntityNotFoundError (404).
"""

from collections.abc import Callable, Iterable, Sequence

from app.core.errors import EntityNotFoundError, InvalidGraphQueryError, ProjectNotFoundError
from app.extraction.models import EntityType
from app.graph.models import (
    Direction,
    EntityResult,
    GraphContext,
    GraphPath,
    ImpactedEntity,
    ImpactResult,
    ProjectGraph,
    RelatedEntity,
)
from app.graph.repository import MAX_TRAVERSAL_DEPTH, GraphRepository
from app.graph.schema import CONTAINS, RELATIONSHIP_TYPES
from app.ingestion.workspace import is_valid_project_id
from app.relationships.models import RelationshipType

DEFAULT_LIMIT = 50  # entities returned by one question
MAX_LIMIT = 200
DEFAULT_TRANSITIVE_DEPTH = 3  # "what does auth.py depend on, indirectly?"
DEFAULT_PATH_DEPTH = 4  # "how is login connected to User.save?"
DEFAULT_PATH_LIMIT = 5
MAX_PATH_LIMIT = 20  # a path carries several entities: fewer paths than entities
MAX_SEARCH_LENGTH = 500
MAX_ID_LENGTH = 4096  # entity IDs contain a file path and a qualified name
DEFAULT_GRAPH_NODES = 150  # whole-project graph for display: readable in a browser
MAX_GRAPH_NODES = 500
MAX_GRAPH_EDGES = 2000  # between the returned nodes
# Whole-project analyses (dependencies, architecture) read more than a display needs,
# but still a bounded amount: a larger project is analyzed partially, and says so.
ANALYSIS_GRAPH_NODES = 5000
ANALYSIS_GRAPH_EDGES = 20000
# "Who references this?": every relationship that makes its source depend on its target.
IMPACT_TYPES = tuple(sorted(relationship_type.value for relationship_type in RelationshipType))
DEFAULT_IMPACT_DEPTH = 3
MAX_IMPACT_ENTITIES = 300


class GraphRetrievalService:
    def __init__(self, repository: GraphRepository) -> None:
        self.repository = repository

    # ----- Whole project -----

    def get_project_graph(
        self, project_id: str, limit: int = DEFAULT_GRAPH_NODES
    ) -> ProjectGraph:
        """A bounded, deterministic view of the project's graph, for display.

        Up to `limit` nodes (files first, then classes and interfaces, functions,
        methods) and up to MAX_GRAPH_EDGES edges between them; `nodes_truncated` and
        `edges_truncated` say whether the project has more. An unanalyzed project has
        an empty graph.
        """
        return self.repository.get_project_graph(
            _project(project_id),
            node_limit=_limit(limit, MAX_GRAPH_NODES),
            edge_limit=MAX_GRAPH_EDGES,
        )

    def get_analysis_graph(self, project_id: str) -> ProjectGraph:
        """The project's graph for whole-project analyses: same query, larger bounds."""
        return self.repository.get_project_graph(
            _project(project_id),
            node_limit=ANALYSIS_GRAPH_NODES,
            edge_limit=ANALYSIS_GRAPH_EDGES,
        )

    # ----- Impact -----

    def get_impact(
        self,
        project_id: str,
        entity_id: str,
        *,
        max_depth: int = DEFAULT_IMPACT_DEPTH,
        limit: int = MAX_IMPACT_ENTITIES,
    ) -> ImpactResult:
        """What may be affected if an entity changes: who references it, and who
        references those, up to `max_depth` steps away.

        A breadth-first walk over incoming CALLS, USES, INHERITS, IMPLEMENTS, IMPORTS
        and DEPENDS_ON relationships. It starts from the entity and from everything it
        defines (changing a class changes its methods). Each affected entity is
        returned once, at its smallest distance, with the relationship that reaches
        it. Bounded by `max_depth` and `limit`: `truncated` says when more exist.
        """
        depth, limit = _depth(max_depth), _limit(limit, MAX_IMPACT_ENTITIES)
        entity = self.get_entity(project_id, entity_id)
        contained = [
            related.entity
            for related in self.repository.get_contained_entities(
                project_id, entity.id, limit=MAX_LIMIT, max_depth=MAX_TRAVERSAL_DEPTH
            )
        ]
        seen = {entity.id, *(item.id for item in contained)}
        frontier = [entity, *contained]
        affected: list[ImpactedEntity] = []
        truncated = False
        for distance in range(1, depth + 1):
            next_frontier: list[EntityResult] = []
            for target in frontier:
                for related in self.repository.get_related(
                    project_id, target.id, IMPACT_TYPES, Direction.INCOMING, limit=MAX_LIMIT
                ):
                    if related.entity.id in seen or related.relationship is None:
                        continue
                    if len(affected) >= limit:
                        truncated = True
                        break
                    seen.add(related.entity.id)
                    affected.append(ImpactedEntity(related.entity, distance, related.relationship))
                    next_frontier.append(related.entity)
                if truncated:
                    break
            if truncated or not next_frontier:
                break
            frontier = next_frontier
        return ImpactResult(
            entity=entity,
            contained=tuple(contained),
            affected=tuple(affected),
            max_depth=depth,
            truncated=truncated,
        )

    # ----- Entities -----

    def find_entities(
        self,
        project_id: str,
        text: str,
        *,
        entity_types: Sequence[EntityType | str] | None = None,
        partial: bool = False,
        limit: int = DEFAULT_LIMIT,
    ) -> list[EntityResult]:
        """Entities matching `text` exactly by ID, qualified name or name (in that order).

        Every match is returned (two classes named User in two files are both returned),
        never one picked at random. With `partial`, entities whose qualified name contains
        `text` (case-insensitive) come after the exact matches.
        """
        project_id = _project(project_id)
        if not isinstance(text, str) or not text.strip():
            raise InvalidGraphQueryError("The search text must not be empty.")
        if len(text) > MAX_SEARCH_LENGTH:
            raise InvalidGraphQueryError(
                f"The search text must be at most {MAX_SEARCH_LENGTH} characters."
            )
        return self.repository.find_entities(
            project_id,
            text.strip(),
            entity_types=_entity_types(entity_types),
            partial=bool(partial),
            limit=_limit(limit),
        )

    def get_entity(self, project_id: str, entity_id: str) -> EntityResult:
        """One entity with its type, names, file, language, location and parent ID."""
        project_id = _project(project_id)
        entity = self.repository.get_entity(project_id, _entity_id(project_id, entity_id))
        if entity is None:
            raise _not_found(entity_id)
        return entity

    def get_entity_context(
        self, project_id: str, entity_id: str, *, limit: int = DEFAULT_LIMIT
    ) -> GraphContext:
        """An entity, its parent (file or class) and its direct neighbors in both directions."""
        limit = _limit(limit)
        entity = self.get_entity(project_id, entity_id)
        parent = (
            self.repository.get_entity(entity.project_id, entity.parent_id)
            if entity.parent_id is not None
            else None
        )
        neighbors = self.repository.get_neighbors(entity.project_id, entity.id, limit=limit)
        return GraphContext(entity=entity, parent=parent, neighbors=tuple(neighbors))

    def get_neighbors(
        self, project_id: str, entity_id: str, *, limit: int = DEFAULT_LIMIT
    ) -> list[RelatedEntity]:
        """Every entity one relationship away (any type): up to `limit` per direction."""
        limit = _limit(limit)
        return self._related(
            project_id, entity_id, lambda p, e: self.repository.get_neighbors(p, e, limit=limit)
        )

    # ----- Containment -----

    def get_contained_entities(
        self,
        project_id: str,
        entity_id: str,
        *,
        entity_types: Sequence[EntityType | str] | None = None,
        max_depth: int = 1,
        limit: int = DEFAULT_LIMIT,
    ) -> list[RelatedEntity]:
        """What a file or class defines (CONTAINS). "What methods does User have?"

        max_depth > 1 also returns nested definitions (File -> Class -> Method).
        """
        types, depth, limit = _entity_types(entity_types), _depth(max_depth), _limit(limit)
        return self._related(
            project_id, entity_id,
            lambda p, e: self.repository.get_contained_entities(
                p, e, limit=limit, max_depth=depth, entity_types=types
            ),
        )  # fmt: skip

    def get_container(self, project_id: str, entity_id: str) -> list[RelatedEntity]:
        """The class or file defining this entity, with the CONTAINS edge (empty for a file)."""
        return self._related(
            project_id, entity_id,
            lambda p, e: self.repository.get_related(p, e, [CONTAINS], Direction.INCOMING, limit=1),
        )  # fmt: skip

    # ----- Calls -----

    def get_callers(
        self, project_id: str, entity_id: str, *, limit: int = DEFAULT_LIMIT
    ) -> list[RelatedEntity]:
        """Who calls this function or method (or creates this class)? "Who calls User.save?" """
        limit = _limit(limit)
        return self._related(
            project_id, entity_id, lambda p, e: self.repository.get_callers(p, e, limit=limit)
        )

    def get_callees(
        self, project_id: str, entity_id: str, *, limit: int = DEFAULT_LIMIT
    ) -> list[RelatedEntity]:
        """What does this function or method call? "What does login call?" """
        limit = _limit(limit)
        return self._related(
            project_id, entity_id, lambda p, e: self.repository.get_callees(p, e, limit=limit)
        )

    # ----- Imports and dependencies (between files) -----

    def get_imports(
        self, project_id: str, file_id: str, *, limit: int = DEFAULT_LIMIT
    ) -> list[RelatedEntity]:
        """Project files this file imports. "What does auth.py import?" """
        limit = _limit(limit)
        return self._related(
            project_id, file_id, lambda p, e: self.repository.get_imports(p, e, limit=limit)
        )

    def get_importers(
        self, project_id: str, file_id: str, *, limit: int = DEFAULT_LIMIT
    ) -> list[RelatedEntity]:
        """Project files importing this file. "Which files import user.py?" """
        limit = _limit(limit)
        return self._related(
            project_id, file_id, lambda p, e: self.repository.get_importers(p, e, limit=limit)
        )

    def get_dependencies(
        self, project_id: str, file_id: str, *, limit: int = DEFAULT_LIMIT
    ) -> list[RelatedEntity]:
        """Files this file directly depends on (DEPENDS_ON), with where the dependency is."""
        return self._dependencies(project_id, file_id, 1, Direction.OUTGOING, limit)

    def get_transitive_dependencies(
        self,
        project_id: str,
        file_id: str,
        *,
        max_depth: int = DEFAULT_TRANSITIVE_DEPTH,
        limit: int = DEFAULT_LIMIT,
    ) -> list[RelatedEntity]:
        """Files reached by following DEPENDS_ON up to `max_depth` times, closest first."""
        return self._dependencies(project_id, file_id, max_depth, Direction.OUTGOING, limit)

    def get_dependents(
        self,
        project_id: str,
        file_id: str,
        *,
        max_depth: int = 1,
        limit: int = DEFAULT_LIMIT,
    ) -> list[RelatedEntity]:
        """Files that depend on this one (directly, or up to `max_depth` steps away)."""
        return self._dependencies(project_id, file_id, max_depth, Direction.INCOMING, limit)

    # ----- Inheritance and implementation -----

    def get_parents(
        self, project_id: str, entity_id: str, *, limit: int = DEFAULT_LIMIT
    ) -> list[RelatedEntity]:
        """Base classes (or extended interfaces). "What does Admin inherit from?" """
        return self._inheritance(project_id, entity_id, Direction.OUTGOING, limit)

    def get_subclasses(
        self, project_id: str, entity_id: str, *, limit: int = DEFAULT_LIMIT
    ) -> list[RelatedEntity]:
        """Direct subclasses (or sub-interfaces). "What classes inherit from User?" """
        return self._inheritance(project_id, entity_id, Direction.INCOMING, limit)

    def get_implemented_interfaces(
        self, project_id: str, entity_id: str, *, limit: int = DEFAULT_LIMIT
    ) -> list[RelatedEntity]:
        """Interfaces a class implements."""
        return self._implementations(project_id, entity_id, Direction.OUTGOING, limit)

    def get_implementations(
        self, project_id: str, entity_id: str, *, limit: int = DEFAULT_LIMIT
    ) -> list[RelatedEntity]:
        """Classes implementing an interface. "Which classes implement IService?" """
        return self._implementations(project_id, entity_id, Direction.INCOMING, limit)

    # ----- Paths -----

    def find_paths(
        self,
        project_id: str,
        source_id: str,
        target_id: str,
        *,
        max_depth: int = DEFAULT_PATH_DEPTH,
        relationship_types: Iterable[str] | None = None,
        directed: bool = True,
        limit: int = DEFAULT_PATH_LIMIT,
    ) -> list[GraphPath]:
        """How are two entities connected? "How is login connected to User.save?"

        Returns the shortest paths (at most `max_depth` relationships, at most `limit`
        paths, always in the same order), or an empty list if there is none. `directed`
        paths follow the arrows; undirected ones also go against them (login and
        User.save both called by the same function are then connected).
        `relationship_types` restricts the relationships followed (default: all).
        """
        project_id = _project(project_id)
        source_id = _entity_id(project_id, source_id)
        target_id = _entity_id(project_id, target_id)
        types = _relationship_types(relationship_types)
        depth, limit = _depth(max_depth), _limit(limit, MAX_PATH_LIMIT)
        if source_id == target_id:
            return [GraphPath(nodes=(self.get_entity(project_id, source_id),), relationships=())]

        paths = self.repository.find_paths(
            project_id, source_id, target_id,
            max_depth=depth, limit=limit, types=types, directed=bool(directed),
        )  # fmt: skip
        if not paths:  # no path, or an unknown end? Only the second one is an error.
            for entity_id in (source_id, target_id):
                self.get_entity(project_id, entity_id)
        return paths

    # ----- Helpers -----

    def _related(
        self,
        project_id: str,
        entity_id: str,
        fetch: Callable[[str, str], list[RelatedEntity]],
    ) -> list[RelatedEntity]:
        """Run `fetch` for a validated entity; an empty result is checked for "not found".

        The common case (something found) costs one query: the entity must exist.
        """
        project_id = _project(project_id)
        entity_id = _entity_id(project_id, entity_id)
        results = fetch(project_id, entity_id)
        if not results and self.repository.get_entity(project_id, entity_id) is None:
            raise _not_found(entity_id)
        return results

    def _dependencies(
        self, project_id: str, file_id: str, max_depth: int, direction: Direction, limit: int
    ) -> list[RelatedEntity]:
        depth, limit = _depth(max_depth), _limit(limit)
        return self._related(
            project_id, file_id,
            lambda p, e: self.repository.get_dependencies(
                p, e, limit=limit, max_depth=depth, direction=direction
            ),
        )  # fmt: skip

    def _inheritance(
        self, project_id: str, entity_id: str, direction: Direction, limit: int
    ) -> list[RelatedEntity]:
        limit = _limit(limit)
        return self._related(
            project_id, entity_id,
            lambda p, e: self.repository.get_inheritance(p, e, direction, limit=limit),
        )  # fmt: skip

    def _implementations(
        self, project_id: str, entity_id: str, direction: Direction, limit: int
    ) -> list[RelatedEntity]:
        limit = _limit(limit)
        return self._related(
            project_id, entity_id,
            lambda p, e: self.repository.get_implementations(p, e, direction, limit=limit),
        )  # fmt: skip


# ----- Validation -----


def _project(project_id: str) -> str:
    # Same rule as the workspace: a project ID is 32 hexadecimal characters.
    if not is_valid_project_id(project_id):
        raise ProjectNotFoundError(f"Project '{project_id}' not found.")
    return project_id


def _entity_id(project_id: str, entity_id: str) -> str:
    """An entity ID of this project. Any other ID is "not found", without asking Neo4j.

    Entity IDs always start with "<project_id>:" (Phase 4), so an ID of another project
    can never be looked up, even by mistake.
    """
    if (
        not isinstance(entity_id, str)
        or len(entity_id) > MAX_ID_LENGTH
        or not entity_id.startswith(f"{project_id}:")
    ):
        raise _not_found(entity_id)
    return entity_id


def _not_found(entity_id: object) -> EntityNotFoundError:
    shown = entity_id if isinstance(entity_id, str) and len(entity_id) <= 200 else "<invalid ID>"
    return EntityNotFoundError(f"Entity '{shown}' not found in this project's graph.")


def _limit(limit: int, maximum: int = MAX_LIMIT) -> int:
    if not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= maximum:
        raise InvalidGraphQueryError(f"limit must be an integer from 1 to {maximum}.")
    return limit


def _depth(max_depth: int) -> int:
    if (
        not isinstance(max_depth, int)
        or isinstance(max_depth, bool)
        or not 1 <= max_depth <= MAX_TRAVERSAL_DEPTH
    ):
        raise InvalidGraphQueryError(
            f"max_depth must be an integer from 1 to {MAX_TRAVERSAL_DEPTH}."
        )
    return max_depth


def _entity_types(entity_types: Sequence[EntityType | str] | None) -> list[EntityType] | None:
    if entity_types is None:
        return None
    if isinstance(entity_types, str):  # "class" would otherwise be read as c, l, a, s, s
        entity_types = [entity_types]
    try:
        types = [EntityType(entity_type) for entity_type in entity_types]
    except ValueError:
        allowed = ", ".join(t.value for t in EntityType)
        raise InvalidGraphQueryError(f"Unknown entity type (allowed: {allowed}).") from None
    if not types:
        raise InvalidGraphQueryError("entity_types must not be empty (use None for all).")
    return types


def _relationship_types(types: Iterable[str] | None) -> list[str] | None:
    if types is None:
        return None
    if isinstance(types, str):
        types = [types]
    checked = [str(type_) for type_ in types]
    if not checked or any(type_ not in RELATIONSHIP_TYPES for type_ in checked):
        allowed = ", ".join(sorted(RELATIONSHIP_TYPES))
        raise InvalidGraphQueryError(f"Unknown relationship type (allowed: {allowed}).")
    return checked
