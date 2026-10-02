import type { GraphEdge, GraphNode, ProjectGraph } from '../../types/api'
import { entityColor, ENTITY_STYLES, relationshipColor } from './graphStyle'

interface Props {
  graph: ProjectGraph
  nodeId: string
  onSelect: (nodeId: string) => void
  onClose: () => void
}

/** The selected node's metadata (as stored in Neo4j) and its relationships in the shown graph. */
export default function NodeDetails({ graph, nodeId, onSelect, onClose }: Props) {
  const byId = new Map(graph.nodes.map((node) => [node.id, node]))
  const node = byId.get(nodeId)
  if (!node) return null

  const outgoing = graph.edges.filter((edge) => edge.source === nodeId)
  const incoming = graph.edges.filter((edge) => edge.target === nodeId)

  return (
    <aside className="node-details" aria-labelledby="node-details-title">
      <div className="node-details-head">
        <span className="type-dot" style={{ background: entityColor(node.entity_type) }} aria-hidden="true" />
        <h3 id="node-details-title">{node.name}</h3>
        <button type="button" className="close" onClick={onClose} aria-label="Close details">
          ×
        </button>
      </div>
      <dl className="node-facts">
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
        <p className="muted node-note">Only relationships within the displayed graph are listed.</p>
      )}
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
    <div className="node-relationships">
      <h4>
        {title} ({edges.length})
      </h4>
      {edges.length === 0 ? (
        <p className="muted">None</p>
      ) : (
        <ul>
          {edges.map((edge) => {
            const node = other(edge)
            return (
              <li key={edge.id}>
                <span className="rel-type" style={{ color: relationshipColor(edge.relationship_type) }}>
                  {edge.relationship_type}
                </span>
                {node && (
                  <button type="button" className="link-button" onClick={() => onSelect(node.id)}>
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
