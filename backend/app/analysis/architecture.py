"""Facts about a project's architecture, computed from its knowledge graph.

Every fact is a count or a list taken from the graph (nodes, relationships, file
paths): nothing is guessed. The facts are numbered; the LLM summary (app/llm/
architecture.py) may only restate them and cite their numbers, and each fact keeps
the entities it is about, so a reader can go from a sentence to the code.

A "module" here is simply a directory: the first two segments of a file's path
("backend/routers", "frontend/src"), or the first one for files near the root. That
is a fact about the layout, not a claim about the design.
"""

from collections import Counter
from dataclasses import dataclass

from app.analysis.insights import DependencyAnalysis
from app.graph.models import EntityResult, ProjectGraph

MAX_LISTED = 8  # items named in one fact
MODULE_DEPTH = 2


@dataclass(frozen=True)
class Fact:
    number: int
    text: str
    entities: tuple[EntityResult, ...] = ()  # what the fact is about (traceability)


def module_of(file_path: str) -> str:
    """The directory a file belongs to, at most MODULE_DEPTH segments deep."""
    directories = file_path.split("/")[:-1]
    return "/".join(directories[:MODULE_DEPTH]) or "(project root)"


def architecture_facts(graph: ProjectGraph, dependencies: DependencyAnalysis) -> list[Fact]:
    """The numbered facts of a project. An empty graph gives no fact."""
    files = [node for node in graph.nodes if node.entity_type == "file"]
    if not files:
        return []
    texts: list[tuple[str, tuple[EntityResult, ...]]] = []

    # Size.
    languages = Counter(file.language for file in files)
    kinds = Counter(node.entity_type for node in graph.nodes if node.entity_type != "file")
    texts.append((
        f"The project has {len(files)} source files ({_counts(languages)}) defining "
        f"{_counts(kinds, plural=True) or 'no classes or functions'}.", (),
    ))  # fmt: skip
    if dependencies.partial:
        texts.append((
            f"Only part of the project was analyzed: {len(graph.nodes)} of {graph.total_nodes} "
            "entities. The facts below describe that part.", (),
        ))  # fmt: skip

    # Layout.
    modules = Counter(module_of(file.file_path) for file in files)
    texts.append((
        "Files per directory: " + _named(
            (f"{name} ({count})" for name, count in modules.most_common()), len(modules)
        ) + ".", (),
    ))  # fmt: skip

    # Dependencies between directories.
    between: Counter[tuple[str, str]] = Counter()
    for dependency in dependencies.dependencies:
        source, target = module_of(dependency.source.file_path), module_of(dependency.target.file_path)
        if source != target:
            between[source, target] += 1
    if between:
        texts.append((
            "File dependencies between directories (number of file-to-file dependencies): "
            + _named((f"{source} -> {target} ({count})"
                      for (source, target), count in between.most_common()), len(between)) + ".", (),
        ))  # fmt: skip
    texts.append((
        f"There are {dependencies.dependency_count} file-to-file dependencies in total "
        "(a file imports another, or its code calls, uses or extends code of another).", (),
    ))  # fmt: skip

    # Hubs.
    if dependencies.file_hubs:
        hubs = dependencies.file_hubs[:MAX_LISTED]
        texts.append((
            "Files most depended on (number of files that depend on them): "
            + ", ".join(f"{hub.entity.file_path} ({hub.incoming})" for hub in hubs) + ".",
            tuple(hub.entity for hub in hubs),
        ))  # fmt: skip
    if dependencies.entity_hubs:
        hubs = dependencies.entity_hubs[:MAX_LISTED]
        texts.append((
            "Most referenced classes and functions (number of entities that call, use or "
            "extend them): " + ", ".join(
                f"{hub.entity.qualified_name} in {hub.entity.file_path} ({hub.incoming})"
                for hub in hubs
            ) + ".",
            tuple(hub.entity for hub in hubs),
        ))  # fmt: skip

    # Files nothing depends on, but that depend on others: where execution may start.
    sources = {dependency.source.id for dependency in dependencies.dependencies}
    starts = [file for file in dependencies.files_without_dependents if file.id in sources]
    if starts:
        texts.append((
            "Files that depend on other files while no file depends on them (possible entry "
            "points; the graph cannot confirm it): "
            + _named((file.file_path for file in starts), len(starts)) + ".",
            tuple(starts[:MAX_LISTED]),
        ))  # fmt: skip

    # Largest classes.
    by_id = {node.id: node for node in graph.nodes}
    methods = Counter(
        edge.source_id for edge in graph.edges
        if edge.type == "CONTAINS" and edge.target_id in by_id
        and by_id[edge.target_id].entity_type == "method"
    )  # fmt: skip
    largest = [(by_id[class_id], count) for class_id, count in methods.most_common(MAX_LISTED)
               if class_id in by_id]  # fmt: skip
    if largest:
        texts.append((
            "Classes with the most methods: " + ", ".join(
                f"{entity.qualified_name} in {entity.file_path} ({count})" for entity, count in largest
            ) + ".",
            tuple(entity for entity, _ in largest),
        ))  # fmt: skip

    # Relationship totals, and the class hierarchy.
    relationships = Counter(edge.type for edge in graph.edges)
    texts.append((
        "Relationships found in the code: " + ", ".join(
            f"{count} {name}" for name, count in sorted(relationships.items())
        ) + ".", (),
    ))  # fmt: skip

    # Circular dependencies.
    if dependencies.cycles:
        first = dependencies.cycles[0]
        loop = " -> ".join([*(file.file_path for file in first), first[0].file_path])
        texts.append((
            f"There are {len(dependencies.cycles)}{'+' if dependencies.cycles_truncated else ''} "
            f"circular dependencies between files, for example: {loop}.", tuple(first),
        ))  # fmt: skip
    else:
        texts.append(("No circular dependency between files was found.", ()))

    return [Fact(number, text, entities) for number, (text, entities) in enumerate(texts, start=1)]


def _counts(counter: Counter[str], plural: bool = False) -> str:
    def name(key: str, count: int) -> str:
        if not plural or count == 1:
            return key
        return f"{key}es" if key.endswith("s") else f"{key}s"

    return ", ".join(f"{count} {name(key, count)}" for key, count in counter.most_common())


def _named(items: object, total: int) -> str:
    listed = list(items)[:MAX_LISTED]  # type: ignore[call-overload]
    more = total - len(listed)
    return ", ".join(listed) + (f" and {more} more" if more > 0 else "")
