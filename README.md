# CodeGraph AI

**GraphRAG-powered codebase analysis assistant.**

CodeGraph AI imports a software repository (GitHub URL or ZIP), analyzes its source code with
Tree-sitter, builds a knowledge graph of files, classes, functions and their relationships in
Neo4j, indexes the code semantically in Qdrant, and answers natural-language questions about the
project — with answers grounded in the code and linked to source locations.

> **Status:** Phase 13 — Knowledge graph visualization. Projects can be imported from GitHub or a ZIP file,
> their source files are parsed, the files, classes, interfaces, functions and methods they
> define are extracted, the relationships between them (imports, inheritance, calls, type uses,
> file dependencies) are resolved, the result is stored as a knowledge graph in Neo4j that can
> be queried (callers, dependencies, inheritance, paths...), and the code is indexed in Qdrant
> for semantic search with a local embedding model, and both are combined into structured
> GraphRAG context (vector hits expanded through the graph), which an LLM turns into a grounded
> answer citing numbered source locations; analysis features are built incrementally (see
> [Roadmap](#roadmap)), available through `POST /projects/{id}/analyze` then
> `POST /projects/{id}/chat`, and from a React frontend that also draws the knowledge graph.

## Tech stack

| Layer          | Technology                                   |
| -------------- | -------------------------------------------- |
| Backend        | Python 3.11+, FastAPI, Pydantic              |
| Code analysis  | Tree-sitter (official grammar packages)      |
| Knowledge graph| Neo4j 5 (official Python driver)             |
| Vector search  | Qdrant + local open-source embeddings (sentence-transformers) |
| LLM            | Provider-agnostic `LLMProvider`: Gemini (default, free tier), Groq, OpenRouter or any OpenAI-compatible API (`openai` SDK), Claude (`anthropic` SDK) |
| Frontend       | React, TypeScript, Vite                      |

See [docs/architecture.md](docs/architecture.md) for the full architecture.

## Repository layout

```
codegraph-ai/
├── backend/          # FastAPI application
│   ├── app/          # application code
│   └── tests/        # pytest tests
├── frontend/         # React + TypeScript + Vite application
├── docs/             # documentation
└── docker-compose.yml  # local Neo4j and Qdrant
```

## Getting started

### Prerequisites

- Python 3.11 or newer
- Git (used to clone GitHub repositories)
- Node.js 20.19+ (or 22.12+) and npm
- Docker (only to run Neo4j and Qdrant locally)

### Backend

```bash
cd backend
python -m venv .venv

# Activate the virtual environment
.venv\Scripts\activate          # Windows (PowerShell / cmd)
source .venv/bin/activate       # macOS / Linux / Git Bash: source .venv/Scripts/activate

pip install -r requirements-dev.txt
cp .env.example .env            # Windows cmd: copy .env.example .env

uvicorn app.main:app --reload
```

The API runs at <http://localhost:8000>:

- Health check: <http://localhost:8000/health> → `{"status": "ok", ...}`
- Interactive API docs: <http://localhost:8000/docs>

Run the tests:

```bash
cd backend
pytest               # fast tests: no internet, no Neo4j needed
pytest -m network    # clones a real repository from GitHub
pytest -m neo4j      # needs a running Neo4j (see "Knowledge graph" below)
pytest -m qdrant     # needs a running Qdrant (see "Semantic code search" below)
pytest -m embeddings # runs the real embedding model (BGE-M3, ~2.3 GB downloaded on first use)
pytest -m llm        # calls the real LLM (needs LLM_API_KEY; free with a free-tier key)
```

### Importing a project (API)

| Method   | Endpoint               | Description                                  |
| -------- | ---------------------- | -------------------------------------------- |
| `POST`   | `/projects/github`     | Clone a public GitHub repo: `{"url": "..."}` |
| `POST`   | `/projects/zip`        | Upload a ZIP archive (form field `file`)     |
| `GET`    | `/projects`            | List imported projects                       |
| `GET`    | `/projects/{id}`       | Project details and per-language file counts |
| `GET`    | `/projects/{id}/files` | Detected source files                        |
| `DELETE` | `/projects/{id}`       | Delete a project and its files               |
| `POST`   | `/projects/{id}/analyze` | Build the graph and vector index: see [Analyze, then chat](#analyze-then-chat-api) |
| `GET`    | `/projects/{id}/analysis` | Analysis state: see [Analysis state and knowledge graph](#analysis-state-and-knowledge-graph-api) |
| `GET`    | `/projects/{id}/graph` | Bounded knowledge graph: same section |
| `POST`   | `/projects/{id}/chat`  | Ask a question: see [Chat API](#chat-api)    |

```bash
curl -X POST http://localhost:8000/projects/github \
     -H "Content-Type: application/json" \
     -d '{"url": "https://github.com/pallets/itsdangerous"}'

curl -X POST http://localhost:8000/projects/zip -F "file=@my-project.zip"
```

The easiest way to try them is the interactive docs at <http://localhost:8000/docs>.
Imported projects are stored in `backend/workspace/<project_id>/` (ignored by Git).

### Parsing source files (Python API)

The parsing layer is not exposed over HTTP yet; later phases use it internally:

```python
from pathlib import Path
from app.ingestion.scanner import scan_directory
from app.parsing.service import ParserService

source_dir = Path("workspace/<project_id>/source")
service = ParserService(max_file_bytes=1024 * 1024)
report = service.parse_files(source_dir, scan_directory(source_dir, 1024 * 1024).files)

for result in report.results:
    print(result.path, result.language, result.root_info.type, result.has_syntax_errors)
    for error in result.syntax_errors:
        print("   ", error.message)
for failure in report.failures:
    print("skipped", failure.path, failure.reason)
```

Extracting entities (classes, interfaces, functions, methods) reuses the same parser:

```python
from app.extraction.service import EntityExtractionService

extraction = EntityExtractionService(service)
files = scan_directory(source_dir, 1024 * 1024).files
report = extraction.extract_project("<project_id>", source_dir, files)

print(report.entity_counts)          # {"class": 12, "file": 30, "function": 41, ...}
for entity in report.entities:
    print(entity.type, entity.qualified_name, f"{entity.file_path}:{entity.start_line}")
```

Extracting relationships parses each file once and returns the entities too:

```python
from app.relationships.service import RelationshipExtractionService

relationships = RelationshipExtractionService(extraction)
report = relationships.extract_project("<project_id>", source_dir, files)

print(report.relationship_counts)    # {"CALLS": 700, "IMPORTS": 200, "INHERITS": 21, ...}
for relationship in report.relationships:
    print(relationship.source_id, relationship.type, relationship.target_id)
for reference in report.unresolved:  # library calls, built-ins, ambiguous names...
    print(reference.source_id, reference.type, reference.target_name, reference.reason)
```

### Knowledge graph (Neo4j)

The analysis is stored in [Neo4j](https://neo4j.com/), a graph database: code *is* a graph
(files contain classes, functions call methods, classes extend classes), and Neo4j stores and
queries those connections directly, which later phases need for graph retrieval.

**1. Start Neo4j** (from the repository root; Neo4j 5.26 LTS, Community edition):

```bash
docker compose up -d neo4j
docker compose ps               # wait until the status says "healthy" (about 20 s)
```

The password comes from `NEO4J_PASSWORD` (default `change-me-please`). To choose another one,
set it in a `.env` file at the repository root (read by Docker Compose) **before the first
start**, and use the same value in `backend/.env`.

**2. Configure the backend** in `backend/.env` (already in `.env.example`):

```
NEO4J_URI=bolt://localhost:7687
NEO4J_USERNAME=neo4j
NEO4J_PASSWORD=change-me-please
NEO4J_DATABASE=neo4j
```

**3. Check that it works:** open the Neo4j Browser at <http://localhost:7474> (user `neo4j`),
or run `pytest -m neo4j` from `backend/`.

**4. Build a project's graph** (no API endpoint yet; from `backend/`, with the virtual
environment active):

```python
from app.core.config import get_settings
from app.graph.client import Neo4jClient
from app.graph.repository import GraphRepository
from app.services.graph_service import GraphService

settings = get_settings()
with Neo4jClient.from_settings(settings) as client:
    service = GraphService(settings, GraphRepository(client, settings.graph_batch_size))
    report = service.build_project_graph("<project_id>")   # an imported project
    print(report.summary)  # Graph built successfully: 73 files, 412 entities, 1,204 relationships
```

Building again is safe: it updates the same nodes and relationships (no duplicates) and removes
what disappeared from the code. `service.delete_project_graph("<project_id>")` removes one
project's graph only.

**5. Explore it** in the Neo4j Browser:

```cypher
MATCH (n:Entity {project_id: "<project_id>"}) RETURN n.entity_type, count(*);
MATCH (:Entity {project_id: "<project_id>"})-[r]->() RETURN type(r), count(*);
MATCH (f:Function {project_id: "<project_id>"})-[:CALLS]->(m) RETURN f, m LIMIT 50;
MATCH (c:Class {project_id: "<project_id>", name: "User"})-[:CONTAINS]->(m:Method) RETURN c, m;
```

**6. Stop it:** `docker compose stop neo4j` (data is kept in a Docker volume);
`docker compose down -v` removes the container **and** the data.

### Graph retrieval

Graph retrieval reads the knowledge graph back to answer **structural** questions about the
code: who calls a method, what a file depends on, which classes implement an interface, how two
functions are connected. Later phases will use these exact, located answers as the context of
natural-language answers (GraphRAG). There is no API endpoint yet; from `backend/`:

```python
from app.core.config import get_settings
from app.graph.client import Neo4jClient
from app.graph.repository import GraphRepository
from app.services.graph_retrieval_service import GraphRetrievalService

settings = get_settings()
project_id = "<project_id>"  # a project whose graph was built (see above)
with Neo4jClient.from_settings(settings) as client:
    retrieval = GraphRetrievalService(GraphRepository(client))

    [user] = retrieval.find_entities(project_id, "User", entity_types=["class"])
    methods = retrieval.get_contained_entities(project_id, user.id)        # User's methods
    [save] = retrieval.find_entities(project_id, "User.save")
    for caller in retrieval.get_callers(project_id, save.id):              # who calls User.save?
        call = caller.relationship
        print(caller.entity.qualified_name, f"{call.file_path}:{call.line}")
    [auth] = retrieval.find_entities(project_id, "auth.py")
    retrieval.get_transitive_dependencies(project_id, auth.id, max_depth=3)
    retrieval.find_paths(project_id, auth.id, save.id)                     # how are they connected?
```

| Question | Method |
| --- | --- |
| Find an entity by ID, qualified name or name (`partial=True`: contains) | `find_entities` |
| An entity with its metadata / with its parent and neighbors | `get_entity` / `get_entity_context` |
| What is connected to it? | `get_neighbors` |
| What does a class or file define? | `get_contained_entities` |
| Who calls it? What does it call? | `get_callers` / `get_callees` |
| What does a file import? Who imports it? | `get_imports` / `get_importers` |
| What does a file depend on (directly, transitively)? What depends on it? | `get_dependencies` / `get_transitive_dependencies` / `get_dependents` |
| Base classes / subclasses | `get_parents` / `get_subclasses` |
| Interfaces of a class / classes implementing an interface | `get_implemented_interfaces` / `get_implementations` |
| How are two entities connected? | `find_paths` |

Every operation is scoped to one project, results are limited (`limit`, default 50, at most 200)
and traversals are bounded (`max_depth`, at most 5). Names shared by several entities return all
of them. An unknown entity ID raises `EntityNotFoundError`; a question with no answer returns an
empty list. See [docs/architecture.md](docs/architecture.md#graph-retrieval-phase-7).

**Tests:** `pytest` checks every retrieval operation against an in-memory fake (no server
needed). `pytest -m neo4j` (Neo4j started, `NEO4J_PASSWORD` in `backend/.env` matching the
Docker Compose password) also asks every retrieval question to the real server and to the fake,
and requires identical answers.

### Semantic code search (Qdrant)

The knowledge graph answers *how code is connected*; semantic search answers *where something
is done*, even when the question names no class or function ("How is authentication
implemented?"). Source files are cut into **code-aware chunks** (one per function or method, a
skeleton per class, module-level code per file), each chunk is turned into a vector by a
**local embedding model**, and the vectors are stored in [Qdrant](https://qdrant.tech/), a vector
database. A question is embedded with the same model, and Qdrant returns the closest chunks of
that project.

```
source files ─> ParserService ─> entities ─> CodeChunker ─> EmbeddingProvider ─> Qdrant
                                                                                   ▲
question ─────────────────────────────────────> EmbeddingProvider ─> search (project filter)
```

**1. Start Qdrant** (from the repository root; data is kept in the `qdrant-data` Docker volume):

```bash
docker compose up -d qdrant     # REST API on http://127.0.0.1:6333 (dashboard: /dashboard)
docker compose stop qdrant      # stop it, data kept; `docker compose down -v` deletes the data
```

**2. The embedding model** (`EMBEDDING_MODEL`, default `BAAI/bge-m3`: dense vectors of **1024
dimensions**) runs on your machine with sentence-transformers, on the CPU by default
(`EMBEDDING_DEVICE=cpu`; `cuda` or `mps` for a GPU, never required). It is downloaded once from
Hugging Face into the standard Hugging Face cache (about 2.3 GB; `HF_HOME` moves it) the first
time it is used, then loaded from there, once per process. No code is ever sent to an external
service and no API key is needed. To download it ahead of time (from `backend/`):

```bash
python -c "from app.core.config import Settings; from app.rag.embeddings import embedding_provider_from_settings as p; print(p(Settings()).dimension)"   # prints 1024
```

If the download stalls on Windows, set `HF_HUB_DISABLE_XET=1` and run it again: it resumes.
`BAAI/bge-small-en-v1.5` (384 dimensions, ~130 MB) is a much faster alternative on a CPU; see
[Changing the embedding model](#changing-the-embedding-model) below.

**3. Index a project and search it** (no API endpoint yet; from `backend/`):

```python
from app.core.config import get_settings
from app.rag.embeddings import embedding_provider_from_settings
from app.rag.vector_store import QdrantVectorStore
from app.services.vector_index_service import VectorIndexService
from app.services.vector_retrieval_service import VectorRetrievalService

settings = get_settings()
embeddings = embedding_provider_from_settings(settings)   # loaded once, reused
with QdrantVectorStore.from_settings(settings) as store:
    report = VectorIndexService(settings, store, embeddings).index_project("<project_id>")
    print(report.summary)   # Vector index built successfully: 73 files, 640 chunks (...)

    retrieval = VectorRetrievalService(store, embeddings, settings)
    for hit in retrieval.retrieve("<project_id>", "How are users authenticated?", top_k=5):
        chunk = hit.chunk
        print(f"{hit.score:.2f} {chunk.qualified_name} {chunk.file_path}:{chunk.start_line}")
```

Indexing again is safe: chunks keep the same IDs (no duplicates) and code deleted since the last
indexing is removed. Results are limited (`top_k`, default 10, at most 50), always filtered by
project, and can be filtered by language or entity type (`languages=["python"]`,
`entity_types=["method"]`). See [docs/architecture.md](docs/architecture.md#vector-rag-phase-8).

#### Changing the embedding model

Vectors of two models cannot be compared, and a Qdrant collection holds vectors of one
dimension. Changing `EMBEDDING_MODEL` therefore does not update anything already indexed:

1. Set `EMBEDDING_MODEL` **and a new `QDRANT_COLLECTION`** in `backend/.env` (the default
   collection, `codegraph_chunks_bge_m3`, is for BGE-M3). The old collection is left untouched.
2. Re-index every imported project (the Neo4j graph does not need rebuilding):

   ```python
   from app.core.config import get_settings
   from app.rag.embeddings import embedding_provider_from_settings
   from app.rag.vector_store import QdrantVectorStore
   from app.services.vector_index_service import VectorIndexService

   settings = get_settings()
   with QdrantVectorStore.from_settings(settings) as store:
       service = VectorIndexService(settings, store, embedding_provider_from_settings(settings))
       reports, failures = service.index_all_projects()   # one failure never stops the others
       for report in reports:
           print(report.project_id, report.summary)
       print("failed:", failures)
   ```

3. Once search works, delete the old collection if you no longer need it (Qdrant dashboard at
   <http://127.0.0.1:6333/dashboard>, or `curl -X DELETE http://127.0.0.1:6333/collections/<old name>`).

Searching a collection built with a model of another dimension fails with a clear
`VectorCollectionError` (never a mixed result). Within one collection, points record their model
and searches only compare vectors of the same model, so an interrupted re-indexing never breaks
search with the previous model.

**Tests:** `pytest` covers chunking, indexing and search with an in-memory Qdrant and a tiny test
embedding (no server, no download). `pytest -m qdrant` (Qdrant started) runs them against the
real server; `pytest -m embeddings` runs the real model (dimension, finite normalized vectors,
batches, an end-to-end semantic search). `pytest -m "neo4j and qdrant and embeddings"` runs
GraphRAG with the real model on both servers. On a CPU, BGE-M3 indexes about 3–4 s per chunk
(roughly an hour for a repository of 1,000 chunks); searching takes about 0.1 s.

### GraphRAG retrieval

Semantic search finds code *about* a question; the knowledge graph knows *how that code is
connected*. GraphRAG uses both: the vector hits become **starting points** (their `entity_id` is
also a Neo4j node ID), and the graph adds each one's class or file, what it calls, who calls it,
its members, subclasses, dependencies... and how the top results connect to each other.

```
question ─> vector search (Qdrant) ─> hits ─> distinct entities (seeds)
        ─> graph questions per seed type (Neo4j, one hop, bounded) ─> GraphRAGContext
```

The result is **structured context, not an answer** (the LLM assistant below writes the answer): the vector hits with their
scores, the seeds, the graph entities and relationships, shortest paths between seeds, and the
**sources** to cite (file + line range + why each one is there). With both databases running and
the project analyzed and indexed (see above), from `backend/`:

```python
from app.core.config import get_settings
from app.graph.client import Neo4jClient
from app.graph.repository import GraphRepository
from app.rag.embeddings import embedding_provider_from_settings
from app.rag.vector_store import QdrantVectorStore
from app.services.graph_retrieval_service import GraphRetrievalService
from app.services.graphrag_service import GraphRAGService
from app.services.vector_retrieval_service import VectorRetrievalService

settings = get_settings()
embeddings = embedding_provider_from_settings(settings)
with Neo4jClient.from_settings(settings) as client, QdrantVectorStore.from_settings(settings) as store:
    graphrag = GraphRAGService(
        VectorRetrievalService(store, embeddings, settings),
        GraphRetrievalService(GraphRepository(client)),
        settings,
    )
    context = graphrag.build_context("<project_id>", "How is authentication implemented?")
    print("\n".join(context.describe()))
```

Example outline (abridged; scores depend on the model):

```
Question: How is authentication implemented?
Vector evidence:
  0.712  AuthService.login  auth/service.py:14-20
  ...
Graph evidence (complete):
  AuthService CONTAINS AuthService.login
  AuthService.login CALLS UserRepository.find_user
  AuthService.login CALLS AuthService.create_token
  path: AuthService.login -> UserRepository.find_user -> Database.query
Sources:
  auth/service.py:14-20  AuthService.login (vector)
  repository/user.py:7-9  UserRepository.find_user (graph)
```

Everything is bounded (5 seeds, 5 neighbors per graph question, 40 entities, paths between the
top 3 seeds, at most 3 relationships long), deterministic, and scoped to one project. If Neo4j is
down, the context keeps the vector evidence with `graph_status="unavailable"` and a warning (or
fails, with `GRAPHRAG_REQUIRE_GRAPH=true`). See
[docs/architecture.md](docs/architecture.md#graphrag-retrieval-phase-9).

**Tests:** `pytest` covers GraphRAG with the fake Neo4j and the in-memory Qdrant;
`pytest -m "neo4j and qdrant"` (both started) checks the real servers give the same context.

### LLM assistant (grounded answers)

The LLM turns a GraphRAG context into an answer. It **retrieves nothing itself**: it only reads
the context GraphRAG built, and every claim should cite a numbered source.

```
question ─> GraphRAGService ─> GraphRAGContext ─> PromptBuilder ─> LLMProvider (Gemini...)
                                                                        │
          AssistantResponse <── citations checked against the sources <─┘
          (answer, numbered sources, cited numbers, graph_status, warnings)
```

**1. Get a free API key and configure it** in `backend/.env`:

| `LLM_PROVIDER` | Free key | Default model | Notes |
| --- | --- | --- | --- |
| `gemini` (default) | <https://aistudio.google.com/apikey> (Google account, no card) | `gemini-flash-lite-latest` | large context, fits the prompt easily; free-tier data may be used by Google to improve its products |
| `groq` | <https://console.groq.com/keys> | `openai/gpt-oss-120b` | very fast; small tokens-per-minute quota on the free tier (lower `LLM_MAX_TOKENS`) |
| `openrouter` | <https://openrouter.ai/keys> | set `LLM_MODEL` to a `:free` model | about 50 free requests per day |
| `anthropic` | paid (<https://console.anthropic.com>) | `claude-opus-5-5` | `LLM_EFFORT` applies |
| `openai_compatible` | depends | set `LLM_MODEL` | any OpenAI-compatible server via `LLM_BASE_URL` (a local one included) |

```bash
# backend/.env
LLM_PROVIDER=gemini
LLM_API_KEY=<your key>
```

Free tiers, quotas and model names change: check the provider's console. See
[Configuration](#configuration).

**2. Ask a question** (no API endpoint yet; from `backend/`, with Neo4j and Qdrant running and
the project analyzed and indexed):

```python
from app.llm.generator import LLMGenerationService
from app.llm.provider import create_llm_provider

# `graphrag` built as in "GraphRAG retrieval" above
context = graphrag.build_context("<project_id>", "How is authentication implemented?")
response = LLMGenerationService(create_llm_provider(settings)).generate(context)

print(response.answer)              # Markdown, citing sources as [1], [2]...
for source in response.cited_sources:
    print(source.label)             # [1] AuthService.login — app/auth/service.py:14-20
print(response.graph_status, response.warnings)
```

- **Grounding:** the instructions (system prompt) say to answer only from the context, to say
  "The available repository context is insufficient..." when it is, to state relationships only
  if the graph lists them, and to never invent code, files or line numbers.
- **Citations:** sources are numbered by the application, in GraphRAG's order. The model only
  cites numbers; `cited` keeps the numbers that exist, and a number pointing at nothing is
  reported in `warnings`. File paths and lines always come from the retrieved metadata.
- **Nothing retrieved:** no LLM call; the answer says the context is insufficient.
- **Failures are never hidden:** a missing key, an unreachable or overloaded provider, a timeout,
  an empty or declined answer raise `LLMConfigurationError`, `LLMUnavailableError` or
  `LLMResponseError`; no fallback answer is made up. If Neo4j was down, the answer is still
  generated from the vector evidence, with `graph_status="unavailable"` and a warning.
- **Replaceable:** the rest of the application only sees `LLMProvider`. Two implementations
  cover every provider above (OpenAI-compatible protocol, Anthropic); another OpenAI-compatible
  service is one preset line in `create_llm_provider()`.

See [docs/architecture.md](docs/architecture.md#llm-assistant-phase-10).

**Tests:** `pytest` covers prompt building, citations, errors and the provider with fakes (no
network, no key). `pytest -m llm` asks the real model two questions (needs a key; free with a
free-tier key).

### Analyze, then chat (API)

Three calls take a project from source code to answers, with no Python snippet:

1. **Import** (`POST /projects/zip` or `/projects/github`) stores the source code. Nothing is
   analyzed yet.
2. **Analyze** (`POST /projects/{id}/analyze`, no body) builds the Neo4j knowledge graph
   (Phases 3–6) and the Qdrant vector index with BGE-M3 (Phase 8), by calling the same
   `GraphService` and `VectorIndexService` shown above.
3. **Chat** (`POST /projects/{id}/chat`) queries both indexes through GraphRAG (see
   [Chat API](#chat-api)).

```bash
curl -X POST http://localhost:8000/projects/zip -F "file=@my-project.zip"   # -> "id"
curl -X POST http://localhost:8000/projects/<project_id>/analyze
curl -X POST http://localhost:8000/projects/<project_id>/chat \
     -H "Content-Type: application/json" \
     -d '{"question": "How is authentication implemented?"}'
```

```json
{
  "project_id": "449fc24fe9744306bf10670bd9a7bfac",
  "status": "ready",
  "graph": {"files": 4, "entities": 17, "relationships": 26,
            "entities_by_type": {"Class": 4, "File": 4, "Function": 2, "Method": 7},
            "relationships_by_type": {"CALLS": 8, "CONTAINS": 13, "DEPENDS_ON": 2, "IMPORTS": 2, "INHERITS": 1},
            "unresolved_references": 8, "stale_entities_removed": 0},
  "vectors": {"files": 4, "chunks": 15, "chunks_by_type": {"class": 4, "file": 2, "function": 2, "method": 7},
              "embedding_model": "BAAI/bge-m3", "stale_chunks_removed": 0},
  "failed_files": 0,
  "warnings": [],
  "duration_seconds": 6.951,
  "analyzed_at": "2026-10-02T18:30:00Z"
}
```

- `status` is `ready` only when both the graph and the vector index were built. Any failure
  returns an error instead: 404 unknown project, 409 already being analyzed, 503 Neo4j,
  Qdrant or the embedding model unavailable.
- **Analyze again** after the code changes: both steps are idempotent, so nothing is
  duplicated and removed code is cleaned up (`stale_*_removed`).
- **Synchronous:** the request returns when analysis is done. Almost all of the time is
  embedding: building the graph of a 69-file project takes about 1 s. Measured on an Intel
  i5-12450H CPU for that project (378 chunks):
  - **BGE-M3** (the default): about 3.1 s per chunk, so about 20 minutes.
  - **`BAAI/bge-base-en-v1.5`:** 4.7 minutes end to end. It is English-only, with 768
    dimensions and 512 tokens.
  - **To switch:** set `EMBEDDING_MODEL` and a new `QDRANT_COLLECTION` in `backend/.env`, see
    "Changing the embedding model".
  - Loading the model adds 10–30 s to the first call, and re-analysis embeds every chunk
    again.
- If indexing fails after the graph was built, the graph is kept and any previous vectors are
  untouched: analyze again to complete it.

### Analysis state and knowledge graph (API)

| Method | Endpoint | Returns |
| --- | --- | --- |
| `GET` | `/projects/{id}/analysis` | `status` (`not_analyzed` or `ready`) and the report of the last successful analysis |
| `GET` | `/projects/{id}/graph?limit=150` | a bounded view of the project's Neo4j graph: nodes, edges, `truncated`, totals |

**Analysis state.** A successful `POST /analyze` saves its report (the same counts, warnings
and `analyzed_at`) in `backend/workspace/<id>/analysis.json`, next to `project.json`. So
`GET /analysis` answers after a reload or a server restart, without querying any database. The
file is removed when an analysis starts and written only once both steps succeeded: after a
failed or interrupted analysis the project reads `not_analyzed`. Projects analyzed before this
file existed also read `not_analyzed` until they are analyzed again.

**Graph.** `GET /graph` returns up to `limit` nodes (1–500, default 150) and up to 2,000
relationships between those nodes. Nodes come in a fixed order, structure first (files, then
classes and interfaces, functions, methods), so a large project keeps its skeleton and the same
project always gives the same graph. `truncated: true` says the project has more, and
`total_nodes` / `total_edges` give its full size.

```json
{
  "project_id": "57543d6494b949e2ba6aa33cc1a6f452",
  "nodes": [{"id": "57543d…:auth/service.py:AuthService.login", "entity_type": "method",
             "name": "login", "qualified_name": "AuthService.login", "file_path": "auth/service.py",
             "language": "python", "start_line": 14, "end_line": 20}],
  "edges": [{"id": "CALLS:…", "source": "…AuthService.login", "target": "…UserRepository.find_user",
             "relationship_type": "CALLS"}],
  "truncated": false,
  "total_nodes": 17,
  "total_edges": 26
}
```

- **Fixed and read-only:** it is one more fixed, read-only query in `GraphRepository`, called
  through `GraphRetrievalService`, and it is always filtered by `project_id`.
- **Nothing to inject:** the client chooses `limit` only; any other query parameter is ignored,
  and no Cypher, label or relationship type can be passed.
- **Errors:** 404 unknown project, 422 invalid `limit`, 503 Neo4j unavailable. An unanalyzed
  project returns an empty graph.

### Chat API

Ask a question about an imported project over HTTP: the endpoint runs GraphRAG and the LLM
assistant described above and returns the answer with its sources.

| Method | Endpoint | Body | Returns |
| --- | --- | --- | --- |
| `POST` | `/projects/{id}/chat` | `{"question": "..."}` (1–2000 characters, nothing else) | answer, numbered sources, status |

**Before chatting**, the project must be analyzed (`POST /projects/{id}/analyze`, see
[Analyze, then chat](#analyze-then-chat-api)), and `LLM_API_KEY` must be set. An unindexed project gets "The available repository context is
insufficient..." instead of an answer.

```bash
curl -X POST http://localhost:8000/projects/<project_id>/chat \
     -H "Content-Type: application/json" \
     -d '{"question": "How is authentication implemented?"}'
```

```json
{
  "question": "How is authentication implemented?",
  "answer": "Authentication is handled by `AuthService.login` [1], which loads the user with `UserRepository.find_user` [3]...",
  "sources": [
    {"id": 1, "entity": "AuthService.login", "entity_type": "method", "file": "auth/service.py",
     "start_line": 14, "end_line": 20, "found_by": "semantic_search", "cited": true},
    {"id": 3, "entity": "UserRepository.find_user", "entity_type": "method", "file": "repository/user.py",
     "start_line": 7, "end_line": 9, "found_by": "graph", "cited": true}
  ],
  "cited": [1, 3],
  "graph_status": "complete",
  "warnings": [],
  "model": "gemini-flash-lite-latest"
}
```

- `sources`: every piece of code given to the model, numbered as the answer cites it (`[1]`),
  with its file and line range; `found_by` says whether semantic search found it or the
  knowledge graph connected it; `cited` whether the answer uses it.
- `graph_status`: `complete`, or `partial` / `unavailable` when Neo4j failed (the answer then
  relies on semantic search only, with a warning).
- Errors use the usual `{"detail": ...}` shape: 404 unknown project, 422 invalid body (empty
  or too long question, unknown field), 502 no usable LLM answer, 503 Qdrant, embedding model
  or LLM unavailable or misconfigured. Messages never contain keys or passwords.
- Interactive documentation, with the schemas and examples: <http://localhost:8000/docs>.
- The first question after starting the server also loads the embedding model (about 20–30 s
  on a CPU); the next ones reuse it.

**Tests:** `pytest` covers both endpoints with fake databases, fake retrieval and a fake LLM
(no server, no key). `pytest -m "neo4j and qdrant and embeddings and llm"` runs import ->
analyze -> chat over HTTP with every real service.

### Frontend

```bash
cd frontend
npm install
cp .env.example .env            # Windows cmd: copy .env.example .env
npm run dev
```

Open <http://localhost:5173> (the backend must be running on port 8000).

- **Home (`/`):** import from a GitHub URL or a ZIP upload, and the list of imported projects.
- **Project (`/projects/:id`):**
  - The project's metadata.
  - The **Analysis** panel. Its state comes from `GET /analysis`, so "Ready" and the counts
    survive a reload. **Analyze Project** or **Analyze again** runs `POST /analyze`; there is no
    fake progress bar.
  - Two tabs, Chat and Knowledge Graph (below).
- **Chat:**
  - Markdown answers rendered safely (no raw HTML, no images, no `javascript:` links).
  - `[n]` citations are buttons that highlight the matching source (file and lines).
  - Graph status and warnings are shown when retrieval was degraded.
- **Knowledge Graph:** the real Neo4j graph from `GET /graph`, drawn with
  [Cytoscape.js](https://js.cytoscape.org/), which is loaded only when the tab is opened.
  - Zoom (wheel, pinch or buttons), pan, and **Fit**.
  - Colors and shapes per entity type, colors per relationship type.
  - Relationship types can be hidden.
  - **Find a node** by name.
  - Clicking a node shows its type, qualified name, file, lines and language, plus its
    incoming and outgoing relationships; each one can be followed to the other node.
  - A truncated graph says so, with the totals, and the node limit can be raised up to 500.

All HTTP calls are in `src/services/api.ts`, typed by `src/types/api.ts`, which mirrors the
backend's Pydantic schemas.

Scripts: `npm run dev`, `npm run build`, `npm run lint`, `npm run typecheck`, `npm test`
(Vitest + Testing Library, with a fake backend: no server needed).

## Configuration

Configuration is read from environment variables / `.env` files. Real `.env` files are ignored by
Git; each app has a committed `.env.example` template.

| File                    | Variable        | Default                  | Description                       |
| ----------------------- | --------------- | ------------------------ | --------------------------------- |
| `backend/.env`          | `APP_NAME`      | `CodeGraph AI`           | Name shown by the API             |
|                         | `ENVIRONMENT`   | `development`            | Environment label                 |
|                         | `LOG_LEVEL`     | `INFO`                   | Python logging level              |
|                         | `CORS_ORIGINS`  | `http://localhost:5173`  | Comma-separated allowed origins   |
|                         | `WORKSPACE_DIR` | `workspace`              | Where imported projects are stored |
|                         | `MAX_UPLOAD_SIZE_MB` | `50`                | Maximum ZIP upload size           |
|                         | `MAX_EXTRACTED_SIZE_MB` | `500`            | Maximum size after extraction     |
|                         | `MAX_ARCHIVE_FILES` | `20000`              | Maximum number of entries in a ZIP |
|                         | `MAX_REPOSITORY_SIZE_MB` | `500`           | Maximum size of a cloned repository |
|                         | `MAX_SOURCE_FILE_KB` | `1024`              | Larger source files are skipped (scanning and parsing) |
|                         | `GIT_CLONE_TIMEOUT_SECONDS` | `120`        | Clone timeout                     |
|                         | `NEO4J_URI`     | `bolt://localhost:7687`  | Neo4j server (Bolt protocol)      |
|                         | `NEO4J_USERNAME` | `neo4j`                 | Neo4j user                        |
|                         | `NEO4J_PASSWORD` | *(empty)*               | Neo4j password (never logged)     |
|                         | `NEO4J_DATABASE` | `neo4j`                 | Neo4j database name               |
|                         | `GRAPH_BATCH_SIZE` | `1000`                | Nodes / relationships per write query |
|                         | `QDRANT_URL`    | `http://127.0.0.1:6333`  | Qdrant server (REST API); not `localhost`, slow on Windows |
|                         | `QDRANT_API_KEY` | *(empty)*               | Qdrant API key, for a secured server (never logged) |
|                         | `QDRANT_COLLECTION` | `codegraph_chunks_bge_m3` | Collection holding every project's chunks (one per model) |
|                         | `QDRANT_TIMEOUT_SECONDS` | `10`            | Qdrant request timeout            |
|                         | `EMBEDDING_MODEL` | `BAAI/bge-m3`         | Local sentence-transformers model (1024 dimensions) |
|                         | `EMBEDDING_DEVICE` | `cpu`                | `cpu`, or `cuda` / `mps` for a GPU |
|                         | `EMBEDDING_BATCH_SIZE` | `32`              | Chunks embedded and written together |
|                         | `VECTOR_CHUNK_MAX_CHARS` | `2000`          | Longer chunks are split on line boundaries |
|                         | `VECTOR_CHUNK_OVERLAP_LINES` | `3`         | Lines repeated between split parts |
|                         | `VECTOR_TOP_K` / `VECTOR_MAX_TOP_K` | `10` / `50` | Default and maximum results per search |
|                         | `VECTOR_MIN_SCORE` | *(unset)*             | Optional minimum cosine similarity |
|                         | `GRAPHRAG_VECTOR_TOP_K` | `10`             | Vector hits per GraphRAG question |
|                         | `GRAPHRAG_MAX_SEEDS` | `5`                 | Distinct hit entities expanded in the graph |
|                         | `GRAPHRAG_NEIGHBORS_PER_EXPANSION` | `5`   | Results per graph question (callers...) |
|                         | `GRAPHRAG_MAX_CONTEXT_ENTITIES` | `40`     | Seeds + neighbors in the context |
|                         | `GRAPHRAG_PATH_SEEDS` / `GRAPHRAG_PATH_MAX_DEPTH` | `3` / `3` | Paths between the top seeds |
|                         | `GRAPHRAG_REQUIRE_GRAPH` | `false`         | Fail (instead of vector-only context) when Neo4j fails |
|                         | `LLM_PROVIDER`  | `gemini`                 | `gemini`, `groq`, `openrouter`, `anthropic`, `openai_compatible` |
|                         | `LLM_MODEL`     | *(provider default)*     | Model answering the questions      |
|                         | `LLM_BASE_URL`  | *(provider preset)*      | Only for `openai_compatible`       |
|                         | `LLM_API_KEY`   | *(empty)*                | API key (never logged); empty: `GEMINI_API_KEY`, `GROQ_API_KEY`... |
|                         | `LLM_MAX_TOKENS` | `16000`                 | Output limit per answer (reasoning included) |
|                         | `LLM_EFFORT`    | `medium`                 | Claude only: `low` … `max`; empty: model default |
|                         | `LLM_TEMPERATURE` | *(unset)*              | Only for models that accept it (current Claude models don't) |
|                         | `LLM_TIMEOUT_SECONDS` | `120`              | Timeout of one LLM request         |
|                         | `LLM_MAX_RETRIES` | `2`                    | SDK retries (network, 429, 5xx)    |
| `.env` (root, optional) | `NEO4J_PASSWORD` | `change-me-please`      | Password of the Docker Compose Neo4j |
| `frontend/.env`         | `VITE_API_URL`  | `http://localhost:8000`  | Backend base URL                  |

## Security

CodeGraph AI is a **static** analysis tool: it never executes code from imported repositories.
Secrets (API keys, database passwords) are only ever read from environment variables.

Imported code is treated as untrusted input:

- **ZIP uploads** are checked before anything is written: entries with absolute paths or `..`
  (path traversal / "zip slip") are rejected, symlinks are skipped, and the number of entries,
  the declared size and the bytes actually extracted are all limited ("zip bomb" protection).
- **GitHub URLs** are validated and the clone URL is rebuilt from the owner and repository name,
  so user input never reaches the `git` command line. Clones are shallow, never prompt for
  credentials, don't fetch submodules or LFS files, and check out symlinks as plain files.
- **Project IDs** from URLs are validated before being turned into file paths.
- The **scanner** never follows symlinks and only reads the first 8 KB of a file to detect binaries.
- The **parser** re-checks every file before reading it (no symlinks, no paths outside the
  project, size limit, no binary content) and only builds a syntax tree: Tree-sitter never
  executes code. Grammars are installed from pinned packages; nothing is downloaded at runtime.
- **Entity and relationship extraction** only read the syntax tree: repository code is never
  imported, evaluated or run. Imports are resolved against the list of scanned files only, so an
  import path such as `../../../etc/passwd` can never make the analyzer open a file, and a
  relative import that leaves the project is simply left unresolved.
- **Neo4j**: every value (names, paths, IDs) is sent as a Cypher **parameter**, never pasted into
  a query; labels and relationship types come from fixed lists in the code. Credentials come
  from environment variables, the password is a `SecretStr` and never appears in logs or error
  messages, and Docker Compose exposes Neo4j on `127.0.0.1` only. Every project operation is
  scoped by `project_id`, so deleting one project's graph never touches another's.
- **Graph retrieval** offers fixed, typed questions only (no "run this Cypher"). Every query is
  scoped by `project_id` (an entity ID of another project is rejected before reaching Neo4j),
  traversal depths are checked integers from 1 to 5 (never an unbounded `*`), and every result
  list has a limit.
- **Semantic search** runs the embedding model locally (`trust_remote_code=False`): repository
  code never leaves the machine. Every Qdrant search, count and delete is filtered by
  `project_id` through qdrant-client filter objects (never text), searches are bounded
  (`top_k` ≤ 50), the collection name is validated, and the Qdrant API key is a `SecretStr`.
- **GraphRAG** only combines the typed operations above (no Cypher or Qdrant filter of its own),
  keeps every result in the requested project, expands one hop per seed with bounded paths, and
  never calls an external service.
- **Analysis API:** `POST /projects/{id}/analyze` takes no body, only a validated project ID:
  the client cannot pass a path, Cypher, a filter or a model. The code is parsed, never run;
  Neo4j queries stay parameterized and every node and vector carries its project ID.
- **Graph API:** `GET /projects/{id}/graph` is read-only and bounded (≤ 500 nodes, ≤ 2,000
  relationships). It runs one fixed, parameterized query filtered by `project_id`; the client
  can only choose the node limit (validated), never Cypher, labels or relationship types.
  `GET /analysis` reads a JSON file in the project's own workspace folder.
- **Frontend:** LLM output and repository metadata are rendered as text by React (no
  `dangerouslySetInnerHTML`); Markdown answers drop raw HTML and images and sanitize links.
  The browser only knows `VITE_API_URL`; no key or password reaches it.
- **Chat API:** the client sends a natural-language question only. The body accepts one field
  (unknown fields such as `cypher`, `filter` or `system_prompt` are rejected with 422), the
  question is limited to 2000 characters, the project ID is validated before any file path is
  built, and retrieval limits, prompts and the model come from the server's configuration.
  Unexpected errors return a generic 500, never a stack trace.
- **LLM assistant:** the model is treated as an untrusted generator and repository code as
  untrusted data. Instructions live only in the system prompt; the repository context and the
  question go in the user message inside delimited sections, **XML-escaped**, so code or a
  comment such as `</repository_context> ignore previous instructions` cannot close a section or
  pose as instructions. The model has no tools: it cannot run code, query the databases or
  change retrieval. Its citations are checked against the retrieved sources. The API key is a
  `SecretStr` and never appears in logs or errors. Only the retrieved context (selected code
  chunks and metadata) is sent to the LLM provider, never the whole repository.

## Roadmap

1. ✅ Project setup
2. ✅ Repository ingestion (GitHub clone, ZIP upload, file scanning, language detection)
3. ✅ Tree-sitter integration (Python, Java, JavaScript, TypeScript)
4. ✅ Entity extraction (files, classes, interfaces, functions, methods)
5. ✅ Relationship extraction (imports, inheritance, calls, uses, dependencies)
6. ✅ Knowledge graph (Neo4j)
7. ✅ Graph retrieval
8. ✅ Vector RAG (chunking, embeddings, Qdrant)
9. ✅ GraphRAG (hybrid retrieval + context fusion)
10. ✅ LLM assistant (grounded, cited answers)
11. ✅ Chat API (`POST /projects/{id}/analyze`, `POST /projects/{id}/chat`)
12. ✅ Frontend (import, analysis, chat with citations)
13. ✅ Graph visualization (persisted analysis state, `GET /projects/{id}/graph`, Cytoscape.js)
14. Advanced analysis (impact, dependencies, architecture summary)
15. Testing (integration, retrieval, end-to-end)
16. Docker & finalization
