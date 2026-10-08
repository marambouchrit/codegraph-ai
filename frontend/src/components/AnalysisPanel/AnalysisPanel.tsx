import { useEffect, useState } from 'react'
import { isActive, type AnalysisView } from '../../hooks/useAnalysis'
import type { AnalysisJob, AnalysisPhase, AnalysisResponse, AnalysisStatus } from '../../types/api'
import { alert, badge, button, buttonSecondary, card, cardSubtitle, cardTitle, sectionTitle, spinner } from '../../ui'
import { formatDate, formatNumber } from '../../utils/format'
import LoadingState from '../LoadingState/LoadingState'

const note = 'mt-3 text-sm text-muted'

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
    <section className={card} aria-labelledby="analysis-title">
      <div className="flex items-start justify-between gap-3">
        <div>
          <h2 id="analysis-title" className={cardTitle}>
            Analysis
          </h2>
          <p className={cardSubtitle}>
            Builds the knowledge graph and the semantic index that chat answers from. It runs in
            the background; after the first time, only the files that changed are processed.
          </p>
        </div>
        <StatusBadge view={view} />
      </div>

      {view.state === 'loading' && <LoadingState label="Checking the analysis state…" />}
      {view.state === 'load-error' && (
        <p className={alert.error} role="alert">
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
      <div className="flex flex-wrap gap-2">
        <button className={button} type="button" onClick={() => onAnalyze(false)} disabled={active}>
          {active && <span className={spinner} aria-hidden="true" />}
          {active ? 'Analyzing…' : analysis ? 'Analyze again' : 'Analyze Project'}
        </button>
        {analysis && !active && (
          <button
            className={buttonSecondary}
            type="button"
            onClick={() => onAnalyze(true)}
            title="Parse and embed every file again, instead of only what changed"
          >
            Full re-analysis
          </button>
        )}
      </div>

      {startError && (
        <p className={`${alert.error} mt-3`} role="alert">
          The analysis could not be started: {startError}
        </p>
      )}
      {status.status === 'not_analyzed' && (
        <p className={note}>
          This project has not been analyzed yet. Analyze it to build its knowledge graph and
          make it ready for chat.
        </p>
      )}
      {active && job && <Progress job={job} pollError={pollError} />}
      {status.status === 'failed' && job && (
        <p className={`${alert.error} mt-3`} role="alert">
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
  const [tone, label] = statusBadge(view)
  return (
    <span className={badge[tone]} data-testid="analysis-status">
      {label}
    </span>
  )
}

function statusBadge(view: AnalysisView): [keyof typeof badge, string] {
  if (view.state === 'loading') return ['neutral', 'Checking…']
  if (view.state === 'load-error') return ['error', 'Unknown']
  switch (view.status.status) {
    case 'queued':
      return ['accent', 'Queued']
    case 'running':
      return ['accent', 'Analyzing']
    case 'ready':
      return ['ok', 'Ready']
    case 'failed':
      return ['error', 'Failed']
    default:
      return ['neutral', 'Not analyzed']
  }
}

/** The job's real progress: its phase, and counts only when the backend counted something. */
function Progress({ job, pollError }: { job: AnalysisJob; pollError: string | null }) {
  const seconds = useElapsedSeconds(job.started_at)
  const current = PHASES.findIndex(([phase]) => phase === job.phase)
  const counted = job.completed !== null && job.total !== null
  return (
    <div className="mt-4 rounded-[10px] border border-line bg-surface-muted px-4 py-3.5" role="status">
      <p className="mb-2 flex items-center gap-2.5 font-semibold">
        <span className={spinner} aria-hidden="true" />
        {job.status === 'queued' ? 'Waiting for the analysis worker…' : 'Analysis running'}
        {job.status === 'running' && seconds !== null && <span className="text-muted">({seconds}s elapsed)</span>}
        {job.mode && <span className={badge.neutral}>{job.mode === 'full' ? 'Full analysis' : 'Incremental'}</span>}
      </p>
      {job.status === 'running' && (
        <ol className="list-decimal pl-6 text-sm text-muted">
          {PHASES.map(([phase, label], index) => (
            <li
              key={phase}
              className={index < current ? 'text-ok' : index === current ? 'font-semibold text-fg' : ''}
              aria-current={index === current ? 'step' : undefined}
            >
              {label}
              {index === current && counted && (
                <span className="text-accent tabular-nums">
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
          className="mt-2.5 block h-2 w-full accent-accent"
          value={job.completed ?? 0}
          max={job.total ?? 0}
          aria-label={`${job.phase}: ${job.completed} of ${job.total} ${job.unit}`}
        />
      )}
      {pollError && <p className={note}>Could not refresh the progress ({pollError}). Retrying…</p>}
      <p className={note}>
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
    <div className="mt-4">
      <p className={stale ? alert.neutral : alert.ok} role="status">
        {stale ? 'Last analysis: ' : 'Analyzed on '}
        {formatDate(result.analyzed_at)} in {result.duration_seconds.toFixed(1)}s.
        {!stale && ' The project is ready for chat.'}
      </p>

      <dl className="mt-4 grid grid-cols-[repeat(auto-fit,minmax(130px,1fr))] gap-2.5">
        <Stat label="Files analyzed" value={graph.files} />
        <Stat label="Entities" value={graph.entities} />
        <Stat label="Relationships" value={graph.relationships} />
        <Stat label="Code chunks" value={vectors.chunks} />
      </dl>

      <p className="mt-3.5 text-sm">
        <span className={badge.neutral}>{result.mode === 'full' ? 'Full analysis' : 'Incremental analysis'}</span>{' '}
        {result.mode === 'full'
          ? `Every file was processed: ${formatNumber(changes.files_parsed)} files parsed, ${formatNumber(changes.chunks_embedded)} chunks embedded.`
          : `${formatNumber(changes.files_parsed)} of ${formatNumber(graph.files + result.failed_files)} files parsed ` +
            `(${changes.files_added} added, ${changes.files_modified} modified, ${changes.files_deleted} deleted, ` +
            `${formatNumber(changes.files_unchanged)} unchanged); ${formatNumber(changes.chunks_embedded)} chunks embedded, ` +
            `${formatNumber(changes.chunks_reused)} reused` +
            (changes.chunks_deleted > 0 ? `, ${formatNumber(changes.chunks_deleted)} removed.` : '.')}
      </p>

      <div className="mt-3.5 grid grid-cols-[repeat(auto-fit,minmax(220px,1fr))] gap-3">
        <Breakdown title="Entities by type" counts={graph.entities_by_type} />
        <Breakdown title="Relationships by type" counts={graph.relationships_by_type} />
        <Breakdown title="Chunks by type" counts={vectors.chunks_by_type} />
      </div>

      <p className={note}>
        Embedding model: <code>{vectors.embedding_model}</code> · Unresolved references
        (e.g. libraries): {formatNumber(graph.unresolved_references)}
        {result.failed_files > 0 && ` · Files skipped: ${formatNumber(result.failed_files)}`}
      </p>

      {result.warnings.length > 0 && (
        <div className={`${alert.warn} mt-3`} role="status">
          <strong>Warnings</strong>
          <ul className="mt-1 list-disc pl-5">
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
    <div className="rounded-[10px] border border-line px-3.5 py-3">
      <dt className="text-xs text-muted">{label}</dt>
      <dd className="mt-0.5 text-2xl font-bold tabular-nums">{formatNumber(value)}</dd>
    </div>
  )
}

function Breakdown({ title, counts }: { title: string; counts: Record<string, number> }) {
  const entries = Object.entries(counts).sort((a, b) => b[1] - a[1])
  if (entries.length === 0) return null
  return (
    <div>
      <h3 className={`${sectionTitle} mb-1.5`}>{title}</h3>
      <div className="flex flex-wrap gap-1.5">
        {entries.map(([name, count]) => (
          <span key={name} className={badge.neutral}>
            {name} · {formatNumber(count)}
          </span>
        ))}
      </div>
    </div>
  )
}
