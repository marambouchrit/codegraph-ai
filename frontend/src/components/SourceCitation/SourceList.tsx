import { useState } from 'react'
import type { ChatSource } from '../../types/api'
import './SourceList.css'

interface Props {
  sources: ChatSource[]
  activeId: number | null
  idPrefix: string // makes element IDs unique per message
}

const FOUND_BY: Record<ChatSource['found_by'], string> = {
  semantic_search: 'Semantic search',
  graph: 'Knowledge graph',
}

/**
 * The sources given to the model, exactly as the API returned them. The cited ones are
 * listed first; the others (context the answer did not use) are folded away.
 */
export default function SourceList({ sources, activeId, idPrefix }: Props) {
  const cited = sources.filter((source) => source.cited)
  const other = sources.filter((source) => !source.cited)
  // Open the folded list when a citation points into it.
  const otherActive = other.some((source) => source.id === activeId)

  if (sources.length === 0) {
    return null
  }
  return (
    <div className="sources">
      {cited.length > 0 && (
        <>
          <h3 className="sources-title">Sources</h3>
          <ol className="source-list">
            {cited.map((source) => (
              <SourceCitation key={source.id} source={source} active={source.id === activeId} idPrefix={idPrefix} />
            ))}
          </ol>
        </>
      )}
      {other.length > 0 && (
        <details className="other-sources" open={otherActive || cited.length === 0 || undefined}>
          <summary>
            {cited.length > 0 ? 'Other context given to the model' : 'Context given to the model'} ({other.length})
          </summary>
          <ol className="source-list">
            {other.map((source) => (
              <SourceCitation key={source.id} source={source} active={source.id === activeId} idPrefix={idPrefix} />
            ))}
          </ol>
        </details>
      )}
    </div>
  )
}

function SourceCitation({ source, active, idPrefix }: { source: ChatSource; active: boolean; idPrefix: string }) {
  const [copied, setCopied] = useState(false)
  const location = `${source.file}:${source.start_line}-${source.end_line}`

  function copy() {
    navigator.clipboard
      ?.writeText(location)
      .then(() => {
        setCopied(true)
        setTimeout(() => setCopied(false), 1500)
      })
      .catch(() => undefined)
  }

  return (
    <li id={`${idPrefix}-source-${source.id}`} className={`source${active ? ' active' : ''}`} tabIndex={-1}>
      <span className="source-id">[{source.id}]</span>
      <div className="source-body">
        <div className="source-head">
          <code className="source-entity">{source.entity}</code>
          <span className="badge">{source.entity_type}</span>
          <span className={`badge ${source.found_by === 'graph' ? 'accent' : ''}`}>{FOUND_BY[source.found_by]}</span>
        </div>
        <div className="source-location">
          <code>{source.file}</code>
          <span className="muted">
            {source.start_line === source.end_line
              ? `Line ${source.start_line}`
              : `Lines ${source.start_line}–${source.end_line}`}
          </span>
          <button type="button" className="copy" onClick={copy} title="Copy file:lines">
            {copied ? 'Copied' : 'Copy'}
          </button>
        </div>
      </div>
    </li>
  )
}
