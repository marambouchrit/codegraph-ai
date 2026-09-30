# CodeGraph AI

**GraphRAG-powered codebase analysis assistant.**

CodeGraph AI imports a software repository (GitHub URL or ZIP), analyzes its source code with
Tree-sitter, builds a knowledge graph of files, classes, functions and their relationships in
Neo4j, indexes the code semantically in Qdrant, and answers natural-language questions about the
project — with answers grounded in the code and linked to source locations.

> **Status:** Phase 9 — GraphRAG retrieval. Projects can be imported from GitHub or a ZIP file,
> their source files are parsed, the files, classes, interfaces, functions and methods they
> define are extracted, the relationships between them (imports, inheritance, calls, type uses,
> file dependencies) are resolved, the result is stored as a knowledge graph in Neo4j that can
> be queried (callers, dependencies, inheritance, paths...), and the code is indexed in Qdrant
> for semantic search with a local embedding model, and both are combined into structured
> GraphRAG context (vector hits expanded through the graph); analysis features are built
> incrementally (see [Roadmap](#roadmap)).

## Tech stack

| Layer          | Technology                                   |
| -------------- | -------------------------------------------- |
| Backend        | Python 3.11+, FastAPI, Pydantic              |
| Code analysis  | Tree-sitter (official grammar packages)      |
| Knowledge graph| Neo4j 5 (official Python driver)             |
| Vector search  | Qdrant + local open-source embeddings (sentence-transformers) |
| LLM            | Provider-agnostic `LLMProvider` *(Phase 10)* |
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
pytest -m embeddings # runs the real embedding model (downloaded on first use)
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

**2. The embedding model** (`EMBEDDING_MODEL`, default `BAAI/bge-small-en-v1.5`: 384 dimensions,
~130 MB) runs on your machine with sentence-transformers. It is downloaded once from Hugging Face
into the local cache on first use; no code is ever sent to an external service and no API key is
needed. `BAAI/bge-m3` (1024 dimensions, ~2.3 GB, slower on a CPU) also works: set it together
with another `QDRANT_COLLECTION`, since a collection holds vectors of one dimension.

**3. Index a project and search it** (no API endpoint yet; from `backend/`):

```python
from app.core.config import get_settings
from app.rag.embeddings import get_embedding_provider
from app.rag.vector_store import QdrantVectorStore
from app.services.vector_index_service import VectorIndexService
from app.services.vector_retrieval_service import VectorRetrievalService

settings = get_settings()
embeddings = get_embedding_provider(settings.embedding_model)   # loaded once, reused
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

**Tests:** `pytest` covers chunking, indexing and search with an in-memory Qdrant and a tiny test
embedding (no server, no download). `pytest -m qdrant` (Qdrant started) runs them against the
real server; `pytest -m embeddings` runs the real model, including an end-to-end semantic search.

### GraphRAG retrieval

Semantic search finds code *about* a question; the knowledge graph knows *how that code is
connected*. GraphRAG uses both: the vector hits become **starting points** (their `entity_id` is
also a Neo4j node ID), and the graph adds each one's class or file, what it calls, who calls it,
its members, subclasses, dependencies... and how the top results connect to each other.

```
question ─> vector search (Qdrant) ─> hits ─> distinct entities (seeds)
        ─> graph questions per seed type (Neo4j, one hop, bounded) ─> GraphRAGContext
```

The result is **structured context, not an answer** (no LLM yet): the vector hits with their
scores, the seeds, the graph entities and relationships, shortest paths between seeds, and the
**sources** to cite (file + line range + why each one is there). With both databases running and
the project analyzed and indexed (see above), from `backend/`:

```python
from app.core.config import get_settings
from app.graph.client import Neo4jClient
from app.graph.repository import GraphRepository
from app.rag.embeddings import get_embedding_provider
from app.rag.vector_store import QdrantVectorStore
from app.services.graph_retrieval_service import GraphRetrievalService
from app.services.graphrag_service import GraphRAGService
from app.services.vector_retrieval_service import VectorRetrievalService

settings = get_settings()
embeddings = get_embedding_provider(settings.embedding_model)
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

### Frontend

```bash
cd frontend
npm install
cp .env.example .env            # Windows cmd: copy .env.example .env
npm run dev
```

Open <http://localhost:5173>. The page shows the backend status by calling `GET /health`.

Other scripts: `npm run build`, `npm run lint`, `npm run typecheck`.

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
|                         | `QDRANT_COLLECTION` | `codegraph_chunks`   | Collection holding every project's chunks |
|                         | `QDRANT_TIMEOUT_SECONDS` | `10`            | Qdrant request timeout            |
|                         | `EMBEDDING_MODEL` | `BAAI/bge-small-en-v1.5` | Local sentence-transformers model |
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
10. LLM assistant (`POST /projects/{id}/chat`)
11. Frontend (import, dashboard, explorer, chat)
12. Graph visualization
13. Advanced analysis (impact, dependencies, architecture summary)
14. Testing (integration, retrieval, end-to-end)
15. Docker & finalization
