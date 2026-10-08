import type { ReactNode } from 'react'
import type { GraphEdge, GraphNode, ProjectGraph } from '../../types/api'
import { linkButton, relType, sectionTitle, typeDot } from '../../ui'
import { entityColor, ENTITY_STYLES, relationshipColor } from './graphStyle'

interface Props {
  graph: ProjectGraph
  nodeId: string
  onSelect: (nodeId: string) => void
  onClose: () => void
  children?: ReactNode // extra sections about the node (impact analysis)
}

/** The selected node's metadata (as stored in Neo4j) and its relationships in the shown graph. */
export default function NodeDetails({ graph, nodeId, onSelect, onClose, children }: Props) {
  const byId = new Map(graph.nodes.map((node) => [node.id, node]))
  const node = byId.get(nodeId)
  if (!node) return null

  const outgoing = graph.edges.filter((edge) => edge.source === nodeId)
  const incoming = graph.edges.filter((edge) => edge.target === nodeId)

  return (
    <aside
      className="min-w-0 rounded-[10px] border border-line bg-surface p-3.5 text-sm"
      aria-labelledby="node-details-title"
    >
      <div className="flex items-center gap-2">
        <span className={typeDot} style={{ background: entityColor(node.entity_type) }} aria-hidden="true" />
        <h3 id="node-details-title" className="flex-1 text-base font-semibold wrap-anywhere">
          {node.name}
        </h3>
        <button type="button" className="cursor-pointer text-xl leading-none text-muted" onClick={onClose} aria-label="Close details">
          ×
        </button>
      </div>
      <dl className="my-3 grid grid-cols-[auto_minmax(0,1fr)] gap-x-3 gap-y-1 wrap-anywhere [&>dt]:text-muted">
        <dt>Type</dt>
        <dd>{ENTITY_STYLES[node.entity_type]?.label ?? node.entity_type}</dd>
        <dt>Qualified name</dt>
        <dd>
          <code>{node.qualified_name}</code>
        </dd>
        <dt>File</dt>
        <dd>
          <code>{node.file_path}</code>
        </dd>
        <dt>Lines</dt>
        <dd>
          {node.start_line === node.end_line ? node.start_line : `${node.start_line}–${node.end_line}`}
        </dd>
        <dt>Language</dt>
        <dd>{node.language}</dd>
      </dl>

      <Relationships title="Outgoing" edges={outgoing} other={(edge) => byId.get(edge.target)} onSelect={onSelect} />
      <Relationships title="Incoming" edges={incoming} other={(edge) => byId.get(edge.source)} onSelect={onSelect} />
      {graph.truncated && (
        <p className="mt-2.5 text-xs text-muted">Only relationships within the displayed graph are listed.</p>
      )}
      {children}
    </aside>
  )
}

function Relationships({
  title,
  edges,
  other,
  onSelect,
}: {
  title: string
  edges: GraphEdge[]
  other: (edge: GraphEdge) => GraphNode | undefined
  onSelect: (nodeId: string) => void
}) {
  return (
    <div>
      <h4 className={`${sectionTitle} mt-3 mb-1`}>
        {title} ({edges.length})
      </h4>
      {edges.length === 0 ? (
        <p className="text-muted">None</p>
      ) : (
        <ul className="grid max-h-55 gap-1 overflow-y-auto">
          {edges.map((edge) => {
            const node = other(edge)
            return (
              <li key={edge.id} className="flex flex-wrap gap-x-2 gap-y-0.5">
                <span className={relType} style={{ color: relationshipColor(edge.relationship_type) }}>
                  {edge.relationship_type}
                </span>
                {node && (
                  <button type="button" className={linkButton} onClick={() => onSelect(node.id)}>
                    {node.qualified_name}
                  </button>
                )}
              </li>
            )
          })}
        </ul>
      )}
    </div>
  )
}
