"""Pipeline benchmark: analyze one real open-source repository and record the numbers.

    docker compose up -d neo4j
    python -m evaluation.pipeline_benchmark                 (from backend/)
    python -m evaluation.pipeline_benchmark <github url>    (another repository)

The repository goes through the application's own services, like a project imported in the
application:

1. import: shallow clone from GitHub, scan the source files;
2. analyze: parse every file (Tree-sitter), extract the entities, resolve the relationships;
3. graph: write the nodes and relationships to Neo4j, then count what Neo4j holds.

The default repository is Django: about 3,000 Python files. The embedding step (Qdrant) is
left out: on a CPU it would take hours for a repository of this size.

It measures size and time. It does not measure whether the extracted relationships are
complete: a reference the static analysis cannot resolve (a library, a dynamic call) is
counted as unresolved, not as an error.

The clone goes to a temporary folder and the graph is deleted from Neo4j once counted.
"""

import argparse
import json
import subprocess
import tempfile
import time
from collections.abc import Callable
from pathlib import Path
from typing import TypeVar

from app.core.config import Settings
from app.graph.builder import GraphBuilder
from app.graph.client import Neo4jClient
from app.graph.repository import GraphRepository
from app.services.graph_service import GraphService
from app.services.project_service import ProjectService

HERE = Path(__file__).parent
REPOSITORY = "https://github.com/django/django"
T = TypeVar("T")


def timed(action: Callable[[], T]) -> tuple[T, float]:
    started = time.perf_counter()
    result = action()
    return result, round(time.perf_counter() - started, 1)


def commit_of(source_dir: Path) -> str | None:
    """The commit that was cloned (a repository changes: the numbers belong to a commit)."""
    result = subprocess.run(
        ["git", "-C", str(source_dir), "rev-parse", "HEAD"], capture_output=True, text=True, check=False
    )
    return result.stdout.strip() or None


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("repository", nargs="?", default=REPOSITORY)
    parser.add_argument("--output", type=Path, default=HERE / "pipeline_results.json")
    args = parser.parse_args()

    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as workspace:
        settings = Settings().model_copy(update={"workspace_dir": Path(workspace)})
        projects = ProjectService(settings)
        with Neo4jClient.from_settings(settings) as neo4j:
            repository = GraphRepository(neo4j, settings.graph_batch_size)
            graph_service = GraphService(settings, repository, projects)

            project, import_seconds = timed(lambda: projects.create_from_github(args.repository))
            report, analyze_seconds = timed(lambda: graph_service.analyze_project(project.id))
            _, graph_seconds = timed(lambda: GraphBuilder(repository).build(report))
            stored = repository.statistics(project.id)
            result = {
                "repository": args.repository,
                "commit": commit_of(projects.workspace.source_dir(project.id)),
                "files": project.file_count,
                "languages": project.languages,
                "failed_files": len(report.failures),
                "entities_by_type": report.extraction.entity_counts,
                "unresolved_references": len(report.unresolved),
                "neo4j_nodes": stored.node_count,
                "neo4j_relationships": sum(stored.relationships_by_type.values()),
                "neo4j_relationships_by_type": stored.relationships_by_type,
                "import_seconds": import_seconds,
                "analyze_seconds": analyze_seconds,
                "graph_seconds": graph_seconds,
            }
            repository.delete_project(project.id)

    args.output.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    print(f"\n{args.repository}")
    print(f"  files            {result['files']:>9,}   ({result['failed_files']} failed to parse)")
    print(f"  nodes in Neo4j   {result['neo4j_nodes']:>9,}   {result['entities_by_type']}")
    print(f"  relationships    {result['neo4j_relationships']:>9,}   {result['neo4j_relationships_by_type']}")
    print(f"  time             import {import_seconds}s, analysis {analyze_seconds}s, graph {graph_seconds}s")
    print(f"\nResults written to {args.output}")


if __name__ == "__main__":
    main()
