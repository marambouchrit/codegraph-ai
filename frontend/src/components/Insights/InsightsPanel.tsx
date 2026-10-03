import { useEffect, useState } from 'react'
import { getArchitecture, getDependencies } from '../../services/api'
import type { ArchitectureOverview, DependencyAnalysis, GraphNode, Hub } from '../../types/api'
import { errorText, formatNumber } from '../../utils/format'
import MarkdownAnswer from '../Chat/MarkdownAnswer'
import LoadingState from '../LoadingState/LoadingState'
import './Insights.css'

interface Props {
  projectId: string
  analyzed: boolean | null // null while the analysis state is loading
  analyzedAt: string | null // changes after a re-analysis: the insights are reloaded
}

/** Advanced analysis of the project's graph: dependencies (computed) and architecture (LLM over facts). */
export default function InsightsPanel({ projectId, analyzed, analyzedAt }: Props) {
  if (analyzed === null) {
    return (
      <section className="card">
        <LoadingState label="Checking the analysis state…" />
      </section>
    )
  }
  return (
    <div className="insights">
      {/* A new analysis mounts fresh cards, which load the new results. */}
      <Dependencies key={`dependencies|${analyzedAt}`} projectId={projectId} />
      <Architecture key={`architecture|${analyzedAt}`} projectId={projectId} />
    </div>
  )
}

// ----- Dependencies -----

type DependenciesState =
  | { state: 'loading' }
  | { state: 'error'; message: string }
  | { state: 'loaded'; analysis: DependencyAnalysis }

function Dependencies({ projectId }: { projectId: string }) {
  const [view, setView] = useState<DependenciesState>({ state: 'loading' })

  useEffect(() => {
    let current = true
    getDependencies(projectId)
      .then((analysis) => current && setView({ state: 'loaded', analysis }))
      .catch((error: unknown) => current && setView({ state: 'error', message: errorText(error) }))
    return () => {
      current = false
    }
  }, [projectId])

  return (
    <section className="card" aria-labelledby="dependencies-title">
      <h2 id="dependencies-title">Dependency analysis</h2>
      <p className="card-subtitle">
        Computed from the knowledge graph (no AI): a file depends on another when it imports it,
        or when its code calls, uses or extends code of it.
      </p>
      {view.state === 'loading' && <LoadingState label="Analyzing dependencies…" />}
      {view.state === 'error' && (
        <p className="alert error" role="alert">
          The dependency analysis could not be loaded. {view.message}
        </p>
      )}
      {view.state === 'loaded' &&
        (view.analysis.files === 0 ? (
          <div className="empty">
            <p>No knowledge graph yet.</p>
            <p className="muted">Analyze the project to see its dependencies.</p>
          </div>
        ) : (
          <DependencyResult analysis={view.analysis} />
        ))}
    </section>
  )
}

function DependencyResult({ analysis }: { analysis: DependencyAnalysis }) {
  return (
    <>
      {analysis.partial && (
        <p className="alert warn" role="status">
          The project is larger than what was analyzed: these results cover part of it.
        </p>
      )}
      <p className="insight-summary">
        <strong>{formatNumber(analysis.files)}</strong> files ·{' '}
        <strong>{formatNumber(analysis.dependency_count)}</strong> file-to-file dependencies ·{' '}
        <strong>{analysis.cycles.length}{analysis.cycles_truncated ? '+' : ''}</strong> circular{' '}
        {analysis.cycles.length === 1 ? 'dependency' : 'dependencies'}
      </p>

      <h3>Circular dependencies</h3>
      {analysis.cycles.length === 0 ? (
        <p className="muted">No circular dependency between files was found.</p>
      ) : (
        <ul className="cycles">
          {analysis.cycles.map((cycle) => (
            <li key={cycle.entity_ids.join('>')}>
              {[...cycle.files, cycle.files[0]].map((file, index) => (
                <span key={`${file}-${index}`}>
                  {index > 0 && <span className="arrow"> → </span>}
                  <code>{file}</code>
                </span>
              ))}
            </li>
          ))}
        </ul>
      )}

      <div className="hub-grid">
        <HubList title="Files most depended on" hubs={analysis.file_hubs} unit="files depend on it" name={(e) => e.file_path} />
        <HubList
          title="Most referenced classes and functions"
          hubs={analysis.entity_hubs}
          unit="entities call, use or extend it"
          name={(e) => e.qualified_name}
          detail={(e) => e.file_path}
        />
      </div>

      <details className="insight-details">
        <summary>
          No detected references · {formatNumber(analysis.unreferenced_count)}{' '}
          {analysis.unreferenced_count === 1 ? 'entity' : 'entities'}
        </summary>
        <p className="alert warn">{analysis.unreferenced_note}</p>
        <ul className="entity-list">
          {analysis.unreferenced.map((entity) => (
            <li key={entity.id}>
              <code>{entity.qualified_name}</code>{' '}
              <span className="muted">
                {entity.entity_type} · {entity.file_path}:{entity.start_line}
              </span>
            </li>
          ))}
        </ul>
        {analysis.unreferenced_count > analysis.unreferenced.length && (
          <p className="muted">
            Showing the first {analysis.unreferenced.length} of {formatNumber(analysis.unreferenced_count)}.
          </p>
        )}
      </details>

      <details className="insight-details">
        <summary>Files no other file depends on · {analysis.files_without_dependents.length}</summary>
        <p className="muted">Entry points, scripts, tests, or files that are not used: the graph cannot tell which.</p>
        <ul className="entity-list">
          {analysis.files_without_dependents.map((file) => (
            <li key={file.id}>
              <code>{file.file_path}</code>
            </li>
          ))}
        </ul>
      </details>

      <details className="insight-details">
        <summary>
          All file dependencies · {formatNumber(analysis.dependency_count)}
          {analysis.dependencies_truncated && ` (first ${analysis.dependencies.length} shown)`}
        </summary>
        <ul className="entity-list">
          {analysis.dependencies.map((dependency) => (
            <li key={`${dependency.source_id}>${dependency.target_id}`}>
              <code>{dependency.source}</code>
              <span className="arrow"> → </span>
              <code>{dependency.target}</code> <span className="muted">{dependency.types.join(', ')}</span>
            </li>
          ))}
        </ul>
      </details>
    </>
  )
}

function HubList({
  title,
  hubs,
  unit,
  name,
  detail,
}: {
  title: string
  hubs: Hub[]
  unit: string
  name: (entity: GraphNode) => string
  detail?: (entity: GraphNode) => string
}) {
  return (
    <div>
      <h3>{title}</h3>
      {hubs.length === 0 ? (
        <p className="muted">None detected.</p>
      ) : (
        <ol className="hubs">
          {hubs.map((hub) => (
            <li key={hub.entity.id}>
              <span className="hub-count" title={`${hub.incoming} ${unit}`}>
                {hub.incoming}
              </span>
              <span className="hub-name">
                <code>{name(hub.entity)}</code>
                {detail && <span className="muted"> {detail(hub.entity)}</span>}
              </span>
            </li>
          ))}
        </ol>
      )}
    </div>
  )
}

// ----- Architecture -----

type ArchitectureState =
  | { state: 'idle' }
  | { state: 'loading' }
  | { state: 'error'; message: string }
  | { state: 'loaded'; overview: ArchitectureOverview }

function Architecture({ projectId }: { projectId: string }) {
  const [view, setView] = useState<ArchitectureState>({ state: 'idle' })
  const [activeFact, setActiveFact] = useState<number | null>(null)

  async function generate() {
    if (view.state === 'loading') return
    setView({ state: 'loading' })
    setActiveFact(null)
    try {
      setView({ state: 'loaded', overview: await getArchitecture(projectId) })
    } catch (error) {
      setView({ state: 'error', message: errorText(error) })
    }
  }

  function showFact(number: number) {
    setActiveFact(number)
    requestAnimationFrame(() => {
      document.getElementById(`fact-${number}`)?.scrollIntoView({ behavior: 'smooth', block: 'nearest' })
    })
  }

  return (
    <section className="card" aria-labelledby="architecture-title">
      <h2 id="architecture-title">Architecture summary</h2>
      <p className="card-subtitle">
        Facts are computed from the knowledge graph; the AI then summarizes those facts only and
        cites them. It does not read the repository and is told not to guess a pattern.
      </p>
      <button className="button" type="button" onClick={generate} disabled={view.state === 'loading'}>
        {view.state === 'loading' && <span className="spinner" aria-hidden="true" />}
        {view.state === 'loading'
          ? 'Generating…'
          : view.state === 'loaded'
            ? 'Generate again'
            : 'Generate architecture summary'}
      </button>

      {view.state === 'error' && (
        <p className="alert error" role="alert">
          The architecture summary could not be generated. {view.message}
        </p>
      )}
      {view.state === 'loaded' && (
        <div className="architecture">
          {view.overview.warnings.length > 0 && (
            <div className="alert warn" role="status">
              <ul>
                {view.overview.warnings.map((warning) => (
                  <li key={warning}>{warning}</li>
                ))}
              </ul>
            </div>
          )}
          {view.overview.summary && (
            <div className="architecture-summary">
              <div className="message-author">
                Summary {view.overview.model && <span className="badge">{view.overview.model}</span>}
              </div>
              <MarkdownAnswer
                markdown={view.overview.summary}
                sourceIds={view.overview.facts.map((fact) => fact.number)}
                onCite={showFact}
                citeLabel="fact"
              />
            </div>
          )}
          {view.overview.facts.length > 0 && (
            <>
              <h3>Facts from the graph</h3>
              <ol className="facts">
                {view.overview.facts.map((fact) => (
                  <li
                    key={fact.number}
                    id={`fact-${fact.number}`}
                    className={fact.number === activeFact ? 'fact active' : 'fact'}
                    value={fact.number}
                  >
                    {fact.text}
                  </li>
                ))}
              </ol>
            </>
          )}
        </div>
      )}
    </section>
  )
}
