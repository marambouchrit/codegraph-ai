import { useState } from 'react'
import type { ChatResponse } from '../../types/api'
import { alert, badge, messageAuthor } from '../../ui'
import SourceList from '../SourceCitation/SourceList'
import MarkdownAnswer from './MarkdownAnswer'

export type NewMessage =
  | { role: 'user'; text: string }
  | { role: 'assistant'; response: ChatResponse }
  | { role: 'error'; text: string }

export type Message = NewMessage & { id: number }

const GRAPH_NOTICE: Record<ChatResponse['graph_status'], string | null> = {
  complete: null,
  partial: 'Graph context was only partly available: the answer relies mostly on the code found by semantic search.',
  unavailable: 'Graph context was temporarily unavailable: the answer was generated from the code found by semantic search only.',
}

export default function ChatMessage({ message }: { message: Message }) {
  if (message.role === 'user') {
    return (
      <div className="grid min-w-0 justify-items-end gap-1.5">
        <div className={messageAuthor}>You</div>
        <div className="max-w-[min(100%,680px)] rounded-xl rounded-br-sm bg-accent px-3.5 py-2.5 wrap-anywhere whitespace-pre-wrap text-on-accent">
          {message.text}
        </div>
      </div>
    )
  }
  if (message.role === 'error') {
    return (
      <div className="grid min-w-0 gap-1.5">
        <div className={messageAuthor}>CodeGraph AI</div>
        <p className={alert.error} role="alert">
          {message.text}
        </p>
      </div>
    )
  }
  return <AssistantMessage id={message.id} response={message.response} />
}

function AssistantMessage({ id, response }: { id: number; response: ChatResponse }) {
  const [activeSource, setActiveSource] = useState<number | null>(null)
  const idPrefix = `message-${id}`
  const notice = GRAPH_NOTICE[response.graph_status]

  function showSource(sourceId: number) {
    setActiveSource(sourceId)
    // Wait one frame: a folded source list opens on this render.
    requestAnimationFrame(() => {
      const element = document.getElementById(`${idPrefix}-source-${sourceId}`)
      element?.scrollIntoView({ behavior: 'smooth', block: 'nearest' })
      element?.focus({ preventScroll: true })
    })
  }

  return (
    <div className="grid min-w-0 gap-1.5" data-testid="assistant-message">
      <div className={messageAuthor}>
        CodeGraph AI
        {response.model && <span className={badge.neutral}>{response.model}</span>}
      </div>
      <div className="min-w-0 rounded-xl rounded-tl-sm border border-line bg-surface-muted px-4 py-3.5">
        {(notice || response.warnings.length > 0) && (
          <div className={`${alert.warn} mb-3`} role="status">
            {notice && <div>{notice}</div>}
            {response.warnings.length > 0 && (
              <ul className="list-disc pl-5">
                {response.warnings.map((warning) => (
                  <li key={warning}>{warning}</li>
                ))}
              </ul>
            )}
          </div>
        )}
        <MarkdownAnswer
          markdown={response.answer}
          sourceIds={response.sources.map((source) => source.id)}
          onCite={showSource}
        />
        <SourceList sources={response.sources} activeId={activeSource} idPrefix={idPrefix} />
      </div>
    </div>
  )
}
