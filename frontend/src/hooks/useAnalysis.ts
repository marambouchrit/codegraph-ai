import { useEffect, useRef, useState } from 'react'
import { analyzeProject, getAnalysis } from '../services/api'
import type { AnalysisStatus } from '../types/api'
import { errorText } from '../utils/format'

export const POLL_INTERVAL_MS = 1500

export type AnalysisView =
  | { state: 'loading' } // reading the persisted state
  | { state: 'load-error'; message: string }
  | {
      state: 'loaded'
      status: AnalysisStatus // exactly what the backend says
      startError: string | null // the last "Analyze" click was refused (e.g. 409)
      pollError: string | null // the state could not be refreshed; polling continues
    }

/** A job is waiting or working on the server. */
export function isActive(status: AnalysisStatus): boolean {
  return status.status === 'queued' || status.status === 'running'
}

/**
 * The analysis state of a project, from the backend (the source of truth).
 *
 * On mount it reads GET /projects/{id}/analysis, so a reload shows what the server
 * knows, including a job that is still running. `analyze()` starts a background job
 * (POST /analyze returns at once). While a job is queued or running, the state is
 * read again every POLL_INTERVAL_MS; polling stops when it is ready or failed.
 */
export function useAnalysis(projectId: string) {
  const [view, setView] = useState<AnalysisView>({ state: 'loading' })
  const starting = useRef(false) // blocks a second click even before React re-renders
  const active = view.state === 'loaded' && isActive(view.status)

  useEffect(() => {
    let current = true // ignore a late answer after leaving the page
    getAnalysis(projectId)
      .then((status) => current && setView({ state: 'loaded', status, startError: null, pollError: null }))
      .catch((error: unknown) => current && setView({ state: 'load-error', message: errorText(error) }))
    return () => {
      current = false
    }
  }, [projectId])

  // Poll while a job is active. The effect ends (and the timer with it) as soon as
  // the status is no longer queued or running, or when the page is left.
  useEffect(() => {
    if (!active) return
    let current = true
    const timer = setInterval(() => {
      getAnalysis(projectId)
        .then((status) => {
          if (current) setView((old) => loaded(old, { status, pollError: null }))
        })
        .catch((error: unknown) => {
          if (current) setView((old) => loaded(old, { pollError: errorText(error) }))
        })
    }, POLL_INTERVAL_MS)
    return () => {
      current = false
      clearInterval(timer)
    }
  }, [active, projectId])

  async function analyze(full = false) {
    if (starting.current || active) return
    starting.current = true
    try {
      const status = await analyzeProject(projectId, full)
      setView({ state: 'loaded', status, startError: null, pollError: null })
    } catch (error) {
      // Refused (already running, backend down...): show why, and what the server says now.
      const message = errorText(error)
      const status = await getAnalysis(projectId).catch(() => null)
      setView((old) =>
        status
          ? { state: 'loaded', status, startError: message, pollError: null }
          : loaded(old, { startError: message }),
      )
    } finally {
      starting.current = false
    }
  }

  return { view, analyze }
}

function loaded(
  old: AnalysisView,
  change: Partial<Extract<AnalysisView, { state: 'loaded' }>>,
): AnalysisView {
  return old.state === 'loaded' ? { ...old, ...change } : old
}
