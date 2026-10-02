import { useEffect, useRef, useState } from 'react'
import { analyzeProject, getAnalysis } from '../services/api'
import type { AnalysisResponse } from '../types/api'
import { errorText } from '../utils/format'

export type AnalysisState =
  | { state: 'loading' } // reading the persisted state
  | { state: 'load-error'; message: string }
  | { state: 'not_analyzed' }
  | { state: 'running'; startedAt: number }
  | { state: 'failed'; message: string } // the backend reports "not_analyzed" after a failure
  | { state: 'ready'; result: AnalysisResponse }

/**
 * The analysis state of a project, from the backend (the source of truth).
 *
 * On mount it reads GET /projects/{id}/analysis, so a reload shows what the server
 * knows. `analyze()` runs POST /analyze once at a time; its result (or failure) is
 * exactly what the backend now stores.
 */
export function useAnalysis(projectId: string) {
  const [analysis, setAnalysis] = useState<AnalysisState>({ state: 'loading' })
  const running = useRef(false) // blocks a second request even before React re-renders

  useEffect(() => {
    let current = true // ignore a late answer after leaving the page
    getAnalysis(projectId)
      .then((status) => {
        if (!current) return
        setAnalysis(
          status.status === 'ready' && status.analysis
            ? { state: 'ready', result: status.analysis }
            : { state: 'not_analyzed' },
        )
      })
      .catch((error: unknown) => current && setAnalysis({ state: 'load-error', message: errorText(error) }))
    return () => {
      current = false
    }
  }, [projectId])

  async function analyze() {
    if (running.current) return
    running.current = true
    setAnalysis({ state: 'running', startedAt: Date.now() })
    try {
      setAnalysis({ state: 'ready', result: await analyzeProject(projectId) })
    } catch (error) {
      setAnalysis({ state: 'failed', message: errorText(error) })
    } finally {
      running.current = false
    }
  }

  return { analysis, analyze }
}
