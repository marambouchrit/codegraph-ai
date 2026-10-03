// All HTTP calls to the FastAPI backend. Components never call fetch themselves.

import type {
  AnalysisStatus,
  ArchitectureOverview,
  ChatRequest,
  ChatResponse,
  DependencyAnalysis,
  GitHubProjectCreate,
  HealthResponse,
  ImpactAnalysis,
  Project,
  ProjectGraph,
} from '../types/api'

// Base URL of the FastAPI backend, configured in `.env`.
export const API_URL = import.meta.env.VITE_API_URL ?? 'http://localhost:8000'

/** An API failure with a message that is safe to show to the user. */
export class ApiError extends Error {
  readonly status: number // HTTP status, 0 when the backend could not be reached

  constructor(status: number, message: string) {
    super(message)
    this.name = 'ApiError'
    this.status = status
  }
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  let response: Response
  try {
    response = await fetch(`${API_URL}${path}`, init)
  } catch {
    throw new ApiError(0, `Cannot reach the backend at ${API_URL}. Is it running?`)
  }
  if (!response.ok) {
    throw new ApiError(response.status, await errorMessage(response))
  }
  if (response.status === 204) {
    return undefined as T
  }
  return (await response.json()) as T
}

const GENERIC_MESSAGES: Record<number, string> = {
  404: 'Project not found.',
  409: 'This project is already being analyzed. Try again when it has finished.',
  413: 'The upload is too large.',
  422: 'Please check your input.',
  502: 'The AI service did not return a valid response. Please try again.',
  503: 'The analysis or AI service is currently unavailable. Please try again.',
}

/**
 * Turn an error response into a user-facing message.
 *
 * Application errors return `{"detail": "<message>"}`: these messages are written for
 * users and never contain secrets. Validation errors (422) return a list of problems.
 * Server errors (5xx other than 502/503) only ever get a generic message.
 */
async function errorMessage(response: Response): Promise<string> {
  const fallback = GENERIC_MESSAGES[response.status] ?? 'Something went wrong. Please try again.'
  if (response.status >= 500 && response.status !== 502 && response.status !== 503) {
    return fallback
  }
  try {
    const body: unknown = await response.json()
    const detail = (body as { detail?: unknown } | null)?.detail
    if (typeof detail === 'string' && detail.trim()) {
      return detail
    }
    if (Array.isArray(detail) && detail.length > 0) {
      const first = detail[0] as { msg?: unknown }
      if (typeof first.msg === 'string') {
        return `${fallback} ${first.msg.replace(/^Value error, /, '')}`
      }
    }
  } catch {
    // Not JSON: keep the generic message.
  }
  return fallback
}

function postJson<T>(path: string, body: unknown): Promise<T> {
  return request<T>(path, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
  })
}

export function getHealth(): Promise<HealthResponse> {
  return request<HealthResponse>('/health')
}

export function getProjects(): Promise<Project[]> {
  return request<Project[]>('/projects')
}

export function getProject(projectId: string): Promise<Project> {
  return request<Project>(`/projects/${encodeURIComponent(projectId)}`)
}

export function importFromGithub(url: string): Promise<Project> {
  const body: GitHubProjectCreate = { url }
  return postJson<Project>('/projects/github', body)
}

export function uploadProjectZip(file: File): Promise<Project> {
  const form = new FormData()
  form.append('file', file) // the backend's multipart field name
  return request<Project>('/projects/zip', { method: 'POST', body: form })
}

/**
 * Start analyzing a project. Returns at once (202) with a queued job: the analysis runs
 * in the background on the server; follow it with getAnalysis().
 * `full` forces everything to be analyzed again instead of only what changed.
 */
export function analyzeProject(projectId: string, full = false): Promise<AnalysisStatus> {
  const query = full ? '?full=true' : ''
  return request<AnalysisStatus>(`/projects/${encodeURIComponent(projectId)}/analyze${query}`, {
    method: 'POST',
  })
}

/** The analysis state: status, the current or last job with real progress, the last report. */
export function getAnalysis(projectId: string): Promise<AnalysisStatus> {
  return request<AnalysisStatus>(`/projects/${encodeURIComponent(projectId)}/analysis`)
}

/** A bounded view of the project's knowledge graph (up to `limit` nodes). */
export function getGraph(projectId: string, limit?: number): Promise<ProjectGraph> {
  const query = limit === undefined ? '' : `?limit=${encodeURIComponent(limit)}`
  return request<ProjectGraph>(`/projects/${encodeURIComponent(projectId)}/graph${query}`)
}

/** What may be affected if an entity changes (reverse references, up to `depth` steps). */
export function getImpact(projectId: string, entityId: string, depth?: number): Promise<ImpactAnalysis> {
  const query = new URLSearchParams({ entity_id: entityId })
  if (depth !== undefined) query.set('depth', String(depth))
  return request<ImpactAnalysis>(`/projects/${encodeURIComponent(projectId)}/analysis/impact?${query}`)
}

/** File dependencies, circular dependencies, hubs and unreferenced entities (no LLM). */
export function getDependencies(projectId: string): Promise<DependencyAnalysis> {
  return request<DependencyAnalysis>(`/projects/${encodeURIComponent(projectId)}/analysis/dependencies`)
}

/** Architecture facts computed from the graph, and an LLM summary of them (one LLM call). */
export function getArchitecture(projectId: string): Promise<ArchitectureOverview> {
  return request<ArchitectureOverview>(`/projects/${encodeURIComponent(projectId)}/analysis/architecture`)
}

export function askQuestion(projectId: string, question: string): Promise<ChatResponse> {
  const body: ChatRequest = { question }
  return postJson<ChatResponse>(`/projects/${encodeURIComponent(projectId)}/chat`, body)
}
