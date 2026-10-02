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

export interface AnalysisResponse {
  project_id: string
  status: 'ready' // a failed analysis is an HTTP error, never a response
  graph: GraphSummary
  vectors: VectorSummary
  failed_files: number
  warnings: string[]
  duration_seconds: number
  analyzed_at: string // ISO 8601 datetime (UTC)
}

// GET /projects/{id}/analysis: the last successful analysis, persisted by the backend.
// not_analyzed also covers an analysis that failed or was interrupted.
export interface AnalysisStatus {
  project_id: string
  status: 'not_analyzed' | 'ready'
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
