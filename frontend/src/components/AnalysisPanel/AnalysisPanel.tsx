import { useEffect, useState } from 'react'
import type { AnalysisState } from '../../hooks/useAnalysis'
import type { AnalysisResponse } from '../../types/api'
import { formatDate, formatNumber } from '../../utils/format'
import LoadingState from '../LoadingState/LoadingState'
import './AnalysisPanel.css'

interface Props {
  analysis: AnalysisState
  onAnalyze: () => void
}

// What POST /analyze does on the server. Shown as a description, not as progress:
// the endpoint is synchronous and reports nothing until it has finished.
const STEPS = [
  'Parsing source files',
  'Building the knowledge graph (Neo4j)',
  'Generating embeddings (BGE-M3)',
  'Indexing code chunks (Qdrant)',
]

/** The analysis state persisted by the backend, the Analyze button, and the last report. */
export default function AnalysisPanel({ analysis, onAnalyze }: Props) {
  const isRunning = analysis.state === 'running'
  const isLoading = analysis.state === 'loading'
  return (
    <section className="card" aria-labelledby="analysis-title">
      <div className="analysis-head">
        <div>
          <h2 id="analysis-title">Analysis</h2>
          <p className="card-subtitle">
            Builds the knowledge graph and the semantic index that chat answers from. Run it
            after importing, and again after the code changes (re-running is safe).
          </p>
        </div>
        <StatusBadge analysis={analysis} />
      </div>

      {isLoading ? (
        <LoadingState label="Checking the analysis state…" />
      ) : (
        <button className="button" type="button" onClick={onAnalyze} disabled={isRunning}>
          {isRunning && <span className="spinner" aria-hidden="true" />}
          {isRunning ? 'Analyzing…' : analysis.state === 'ready' ? 'Analyze again' : 'Analyze Project'}
        </button>
      )}

      {analysis.state === 'not_analyzed' && (
        <p className="muted analysis-note">
          This project has not been analyzed yet. Analyze it to build its knowledge graph and
          make it ready for chat.
        </p>
      )}
      {analysis.state === 'load-error' && (
        <p className="alert error" role="alert">
          Could not read the analysis state. {analysis.message}
        </p>
      )}
      {analysis.state === 'running' && <Running startedAt={analysis.startedAt} />}
      {analysis.state === 'failed' && (
        <p className="alert error" role="alert">
          Analysis failed: {analysis.message} The project is not ready: analyze it again.
        </p>
      )}
      {analysis.state === 'ready' && <AnalysisResult result={analysis.result} />}
    </section>
  )
}

function StatusBadge({ analysis }: { analysis: AnalysisState }) {
  switch (analysis.state) {
    case 'loading':
      return <span className="badge">Checking…</span>
    case 'running':
      return <span className="badge accent">Analyzing</span>
    case 'ready':
      return <span className="badge ok">Ready</span>
    case 'failed':
      return <span className="badge error">Not analyzed</span>
    case 'load-error':
      return <span className="badge error">Unknown</span>
    default:
      return <span className="badge">Not analyzed</span>
  }
}

function Running({ startedAt }: { startedAt: number }) {
  const [seconds, setSeconds] = useState(0)
  useEffect(() => {
    const timer = setInterval(() => setSeconds(Math.floor((Date.now() - startedAt) / 1000)), 1000)
    return () => clearInterval(timer)
  }, [startedAt])

  return (
    <div className="analysis-running" role="status">
      <p className="analysis-running-title">
        <span className="spinner" aria-hidden="true" />
        Analyzing codebase… <span className="muted">({seconds}s elapsed)</span>
      </p>
      <ul className="steps">
        {STEPS.map((step) => (
          <li key={step}>{step}</li>
        ))}
      </ul>
      <p className="muted analysis-note">
        Embedding with BGE-M3 runs on the server and can take from seconds for a small project
        to several minutes for a large one; the first analysis also loads the model.
      </p>
    </div>
  )
}

function AnalysisResult({ result }: { result: AnalysisResponse }) {
  const { graph, vectors } = result
  const stale = graph.stale_entities_removed + vectors.stale_chunks_removed
  return (
    <div className="analysis-result">
      <p className="alert ok" role="status">
        Analyzed on {formatDate(result.analyzed_at)} in {result.duration_seconds.toFixed(1)}s.
        The project is ready for chat.
      </p>

      <dl className="stats">
        <Stat label="Files analyzed" value={graph.files} />
        <Stat label="Entities" value={graph.entities} />
        <Stat label="Relationships" value={graph.relationships} />
        <Stat label="Code chunks" value={vectors.chunks} />
      </dl>

      <div className="breakdowns">
        <Breakdown title="Entities by type" counts={graph.entities_by_type} />
        <Breakdown title="Relationships by type" counts={graph.relationships_by_type} />
        <Breakdown title="Chunks by type" counts={vectors.chunks_by_type} />
      </div>

      <p className="muted analysis-note">
        Embedding model: <code>{vectors.embedding_model}</code> · Unresolved references
        (e.g. libraries): {formatNumber(graph.unresolved_references)}
        {stale > 0 &&
          ` · Removed from a previous analysis: ${formatNumber(graph.stale_entities_removed)} entities, ${formatNumber(vectors.stale_chunks_removed)} chunks`}
        {result.failed_files > 0 && ` · Files skipped: ${formatNumber(result.failed_files)}`}
      </p>

      {result.warnings.length > 0 && (
        <div className="alert warn" role="status">
          <strong>Warnings</strong>
          <ul>
            {result.warnings.map((warning) => (
              <li key={warning}>{warning}</li>
            ))}
          </ul>
        </div>
      )}
    </div>
  )
}

function Stat({ label, value }: { label: string; value: number }) {
  return (
    <div className="stat">
      <dt>{label}</dt>
      <dd>{formatNumber(value)}</dd>
    </div>
  )
}

function Breakdown({ title, counts }: { title: string; counts: Record<string, number> }) {
  const entries = Object.entries(counts).sort((a, b) => b[1] - a[1])
  if (entries.length === 0) return null
  return (
    <div className="breakdown">
      <h3>{title}</h3>
      <div className="breakdown-items">
        {entries.map(([name, count]) => (
          <span key={name} className="badge">
            {name} · {formatNumber(count)}
          </span>
        ))}
      </div>
    </div>
  )
}
