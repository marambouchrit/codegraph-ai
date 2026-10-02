// A fake backend for tests: replaces fetch, records the requests, answers per "METHOD /path".

import { vi } from 'vitest'
import type { AnalysisResponse, AnalysisStatus, ChatResponse, Project, ProjectGraph } from '../types/api'

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

export const NOT_ANALYZED: AnalysisStatus = { project_id: PROJECT.id, status: 'not_analyzed', analysis: null }
export const READY: AnalysisStatus = { project_id: PROJECT.id, status: 'ready', analysis: ANALYSIS }

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
