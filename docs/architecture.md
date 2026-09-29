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
| RAG engine         | Chunking, embeddings, Qdrant, hybrid retrieval      | Phases 8–9 |
| Chat engine        | Prompting and LLM provider abstraction              | Phase 10 |

## Current backend layout (Phase 7)

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
├── services/                # orchestration: the only places combining the packages below
│   ├── project_service.py   # ingestion: workspace, clone/ZIP, scan, project.json
│   ├── graph_service.py     # knowledge graph: analysis (Phases 3-5) -> Neo4j
│   └── graph_retrieval_service.py  # graph retrieval: validation, defaults, "not found"
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
└── graph/                   # Neo4j knowledge graph (Phase 6); no Tree-sitter, no FastAPI
    ├── client.py            # Neo4jClient: driver, sessions, transactions, error translation
    ├── schema.py            # labels, relationship types, constraint/index (whitelists)
    ├── models.py            # GraphNode, GraphEdge, GraphBuildReport, GraphStatistics,
    │                        # retrieval results: EntityResult, RelatedEntity, GraphPath...
    ├── repository.py        # GraphRepository: all the Cypher (writes, stats, retrieval reads)
    └── builder.py           # GraphBuilder: RelationshipReport -> nodes and edges
```

Dependencies point one way: `routes → services → ingestion`, `parsing → ingestion`,
`relationships → extraction → parsing → ingestion` and `services → graph → relationships`
(`graph/` only consumes Phase 4–5 data: it never parses code). None of these layers knows anything
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

## Future packages

New packages (`rag/`, `llm/`) are added only when the phase that needs them is reached. GraphRAG
(Phase 9) will combine `GraphRetrievalService` with vector search (Phase 8).
