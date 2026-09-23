import type { HealthResponse } from '../types/api'

// Base URL of the FastAPI backend, configured in `.env`.
export const API_URL = import.meta.env.VITE_API_URL ?? 'http://localhost:8000'

async function getJson<T>(path: string): Promise<T> {
  const response = await fetch(`${API_URL}${path}`)
  if (!response.ok) {
    throw new Error(`Request to ${path} failed with status ${response.status}`)
  }
  return (await response.json()) as T
}

export function getHealth(): Promise<HealthResponse> {
  return getJson<HealthResponse>('/health')
}
