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
| Code analyzer      | Tree-sitter parsing, entity & relationship extraction | ✅ Parsing (Phase 3); extraction Phases 4–5 |
| Graph engine       | Neo4j storage and graph queries                     | Phases 6–7 |
| RAG engine         | Chunking, embeddings, Qdrant, hybrid retrieval      | Phases 8–9 |
| Chat engine        | Prompting and LLM provider abstraction              | Phase 10 |

## Current backend layout (Phase 3)

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
├── ingestion/               # small, independent building blocks
│   ├── github.py            # URL validation + safe shallow `git clone`
│   ├── zip_handler.py       # safe ZIP extraction
│   ├── workspace.py         # workspace folders, project IDs, deletion
│   ├── scanner.py           # recursive source-file discovery
│   └── languages.py         # file extension -> Language
└── parsing/                 # Tree-sitter: source file -> syntax tree (Phase 3)
    ├── base.py              # LanguageParser base class, ParseResult, syntax-error detection
    ├── python_parser.py     # one small parser per language: it only picks the grammar
    ├── java_parser.py
    ├── javascript_parser.py
    ├── typescript_parser.py # .ts/.mts/.cts -> TypeScript grammar, .tsx -> TSX grammar
    └── service.py           # ParserService: safe file reading + parser selection
```

Dependencies point one way: `routes → services → ingestion` and `parsing → ingestion`
(`parsing` reuses `Language` and `ScannedFile`). Neither `ingestion` nor `parsing` knows anything
about FastAPI, so they are easy to test and to reuse in later phases.

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

## Parsing layer (Phase 3)

The parsing layer turns the files found by `scan_directory()` into
[Tree-sitter](https://tree-sitter.github.io/) syntax trees (ASTs). It only parses: nothing is
executed, and no entities are extracted yet (Phase 4).

```
ScannedFile (path, language)                    e.g. from scan_directory(workspace/<id>/source)
   │
   ▼  ParserService.parse_file(root, path, language)
read_source_file()     refuse symlinks, paths outside the project, files > MAX_SOURCE_FILE_KB,
   │                   binary files (NUL bytes); the raw bytes are never decoded
   ▼
get_parser(language)   Language -> PythonParser | JavaParser | JavaScriptParser | TypeScriptParser
   │                   unknown language -> UnsupportedLanguageError
   ▼
LanguageParser.parse() grammar_for(path) -> tree_sitter.Parser(grammar).parse(bytes)
   │
   ▼
ParseResult            path, language, source, tree, root_node, root_info,
                       has_syntax_errors, syntax_errors (ERROR / MISSING nodes, 1-based lines)
```

- **Syntax errors never raise.** Tree-sitter always returns a complete tree and marks the parts
  it could not understand with `ERROR` nodes (unexpected code) or `MISSING` nodes (a token it
  had to invent, such as a missing `;`). `find_syntax_errors()` lists up to 50 of them, in
  source order, so later phases can still use the valid parts of a broken file.
- **One bad file never stops a project.** `ParserService.parse_files()` returns a `ParseReport`:
  `results` (parsed files, with or without syntax errors) and `failures` (files that could not be
  read or have no parser, with a reason).
- **Adding a language** means adding a grammar package, a `Language` value with its extensions,
  and a `LanguageParser` subclass that returns the grammar.

### Why individual grammar packages

The official per-language packages (`tree-sitter-python`, `tree-sitter-java`,
`tree-sitter-javascript`, `tree-sitter-typescript`) were chosen over `tree-sitter-language-pack`:

| | Individual packages | `tree-sitter-language-pack` (1.x) |
| --- | --- | --- |
| Grammars | Compiled into the wheel at install time | Wheel ships none; native parser libraries are **downloaded at runtime** into a cache |
| Offline / tests | Work offline | First use of a language needs internet |
| Security | Only code pinned in `requirements.txt` | Native code fetched while the server runs |
| Size | ~0.5 MB for our 4 languages | Pack for 300+ languages we do not need |
| Maintainer | The Tree-sitter organization | Third party |

We need only four languages, so the pack's breadth brings no benefit, while downloading native
code at runtime conflicts with the project's security and offline-testing rules.

## Future packages

New packages (`graph/`, `rag/`, `llm/`) are added only when the phase that needs them is
reached. Entity and relationship extraction (Phases 4–5) will build on `parsing/`.
