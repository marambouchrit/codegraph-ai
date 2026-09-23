import { useEffect, useState } from 'react'
import { API_URL, getHealth } from '../../services/api'
import type { HealthResponse } from '../../types/api'
import './HomePage.css'

type BackendStatus =
  | { state: 'loading' }
  | { state: 'online'; health: HealthResponse }
  | { state: 'offline'; error: string }

export default function HomePage() {
  const [status, setStatus] = useState<BackendStatus>({ state: 'loading' })

  useEffect(() => {
    getHealth()
      .then((health) => setStatus({ state: 'online', health }))
      .catch((error: unknown) =>
        setStatus({
          state: 'offline',
          error: error instanceof Error ? error.message : String(error),
        }),
      )
  }, [])

  return (
    <main className="home">
      <h1>CodeGraph AI</h1>
      <p className="tagline">GraphRAG-powered codebase analysis assistant</p>

      <section className="status-card">
        <h2>System status</h2>
        <p>
          <span className="label">Frontend:</span>
          <span className="badge online">running</span>
        </p>
        <p>
          <span className="label">Backend:</span>
          {status.state === 'loading' && <span className="badge">checking…</span>}
          {status.state === 'online' && (
            <span className="badge online">
              {status.health.status} (v{status.health.version})
            </span>
          )}
          {status.state === 'offline' && <span className="badge offline">unreachable</span>}
        </p>
        {status.state === 'offline' && (
          <p className="hint">
            Could not reach <code>{API_URL}/health</code> ({status.error}). Is the backend running?
          </p>
        )}
      </section>
    </main>
  )
}
