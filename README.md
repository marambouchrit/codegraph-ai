# CodeGraph AI

**GraphRAG-powered codebase analysis assistant.**

CodeGraph AI imports a software repository (GitHub URL or ZIP), analyzes its source code with
Tree-sitter, builds a knowledge graph of files, classes, functions and their relationships in
Neo4j, indexes the code semantically in Qdrant, and answers natural-language questions about the
project — with answers grounded in the code and linked to source locations.

> **Status:** Phase 5 — relationship extraction. Projects can be imported from GitHub or a ZIP
> file, their source files are scanned and parsed into syntax trees, the files, classes,
> interfaces, functions and methods they define are extracted, and the relationships between
> them (imports, inheritance, calls, type uses, file dependencies) are resolved; analysis
> features are built incrementally (see [Roadmap](#roadmap)).

## Tech stack

| Layer          | Technology                                   |
| -------------- | -------------------------------------------- |
| Backend        | Python 3.11+, FastAPI, Pydantic              |
| Code analysis  | Tree-sitter (official grammar packages)      |
| Knowledge graph| Neo4j *(Phase 6)*                            |
| Vector search  | Qdrant + open-source embeddings *(Phase 8)*  |
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
└── docs/             # documentation
```

## Getting started

### Prerequisites

- Python 3.11 or newer
- Git (used to clone GitHub repositories)
- Node.js 20.19+ (or 22.12+) and npm

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
pytest               # fast tests, no internet needed
pytest -m network    # clones a real repository from GitHub
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

## Roadmap

1. ✅ Project setup
2. ✅ Repository ingestion (GitHub clone, ZIP upload, file scanning, language detection)
3. ✅ Tree-sitter integration (Python, Java, JavaScript, TypeScript)
4. ✅ Entity extraction (files, classes, interfaces, functions, methods)
5. ✅ Relationship extraction (imports, inheritance, calls, uses, dependencies)
6. Knowledge graph (Neo4j)
7. Graph retrieval
8. Vector RAG (chunking, embeddings, Qdrant)
9. GraphRAG (hybrid retrieval + context fusion)
10. LLM assistant (`POST /projects/{id}/chat`)
11. Frontend (import, dashboard, explorer, chat)
12. Graph visualization
13. Advanced analysis (impact, dependencies, architecture summary)
14. Testing (integration, retrieval, end-to-end)
15. Docker & finalization
