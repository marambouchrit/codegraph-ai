// A fake backend for tests: replaces fetch, records the requests, answers per "METHOD /path".

import { vi } from 'vitest'
import type {
  AnalysisJob,
  AnalysisResponse,
  AnalysisStatus,
  ArchitectureOverview,
  ChatResponse,
  DependencyAnalysis,
  ImpactAnalysis,
  Project,
  ProjectGraph,
} from '../types/api'

export interface Call {
  method: string
  path: string
  init?: RequestInit
}

type Handler = (call: Call) => Response | Promise<Response>

export function json(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { 'Content-Type': 'application/json' },
  })
}

/** Stub fetch; unknown routes answer 404 so a missing handler shows up in the test. */
export function mockApi(handlers: Record<string, Handler>): Call[] {
  const calls: Call[] = []
  vi.stubGlobal(
    'fetch',
    vi.fn(async (input: string, init?: RequestInit) => {
      const path = new URL(input).pathname
      const call = { method: init?.method ?? 'GET', path, init }
      calls.push(call)
      const handler = handlers[`${call.method} ${path}`]
      return handler ? handler(call) : json({ detail: 'Not Found' }, 404)
    }),
  )
  return calls
}

/** A handler answering with each response in turn; the last one is repeated. */
export function sequence(...bodies: unknown[]): Handler {
  let index = 0
  return () => json(bodies[Math.min(index++, bodies.length - 1)])
}

export const PROJECT: Project = {
  id: 'a'.repeat(32),
  name: 'auth-service',
  source_type: 'zip',
  source: 'auth.zip',
  created_at: '2026-10-02T10:00:00Z',
  file_count: 4,
  total_size_bytes: 2048,
  languages: { python: 4 },
}

export const ANALYSIS: AnalysisResponse = {
  project_id: PROJECT.id,
  status: 'ready',
  mode: 'full',
  changes: {
    files_added: 4, files_modified: 0, files_unchanged: 0, files_deleted: 0, files_parsed: 4,
    nodes_written: 17, nodes_deleted: 0, relationships_written: 26, relationships_deleted: 0,
    chunks_embedded: 15, chunks_reused: 0, chunks_updated: 0, chunks_deleted: 0,
  },
  graph: {
    files: 4,
    entities: 17,
    relationships: 26,
    entities_by_type: { Class: 4, File: 4, Function: 2, Method: 7 },
    relationships_by_type: { CALLS: 8, CONTAINS: 13, DEPENDS_ON: 2, IMPORTS: 2, INHERITS: 1 },
    unresolved_references: 8,
    stale_entities_removed: 0,
  },
  vectors: {
    files: 4,
    chunks: 15,
    chunks_by_type: { class: 4, file: 2, function: 2, method: 7 },
    embedding_model: 'BAAI/bge-m3',
    stale_chunks_removed: 0,
  },
  failed_files: 0,
  warnings: [],
  duration_seconds: 6.9,
  analyzed_at: '2026-10-02T18:30:00Z',
}

// An incremental re-analysis after one file changed: one chunk embedded, the others reused.
export const INCREMENTAL: AnalysisResponse = {
  ...ANALYSIS,
  mode: 'incremental',
  changes: {
    files_added: 0, files_modified: 1, files_unchanged: 3, files_deleted: 0, files_parsed: 1,
    nodes_written: 1, nodes_deleted: 0, relationships_written: 0, relationships_deleted: 0,
    chunks_embedded: 1, chunks_reused: 14, chunks_updated: 0, chunks_deleted: 0,
  },
  duration_seconds: 1.2,
  analyzed_at: '2026-10-03T09:00:00Z',
}

export function job(overrides: Partial<AnalysisJob> = {}): AnalysisJob {
  return {
    job_id: 'j'.repeat(32), status: 'running', mode: null, phase: null, completed: null,
    total: null, unit: null, queued_at: '2026-10-03T08:59:00Z',
    started_at: '2026-10-03T08:59:01Z', finished_at: null, error: null, ...overrides,
  }
}

export function status(
  value: AnalysisStatus['status'],
  jobOverrides: Partial<AnalysisJob> | null = null,
  analysis: AnalysisResponse | null = null,
): AnalysisStatus {
  return {
    project_id: PROJECT.id,
    status: value,
    job: jobOverrides === null ? null : job(jobOverrides),
    analysis,
  }
}

export const NOT_ANALYZED = status('not_analyzed')
export const QUEUED = status('queued', { status: 'queued', started_at: null })
export const READY = status('ready', { status: 'ready', mode: 'full', phase: 'finalizing',
  finished_at: '2026-10-02T18:30:00Z' }, ANALYSIS) // prettier-ignore

const id = (name: string) => `${PROJECT.id}:${name}`

// A small graph shaped like GET /graph returns it (IDs, types and fields of the backend).
export const GRAPH: ProjectGraph = {
  project_id: PROJECT.id,
  nodes: [
    { id: id('auth/service.py'), entity_type: 'file', name: 'service.py', qualified_name: 'auth/service.py',
      file_path: 'auth/service.py', language: 'python', start_line: 1, end_line: 28 },
    { id: id('auth/service.py:AuthService'), entity_type: 'class', name: 'AuthService',
      qualified_name: 'AuthService', file_path: 'auth/service.py', language: 'python', start_line: 11, end_line: 23 },
    { id: id('auth/service.py:AuthService.login'), entity_type: 'method', name: 'login',
      qualified_name: 'AuthService.login', file_path: 'auth/service.py', language: 'python', start_line: 14, end_line: 20 },
    { id: id('repository/user.py:UserRepository.find_user'), entity_type: 'method', name: 'find_user',
      qualified_name: 'UserRepository.find_user', file_path: 'repository/user.py', language: 'python', start_line: 7, end_line: 9 },
  ],
  edges: [
    { id: 'c1', source: id('auth/service.py'), target: id('auth/service.py:AuthService'), relationship_type: 'CONTAINS' },
    { id: 'c2', source: id('auth/service.py:AuthService'), target: id('auth/service.py:AuthService.login'), relationship_type: 'CONTAINS' },
    { id: 'k1', source: id('auth/service.py:AuthService.login'), target: id('repository/user.py:UserRepository.find_user'), relationship_type: 'CALLS' },
  ],
  truncated: false,
  total_nodes: 4,
  total_edges: 3,
}

const node = (index: number) => GRAPH.nodes[index]

// GET /analysis/impact for find_user: login calls it (depth 1); a route outside the
// displayed graph calls login (depth 2).
export const IMPACT: ImpactAnalysis = {
  project_id: PROJECT.id,
  entity: node(3),
  contained: 0,
  max_depth: 3,
  affected: [
    { entity: node(2), depth: 1, relationship_type: 'CALLS', via: node(3).id },
    { entity: { id: id('api/routes.py:login_route'), entity_type: 'function', name: 'login_route',
        qualified_name: 'login_route', file_path: 'api/routes.py', language: 'python',
        start_line: 4, end_line: 6 }, depth: 2, relationship_type: 'CALLS', via: node(2).id },
  ],
  total: 2,
  by_depth: { '1': 1, '2': 1 },
  truncated: false,
} // prettier-ignore

export const DEPENDENCIES: DependencyAnalysis = {
  project_id: PROJECT.id,
  files: 4,
  dependency_count: 3,
  dependencies: [
    { source: 'a.py', target: 'b.py', source_id: id('a.py'), target_id: id('b.py'), types: ['DEPENDS_ON', 'IMPORTS'] },
    { source: 'b.py', target: 'a.py', source_id: id('b.py'), target_id: id('a.py'), types: ['IMPORTS'] },
    { source: 'auth/service.py', target: 'repository/user.py', source_id: id('auth/service.py'),
      target_id: id('repository/user.py'), types: ['DEPENDS_ON'] },
  ],
  dependencies_truncated: false,
  cycles: [{ files: ['a.py', 'b.py'], entity_ids: [id('a.py'), id('b.py')] }],
  cycles_truncated: false,
  file_hubs: [{ entity: node(0), incoming: 3 }],
  entity_hubs: [{ entity: node(3), incoming: 2 }],
  unreferenced_count: 1,
  unreferenced: [node(2)],
  unreferenced_note: 'No call, use or inheritance of these entities was detected. This is not proof of dead code.',
  files_without_dependents: [node(0)],
  partial: false,
} // prettier-ignore

export const ARCHITECTURE: ArchitectureOverview = {
  project_id: PROJECT.id,
  facts: [
    { number: 1, text: 'The project has 4 source files (4 python).', entities: [] },
    { number: 2, text: 'Files most depended on: auth/service.py (3).', entities: [node(0)] },
  ],
  summary: 'A small Python project [1]. `auth/service.py` is its **hub** [2].',
  cited: [1, 2],
  model: 'gemini-flash-lite-latest',
  warnings: [],
}

export const CHAT: ChatResponse = {
  question: 'How is authentication implemented?',
  answer: '## Login\n\n`AuthService.login` checks the **password** [1] after `find_user` [2].',
  sources: [
    { id: 1, entity: 'AuthService.login', entity_type: 'method', file: 'auth/service.py',
      start_line: 14, end_line: 20, found_by: 'semantic_search', cited: true },
    { id: 2, entity: 'UserRepository.find_user', entity_type: 'method', file: 'repository/user.py',
      start_line: 7, end_line: 9, found_by: 'graph', cited: true },
    { id: 3, entity: 'Database.insert', entity_type: 'method', file: 'db/database.py',
      start_line: 7, end_line: 8, found_by: 'semantic_search', cited: false },
  ],
  cited: [1, 2],
  graph_status: 'complete',
  warnings: [],
  model: 'gemini-flash-lite-latest',
}
