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
| Graph engine       | Neo4j storage and graph queries                     | ✅ Storage (6), retrieval (7) |
| RAG engine         | Chunking, embeddings, Qdrant, hybrid retrieval      | ✅ Vector search (8), GraphRAG (9) |
| Chat engine        | Prompting and LLM provider abstraction              | Phase 10 |

## Current backend layout (Phase 9)

```
backend/app/
├── main.py                  # create_app(): FastAPI instance, CORS, error handler, routers
├── core/
│   ├── config.py            # Settings loaded from environment / .env
│   ├── urls.py              # safe_uri(): server URLs without credentials
│   ├── errors.py            # AppError + subclasses, each with an HTTP status code
│   └── logging.py           # logging setup
├── api/
│   ├── dependencies.py      # get_project_service() for Depends(...)
│   └── routes/
│       ├── health.py        # GET /health
│       └── projects.py      # /projects endpoints (thin: call the service, return schemas)
├── schemas/project.py       # API request/response models
├── services/                # orchestration: the only places combining the packages below
│   ├── project_service.py   # ingestion: workspace, clone/ZIP, scan, project.json
│   ├── graph_service.py     # knowledge graph: analysis (Phases 3-5) -> Neo4j
│   ├── graph_retrieval_service.py  # graph retrieval: validation, defaults, "not found"
│   ├── vector_index_service.py     # vector index: files -> chunks -> embeddings -> Qdrant
│   ├── vector_retrieval_service.py # semantic search: validation, top_k, filters
│   └── graphrag_service.py  # GraphRAG: vector hits -> seeds -> graph expansion -> context
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
├── relationships/           # entities -> relationships between them (Phase 5)
│   ├── models.py            # Relationship, RelationshipType, UnresolvedReference, RelationshipReport
│   ├── references.py        # raw facts found in one file: Reference, Import, VariableType
│   ├── base.py              # ReferenceCollector: visits EntityExtractor.walk(), shared helpers
│   ├── python_references.py # one collector per language: which nodes are imports, calls...
│   ├── java_references.py
│   ├── javascript_references.py
│   ├── typescript_references.py # extends the JavaScript collector
│   ├── modules.py           # ModuleIndex: import -> project file (Python, JS/TS, Java packages)
│   ├── resolver.py          # ReferenceResolver: name -> entity, shared by all languages
│   └── service.py           # RelationshipExtractionService: collect per file, then resolve
├── graph/                   # Neo4j knowledge graph (Phase 6); no Tree-sitter, no FastAPI
│   ├── client.py            # Neo4jClient: driver, sessions, transactions, error translation
│   ├── schema.py            # labels, relationship types, constraint/index (whitelists)
│   ├── models.py            # GraphNode, GraphEdge, GraphBuildReport, GraphStatistics,
│   │                        # retrieval results: EntityResult, RelatedEntity, GraphPath...
│   ├── repository.py        # GraphRepository: all the Cypher (writes, stats, retrieval reads)
│   └── builder.py           # GraphBuilder: RelationshipReport -> nodes and edges
├── rag/                     # vector search (Phase 8); no Neo4j, no FastAPI
│   ├── models.py            # CodeChunk, ChunkSearchResult, VectorIndexReport
│   ├── chunker.py           # CodeChunker: source + entities -> code-aware chunks
│   ├── embeddings.py        # EmbeddingProvider, SentenceTransformerEmbeddings (local)
│   └── vector_store.py      # QdrantVectorStore: collection, upsert, search, deletes
└── graphrag/                # GraphRAG (Phase 9): uses graph/ and rag/ models, no queries
    ├── models.py            # GraphRAGContext, Seed, ContextEntity, ContextRelationship, Source
    └── expansion.py         # which graph questions to ask per seed type
```

Dependencies point one way: `routes → services → ingestion`, `parsing → ingestion`,
`relationships → extraction → parsing → ingestion`, `services → graph → relationships` and
`services → rag → extraction` (`graph/` only consumes Phase 4–5 data: it never parses code;
`rag/` never imports `graph/`; `graphrag/` uses both). None of these layers knows anything
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

## Knowledge graph (Phase 6)

**Why Neo4j:** code is naturally a graph, and the questions later phases must answer are graph
questions ("who calls `User.save`?", "what depends on `auth.py`?"). A graph database stores the
connections themselves, so following them is cheap, and Cypher queries read like the question:
`MATCH (f)-[:CALLS]->(m:Method {name: "save"}) RETURN f`.

```
GraphService.build_project_graph(project_id)
   1. ProjectService.get_project()            unknown project -> ProjectNotFoundError
   2. Neo4jClient.verify_connectivity()       fail fast, before the analysis
   3. RelationshipExtractionService           Phases 3-5: entities + relationships (one parse)
   4. GraphBuilder.build(RelationshipReport)
        graph_nodes(): Entity       -> GraphNode  (label = entity type)
        graph_edges(): parent_id    -> CONTAINS edge
                       Relationship -> GraphEdge  (same ID and type as Phase 5)
        GraphRepository:
          ensure_schema()            constraint + index (IF NOT EXISTS)
          upsert_nodes()             UNWIND $rows + MERGE, one batch per transaction
          upsert_relationships()
          delete_stale()             what an older build of this project left
   -> GraphBuildReport: files, nodes/relationships written (by label/type), stale deleted,
                        unresolved references, failed files, duration, summary
```

### Node model

Every node has two labels, `:Entity` and its type (`:File`, `:Class`, `:Interface`, `:Function`,
`:Method`), and the properties `id` (the Phase 4 entity ID, the node identity; no other ID is
generated), `project_id`, `entity_type`, `name`, `qualified_name`, `file_path`, `language`,
`start_line`, `start_column`, `end_line`, `end_column`, `parent_id` (absent on files) and
`build_id`. For a File, `name` is the file name and `qualified_name` its path. There is no
Project node: the project lives in `project.json`, and `project_id` on every node scopes it.

### Relationship model

The six Phase 5 types (`IMPORTS`, `INHERITS`, `IMPLEMENTS`, `CALLS`, `USES`, `DEPENDS_ON`) with
properties `id` (the Phase 5 ID `<TYPE>:<source_id>-><target_id>`), `file_path`, `line`,
`column` (first occurrence) and `build_id`, plus `CONTAINS` (File → Class/Function, Class →
Method…), derived from `parent_id`, with an ID in the same format. Unresolved references are
only counted in the report: they never become nodes or edges.

### Idempotency and stale data

- **Nodes:** `MERGE (n:Entity {id: row.id})` finds the node or creates it, then
  `SET n += row.properties` updates it. The type label is removed and set again, so an entity
  that changed type (a function turned into a class) never keeps two labels.
- **Relationships:** both ends are `MATCH`ed by `id` *and* `project_id`, then
  `MERGE (source)-[r:CALLS {id: row.id}]->(target)`.
- **Stale data:** each build stamps a new `build_id` on everything it writes. Once all batches
  have succeeded, the project's nodes and relationships with another `build_id` (code deleted
  since the previous build) are deleted. Building twice gives the same graph, and the graph is
  never emptied before a rebuild. If a batch fails, the error is raised and nothing is deleted:
  the graph holds old and new data until the next successful build.

### Constraint and index

| Schema | Why |
| --- | --- |
| `CONSTRAINT entity_id FOR (n:Entity) REQUIRE n.id IS UNIQUE` | at most one node per entity ID; it also creates the index every `MERGE`/`MATCH` on `id` uses |
| `INDEX entity_project_id FOR (n:Entity) ON (n.project_id)` | every project operation (delete, stale cleanup, statistics) starts from `project_id` |

No `(project_id, id)` constraint: entity IDs already start with the project ID. The shared
`:Entity` label exists because a constraint applies to a single label. Both statements use
`IF NOT EXISTS`, so they run at every build without effect after the first.

### Project isolation

Nodes carry `project_id`; every project-level query filters on it (`delete_project`,
`delete_stale`, `statistics`, `project_exists`), and relationships are only created between two
nodes of the same project. The builder refuses an entity whose ID does not start with the
project ID. Deleting one project's graph never touches the others, and nothing ever deletes the
whole database.

### Batching

Rows are grouped by label (nodes) or type (relationships), because labels and types are part of
the query text, and sent `GRAPH_BATCH_SIZE` (1000) at a time as one `$rows` list parameter
expanded by `UNWIND`: a few queries per thousand entities instead of one per node. Each batch is
one managed transaction, which the driver retries on temporary errors (safe, because MERGE can be
repeated). Deletions also run in batches (`WITH n LIMIT $limit DETACH DELETE n`) until nothing is
left, so a large project never needs one huge transaction.

### Errors

`Neo4jClient.neo4j_errors()` is the only place that sees driver exceptions. It turns them into
`AppError`s: `GraphDatabaseUnavailableError` (503: server down, session expired, transient error
still failing after retries), `GraphDatabaseConfigError` (503: wrong credentials, invalid URI,
unknown database) and `GraphDatabaseError` (500: constraint violation, failed transaction).
Messages never contain the password, and URIs are shown without any `user:password@` part.

### Security

Values always travel as Cypher parameters (`$rows`, `$project_id`…). Labels and relationship
types, which Cypher cannot parametrize, come only from the `schema.py` whitelists
(`node_label()` and `relationship_type()` reject anything else). Credentials come from the
environment and the password is a `SecretStr`. Docker Compose binds Neo4j to `127.0.0.1`.

### Testing

`pytest` needs no Neo4j: `tests/graph_fakes.py` replaces the **driver** with an in-memory store
that executes the repository's few queries, so the real client, repository (batches,
parameters, MERGE keys, project filters) and builder run unchanged. The optional
`pytest -m neo4j` tests run builds against a real server (random project IDs, cleaned up
afterwards) and check idempotency, isolation and a `(:Function)-[:CALLS]->()` traversal.

Not in Phase 6, on purpose: graph API endpoints and deleting the graph when a project is deleted
through the API (that would make project deletion depend on Neo4j being up; it will be wired
together with the graph endpoints).

## Graph retrieval (Phase 7)

**What it is:** reading the knowledge graph back to answer *structural* questions about the
code: who calls this method, what does this file depend on, how are these two functions
connected. Text search or embeddings (Phase 8) find code that *looks* relevant; only the graph
knows how pieces are *connected*, and its answers are exact and come with source locations.
Phase 9 (GraphRAG) will combine both to build the context of an LLM answer.

```
GraphRetrievalService          app/services/graph_retrieval_service.py
   validates: project ID format, entity ID of this project, depth, limit, search text, types
   defaults:  limit 50 (max 200), transitive depth 3, path depth 4, 5 paths (max 20)
   errors:    unknown entity -> EntityNotFoundError (404), bad argument -> InvalidGraphQueryError (400)
        ↓
GraphRepository                app/graph/repository.py: one fixed query shape per operation
        ↓
Neo4jClient.read()             read transaction; driver errors -> GraphDatabase*Error
        ↓
Neo4j
```

The service has no Cypher; the repository validates only what could change the query text.
Results are typed models (`app/graph/models.py`), never raw Neo4j records: `EntityResult` (ID,
type, name, qualified name, project, file, language, start/end line and column, parent ID),
`RelationshipResult` (ID, type, source, target, and the file, line and column where the call or
import is), `RelatedEntity` (an entity, the direction, and the relationship followed or the depth
reached), `GraphPath` (nodes and the relationships between them) and `GraphContext` (an entity,
its parent and its neighbors).

### Operations

| Question | Service method | Pattern |
| --- | --- | --- |
| Find the User class | `find_entities(p, "User", entity_types=["class"])` | exact ID, then qualified name, then name; `partial=True` adds case-insensitive "contains" matches |
| Everything about an entity | `get_entity`, `get_entity_context` | entity + parent + neighbors both ways |
| What is connected to User? | `get_neighbors` | `(start)-[r]->(other)` and `(start)<-[r]-(other)`, all types |
| What methods does User have? | `get_contained_entities` | `-[:CONTAINS]->`; `max_depth` > 1 for nested definitions |
| Who calls User.save? / What does login call? | `get_callers` / `get_callees` | `<-[:CALLS]-` / `-[:CALLS]->` |
| What does auth.py import? / Who imports user.py? | `get_imports` / `get_importers` | `-[:IMPORTS]->` / `<-[:IMPORTS]-` |
| What does auth.py depend on (indirectly)? | `get_dependencies` / `get_transitive_dependencies(max_depth=3)` | `-[:DEPENDS_ON]->` / `-[:DEPENDS_ON*1..3]->` |
| What depends on user.py? | `get_dependents(max_depth=1)` | `<-[:DEPENDS_ON*1..N]-` |
| What does Admin inherit from? / Who inherits from User? | `get_parents` / `get_subclasses` | `-[:INHERITS]->` / `<-[:INHERITS]-` |
| What does UserService implement? / Who implements IService? | `get_implemented_interfaces` / `get_implementations` | `-[:IMPLEMENTS]->` / `<-[:IMPLEMENTS]-` |
| How is login connected to User.save? | `find_paths(max_depth=4, directed=True)` | `allShortestPaths((source)-[*1..4]->(target))` |

IMPORTS and DEPENDS_ON link files (Phase 5), so those operations take a file ID; asked about a
class, they return an empty list. For example, "who calls `User.save`?":

```cypher
MATCH (start:Entity {id: $entity_id, project_id: $project_id})
      <-[r:CALLS]-(other:Entity {project_id: $project_id})
WHERE $entity_types IS NULL OR other.entity_type IN $entity_types
RETURN properties(other) AS entity, type(r) AS type, properties(r) AS relationship,
       startNode(r).id AS source_id, endNode(r).id AS target_id
ORDER BY type(r), other.file_path, other.start_line, other.id
LIMIT $limit
```

**Search and ambiguity:** entities sharing a name (three `run` methods) are all returned, exact
matches ranked before partial ones, then sorted by qualified name and path. The caller chooses;
nothing is picked at random. **Not found vs. nothing found:** an empty result about an existing
entity is an empty list; if the entity itself does not exist, a second, cheap lookup turns the
empty result into `EntityNotFoundError` (so the common case costs one query).

### Project isolation

- The project ID must be 32 hexadecimal characters (the workspace rule), else
  `ProjectNotFoundError` before any query.
- Entity IDs always start with `<project_id>:` (Phase 4). An ID without the project's prefix is
  "not found" without asking Neo4j, so another project's entity cannot be reached, even by
  mistake.
- Every query matches its nodes with `{project_id: $project_id}`: the start node, the node at
  the other end of each relationship and, for multi-hop traversals and paths, every node of the
  path (`all(n IN nodes(path) WHERE n.project_id = $project_id)`), even though Phase 6 never
  links two projects.

### Depth and limits

Cypher cannot take the bounds of a variable-length pattern as parameters, so `*1..3` is part of
the query text. `traversal_depth()` only lets through an `int` from 1 to `MAX_TRAVERSAL_DEPTH`
(5; booleans, strings and floats are refused), and the service turns bad values into a 400
first. There is never an unbounded `[:DEPENDS_ON*]`: the number of paths a variable-length
pattern visits can grow exponentially with its length. Multi-hop results list each entity once,
at its shortest distance (`min(length(path))`). `find_paths` returns the shortest paths only
(`allShortestPaths`), sorted by the IDs along them, so the same question always gets the same
answer. Every query ends with `LIMIT $limit`; `get_neighbors` applies the limit per direction.
Every query starts with an index seek (the `id` constraint or the `project_id` index): no new
index was needed.

### Security

The Phase 6 rules apply: values are parameters (`$project_id`, `$entity_id`, `$text`,
`$entity_types`, `$limit`); labels and relationship types come from the `schema.py` whitelists
(sorted, so a set of types always gives the same query), directions from the `Direction` enum and
depths from `traversal_depth()`. There is no "run this Cypher" method; retrieval queries are
read-only and run in read transactions.

### Testing

`tests/test_graph_retrieval.py` writes a small Java project (`tests/retrieval_helpers.py`) into
two projects with the real `GraphBuilder`, on the fake driver, and checks every operation,
ordering, ambiguity, depth and limits, isolation, not-found cases, invalid arguments, malicious
values and error translation, plus one project analyzed from real Python sources by Phases 3-5.
`tests/graph_fakes.py` answers the retrieval queries in memory, and only accepts a query that is
exactly the text the repository's builder produces. `pytest -m neo4j` asks every question of
`retrieval_helpers.QUESTIONS` to a real Neo4j and to the fake, and requires identical answers.

Not in Phase 7, on purpose: HTTP endpoints for retrieval (they will come with the phase that
uses them), vector search, and anything LLM-related.

## Vector RAG (Phase 8)

**Why:** the knowledge graph answers *how code is connected* ("who calls `authenticate()`?"),
but not *where something is done* when the question does not name the code: "how is
authentication implemented?" names no class or function. Vector search finds code whose
*meaning* is close to the question, even without shared words. It does not replace the graph:
it cannot say who calls what. Phase 9 combines both.

```
Repository ─> Phase 2 files ─> Phase 3 ParserService ─> Phase 4 EntityExtractionService
                                     (one parse per file, on_file callback)
                                                   │ source bytes + entities
                                                   ▼
                                  CodeChunker            app/rag/chunker.py
                                                   │ CodeChunk (text + metadata)
                                                   ▼
                                  EmbeddingProvider      app/rag/embeddings.py (local model)
                                                   │ normalized vectors
                                                   ▼
                                  QdrantVectorStore      app/rag/vector_store.py
                                                   │
                                                   ▼
                                               Qdrant
                                                   ▲
            question ─> VectorRetrievalService ─> embed_query ─> search (project filter)
```

`VectorIndexService` (indexing) and `VectorRetrievalService` (search) live in `app/services/`,
next to the graph services; `app/rag/` holds the building blocks and never imports `app/graph/`.
The source files are the source of truth: nothing is read from Neo4j. The link to the graph is
the chunk's `entity_id`: it is the Phase 4 entity ID, which is also the Neo4j node ID.

### Chunking strategy

Chunks follow the Phase 4 entity tree instead of cutting every N characters (which would split
functions in half and glue unrelated code together):

| Unit | Chunk text |
| --- | --- |
| Function, method | its full source, decorators/annotations included; functions and classes defined inside it stay in it |
| Class, interface | a **skeleton**: the class with each member that has its own chunk collapsed to its signature and `...` (fields, docstrings, signatures stay) |
| File | module-level code (imports, constants, scripts) with each top-level definition collapsed; no file chunk when a file only holds definitions |

Every line of code is in exactly one chunk body; only signatures are repeated as context. An
entity already shown whole by its parent (a one-line method, `interface Listener { void
changed(); }`) gets no chunk of its own. A chunk longer than `VECTOR_CHUNK_MAX_CHARS` (2000,
about 500 tokens; BGE-M3 accepts 8192, but one chunk per function keeps results precise) is split on line boundaries into parts that
repeat `VECTOR_CHUNK_OVERLAP_LINES` (3) lines; a longer single line (minified code) is cut.
Files with syntax errors are chunked like the others (Phase 4 still extracts their well-formed
definitions); unreadable files are counted in the report. What is embedded is the chunk text
after a short header built from the metadata (`python method AuthService.login` / `file:
app/auth/service.py`): paths and entity kinds carry meaning the code alone may not.

### Embedding model

`EmbeddingProvider` (`model_name`, `dimension`, `embed_documents`, `embed_query`) hides the
model; `SentenceTransformerEmbeddings` runs any sentence-transformers model locally. The model
is loaded on first use, once per process (`embedding_provider_from_settings()`, cached and
thread-safe), on `EMBEDDING_DEVICE` (default `cpu`; `cuda`/`mps` optional), with
`trust_remote_code=False`. No code leaves the machine and no API key is needed.

Default: **`BAAI/bge-m3`**, its **dense** output only (no sparse or multi-vector retrieval):
1024 dimensions, 8192 input tokens, multilingual. It is downloaded once into the standard
Hugging Face cache (`HF_HOME`): 2.3 GB of weights (`pytorch_model.bin`); on first load,
`transformers` also fetches a `safetensors` copy of the weights in the background, so plan for
about 4.5 GB of disk. Measured on a 12-thread laptop CPU: load 19–30 s from the cache, about
2 GB of RAM once embedding, a query in about 0.1 s, and an indexing speed given in
[Changing the embedding model](#changing-the-embedding-model). `BAAI/bge-small-en-v1.5` (384
dimensions, ~130 MB) is much faster on a CPU and remains one setting away.

Documents and queries use the **same model** (vectors of two models live in unrelated spaces).
BGE-M3 needs no query instruction; bge v1.5 models expect one before queries only, which the
provider adds. Every vector is checked: the model's dimension, and finite values only (a NaN
would silently break every score). The dimension is never configured: it is read from the model
(`EmbeddingProvider.dimension`), and the collection is created from it.

**Distance:** cosine. Vectors are normalized, the similarity the bge models are trained for;
scores range from -1 to 1, higher is closer.

### Qdrant

One collection (`QDRANT_COLLECTION`, default `codegraph_chunks_bge_m3`) for every project,
Qdrant's recommended layout for many tenants, and one collection per embedding model. Each
point:

| Part | Content |
| --- | --- |
| ID | `uuid5(embedding model + chunk ID)`: Qdrant IDs must be UUIDs or integers |
| Vector | the chunk's embedding |
| Payload | `chunk_id`, `project_id`, `file_path`, `language`, `entity_id`, `entity_type`, `name`, `qualified_name`, `start_line`, `end_line`, `text`, `part`, `part_count`, `embedding_model`, `index_id` |

The payload keeps the chunk text so results need no file access, and lines let later phases
show or re-read the source. Payload indexes: `project_id` (tenant index: Qdrant stores each
project's points together), `embedding_model`, `index_id`, `language`, `entity_type`. A
collection whose dimension or distance does not match the model is refused
(`VectorCollectionError`) instead of mixing vectors, when indexing and when searching (a query
vector of another dimension gets that clear error, not Qdrant's HTTP 400).

**Chunk IDs** are `<entity_id>|<part>`, for example
`<project_id>:app/auth/service.py:AuthService.login|1`. They depend on the project, file and
qualified name (all already deterministic in Phase 4), not on line numbers: code moving down a
file keeps its IDs. Point IDs are derived from them and from the model, so indexing again with
the same model **overwrites** the same points (upsert) instead of adding duplicates, and indexing
with another model never overwrites the previous model's points.

### Re-indexing

Same approach as the Phase 6 graph: every point written by an indexing carries its random
`index_id`. Once every chunk is embedded and written, the project's points with another
`index_id` are deleted: code deleted or renamed since the last indexing. Nothing is deleted
before the new points are written, so a failure half-way (model error, Qdrant down) never
empties the index; search keeps working on old and new points until the next successful
indexing. Unchanged chunks are embedded again (no content-hash cache yet).

### Changing the embedding model

Changing `EMBEDDING_MODEL` does not change any stored vector. The safe procedure:

1. Set the new `EMBEDDING_MODEL` and a **new `QDRANT_COLLECTION`**. The old collection is never
   touched, so nothing is deleted before the new index exists.
2. Re-index the projects: `VectorIndexService.index_all_projects()` indexes every imported
   project, reports each failure without stopping the others, and stops at once if the model,
   Qdrant or the collection is unusable (it would fail the same way for every project).
3. Delete the old collection by hand once the new one works.

The knowledge graph does not change: chunk entity IDs are the Neo4j node IDs whatever the model.
If the collection is kept instead (same dimension), it stays safe: searches filter on
`embedding_model`, a new-model indexing writes new points next to the old ones, and the old
model's points are removed only after it succeeds, as stale points.

**CPU cost, measured** (12-thread laptop CPU, no GPU): migrating the Flask repository (83 files,
1,055 chunks) from bge-small to BGE-M3 wrote **352 chunks in about 21 minutes** (about 3.6 s per
chunk, so roughly an hour for the whole repository) before the run was stopped on purpose; the
old 384-dimension collection kept its 1,055 points untouched. bge-small indexes the same project
in under 5 minutes. Search stays fast (about 0.1 s per question). **Limitation:** the Flask
benchmark with BGE-M3 is incomplete, and indexing large repositories with BGE-M3 on a CPU is
slow; `EMBEDDING_DEVICE=cuda` (a GPU) or bge-small are the options when that matters.

### Retrieval

`VectorRetrievalService.retrieve(project_id, query, top_k=None, languages=None,
entity_types=None)`: validate (project ID format, non-empty query of at most 2000 characters,
`top_k` from 1 to `VECTOR_MAX_TOP_K` (50), default `VECTOR_TOP_K` (10), known languages and
entity types), embed the query, search Qdrant, return `ChunkSearchResult(chunk, score)` best
first (equal scores ordered by chunk ID). A project never indexed returns `[]`.

**No similarity threshold by default.** Cosine scores depend on the model, so "0.5" means
different things for different models. Measured with BGE-M3 on the test project (15 chunks):
the relevant hits of four real questions scored **0.52–0.68**, and the best hits of two
unrelated questions ("How is the weather forecast downloaded?") **0.38–0.45**. A threshold
around 0.5 would separate them there, but 6 questions on 15 chunks are not enough evidence to
choose one for every repository, so none is set: ranking plus `top_k` bounds the results, and
an unrelated question still gets low-scoring context. `VECTOR_MIN_SCORE` can be set once
measured on labelled questions; it is passed to Qdrant's `score_threshold`.

### Project isolation

Every search, count and delete goes through one filter builder that always starts with
`project_id == <id>` and refuses an empty project ID, so no call can cover every project.
Searches also filter on `embedding_model`. The project ID format is validated before anything
is embedded. Tests index the same code in two projects and check that results, counts and
deletions never cross.

### Errors and security

`QdrantVectorStore.qdrant_errors()` translates client exceptions: `VectorStoreUnavailableError`
(503: unreachable, timeout, rejected API key), `VectorCollectionError` (500: missing or
incompatible collection), `VectorStoreError` (500). The provider raises `EmbeddingModelError`
(503) when the model cannot be loaded or fails. `InvalidVectorQueryError` (400) reports bad
parameters. Messages never contain the API key (`SecretStr`) or source code; logs only name
exception types. Filters are built with qdrant-client objects (`FieldCondition`,
`MatchValue`...), never from text; collection names come from settings and must match
`[A-Za-z0-9_-]{1,64}`. Repository code is only read as text: never imported or executed.

### Testing

Unit tests need no server and no model: the real `QdrantVectorStore` runs on qdrant-client's
local in-memory mode (`QdrantClient(":memory:")`), and `HashingEmbeddings` (tests only) hashes
words into a normalized vector so texts sharing words are close. Chunker tests use real Phase 3-4
output in the four languages. Optional tests: `pytest -m qdrant` indexes and searches on a real
server (temporary collection) and checks it answers exactly like the in-memory mode;
`pytest -m embeddings` runs the real model (1024 dimensions for BGE-M3, finite normalized
vectors, batches equal to single embeddings), including the end-to-end check that "How does the
application authenticate users?" finds the login code; with Neo4j and Qdrant started,
`pytest -m "neo4j and qdrant and embeddings"` also runs GraphRAG with the real model. Unit tests
cover the model change itself: another model's vectors are never searched, a re-indexing with a
new model replaces the old points only once it succeeds, a failed one keeps the old index
searchable, and a query vector of the wrong dimension gets a clear error.

Not in Phase 8, on purpose: API endpoints, hybrid retrieval with the graph, reranking, LLM.

## GraphRAG retrieval (Phase 9)

**Why:** each retrieval alone gives half an answer. Vector search (Phase 8) finds code *about*
the question ("How is authentication implemented?" → `AuthService.login`) but knows nothing of
connections: that `login` calls `UserRepository.find_user`, which queries `Database`. The graph
(Phase 7) knows every connection, but only from an entity you name. GraphRAG uses vector hits as
**starting points** and the graph to add **how they are connected**. It builds structured
context only: no LLM, no prompt, no answer (Phase 10).

```
                    QUESTION
                        │
                        ▼
      Vector retrieval (VectorRetrievalService)  ── Qdrant, filtered by project
                        │  hits: chunk + score + entity_id + file + lines
                        ▼
           Seeds: distinct entity IDs, best score first (max 5)
                        │
                        ▼
      Graph retrieval (GraphRetrievalService)    ── Neo4j, same project
        per seed: a few one-hop questions chosen by its type,
        then shortest paths between the top 3 seeds
                        │
                        ▼
      GraphRAGContext: vector evidence + graph evidence + sources
                        │
                        ▼
                  Phase 10 LLM (not yet)
```

`GraphRAGService` (`app/services/graphrag_service.py`) only orchestrates the two retrieval
services: no Cypher, no Qdrant call, no second embedding. `app/graphrag/` holds the models and the
expansion strategy; it depends on `graph/` and `rag/`, which still know nothing of each other.
**The bridge is the Phase 4 entity ID:** a chunk's `entity_id` is the ID of a Neo4j node.

### From vector hits to seeds

The vector search returns `GRAPHRAG_VECTOR_TOP_K` (10) chunks. Several can belong to one entity
(parts of a long method), so they are grouped by `entity_id`: one **seed** per entity, ranked by
its best score, with all its chunk IDs. Only the first `GRAPHRAG_MAX_SEEDS` (5) are expanded; the
other hits stay as vector evidence. Each seed is looked up in the graph first: a seed missing
from the graph (the vector index and the graph built at different times) keeps its vector
evidence and gets a warning, and is not expanded.

### Graph expansion strategy

Not every question for every seed ("vector search + dump the graph" buries the relevant code).
Each type gets the one-hop questions that explain what it is and how it is used
(`app/graphrag/expansion.py`), each limited to `GRAPHRAG_NEIGHBORS_PER_EXPANSION` (5) results:

| Seed type | Expansions, in order |
| --- | --- |
| method, function | container (its class or file), callees, callers |
| class | container, members, parents, subclasses, implemented interfaces, callers (who creates it) |
| interface | container, members, parents, subclasses, implementations |
| file | members, dependencies, dependents (DEPENDS_ON already covers imports) |

Then the shortest path (depth ≤ `GRAPHRAG_PATH_MAX_DEPTH`, 3; one per pair, tried in both
directions) between each pair of the first `GRAPHRAG_PATH_SEEDS` (3) seeds shows how the top
results relate (`login → find_user → Database.query`). A one-relationship path already in the
context is not repeated as a path. Every question is a Phase 7 operation with
its own limits: expansions are one hop, paths are bounded; there is no other traversal.
`GraphRetrievalService.get_container()` was added for the container question (the CONTAINS edge,
read from Neo4j); nothing else in Phase 7 changed.

### Deduplication and ordering

- **Seeds:** one per entity, whatever the number of its chunks; each is expanded once.
- **Entities:** seeds first (by rank), then neighbors in discovery order: seed rank, then the
  strategy's order, then Phase 7's order. A neighbor reached from several seeds appears once,
  with all those seed IDs. A neighbor that is itself a seed stays a seed.
- **Relationships:** one per relationship ID, attributed to the first seed/expansion that found
  it (`login CALLS create_token` is both login's callee and create_token's caller: kept once).
- **Sources:** one per location (file, lines, entity).

There is **no combined score**: vector hits keep the Phase 8 order (rounded score, then chunk ID),
and graph evidence is attached to its seeds. Transparent and deterministic: the same question on
the same project gives the same context.

### The context

`GraphRAGContext` (`app/graphrag/models.py`): `project_id`, `query`, `vector_results` (Phase 8
`ChunkSearchResult`s: chunk text, score, entity, file, lines), `seeds` (entity, rank, best
score, chunk IDs, expansions run), `entities` (`ContextEntity`: Phase 7 `EntityResult`, role
seed/neighbor, the seeds that reached it, vector score if any), `relationships`
(`ContextRelationship`: Phase 7 `RelationshipResult` with type, ends, file/line/column, plus the
seed and expansion that found it), `paths`, `sources`, `graph_status`, `warnings`,
`statistics`. `describe()` gives a readable outline (for logs and debugging, not a prompt).

### Source traceability

Every item carries its entity ID, file path and line range. `sources` lists what to cite: every
vector chunk (its lines, score and chunk ID, reason `vector`), then every graph entity not already
cited through a chunk (its full lines, reason `graph`). "Why is this here?" always has an answer:
a vector score, or a relationship to a seed.

### Limits

| Setting | Default | Bounds |
| --- | --- | --- |
| `GRAPHRAG_VECTOR_TOP_K` | 10 | 1 – `VECTOR_MAX_TOP_K` (50) |
| `GRAPHRAG_MAX_SEEDS` | 5 | 1 – 20 |
| `GRAPHRAG_NEIGHBORS_PER_EXPANSION` | 5 | 1 – 200 |
| `GRAPHRAG_MAX_CONTEXT_ENTITIES` | 40 | max seeds – 200 (seeds always kept; relationships and sources of dropped neighbors are dropped too) |
| `GRAPHRAG_PATH_SEEDS` | 3 | 0 – 5 |
| `GRAPHRAG_PATH_MAX_DEPTH` | 3 | 1 – 5 |

At most 5 seeds × 6 expansions + 6 path queries: a few dozen bounded queries per question.

### Project isolation

The vector search validates the project ID and filters by project (Phase 8); hits of another
project are dropped anyway (with a warning); every graph question uses the same project ID, whose
prefix Phase 7 checks on every entity ID; graph neighbors and path nodes of another project are
dropped. Tests hold two projects with the same code in both stores and check that no ID, query
parameter or source crosses.

### Failure behavior

| Situation | Result |
| --- | --- |
| Invalid project ID, empty question | `ProjectNotFoundError` / `InvalidVectorQueryError`, before any query |
| Qdrant or the embedding model fails | the error is raised (no seeds, nothing to build on); Neo4j is not queried |
| A seed is not in the graph | kept as vector evidence, not expanded, warning; status stays `complete` |
| A seed has no neighbors | no graph evidence for it; not an error |
| Neo4j fails (`GraphDatabaseError`) | expansion stops; the context keeps the vector evidence and the graph evidence already collected, `graph_status` = `unavailable` (nothing collected) or `partial`, plus a warning. With `GRAPHRAG_REQUIRE_GRAPH=true`, the error is raised instead |

Vector evidence alone is still useful context, and the status makes the degradation explicit
instead of silent. Errors are `AppError`s; the service knows nothing about HTTP.

### Security

Nothing new is exposed: no Cypher or Qdrant filter is built here (only the typed operations of
Phases 7 and 8, with their whitelists and parameters), no traversal beyond one hop and bounded
paths, no code executed or sent anywhere, logs hold counts and error types only.

### Testing

`tests/test_graphrag.py` builds a small project (AuthService, UserRepository, Database, a
subclass, an unrelated file) twice, in the fake Neo4j (real Phase 3-6 pipeline) and in the
in-memory Qdrant (real chunker), for projects A and B. It checks seeds and deduplication, method,
class and file expansions, paths, missing seeds, isolation, Neo4j down or failing midway, Qdrant
down, every limit, determinism, source and relationship metadata, and that nothing but the two
retrieval services is used. An end-to-end test imports the project as a ZIP, builds the graph
and the vector index with the real services, and checks that "How is authentication
implemented?" gives `AuthService.login` with its lines, `CALLS UserRepository.find_user` and
`CALLS AuthService.create_token`. `pytest -m "neo4j and qdrant"` builds the same context on the
real servers and requires it to equal the fakes'.

Not in Phase 9, on purpose: API endpoints, LLM calls, prompts, reranking, query rewriting.

## Future packages

New packages (`llm/`...) are added only when the phase that needs them is reached. Phase 10 will
turn a `GraphRAGContext` into a prompt and an answer with citations.
