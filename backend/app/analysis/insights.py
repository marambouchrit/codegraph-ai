"""Deterministic analyses of a project's knowledge graph: dependencies, cycles, hubs.

Pure functions over a `ProjectGraph` (nodes and relationships already read from
Neo4j by graph retrieval): no database, no LLM, no randomness. The same graph always
gives the same result, and every number is a real count of relationships.

    file dependency   file A IMPORTS or DEPENDS_ON file B
    reference         an entity CALLS, USES, INHERITS or IMPLEMENTS another

What the graph cannot prove is named as such: an entity with no incoming reference
has "no detected references". It may still be used (called dynamically, by a
framework, from outside the project, or through a call the resolver could not
resolve), so it is never called dead code.
"""

from collections import Counter, deque
from collections.abc import Iterable, Mapping
from dataclasses import dataclass

from app.graph.models import EntityResult, ProjectGraph

FILE_DEPENDENCY_TYPES = frozenset({"IMPORTS", "DEPENDS_ON"})
REFERENCE_TYPES = frozenset({"CALLS", "USES", "INHERITS", "IMPLEMENTS"})

MAX_DEPENDENCIES = 300  # listed by the API (all are analyzed and counted)
MAX_CYCLES = 50
MAX_HUBS = 10
MAX_UNREFERENCED = 100


@dataclass(frozen=True)
class FileDependency:
    source: EntityResult  # this file...
    target: EntityResult  # ...depends on this one
    types: tuple[str, ...]  # "DEPENDS_ON", "IMPORTS" or both


@dataclass(frozen=True)
class Hub:
    entity: EntityResult
    incoming: int  # distinct files (file hubs) or entities (entity hubs) that point to it


@dataclass(frozen=True)
class DependencyAnalysis:
    files: int
    dependencies: tuple[FileDependency, ...]  # all of them, in a fixed order
    dependency_count: int  # all of them
    cycles: tuple[tuple[EntityResult, ...], ...]  # each: A, B, C meaning A -> B -> C -> A
    cycles_truncated: bool
    file_hubs: tuple[Hub, ...]  # files most depended on
    entity_hubs: tuple[Hub, ...]  # classes, functions... most referenced
    unreferenced: tuple[EntityResult, ...]  # no detected reference (up to MAX_UNREFERENCED)
    unreferenced_count: int
    files_without_dependents: tuple[EntityResult, ...]  # no file depends on them
    partial: bool  # the project is larger than what was analyzed


def analyze_dependencies(graph: ProjectGraph) -> DependencyAnalysis:
    nodes = {node.id: node for node in graph.nodes}
    files = {node.id: node for node in graph.nodes if node.entity_type == "file"}

    # File dependency edges, merged per (source, target).
    types: dict[tuple[str, str], set[str]] = {}
    for edge in graph.edges:
        if (
            edge.type in FILE_DEPENDENCY_TYPES
            and edge.source_id in files and edge.target_id in files
            and edge.source_id != edge.target_id
        ):  # fmt: skip
            types.setdefault((edge.source_id, edge.target_id), set()).add(edge.type)
    pairs = sorted(types, key=lambda pair: (files[pair[0]].file_path, files[pair[1]].file_path))
    adjacency: dict[str, set[str]] = {file_id: set() for file_id in files}
    dependents: Counter[str] = Counter()
    for source, target in pairs:
        adjacency[source].add(target)
        dependents[target] += 1

    cycles = find_cycles(adjacency, sort_key=lambda file_id: files[file_id].file_path)

    # Entities referenced by code (distinct sources per target).
    referencing: dict[str, set[str]] = {}
    for edge in graph.edges:
        if edge.type in REFERENCE_TYPES and edge.source_id in nodes and edge.target_id in nodes:
            referencing.setdefault(edge.target_id, set()).add(edge.source_id)
    code = [node for node in graph.nodes if node.entity_type != "file"]
    unreferenced = sorted(
        (node for node in code if node.id not in referencing), key=_location
    )

    return DependencyAnalysis(
        files=len(files),
        dependencies=tuple(
            FileDependency(files[source], files[target], tuple(sorted(types[source, target])))
            for source, target in pairs
        ),
        dependency_count=len(pairs),
        cycles=tuple(tuple(files[file_id] for file_id in cycle) for cycle in cycles[:MAX_CYCLES]),
        cycles_truncated=len(cycles) > MAX_CYCLES,
        file_hubs=_hubs(files, dependents),
        entity_hubs=_hubs(
            {node.id: node for node in code},
            Counter({target: len(sources) for target, sources in referencing.items()}),
        ),
        unreferenced=tuple(unreferenced[:MAX_UNREFERENCED]),
        unreferenced_count=len(unreferenced),
        files_without_dependents=tuple(
            sorted((file for file_id, file in files.items() if dependents[file_id] == 0),
                   key=_location)
        ),  # fmt: skip
        partial=graph.nodes_truncated or graph.edges_truncated,
    )


def _location(entity: EntityResult) -> tuple[str, int, str]:
    return entity.file_path, entity.start_line, entity.id


def _hubs(entities: Mapping[str, EntityResult], incoming: Counter[str]) -> tuple[Hub, ...]:
    """The entities with the most incoming relationships (ties: by location, so it is stable)."""
    ranked = sorted(
        ((count, entities[entity_id]) for entity_id, count in incoming.items()
         if count > 0 and entity_id in entities),
        key=lambda item: (-item[0], _location(item[1])),
    )  # fmt: skip
    return tuple(Hub(entity, count) for count, entity in ranked[:MAX_HUBS])


# ----- Cycles -----


def find_cycles(adjacency: Mapping[str, Iterable[str]], sort_key=lambda node: node) -> list[list[str]]:  # type: ignore[no-untyped-def]
    """The circular dependencies of a directed graph, as readable paths.

    A cycle is returned as [A, B, C] for A -> B -> C -> A, starting with its smallest
    node (by `sort_key`), each cycle once. Nodes can only be in a cycle together when
    they are in the same strongly connected component, so those are found first
    (Tarjan's algorithm); then, in each component, the shortest cycle through every
    node is taken. That lists the distinct short cycles of a component without
    enumerating every possible loop, whose number can explode.
    """
    cycles: dict[tuple[str, ...], list[str]] = {}
    for component in _strongly_connected_components(adjacency):
        if len(component) < 2:
            continue
        members = set(component)
        for start in sorted(component, key=sort_key):
            cycle = _shortest_cycle(adjacency, start, members, sort_key)
            if cycle:
                smallest = min(range(len(cycle)), key=lambda index: sort_key(cycle[index]))
                rotated = cycle[smallest:] + cycle[:smallest]
                cycles.setdefault(tuple(rotated), rotated)
    return sorted(cycles.values(), key=lambda cycle: (len(cycle), [sort_key(n) for n in cycle]))


def _shortest_cycle(adjacency, start: str, members: set[str], sort_key) -> list[str]:  # type: ignore[no-untyped-def]
    """Breadth-first search from `start` back to `start`, inside its component."""
    parents: dict[str, str] = {}
    queue = deque([start])
    while queue:
        node = queue.popleft()
        for neighbor in sorted((n for n in adjacency.get(node, ()) if n in members), key=sort_key):
            if neighbor == start:
                path = [node]
                while path[-1] != start:
                    path.append(parents[path[-1]])
                return path[::-1]
            if neighbor not in parents:
                parents[neighbor] = node
                queue.append(neighbor)
    return []


def _strongly_connected_components(adjacency: Mapping[str, Iterable[str]]) -> list[list[str]]:
    """Tarjan's algorithm, without recursion (a deep import chain must not overflow the stack)."""
    index: dict[str, int] = {}
    lowest: dict[str, int] = {}
    stack: list[str] = []
    on_stack: set[str] = set()
    components: list[list[str]] = []
    for root in sorted(adjacency):
        if root in index:
            continue
        work = [(root, iter(sorted(adjacency.get(root, ()))))]
        index[root] = lowest[root] = len(index)
        stack.append(root)
        on_stack.add(root)
        while work:
            node, neighbors = work[-1]
            advanced = False
            for neighbor in neighbors:
                if neighbor not in index:
                    index[neighbor] = lowest[neighbor] = len(index)
                    stack.append(neighbor)
                    on_stack.add(neighbor)
                    work.append((neighbor, iter(sorted(adjacency.get(neighbor, ())))))
                    advanced = True
                    break
                if neighbor in on_stack:
                    lowest[node] = min(lowest[node], index[neighbor])
            if advanced:
                continue
            work.pop()
            if work:
                parent = work[-1][0]
                lowest[parent] = min(lowest[parent], lowest[node])
            if lowest[node] == index[node]:
                component = []
                while True:
                    member = stack.pop()
                    on_stack.discard(member)
                    component.append(member)
                    if member == node:
                        break
                components.append(component)
    return components
