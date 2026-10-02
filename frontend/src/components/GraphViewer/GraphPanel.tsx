import { useEffect, useMemo, useState, type FormEvent } from 'react'
import { getGraph } from '../../services/api'
import { DEFAULT_GRAPH_NODES, MAX_GRAPH_NODES, type ProjectGraph } from '../../types/api'
import { errorText, formatNumber } from '../../utils/format'
import LoadingState from '../LoadingState/LoadingState'
import GraphCanvas from './GraphCanvas'
import { entityColor, ENTITY_STYLES, relationshipColor } from './graphStyle'
import NodeDetails from './NodeDetails'
import './GraphViewer.css'

interface Props {
  projectId: string
  analyzed: boolean | null // null while the analysis state is loading
  analyzedAt: string | null // changes after a re-analysis: the graph is reloaded
}

const LIMITS = [100, DEFAULT_GRAPH_NODES, 300, MAX_GRAPH_NODES]

/** The Knowledge Graph section: node limit, the loaded graph, and its interactions. */
export default function GraphPanel({ projectId, analyzed, analyzedAt }: Props) {
  const [limit, setLimit] = useState(DEFAULT_GRAPH_NODES)

  return (
    <section className="card" aria-labelledby="graph-title">
      <div className="graph-head">
        <div>
          <h2 id="graph-title">Knowledge Graph</h2>
          <p className="card-subtitle">
            The real Neo4j graph of this project: files, classes, functions and methods, and how
            they contain, call, import and depend on each other.
          </p>
        </div>
        <label className="graph-limit">
          Nodes
          <select className="input" value={limit} onChange={(event) => setLimit(Number(event.target.value))}>
            {LIMITS.map((value) => (
              <option key={value} value={value}>
                up to {value}
              </option>
            ))}
          </select>
        </label>
      </div>
      {analyzed === null ? (
        <LoadingState label="Checking the analysis state…" />
      ) : (
        // A new limit or a new analysis mounts a fresh view, which loads its graph.
        <GraphView key={`${limit}|${analyzedAt}`} projectId={projectId} limit={limit} analyzed={analyzed} />
      )}
    </section>
  )
}

type GraphState =
  | { state: 'loading' }
  | { state: 'error'; message: string }
  | { state: 'loaded'; graph: ProjectGraph }

function GraphView({ projectId, limit, analyzed }: { projectId: string; limit: number; analyzed: boolean }) {
  const [view, setView] = useState<GraphState>({ state: 'loading' })

  useEffect(() => {
    let current = true
    getGraph(projectId, limit)
      .then((graph) => current && setView({ state: 'loaded', graph }))
      .catch((error: unknown) => current && setView({ state: 'error', message: errorText(error) }))
    return () => {
      current = false
    }
  }, [projectId, limit])

  if (view.state === 'loading') return <LoadingState label="Loading the knowledge graph…" />
  if (view.state === 'error') {
    return (
      <p className="alert error" role="alert">
        The knowledge graph could not be loaded. {view.message}
      </p>
    )
  }
  if (view.graph.nodes.length === 0) {
    return (
      <div className="empty">
        {analyzed ? (
          <>
            <p>The knowledge graph is empty.</p>
            <p className="muted">No entities were found in this project's supported source files.</p>
          </>
        ) : (
          <>
            <p>No knowledge graph yet.</p>
            <p className="muted">Analyze the project to build its knowledge graph.</p>
          </>
        )}
      </div>
    )
  }
  return (
    <>
      {!analyzed && (
        // Neo4j has a graph, but no analysis completed: e.g. an analysis stopped after the
        // graph step, during embedding. Show the real graph, and say why chat is not ready.
        <p className="alert warn" role="status">
          This graph comes from an analysis that did not complete (it may have been stopped
          during embedding), so the project is not ready for chat. Run Analyze to finish it.
        </p>
      )}
      <GraphExplorer graph={view.graph} />
    </>
  )
}

function GraphExplorer({ graph }: { graph: ProjectGraph }) {
  const [selectedId, setSelectedId] = useState<string | null>(null)
  const [hidden, setHidden] = useState<ReadonlySet<string>>(new Set())
  const [search, setSearch] = useState('')
  const [searchError, setSearchError] = useState<string | null>(null)

  // Only the types present in this graph, with their counts.
  const entityCounts = useMemo(() => count(graph.nodes.map((node) => node.entity_type)), [graph])
  const relationshipCounts = useMemo(() => count(graph.edges.map((edge) => edge.relationship_type)), [graph])

  function toggle(type: string) {
    setHidden((current) => {
      const next = new Set(current)
      if (next.has(type)) next.delete(type)
      else next.add(type)
      return next
    })
  }

  function onSearch(event: FormEvent) {
    event.preventDefault()
    const text = search.trim().toLowerCase()
    if (!text) return
    const match =
      graph.nodes.find((node) => node.name.toLowerCase() === text || node.qualified_name.toLowerCase() === text) ??
      graph.nodes.find((node) => node.qualified_name.toLowerCase().includes(text))
    setSearchError(match ? null : `No node named “${search.trim()}” in the displayed graph.`)
    if (match) setSelectedId(match.id)
  }

  return (
    <>
      <p className={graph.truncated ? 'alert warn' : 'muted graph-summary'} role="status">
        {graph.truncated
          ? `Partial graph: showing ${formatNumber(graph.nodes.length)} of ${formatNumber(graph.total_nodes)} nodes and ${formatNumber(graph.edges.length)} of ${formatNumber(graph.total_edges)} relationships. Files, classes and interfaces come first; raise the node limit to see more.`
          : `Complete graph: ${formatNumber(graph.nodes.length)} nodes, ${formatNumber(graph.edges.length)} relationships.`}
      </p>

      <div className="graph-toolbar">
        <form className="graph-search" onSubmit={onSearch} role="search">
          <label htmlFor="graph-search" className="sr-only">
            Find a node
          </label>
          <input
            id="graph-search"
            className="input"
            placeholder="Find a node, e.g. AuthService.login"
            value={search}
            onChange={(event) => {
              setSearch(event.target.value)
              setSearchError(null)
            }}
          />
          <button type="submit" className="button secondary">
            Find
          </button>
        </form>
        <div className="legend" aria-label="Entity types">
          {Object.entries(entityCounts).map(([type, n]) => (
            <span key={type} className="legend-item">
              <span className="type-dot" style={{ background: entityColor(type) }} aria-hidden="true" />
              {ENTITY_STYLES[type]?.label ?? type} · {n}
            </span>
          ))}
        </div>
      </div>
      {searchError && (
        <p className="alert error" role="alert">
          {searchError}
        </p>
      )}
      <fieldset className="relationship-filter">
        <legend>Relationships</legend>
        {Object.entries(relationshipCounts).map(([type, n]) => (
          <label key={type} className="filter-item">
            <input type="checkbox" checked={!hidden.has(type)} onChange={() => toggle(type)} />
            <span className="rel-line" style={{ background: relationshipColor(type) }} aria-hidden="true" />
            {type} · {n}
          </label>
        ))}
      </fieldset>

      <div className={`graph-layout${selectedId ? ' with-details' : ''}`}>
        <GraphCanvas graph={graph} hiddenRelationships={hidden} selectedId={selectedId} onSelect={setSelectedId} />
        {selectedId ? (
          <NodeDetails graph={graph} nodeId={selectedId} onSelect={setSelectedId} onClose={() => setSelectedId(null)} />
        ) : (
          <p className="muted graph-hint">
            Click a node to see its details. Drag to pan, scroll or pinch to zoom.
          </p>
        )}
      </div>
    </>
  )
}

function count(values: string[]): Record<string, number> {
  const counts: Record<string, number> = {}
  for (const value of values) counts[value] = (counts[value] ?? 0) + 1
  return Object.fromEntries(Object.entries(counts).sort((a, b) => b[1] - a[1]))
}
