"""Retrieval evaluation: vector-only against GraphRAG, on a small hand-labelled question set.

    docker compose up -d neo4j qdrant
    python -m evaluation.retrieval_eval <project_id>          (from backend/)

The project must be the repository named in `questions.json`, imported and analyzed with the
current EMBEDDING_MODEL. For each question and each K (5 and 10):

- vector-only: the K chunks most similar to the question;
- GraphRAG: the sources of the context built from those same K chunks, expanded in the graph
  (the K chunks plus the graph entities: what the chat gives to the LLM).

Recall = the share of the question's expected entities found in what was retrieved, averaged
over the questions. The GraphRAG context holds more entities than K, so its average size is
printed next to the scores.

This measures retrieval only: no LLM is called, and nothing here says whether an answer is
correct. One repository and a few questions: it is an indication, not a general result.
"""

import argparse
import json
import statistics
from pathlib import Path
from typing import Any

from app.core.config import Settings
from app.graph.client import Neo4jClient
from app.graph.repository import GraphRepository
from app.rag.embeddings import embedding_provider_from_settings
from app.rag.vector_store import QdrantVectorStore
from app.services.graph_retrieval_service import GraphRetrievalService
from app.services.graphrag_service import GraphRAGService
from app.services.vector_retrieval_service import VectorRetrievalService

HERE = Path(__file__).parent
KS = (5, 10)

Retrieved = set[tuple[str, str]]  # (file path, qualified name)


def recall(expected: list[str], retrieved: Retrieved) -> float:
    """The share of `expected` items present in `retrieved`."""
    files = {file_path for file_path, _ in retrieved}
    found = 0
    for item in expected:
        file_path, _, qualified_name = item.partition("::")
        found += (file_path, qualified_name) in retrieved if qualified_name else file_path in files
    return found / len(expected)


def check_labels(questions: list[dict[str, Any]], known: Retrieved) -> None:
    """Every expected item must exist in the project's graph: a typo would lower both scores."""
    missing = [
        f"question {question['id']}: {item}"
        for question in questions
        for item in question["expected"]
        if recall([item], known) == 0
    ]
    if missing:
        raise SystemExit("Expected items not found in the project's graph:\n  " + "\n  ".join(missing))


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("project_id")
    parser.add_argument("--questions", type=Path, default=HERE / "questions.json")
    parser.add_argument("--output", type=Path, default=HERE / "results.json")
    args = parser.parse_args()

    labelled = json.loads(args.questions.read_text(encoding="utf-8"))
    questions = labelled["questions"]
    settings = Settings()

    with Neo4jClient.from_settings(settings) as neo4j:
        store = QdrantVectorStore.from_settings(settings)
        embeddings = embedding_provider_from_settings(settings)
        graph = GraphRetrievalService(GraphRepository(neo4j))
        vector = VectorRetrievalService(store, embeddings, settings)

        project = graph.get_analysis_graph(args.project_id)
        check_labels(questions, {(node.file_path, node.qualified_name) for node in project.nodes})

        rows = []
        for question in questions:
            row: dict[str, Any] = {key: question[key] for key in ("id", "category", "question")}
            for k in KS:
                hits = vector.retrieve(args.project_id, question["question"], top_k=k)
                # Same retrieval as the application, with only the number of vector hits set to K.
                graphrag = GraphRAGService(
                    vector, graph, settings.model_copy(update={"graphrag_vector_top_k": k})
                )
                context = graphrag.build_context(args.project_id, question["question"])
                if context.graph_status != "complete":
                    raise SystemExit(f"The graph was not fully available: {context.warnings}")
                vector_only = {(h.chunk.file_path, h.chunk.qualified_name) for h in hits}
                # The sources are what the LLM receives: every vector chunk, then the graph entities.
                with_graph = {(s.file_path, s.qualified_name) for s in context.sources}
                row[f"vector_recall@{k}"] = round(recall(question["expected"], vector_only), 3)
                row[f"graphrag_recall@{k}"] = round(recall(question["expected"], with_graph), 3)
                row[f"vector_sources@{k}"] = len(vector_only)
                row[f"graphrag_sources@{k}"] = len(with_graph)
            rows.append(row)

    def mean(key: str) -> float:
        return statistics.mean(row[key] for row in rows)

    summary = {
        "repository": labelled["repository"],
        "commit": labelled["commit"],
        "project_id": args.project_id,
        "embedding_model": embeddings.model_name,
        "questions": len(rows),
        "graph_nodes": project.total_nodes,
        "graph_relationships": project.total_edges,
    }
    for k in KS:
        for method in ("vector", "graphrag"):
            summary[f"{method}_recall@{k}"] = round(mean(f"{method}_recall@{k}"), 3)
            summary[f"{method}_sources@{k}"] = round(mean(f"{method}_sources@{k}"), 1)
    args.output.write_text(
        json.dumps({"summary": summary, "rows": rows}, indent=2) + "\n", encoding="utf-8"
    )

    print(f"\n{len(rows)} questions, {labelled['repository']}, {embeddings.model_name}\n")
    print(f"{'#':>3}  {'category':<13}" + "".join(f"{f'vector@{k}':>10}{f'graphrag@{k}':>13}" for k in KS))
    for row in rows:
        print(
            f"{row['id']:>3}  {row['category']:<13}"
            + "".join(f"{row[f'vector_recall@{k}']:>10.0%}{row[f'graphrag_recall@{k}']:>13.0%}" for k in KS)
        )
    print(f"\n{'':<14}" + "".join(f"{f'Recall@{k}':>11}{'sources':>10}" for k in KS))
    for method, name in (("vector", "Vector-only"), ("graphrag", "GraphRAG")):
        print(
            f"{name:<14}"
            + "".join(f"{summary[f'{method}_recall@{k}']:>11.1%}{summary[f'{method}_sources@{k}']:>10.1f}" for k in KS)
        )
    print(f"\nPer-question results written to {args.output}")


if __name__ == "__main__":
    main()
