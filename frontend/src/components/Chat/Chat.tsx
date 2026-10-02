import { useEffect, useRef, useState, type FormEvent, type KeyboardEvent } from 'react'
import { askQuestion } from '../../services/api'
import { MAX_QUESTION_CHARS } from '../../types/api'
import { errorText } from '../../utils/format'
import ChatMessage, { type Message, type NewMessage } from './ChatMessage'
import './Chat.css'

interface Props {
  projectId: string
}

const EXAMPLES = [
  'How is authentication implemented?',
  'What are the main modules and how do they depend on each other?',
  'Where is data stored or loaded from?',
]

/**
 * Ask questions about the project. The history lives in this component only: it is
 * kept while the page is open, and each question is sent on its own (the backend has
 * no conversation memory).
 */
export default function Chat({ projectId }: Props) {
  const [messages, setMessages] = useState<Message[]>([])
  const [question, setQuestion] = useState('')
  const [inputError, setInputError] = useState<string | null>(null)
  const [pending, setPending] = useState(false)
  const nextId = useRef(1)
  const sending = useRef(false) // blocks a second request even before React re-renders
  const endRef = useRef<HTMLDivElement>(null)

  useEffect(() => {
    endRef.current?.scrollIntoView({ behavior: 'smooth', block: 'end' })
  }, [messages, pending])

  function add(message: NewMessage) {
    const id = nextId.current++
    setMessages((current) => [...current, { ...message, id }])
  }

  async function send(text: string) {
    const trimmed = text.trim()
    if (sending.current) return
    if (!trimmed) {
      setInputError('Please enter a question.')
      return
    }
    if (trimmed.length > MAX_QUESTION_CHARS) {
      setInputError(`Questions are limited to ${MAX_QUESTION_CHARS} characters.`)
      return
    }
    sending.current = true
    setPending(true)
    setInputError(null)
    setQuestion('')
    add({ role: 'user', text: trimmed })
    try {
      const response = await askQuestion(projectId, trimmed)
      add({ role: 'assistant', response })
    } catch (error) {
      add({ role: 'error', text: errorText(error) })
      setQuestion((current) => current || trimmed) // let the user retry
    } finally {
      sending.current = false
      setPending(false)
    }
  }

  function onSubmit(event: FormEvent) {
    event.preventDefault()
    void send(question)
  }

  function onKeyDown(event: KeyboardEvent<HTMLTextAreaElement>) {
    // Enter sends, Shift+Enter adds a new line.
    if (event.key === 'Enter' && !event.shiftKey && !event.nativeEvent.isComposing) {
      event.preventDefault()
      void send(question)
    }
  }

  return (
    <section className="card chat" aria-labelledby="chat-title">
      <h2 id="chat-title">Ask about the code</h2>
      <p className="card-subtitle">
        Answers come only from this project's code and cite their sources. Run the analysis first.
      </p>

      <div className="chat-log" aria-live="polite">
        {messages.length === 0 && !pending && (
          <div className="chat-empty">
            <p className="muted">Try a question, for example:</p>
            <div className="examples">
              {EXAMPLES.map((example) => (
                <button key={example} type="button" className="example" onClick={() => setQuestion(example)}>
                  {example}
                </button>
              ))}
            </div>
          </div>
        )}
        {messages.map((message) => (
          <ChatMessage key={message.id} message={message} />
        ))}
        {pending && (
          <div className="message assistant">
            <div className="message-author">CodeGraph AI</div>
            <p className="thinking" role="status">
              <span className="spinner" aria-hidden="true" />
              CodeGraph AI is analyzing the codebase…
            </p>
          </div>
        )}
        <div ref={endRef} />
      </div>

      <form className="chat-form" onSubmit={onSubmit}>
        <label htmlFor="chat-question" className="sr-only">
          Your question
        </label>
        <textarea
          id="chat-question"
          className="input chat-input"
          rows={2}
          placeholder="Ask a question about this codebase…"
          value={question}
          onChange={(event) => {
            setQuestion(event.target.value)
            if (inputError) setInputError(null)
          }}
          onKeyDown={onKeyDown}
          aria-invalid={inputError ? true : undefined}
          aria-describedby="chat-help"
        />
        <button className="button" type="submit" disabled={pending}>
          {pending ? 'Waiting…' : 'Send'}
        </button>
      </form>
      <div id="chat-help" className="chat-help">
        {inputError ? (
          <span className="chat-input-error" role="alert">
            {inputError}
          </span>
        ) : (
          <span className="muted">Enter to send, Shift+Enter for a new line.</span>
        )}
        <span className={question.length > MAX_QUESTION_CHARS ? 'chat-input-error' : 'muted'}>
          {question.length}/{MAX_QUESTION_CHARS}
        </span>
      </div>
    </section>
  )
}
