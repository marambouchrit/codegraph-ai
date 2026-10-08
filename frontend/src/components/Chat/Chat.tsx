import { useEffect, useRef, useState, type FormEvent, type KeyboardEvent } from 'react'
import { askQuestion } from '../../services/api'
import { MAX_QUESTION_CHARS } from '../../types/api'
import { button, card, cardSubtitle, cardTitle, input, messageAuthor, spinner } from '../../ui'
import { errorText } from '../../utils/format'
import ChatMessage, { type Message, type NewMessage } from './ChatMessage'

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
    <section className={card} aria-labelledby="chat-title">
      <h2 id="chat-title" className={cardTitle}>
        Ask about the code
      </h2>
      <p className={cardSubtitle}>
        Answers come only from this project's code and cite their sources. Run the analysis first.
      </p>

      <div className="mb-4 grid max-h-[min(70vh,900px)] min-h-40 gap-4.5 overflow-y-auto py-1 pr-1" aria-live="polite">
        {messages.length === 0 && !pending && (
          <div className="self-center text-center">
            <p className="mb-3 text-muted">Try a question, for example:</p>
            <div className="flex flex-wrap justify-center gap-2">
              {EXAMPLES.map((example) => (
                <button
                  key={example}
                  type="button"
                  className="cursor-pointer rounded-full border border-line bg-surface-muted px-3 py-1.5 text-sm text-fg hover:border-accent"
                  onClick={() => setQuestion(example)}>
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
          <div className="grid min-w-0 gap-1.5">
            <div className={messageAuthor}>CodeGraph AI</div>
            <p className="flex items-center gap-2.5 text-muted" role="status">
              <span className={spinner} aria-hidden="true" />
              CodeGraph AI is analyzing the codebase…
            </p>
          </div>
        )}
        <div ref={endRef} />
      </div>

      <form className="flex items-end gap-2" onSubmit={onSubmit}>
        <label htmlFor="chat-question" className="sr-only">
          Your question
        </label>
        <textarea
          id="chat-question"
          className={`${input} max-h-50 min-h-12 resize-y aria-invalid:border-danger`}
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
        <button className={button} type="submit" disabled={pending}>
          {pending ? 'Waiting…' : 'Send'}
        </button>
      </form>
      <div id="chat-help" className="mt-1.5 flex justify-between gap-3 text-xs">
        {inputError ? (
          <span className="text-danger" role="alert">
            {inputError}
          </span>
        ) : (
          <span className="text-muted">Enter to send, Shift+Enter for a new line.</span>
        )}
        <span className={question.length > MAX_QUESTION_CHARS ? 'text-danger' : 'text-muted'}>
          {question.length}/{MAX_QUESTION_CHARS}
        </span>
      </div>
    </section>
  )
}
