# CodeGraph AI

**GraphRAG-powered codebase analysis assistant.**

CodeGraph AI imports a software repository (GitHub URL or ZIP), analyzes its source code with
Tree-sitter, builds a knowledge graph of files, classes, functions and their relationships in
Neo4j, indexes the code semantically in Qdrant, and answers natural-language questions about the
project — with answers grounded in the code and linked to source locations.

> **Status:** Phase 1 — project setup. The backend and frontend skeletons run; analysis features
> are built incrementally (see [Roadmap](#roadmap)).

## Tech stack

| Layer          | Technology                                   |
| -------------- | -------------------------------------------- |
| Backend        | Python 3.11+, FastAPI, Pydantic              |
| Code analysis  | Tree-sitter *(Phase 3)*                      |
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
pytest
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
| `frontend/.env`         | `VITE_API_URL`  | `http://localhost:8000`  | Backend base URL                  |

## Security

CodeGraph AI is a **static** analysis tool: it never executes code from imported repositories.
Secrets (API keys, database passwords) are only ever read from environment variables.

## Roadmap

1. ✅ Project setup
2. Repository ingestion (GitHub clone, ZIP upload, file scanning, language detection)
3. Tree-sitter integration (Python, Java, JavaScript, TypeScript)
4. Entity extraction
5. Relationship extraction
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
