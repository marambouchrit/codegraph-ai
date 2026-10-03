// Types mirroring the backend's Pydantic schemas (backend/app/schemas/ and routes/health.py).
// The backend is the source of truth: keep these in sync with it, field for field.

export interface HealthResponse {
  status: string
  app: string
  version: string
}

// ----- Projects (schemas/project.py) -----

export type SourceType = 'github' | 'zip'

export interface GitHubProjectCreate {
  url: string
}

export interface Project {
  id: string
  name: string
  source_type: SourceType
  source: string // GitHub URL or uploaded file name
  created_at: string // ISO 8601 datetime
  file_count: number // supported source files
  total_size_bytes: number
  languages: Record<string, number> // source files per language
}

// ----- Analysis (schemas/analysis.py) -----

export interface GraphSummary {
  files: number
  entities: number
  relationships: number
  entities_by_type: Record<string, number>
  relationships_by_type: Record<string, number>
  unresolved_references: number
  stale_entities_removed: number
}

export interface VectorSummary {
  files: number
  chunks: number
  chunks_by_type: Record<string, number>
  embedding_model: string
  stale_chunks_removed: number
}

// What one analysis really processed (a full analysis processes everything).
export interface AnalysisChanges {
  files_added: number
  files_modified: number
  files_unchanged: number
  files_deleted: number
  files_parsed: number
  nodes_written: number
  nodes_deleted: number
  relationships_written: number
  relationships_deleted: number
  chunks_embedded: number // embeddings generated
  chunks_reused: number // existing vectors kept
  chunks_updated: number // kept vectors whose metadata (lines) was refreshed
  chunks_deleted: number
}

export interface AnalysisResponse {
  project_id: string
  status: 'ready' // this report describes a finished analysis
  mode: 'full' | 'incremental'
  graph: GraphSummary
  vectors: VectorSummary
  changes: AnalysisChanges
  failed_files: number
  warnings: string[]
  duration_seconds: number
  analyzed_at: string // ISO 8601 datetime (UTC)
}

export type AnalysisPhase =
  | 'preparing' | 'detecting_changes' | 'parsing' | 'resolving'
  | 'embedding' | 'graph' | 'vector_index' | 'finalizing' // prettier-ignore

// The latest analysis job of a project (it runs in the background on the server).
export interface AnalysisJob {
  job_id: string
  status: 'queued' | 'running' | 'ready' | 'failed'
  mode: 'full' | 'incremental' | null // known once the changes are detected
  phase: AnalysisPhase | null
  completed: number | null // real count of the current phase; null when nothing is countable
  total: number | null
  unit: 'files' | 'chunks' | null
  queued_at: string
  started_at: string | null
  finished_at: string | null
  error: string | null // safe to display
}

// POST /projects/{id}/analyze (202) and GET /projects/{id}/analysis.
// `analysis` is the report whose data is in the databases: it stays set while a new job
// runs and after a job that failed before writing anything; null otherwise.
export interface AnalysisStatus {
  project_id: string
  status: 'not_analyzed' | 'queued' | 'running' | 'ready' | 'failed'
  job: AnalysisJob | null
  analysis: AnalysisResponse | null
}

// ----- Knowledge graph (schemas/graph.py) -----

export const DEFAULT_GRAPH_NODES = 150 // GET /graph?limit= default
export const MAX_GRAPH_NODES = 500 // and maximum

export interface GraphNode {
  id: string
  entity_type: string // file, class, interface, function or method
  name: string
  qualified_name: string
  file_path: string
  language: string
  start_line: number
  end_line: number
}

export interface GraphEdge {
  id: string
  source: string // node ID
  target: string // node ID
  relationship_type: string // CONTAINS, CALLS, IMPORTS, INHERITS, IMPLEMENTS, USES, DEPENDS_ON
}

export interface ProjectGraph {
  project_id: string
  nodes: GraphNode[]
  edges: GraphEdge[] // both ends are in `nodes`
  truncated: boolean // the project has more nodes or edges than returned
  total_nodes: number
  total_edges: number
}

// ----- Advanced analysis (schemas/insights.py) -----

export const DEFAULT_IMPACT_DEPTH = 3 // GET /analysis/impact?depth= default
export const MAX_IMPACT_DEPTH = 5 // and maximum

export interface ImpactedEntity {
  entity: GraphNode
  depth: number // 1: references the changed entity directly; 2: one step further...
  relationship_type: string // how it depends on `via`
  via: string // ID of the entity it references, one step closer to the change
}

export interface ImpactAnalysis {
  project_id: string
  entity: GraphNode
  contained: number // entities it defines, which change with it
  max_depth: number
  affected: ImpactedEntity[] // closest first, each entity once
  total: number
  by_depth: Record<string, number>
  truncated: boolean
}

export interface FileDependency {
  source: string // file path
  target: string
  source_id: string
  target_id: string
  types: string[] // IMPORTS, DEPENDS_ON or both
}

export interface DependencyCycle {
  files: string[] // [A, B, C] means A -> B -> C -> A
  entity_ids: string[]
}

export interface Hub {
  entity: GraphNode
  incoming: number // distinct files or entities that depend on it
}

export interface DependencyAnalysis {
  project_id: string
  files: number
  dependency_count: number
  dependencies: FileDependency[]
  dependencies_truncated: boolean
  cycles: DependencyCycle[]
  cycles_truncated: boolean
  file_hubs: Hub[]
  entity_hubs: Hub[]
  unreferenced_count: number
  unreferenced: GraphNode[] // no detected reference: NOT proof of dead code
  unreferenced_note: string
  files_without_dependents: GraphNode[]
  partial: boolean
}

export interface ArchitectureFact {
  number: number // the summary cites it as [number]
  text: string
  entities: GraphNode[]
}

export interface ArchitectureOverview {
  project_id: string
  facts: ArchitectureFact[] // computed from the knowledge graph
  summary: string | null // Markdown by the LLM, from the facts only
  cited: number[]
  model: string | null
  warnings: string[]
}

// ----- Chat (schemas/chat.py) -----

export const MAX_QUESTION_CHARS = 2000 // ChatRequest.question max_length

export interface ChatRequest {
  question: string
}

export type GraphStatus = 'complete' | 'partial' | 'unavailable'

export interface ChatSource {
  id: number // the number the answer cites as [id]
  entity: string // qualified name, e.g. AuthService.login
  entity_type: string // file, class, interface, function or method
  file: string
  start_line: number
  end_line: number
  found_by: 'semantic_search' | 'graph'
  cited: boolean
}

export interface ChatResponse {
  question: string
  answer: string // Markdown, citing sources as [id]
  sources: ChatSource[]
  cited: number[]
  graph_status: GraphStatus
  warnings: string[]
  model: string | null
}
