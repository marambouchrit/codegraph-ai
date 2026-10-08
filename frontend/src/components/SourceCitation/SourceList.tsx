import { useState } from 'react'
import type { ChatSource } from '../../types/api'
import { badge, sectionTitle } from '../../ui'

const list = 'grid gap-1.5'

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
    <div className="mt-4 border-t border-line pt-3">
      {cited.length > 0 && (
        <>
          <h3 className={`${sectionTitle} mb-2`}>Sources</h3>
          <ol className={list}>
            {cited.map((source) => (
              <SourceCitation key={source.id} source={source} active={source.id === activeId} idPrefix={idPrefix} />
            ))}
          </ol>
        </>
      )}
      {other.length > 0 && (
        <details className="mt-2" open={otherActive || cited.length === 0 || undefined}>
          <summary className="mb-1.5 cursor-pointer text-sm text-muted">
            {cited.length > 0 ? 'Other context given to the model' : 'Context given to the model'} ({other.length})
          </summary>
          <ol className={list}>
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
    <li
      id={`${idPrefix}-source-${source.id}`}
      data-testid="source"
      className={`flex scroll-m-20 gap-2.5 rounded-lg border px-3 py-2.5 transition-colors ${
        active ? 'border-accent bg-accent-soft' : 'border-line bg-surface'
      }`}
      tabIndex={-1}
    >
      <span className="shrink-0 font-mono font-bold text-accent">[{source.id}]</span>
      <div className="grid min-w-0 gap-1">
        <div className="flex flex-wrap items-center gap-x-2.5 gap-y-1.5">
          <code className="font-semibold break-all">{source.entity}</code>
          <span className={badge.neutral}>{source.entity_type}</span>
          <span className={source.found_by === 'graph' ? badge.accent : badge.neutral}>{FOUND_BY[source.found_by]}</span>
        </div>
        <div className="flex flex-wrap items-center gap-x-2.5 gap-y-1.5 text-sm">
          <code className="break-all">{source.file}</code>
          <span className="text-muted">
            {source.start_line === source.end_line
              ? `Line ${source.start_line}`
              : `Lines ${source.start_line}–${source.end_line}`}
          </span>
          <button
            type="button"
            className="cursor-pointer rounded-md border border-line px-2 py-px text-xs text-muted hover:text-fg"
            onClick={copy}
            title="Copy file:lines"
          >
            {copied ? 'Copied' : 'Copy'}
          </button>
        </div>
      </div>
    </li>
  )
}
