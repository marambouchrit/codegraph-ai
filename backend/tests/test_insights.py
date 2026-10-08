"""Dependency analysis: file dependencies, the most used code, and circular dependencies."""

from app.analysis.insights import analyze_dependencies, find_cycles
from app.graph.builder import graph_edges, graph_nodes
from app.graph.models import EntityResult, ProjectGraph, RelationshipResult
from tests.helpers import FILES, PROJECT_ID, analyze


def project_graph(files: dict[str, str]) -> ProjectGraph:
    """The graph of the files, as Neo4j would return it (built in memory, no database)."""
    report = analyze(files)
    nodes = tuple(EntityResult.from_properties(node.properties) for node in graph_nodes(report))
    edges = tuple(
        RelationshipResult.from_record(edge.type, edge.properties, edge.source_id, edge.target_id)
        for edge in graph_edges(report)
    )
    return ProjectGraph(PROJECT_ID, nodes, edges, False, False, len(nodes), len(edges))


def test_file_dependencies_come_from_the_graph() -> None:
    analysis = analyze_dependencies(project_graph(FILES))

    assert [(d.source.file_path, d.target.file_path) for d in analysis.dependencies] == [
        ("auth/service.py", "repository/user.py"),
        ("repository/user.py", "db/database.py"),
    ]
    assert analysis.cycles == ()
    # The files most depended on, and the files nothing depends on (entry points, or unused).
    assert {hub.entity.file_path for hub in analysis.file_hubs} == {"repository/user.py", "db/database.py"}
    assert {f.file_path for f in analysis.files_without_dependents} == {"auth/service.py", "reports/charts.py"}


def test_a_circular_import_is_reported_as_a_cycle() -> None:
    files = {
        "a.py": "from b import fb\n\n\ndef fa():\n    return fb()\n",
        "b.py": "from a import fa\n\n\ndef fb():\n    return fa()\n",
    }

    analysis = analyze_dependencies(project_graph(files))

    assert [[file.file_path for file in cycle] for cycle in analysis.cycles] == [["a.py", "b.py"]]


def test_cycle_detection() -> None:
    assert find_cycles({"a": ["b"], "b": ["c"], "c": []}) == []  # a chain is not a cycle
    assert find_cycles({"a": ["b"], "b": ["a"]}) == [["a", "b"]]
    assert find_cycles({"b": ["c"], "c": ["a"], "a": ["b"]}) == [["a", "b", "c"]]
    # Two separate cycles, and a node outside any cycle.
    graph = {"a": ["b"], "b": ["a"], "x": ["y"], "y": ["z"], "z": ["x"], "lonely": ["a"]}
    assert find_cycles(graph) == [["a", "b"], ["x", "y", "z"]]


def test_cycle_detection_does_not_use_recursion() -> None:
    """A 5,000-file import chain would exceed Python's recursion limit."""
    chain = {str(i): [str(i + 1)] for i in range(5000)}
    chain["5000"] = ["0"]

    [cycle] = find_cycles(chain, sort_key=int)

    assert len(cycle) == 5001
