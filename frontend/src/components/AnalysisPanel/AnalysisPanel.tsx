import { useEffect, useState } from 'react'
import { isActive, type AnalysisView } from '../../hooks/useAnalysis'
import type { AnalysisJob, AnalysisPhase, AnalysisResponse, AnalysisStatus } from '../../types/api'
import { formatDate, formatNumber } from '../../utils/format'
import LoadingState from '../LoadingState/LoadingState'
import './AnalysisPanel.css'

interface Props {
  view: AnalysisView
  onAnalyze: (full?: boolean) => void
}

// The phases of an analysis job, in the order the backend runs them.
const PHASES: [AnalysisPhase, string][] = [
  ['preparing', 'Preparing (databases, embedding model)'],
  ['detecting_changes', 'Detecting changed files'],
  ['parsing', 'Parsing changed files'],
  ['resolving', 'Resolving relationships'],
  ['embedding', 'Embedding changed code'],
  ['graph', 'Updating the knowledge graph (Neo4j)'],
  ['vector_index', 'Updating the vector index (Qdrant)'],
  ['finalizing', 'Finalizing'],
]

/** The analysis state persisted by the backend: the job's real progress and the last report. */
export default function AnalysisPanel({ view, onAnalyze }: Props) {
  return (
    <section className="card" aria-labelledby="analysis-title">
      <div className="analysis-head">
        <div>
          <h2 id="analysis-title">Analysis</h2>
          <p className="card-subtitle">
            Builds the knowledge graph and the semantic index that chat answers from. It runs in
            the background; after the first time, only the files that changed are processed.
          </p>
        </div>
        <StatusBadge view={view} />
      </div>

      {view.state === 'loading' && <LoadingState label="Checking the analysis state…" />}
      {view.state === 'load-error' && (
        <p className="alert error" role="alert">
          Could not read the analysis state. {view.message}
        </p>
      )}
      {view.state === 'loaded' && (
        <Loaded status={view.status} startError={view.startError} pollError={view.pollError} onAnalyze={onAnalyze} />
      )}
    </section>
  )
}

function Loaded({
  status,
  startError,
  pollError,
  onAnalyze,
}: {
  status: AnalysisStatus
  startError: string | null
  pollError: string | null
  onAnalyze: (full?: boolean) => void
}) {
  const active = isActive(status)
  const { job, analysis } = status
  return (
    <>
      <div className="analysis-actions">
        <button className="button" type="button" onClick={() => onAnalyze(false)} disabled={active}>
          {active && <span className="spinner" aria-hidden="true" />}
          {active ? 'Analyzing…' : analysis ? 'Analyze again' : 'Analyze Project'}
        </button>
        {analysis && !active && (
          <button
            className="button secondary"
            type="button"
            onClick={() => onAnalyze(true)}
            title="Parse and embed every file again, instead of only what changed"
          >
            Full re-analysis
          </button>
        )}
      </div>

      {startError && (
        <p className="alert error" role="alert">
          The analysis could not be started: {startError}
        </p>
      )}
      {status.status === 'not_analyzed' && (
        <p className="muted analysis-note">
          This project has not been analyzed yet. Analyze it to build its knowledge graph and
          make it ready for chat.
        </p>
      )}
      {active && job && <Progress job={job} pollError={pollError} />}
      {status.status === 'failed' && job && (
        <p className="alert error" role="alert">
          Analysis failed: {job.error ?? 'unknown error.'}{' '}
          {analysis
            ? 'Nothing was changed: the previous analysis below is still valid and chat keeps using it.'
            : 'The project is not ready: analyze it again.'}
        </p>
      )}
      {analysis && <AnalysisResult result={analysis} stale={status.status !== 'ready'} />}
    </>
  )
}

function StatusBadge({ view }: { view: AnalysisView }) {
  if (view.state === 'loading') return <span className="badge">Checking…</span>
  if (view.state === 'load-error') return <span className="badge error">Unknown</span>
  switch (view.status.status) {
    case 'queued':
      return <span className="badge accent">Queued</span>
    case 'running':
      return <span className="badge accent">Analyzing</span>
    case 'ready':
      return <span className="badge ok">Ready</span>
    case 'failed':
      return <span className="badge error">Failed</span>
    default:
      return <span className="badge">Not analyzed</span>
  }
}

/** The job's real progress: its phase, and counts only when the backend counted something. */
function Progress({ job, pollError }: { job: AnalysisJob; pollError: string | null }) {
  const seconds = useElapsedSeconds(job.started_at)
  const current = PHASES.findIndex(([phase]) => phase === job.phase)
  const counted = job.completed !== null && job.total !== null
  return (
    <div className="analysis-running" role="status">
      <p className="analysis-running-title">
        <span className="spinner" aria-hidden="true" />
        {job.status === 'queued' ? 'Waiting for the analysis worker…' : 'Analysis running'}
        {job.status === 'running' && seconds !== null && <span className="muted">({seconds}s elapsed)</span>}
        {job.mode && <span className="badge">{job.mode === 'full' ? 'Full analysis' : 'Incremental'}</span>}
      </p>
      {job.status === 'running' && (
        <ol className="steps">
          {PHASES.map(([phase, label], index) => (
            <li
              key={phase}
              className={index < current ? 'done' : index === current ? 'current' : ''}
              aria-current={index === current ? 'step' : undefined}
            >
              {label}
              {index === current && counted && (
                <span className="step-count">
                  {' '}
                  {formatNumber(job.completed ?? 0)} / {formatNumber(job.total ?? 0)} {job.unit}
                </span>
              )}
            </li>
          ))}
        </ol>
      )}
      {job.status === 'running' && counted && (job.total ?? 0) > 0 && (
        // A real measure: completed and total come from the backend's own counters.
        <progress
          className="analysis-progress"
          value={job.completed ?? 0}
          max={job.total ?? 0}
          aria-label={`${job.phase}: ${job.completed} of ${job.total} ${job.unit}`}
        />
      )}
      {pollError && <p className="muted analysis-note">Could not refresh the progress ({pollError}). Retrying…</p>}
      <p className="muted analysis-note">
        The analysis runs on the server: you can leave or reload this page, the progress is kept.
      </p>
    </div>
  )
}

function useElapsedSeconds(startedAt: string | null): number | null {
  const [now, setNow] = useState(() => Date.now())
  useEffect(() => {
    if (!startedAt) return
    const timer = setInterval(() => setNow(Date.now()), 1000)
    return () => clearInterval(timer)
  }, [startedAt])
  if (!startedAt) return null
  return Math.max(0, Math.floor((now - new Date(startedAt).getTime()) / 1000))
}

function AnalysisResult({ result, stale }: { result: AnalysisResponse; stale: boolean }) {
  const { graph, vectors, changes } = result
  return (
    <div className="analysis-result">
      <p className={stale ? 'alert' : 'alert ok'} role="status">
        {stale ? 'Last analysis: ' : 'Analyzed on '}
        {formatDate(result.analyzed_at)} in {result.duration_seconds.toFixed(1)}s.
        {!stale && ' The project is ready for chat.'}
      </p>

      <dl className="stats">
        <Stat label="Files analyzed" value={graph.files} />
        <Stat label="Entities" value={graph.entities} />
        <Stat label="Relationships" value={graph.relationships} />
        <Stat label="Code chunks" value={vectors.chunks} />
      </dl>

      <p className="analysis-changes">
        <span className="badge">{result.mode === 'full' ? 'Full analysis' : 'Incremental analysis'}</span>{' '}
        {result.mode === 'full'
          ? `Every file was processed: ${formatNumber(changes.files_parsed)} files parsed, ${formatNumber(changes.chunks_embedded)} chunks embedded.`
          : `${formatNumber(changes.files_parsed)} of ${formatNumber(graph.files + result.failed_files)} files parsed ` +
            `(${changes.files_added} added, ${changes.files_modified} modified, ${changes.files_deleted} deleted, ` +
            `${formatNumber(changes.files_unchanged)} unchanged); ${formatNumber(changes.chunks_embedded)} chunks embedded, ` +
            `${formatNumber(changes.chunks_reused)} reused` +
            (changes.chunks_deleted > 0 ? `, ${formatNumber(changes.chunks_deleted)} removed.` : '.')}
      </p>

      <div className="breakdowns">
        <Breakdown title="Entities by type" counts={graph.entities_by_type} />
        <Breakdown title="Relationships by type" counts={graph.relationships_by_type} />
        <Breakdown title="Chunks by type" counts={vectors.chunks_by_type} />
      </div>

      <p className="muted analysis-note">
        Embedding model: <code>{vectors.embedding_model}</code> · Unresolved references
        (e.g. libraries): {formatNumber(graph.unresolved_references)}
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
