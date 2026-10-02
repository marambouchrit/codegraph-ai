import { useState } from 'react'
import type { ChatResponse } from '../../types/api'
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
      <div className="message user">
        <div className="message-author">You</div>
        <div className="message-bubble">{message.text}</div>
      </div>
    )
  }
  if (message.role === 'error') {
    return (
      <div className="message assistant">
        <div className="message-author">CodeGraph AI</div>
        <p className="alert error" role="alert">
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
    <div className="message assistant">
      <div className="message-author">
        CodeGraph AI
        {response.model && <span className="badge">{response.model}</span>}
      </div>
      <div className="message-bubble">
        {(notice || response.warnings.length > 0) && (
          <div className="alert warn" role="status">
            {notice && <div>{notice}</div>}
            {response.warnings.length > 0 && (
              <ul>
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
