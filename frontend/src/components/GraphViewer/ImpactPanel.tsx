import { DEFAULT_IMPACT_DEPTH, MAX_IMPACT_DEPTH, type ImpactAnalysis, type ProjectGraph } from '../../types/api'
import { alert, buttonSmall, inputSmall, linkButton, relType, sectionTitle } from '../../ui'
import { relationshipColor } from './graphStyle'

export type ImpactState =
  | { state: 'idle' }
  | { state: 'loading' }
  | { state: 'error'; message: string }
  | { state: 'loaded'; result: ImpactAnalysis }

interface Props {
  graph: ProjectGraph
  impact: ImpactState
  depth: number
  onDepthChange: (depth: number) => void
  onRun: () => void
  onSelect: (nodeId: string) => void
}

const DEPTHS = Array.from({ length: MAX_IMPACT_DEPTH }, (_, index) => index + 1)

/**
 * Impact analysis of the selected node: which entities reference it, directly or
 * through others, as computed by the backend from the graph. Affected entities shown
 * in the graph are highlighted there; the others are listed with their file.
 */
export default function ImpactPanel({ graph, impact, depth, onDepthChange, onRun, onSelect }: Props) {
  const displayed = new Set(graph.nodes.map((node) => node.id))
  return (
    <div className="mt-3.5 border-t border-line pt-3">
      <h4 className="mb-2 font-semibold">Impact analysis</h4>
      <div className="flex flex-wrap items-center gap-2">
        <label>
          Depth{' '}
          <select
            className={inputSmall}
            value={depth}
            onChange={(event) => onDepthChange(Number(event.target.value))}
            aria-label="Impact depth"
          >
            {DEPTHS.map((value) => (
              <option key={value} value={value}>
                {value}
                {value === DEFAULT_IMPACT_DEPTH ? ' (default)' : ''}
              </option>
            ))}
          </select>
        </label>
        <button type="button" className={buttonSmall} onClick={onRun} disabled={impact.state === 'loading'}>
          {impact.state === 'loading' ? 'Analyzing…' : 'Impact analysis'}
        </button>
      </div>

      {impact.state === 'idle' && (
        <p className="mt-2 text-muted">What may be affected if this entity changes: who calls, uses, extends or imports it.</p>
      )}
      {impact.state === 'error' && (
        <p className={`${alert.error} mt-2`} role="alert">
          The impact analysis failed. {impact.message}
        </p>
      )}
      {impact.state === 'loaded' && <ImpactResult result={impact.result} displayed={displayed} onSelect={onSelect} />}
    </div>
  )
}

function ImpactResult({
  result,
  displayed,
  onSelect,
}: {
  result: ImpactAnalysis
  displayed: ReadonlySet<string>
  onSelect: (nodeId: string) => void
}) {
  if (result.total === 0) {
    return (
      <p className="mt-2 text-muted" role="status">
        No detected reference to this entity{result.contained > 0 ? ' or to what it defines' : ''} within{' '}
        {result.max_depth} step{result.max_depth > 1 ? 's' : ''}. Dynamic calls are not detected.
      </p>
    )
  }
  const depths = [...new Set(result.affected.map((item) => item.depth))].sort((a, b) => a - b)
  const hiddenCount = result.affected.filter((item) => !displayed.has(item.entity.id)).length
  return (
    <div role="status">
      <p className="mt-2" data-testid="impact-summary">
        <strong>{result.total}</strong> {result.total === 1 ? 'entity' : 'entities'} may be affected
        {result.truncated && ' (more exist: the list is truncated)'}.
      </p>
      {depths.map((level) => (
        <div key={level} data-testid="impact-level">
          <h5 className={`${sectionTitle} mt-2.5 mb-1`}>
            {level === 1 ? 'Direct (depth 1)' : `Depth ${level}`} · {result.by_depth[String(level)]}
          </h5>
          <ul className="grid max-h-65 gap-1 overflow-y-auto">
            {result.affected
              .filter((item) => item.depth === level)
              .map((item) => (
                <li key={item.entity.id} className="flex flex-wrap items-baseline gap-x-2 gap-y-0.5 wrap-anywhere">
                  <span className={relType} style={{ color: relationshipColor(item.relationship_type) }}>
                    {item.relationship_type}
                  </span>
                  {displayed.has(item.entity.id) ? (
                    <button type="button" className={linkButton} onClick={() => onSelect(item.entity.id)}>
                      {item.entity.qualified_name}
                    </button>
                  ) : (
                    <span title="Not in the displayed graph">{item.entity.qualified_name}</span>
                  )}
                  <span className="text-xs text-muted">{item.entity.file_path}</span>
                </li>
              ))}
          </ul>
        </div>
      ))}
      {hiddenCount > 0 && (
        <p className="mt-2.5 text-xs text-muted">
          {hiddenCount} of them {hiddenCount === 1 ? 'is' : 'are'} not in the displayed graph (raise the node
          limit to see them highlighted).
        </p>
      )}
    </div>
  )
}
