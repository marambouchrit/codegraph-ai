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
| Code analyzer      | Tree-sitter parsing, entity & relationship extraction | ✅ Parsing (3), entities (4), relationships (5) |
| Graph engine       | Neo4j storage and graph queries                     | Phases 6–7 |
| RAG engine         | Chunking, embeddings, Qdrant, hybrid retrieval      | Phases 8–9 |
| Chat engine        | Prompting and LLM provider abstraction              | Phase 10 |

## Current backend layout (Phase 5)

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
├── parsing/                 # Tree-sitter: source file -> syntax tree (Phase 3)
│   ├── base.py              # LanguageParser base class, ParseResult, syntax-error detection
│   ├── python_parser.py     # one small parser per language: it only picks the grammar
│   ├── java_parser.py
│   ├── javascript_parser.py
│   ├── typescript_parser.py # .ts/.mts/.cts -> TypeScript grammar, .tsx -> TSX grammar
│   └── service.py           # ParserService: safe file reading + parser selection
├── extraction/              # syntax tree -> code entities (Phase 4)
│   ├── models.py            # Entity, EntityType, FileEntities, ExtractionReport
│   ├── base.py              # EntityExtractor: shared tree walk (walk()), qualified names, IDs
│   ├── python_extractor.py  # one extractor per language: which nodes define entities
│   ├── java_extractor.py
│   ├── javascript_extractor.py
│   ├── typescript_extractor.py  # extends the JavaScript extractor (.ts and .tsx)
│   └── service.py           # EntityExtractionService: extractor selection, whole projects
└── relationships/           # entities -> relationships between them (Phase 5)
    ├── models.py            # Relationship, RelationshipType, UnresolvedReference, RelationshipReport
    ├── references.py        # raw facts found in one file: Reference, Import, VariableType
    ├── base.py              # ReferenceCollector: visits EntityExtractor.walk(), shared helpers
    ├── python_references.py # one collector per language: which nodes are imports, calls...
    ├── java_references.py
    ├── javascript_references.py
    ├── typescript_references.py # extends the JavaScript collector
    ├── modules.py           # ModuleIndex: import -> project file (Python, JS/TS, Java packages)
    ├── resolver.py          # ReferenceResolver: name -> entity, shared by all languages
    └── service.py           # RelationshipExtractionService: collect per file, then resolve
```

Dependencies point one way: `routes → services → ingestion`, `parsing → ingestion` and
`relationships → extraction → parsing → ingestion`. None of these layers knows anything
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
executed. Entities are extracted from these trees by the extraction layer (Phase 4).

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

## Entity extraction (Phase 4)

The extraction layer walks each syntax tree and lists the **entities** it defines. Each entity
is designed to become one node of the Neo4j graph in Phase 6.

```
ScannedFile ─> ParserService.parse_file() ─> ParseResult ─> EntityExtractionService.extract()
                     (Phase 3)                                  │  get_extractor(language)
                                                                ▼
                                           PythonExtractor | JavaExtractor |
                                           JavaScriptExtractor | TypeScriptExtractor
                                                                │  EntityExtractor.extract()
                                                                ▼
                                           FileEntities: [File, Class, Method, Function...]
```

`EntityExtractionService.extract_project()` does this for every file, one at a time, so each
syntax tree is released as soon as its entities are extracted. Files that cannot be read or have
no parser are recorded as failures (Phase 3's `ParseFailure`); they never stop the others.

### Entity model

| Field | Example |
| --- | --- |
| `id` | `3f2a…c9:src/models/user.py:User.login` |
| `type` | `file`, `class`, `interface`, `function`, `method` |
| `name` / `qualified_name` | `login` / `User.login` |
| `file_path`, `language` | `src/models/user.py`, `python` |
| `start_line`, `start_column`, `end_line`, `end_column` | 1-based, like `SyntaxErrorInfo` |
| `parent_id` | ID of the enclosing entity (`None` only for files) |

- **Hierarchy through `parent_id`:** File → Class → Method, File → Function, Class → nested
  Class, Function → nested Function. Phase 6 turns these links into `CONTAINS` edges.
- **Method or function?** A function whose nearest enclosing entity is a class or interface is a
  METHOD; anywhere else (module level, inside another function) it is a FUNCTION.
- **Language mapping:** Python `class_definition`, `function_definition`; Java classes, enums and
  records → CLASS, interfaces and annotation types → INTERFACE, methods and constructors →
  METHOD; JavaScript/TypeScript classes, function declarations, functions/classes assigned to a
  variable (`const f = () => {}`), class methods and arrow-function fields; TypeScript adds
  interfaces, abstract classes, interface method signatures and abstract methods. Each extractor
  module documents its exact node types.
- **Project** entities are not extracted: the project already exists (`Project` in
  `project.json`, and its ID prefixes every entity ID). **Module** entities are not extracted
  either: in Python, JavaScript and TypeScript a module *is* a file, so the FILE entity plays
  that role. Java packages (and Python packages as folders) can be derived from paths and
  `package` declarations when Phase 5/6 need them for imports.

### IDs

`<project_id>:<file_path>` for files and `<project_id>:<file_path>:<qualified_name>` for the
rest. They are deterministic (the same code always gives the same IDs), readable, unique across
projects, and they do not change when code moves to another line. When one file defines the
same qualified name more than once (Java overloads, a Python function redefined), the second
gets `#2`, the third `#3`, in source order.

### Tree walk instead of Tree-sitter queries

Extraction uses a direct traversal with an explicit stack (like `find_syntax_errors`), carrying
the nearest enclosing entity for every node. That gives the parent of each entity for free,
which is what decides "method or function" and builds qualified names. Tree-sitter queries
return flat lists of matches, so the hierarchy would have to be rebuilt afterwards, and they add
a second pattern language to learn. Queries may be worth it in Phase 5 for call and import
patterns.

### Broken code

Files with syntax errors are not rejected. Every well-formed definition is extracted, but
nothing is invented:
- a definition whose name is missing or broken is skipped **with everything inside it**, so its
  methods are never attached to the wrong parent;
- code inside an `ERROR` node is ignored: Tree-sitter could not understand its context (a
  method there can look like a module-level function).

A broken region can therefore hide a few entities, but no entity is ever reported with the wrong
type or parent.

## Relationship extraction (Phase 5)

The relationship layer connects the entities of Phase 4. Each relationship is designed to become
one edge of the Neo4j graph in Phase 6.

| Type | From → to | Found in |
| --- | --- | --- |
| `IMPORTS` | file → file | `import` / `from … import` / `require()` / `export … from` / Java `import` |
| `INHERITS` | class → class, interface → interface | `class A(B)`, `extends` |
| `IMPLEMENTS` | class → interface | Java / TypeScript `implements` |
| `CALLS` | function, method or file → function, method or class | `f()`, `obj.m()`, `this.m()`, `super.m()`; `User()` / `new User()` call the class |
| `USES` | entity → class, interface (or function for JSX) | type annotations, field / parameter / return types, `<Component />` |
| `DEPENDS_ON` | file → file | derived: code of A calls, uses, extends or implements code of B |

### Two steps, one parse

Resolving `User` needs the entities of the whole project, but syntax trees are released file by
file. So the work is split:

```
per file (tree available)                          whole project (trees released)
─────────────────────────                          ──────────────────────────────
ParseResult ─> EntityExtractionService.extract()   ReferenceResolver(all FileEntities,
         │         -> FileEntities                                   all FileReferences)
         └─> collector.collect(extractor.walk())   stage 1: IMPORTS, INHERITS, IMPLEMENTS
                   -> FileReferences               stage 2: CALLS, USES (needs parent classes)
                      (imports, references,        stage 3: DEPENDS_ON from stages 1–2
                       variable types)                     -> RelationshipReport
```

`EntityExtractionService.extract_project(..., on_file=...)` calls the collector while each tree
is alive, so every file is still parsed exactly once. `EntityExtractor.walk()` (the Phase 4 walk,
now reusable) gives every node with its enclosing entity: a call inside `User.login` has
`User.login` as its source, and code skipped by Phase 4 (ERROR nodes, broken definitions) is
skipped here too.

### Resolution rules

A name is looked up the way the language would: enclosing entities (innermost first; in Java
also the members of the enclosing classes and their parents), then names bound by imports
(following re-exports such as `__init__.py` or `index.ts` barrels), then Java same-package
types and wildcard imports. `obj.method()` is resolved through `self`/`this`, `super`, a class or
module name, or a variable whose type is written in the code (`u = User()`, `User u`,
`u: User`, `this.repo = new Repo()`). Methods are also searched in parent classes.

Modules: Python relative imports are resolved from the importing file; absolute imports match
a file whose source root is an ancestor folder of the importer (the project root, `src/`,
`backend/`…). JavaScript/TypeScript relative specifiers try TypeScript then JavaScript
extensions and `index.*`; bare specifiers (`react`) are packages. Java uses `package`
declarations.

### Unresolved references

Only explicit evidence (a definition or an import) produces a relationship; a name is never
matched against a random entity elsewhere in the project. Anything else becomes an
`UnresolvedReference` with a reason: `external` (library), `not_found` (built-ins, globals),
`ambiguous` (several candidates, e.g. Java overloads) or `unknown_receiver` (`x.m()` with an
unknown `x`). They are kept out of `relationships` so every edge points to a real node, and are
reported separately, so Phase 6 can ignore them or later model external libraries.

### IDs and duplicates

`<TYPE>:<source_id>-><target_id>`, e.g. `CALLS:p:auth.py:login->p:user.py:User.save`. The same
(source, type, target) is one relationship whatever the number of occurrences; it keeps the
position of the first one. Unresolved references are deduplicated the same way.

## Future packages

New packages (`graph/`, `rag/`, `llm/`) are added only when the phase that needs them is
reached. The Neo4j graph (Phase 6) will build on `relationships/`.
