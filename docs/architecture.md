# Architecture

CodeGraph AI analyzes a software repository statically (it never executes imported code),
builds a **knowledge graph** of its structure and a **vector index** of its content, and
answers natural-language questions by combining both (GraphRAG).

## Pipelines

**Indexing** (when a project is imported and analyzed):

```
Repository (GitHub URL or ZIP)
  → File scanner + language detection
  → Tree-sitter AST parsing
  → Entity extraction (files, modules, classes, interfaces, functions, methods)
  → Relationship extraction (CONTAINS, IMPORTS, CALLS, INHERITS, IMPLEMENTS, USES, DEPENDS_ON)
  → Neo4j knowledge graph

Source code → chunking → embeddings → Qdrant vector index
```

**Question answering**:

```
Question → query analysis
  → vector retrieval (Qdrant) + graph retrieval (Neo4j)
  → context fusion → prompt builder → LLM
  → answer + sources (file paths, line ranges)
```

## Components

| Component          | Responsibility                                      | Status   |
| ------------------ | --------------------------------------------------- | -------- |
| React frontend     | Import projects, explore code, chat, view the graph | Skeleton |
| FastAPI backend    | HTTP API, orchestration                             | Skeleton |
| Repository manager | Clone / extract / scan repositories                 | Phase 2  |
| Code analyzer      | Tree-sitter parsing, entity & relationship extraction | Phases 3–5 |
| Graph engine       | Neo4j storage and graph queries                     | Phases 6–7 |
| RAG engine         | Chunking, embeddings, Qdrant, hybrid retrieval      | Phases 8–9 |
| Chat engine        | Prompting and LLM provider abstraction              | Phase 10 |

## Current backend layout (Phase 1)

```
backend/app/
├── main.py              # create_app(): FastAPI instance, CORS, routers
├── core/config.py       # Settings loaded from environment / .env
├── core/logging.py      # logging setup
└── api/routes/health.py # GET /health
```

New packages (`ingestion/`, `analyzers/`, `graph/`, `rag/`, `llm/`, `schemas/`) are added
only when the phase that needs them is reached.
