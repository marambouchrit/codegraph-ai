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
| FastAPI backend    | HTTP API, orchestration                             | Project API |
| Repository manager | Clone / extract / scan repositories                 | ✅ Phase 2 |
| Code analyzer      | Tree-sitter parsing, entity & relationship extraction | Phases 3–5 |
| Graph engine       | Neo4j storage and graph queries                     | Phases 6–7 |
| RAG engine         | Chunking, embeddings, Qdrant, hybrid retrieval      | Phases 8–9 |
| Chat engine        | Prompting and LLM provider abstraction              | Phase 10 |

## Current backend layout (Phase 2)

```
backend/app/
├── main.py                  # create_app(): FastAPI instance, CORS, error handler, routers
├── core/
│   ├── config.py            # Settings loaded from environment / .env
│   ├── errors.py            # AppError + subclasses, each with an HTTP status code
│   └── logging.py           # logging setup
├── api/
│   ├── dependencies.py      # get_project_service() for Depends(...)
│   └── routes/
│       ├── health.py        # GET /health
│       └── projects.py      # /projects endpoints (thin: call the service, return schemas)
├── schemas/project.py       # API request/response models
├── services/
│   └── project_service.py   # orchestrates ingestion; the only place combining the pieces below
└── ingestion/               # small, independent building blocks
    ├── github.py            # URL validation + safe shallow `git clone`
    ├── zip_handler.py       # safe ZIP extraction
    ├── workspace.py         # workspace folders, project IDs, deletion
    ├── scanner.py           # recursive source-file discovery
    └── languages.py         # file extension -> Language
```

Dependencies point one way: `routes → services → ingestion`. The ingestion modules know
nothing about FastAPI, so they are easy to test and to reuse in later phases.

## Ingestion pipeline (Phase 2)

```
POST /projects/github {url}              POST /projects/zip (file)
        │                                         │
 parse_github_url()                        check extension + upload size
        │                                         │
        └──────────────┬──────────────────────────┘
                       ▼
        ProjectService._ingest()
          1. new project ID  → workspace/<id>/
          2. fetch source    → git clone --depth 1     | extract_zip_safely()
                               + repository size check | + unwrap single top-level folder
          3. scan_directory() → supported source files + language counts
          4. no source files? → error
          5. write workspace/<id>/project.json
          any failure → delete workspace/<id>/ and return a clear error
```

Storage on disk:

```
workspace/
└── <project_id>/            # 32 hex characters (uuid4)
    ├── project.json         # metadata (name, source, languages, file count...)
    └── source/              # repository files: read-only input for later phases
```

Project metadata is stored as a JSON file rather than in a database. That is enough for this
phase and needs no extra setup. A relational database can replace it later if needs grow
(for example analysis status and history).

Phase 3 (Tree-sitter) will parse the files listed by `scan_directory()` from `source/`.

New packages (`analyzers/`, `graph/`, `rag/`, `llm/`) are added only when the phase that needs
them is reached.
