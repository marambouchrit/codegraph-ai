import { cleanup, render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { MemoryRouter, Route, Routes } from 'react-router'
import { describe, expect, it } from 'vitest'
import type { ChatResponse } from '../../types/api'
import { ANALYSIS, CHAT, DEPENDENCIES, GRAPH, json, mockApi, NOT_ANALYZED, PROJECT, READY } from '../../test/mockApi'
import ProjectPage from './ProjectPage'

const BASE = `/projects/${PROJECT.id}`

// The project and its persisted analysis state (not analyzed unless a test says otherwise).
const PROJECT_ROUTES = {
  [`GET ${BASE}`]: () => json(PROJECT),
  [`GET ${BASE}/analysis`]: () => json(NOT_ANALYZED),
}

function renderProject(id = PROJECT.id) {
  render(
    <MemoryRouter initialEntries={[`/projects/${id}`]}>
      <Routes>
        <Route path="/projects/:projectId" element={<ProjectPage />} />
      </Routes>
    </MemoryRouter>,
  )
}

/** A response we resolve by hand, to look at the page while the request is pending. */
function deferred() {
  let resolve!: (response: Response) => void
  const promise = new Promise<Response>((done) => (resolve = done))
  return { promise, resolve }
}

describe('ProjectPage', () => {
  it('shows the project metadata', async () => {
    mockApi({ ...PROJECT_ROUTES })
    renderProject()

    expect(await screen.findByRole('heading', { name: 'auth-service' })).toBeInTheDocument()
    expect(screen.getByText('auth.zip')).toBeInTheDocument()
    expect(screen.getByText('python · 4')).toBeInTheDocument()
    expect(await screen.findByText('Not analyzed')).toBeInTheDocument()
    expect(screen.getByText(/has not been analyzed yet/)).toBeInTheDocument()
  })

  it('shows "Project not found" for an unknown project', async () => {
    mockApi({})
    renderProject('f'.repeat(32))

    expect(await screen.findByRole('heading', { name: 'Project not found' })).toBeInTheDocument()
  })
})

describe('Chat', () => {
  async function ask(question: string) {
    await userEvent.type(screen.getByLabelText('Your question'), question)
    await userEvent.click(screen.getByRole('button', { name: 'Send' }))
  }

  it('renders the Markdown answer, citations and sources', async () => {
    const calls = mockApi({ ...PROJECT_ROUTES, [`POST ${BASE}/chat`]: () => json(CHAT) })
    renderProject()
    await screen.findByRole('heading', { name: 'auth-service' })

    await ask('How is authentication implemented?')

    expect(await screen.findByRole('heading', { name: 'Login' })).toBeInTheDocument()
    expect(screen.getByText('password').tagName).toBe('STRONG')
    expect(screen.getByText('AuthService.login', { selector: 'p code' })).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Show source 1' })).toHaveTextContent('1')
    expect(screen.getByText('gemini-flash-lite-latest')).toBeInTheDocument()
    // Sources exactly as returned: entity, file, lines; the uncited one is folded away.
    const source = document.getElementById('message-2-source-1') as HTMLElement
    expect(source).toHaveTextContent('[1]AuthService.login')
    expect(source).toHaveTextContent('auth/service.py')
    expect(source).toHaveTextContent('Lines 14–20')
    expect(screen.getByText('Knowledge graph')).toBeInTheDocument()
    expect(screen.getByText(/Other context given to the model \(1\)/)).toBeInTheDocument()
    expect(JSON.parse(calls.at(-1)?.init?.body as string)).toEqual({ question: 'How is authentication implemented?' })
  })

  it('highlights the source of a clicked citation', async () => {
    mockApi({ ...PROJECT_ROUTES, [`POST ${BASE}/chat`]: () => json(CHAT) })
    renderProject()
    await screen.findByRole('heading', { name: 'auth-service' })
    await ask('How?')

    await userEvent.click(await screen.findByRole('button', { name: 'Show source 2' }))

    expect(document.getElementById('message-2-source-2')).toHaveClass('active')
  })

  it('shows a loading state and blocks a second question while waiting', async () => {
    const pending = deferred()
    const calls = mockApi({ ...PROJECT_ROUTES, [`POST ${BASE}/chat`]: () => pending.promise })
    renderProject()
    await screen.findByRole('heading', { name: 'auth-service' })

    await ask('First?')
    expect(screen.getByText('CodeGraph AI is analyzing the codebase…')).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Waiting…' })).toBeDisabled()
    await userEvent.type(screen.getByLabelText('Your question'), 'Second?{Enter}')
    expect(calls.filter((call) => call.path === `${BASE}/chat`)).toHaveLength(1)

    pending.resolve(json(CHAT))
    expect(await screen.findByRole('heading', { name: 'Login' })).toBeInTheDocument()
  })

  it('keeps the history of several questions', async () => {
    mockApi({ ...PROJECT_ROUTES, [`POST ${BASE}/chat`]: () => json(CHAT) })
    renderProject()
    await screen.findByRole('heading', { name: 'auth-service' })

    await ask('First question')
    await screen.findByRole('heading', { name: 'Login' })
    await ask('Second question')

    expect(await screen.findAllByRole('heading', { name: 'Login' })).toHaveLength(2)
    expect(screen.getByText('First question')).toBeInTheDocument()
    expect(screen.getByText('Second question')).toBeInTheDocument()
  })

  it('rejects an empty question without calling the backend', async () => {
    const calls = mockApi({ ...PROJECT_ROUTES })
    renderProject()
    await screen.findByRole('heading', { name: 'auth-service' })

    await ask('   ')

    expect(screen.getByRole('alert')).toHaveTextContent('Please enter a question.')
    expect(calls.every((call) => call.path !== `${BASE}/chat`)).toBe(true)
  })

  it('shows graph status and warnings when the graph was unavailable', async () => {
    const degraded: ChatResponse = { ...CHAT, graph_status: 'unavailable', warnings: ['Neo4j is not reachable.'] }
    mockApi({ ...PROJECT_ROUTES, [`POST ${BASE}/chat`]: () => json(degraded) })
    renderProject()
    await screen.findByRole('heading', { name: 'auth-service' })

    await ask('How?')

    expect(await screen.findByText(/Graph context was temporarily unavailable/)).toBeInTheDocument()
    expect(screen.getByText('Neo4j is not reachable.')).toBeInTheDocument()
  })

  it('shows API errors in the conversation and keeps the question for a retry', async () => {
    mockApi({
      ...PROJECT_ROUTES,
      [`POST ${BASE}/chat`]: () => json({ detail: 'The LLM is unavailable.' }, 503),
    })
    renderProject()
    await screen.findByRole('heading', { name: 'auth-service' })

    await ask('How?')

    expect(await screen.findByRole('alert')).toHaveTextContent('The LLM is unavailable.')
    expect(screen.getByLabelText('Your question')).toHaveValue('How?')
  })
})

describe('Knowledge Graph tab', () => {
  it('loads the graph only when opened, and keeps the chat history', async () => {
    const calls = mockApi({
      ...PROJECT_ROUTES,
      [`GET ${BASE}/analysis`]: () => json(READY),
      [`POST ${BASE}/chat`]: () => json(CHAT),
      [`GET ${BASE}/graph`]: () => json(GRAPH),
    })
    renderProject()
    await screen.findByText('Ready')
    await userEvent.type(screen.getByLabelText('Your question'), 'How?')
    await userEvent.click(screen.getByRole('button', { name: 'Send' }))
    await screen.findByRole('heading', { name: 'Login' })
    expect(calls.some((call) => call.path === `${BASE}/graph`)).toBe(false)

    await userEvent.click(screen.getByRole('tab', { name: 'Knowledge Graph' }))
    expect(await screen.findByText(/Complete graph: 4 nodes, 3 relationships/)).toBeInTheDocument()
    expect(calls.filter((call) => call.path === `${BASE}/graph`)).toHaveLength(1)

    await userEvent.click(screen.getByRole('tab', { name: 'Chat' }))
    expect(screen.getByRole('heading', { name: 'Login' })).toBeVisible() // history kept
    await userEvent.click(screen.getByRole('tab', { name: 'Knowledge Graph' }))
    expect(calls.filter((call) => call.path === `${BASE}/graph`)).toHaveLength(1) // not reloaded
  })

  it('reloads the graph after a new analysis', async () => {
    const later = { ...READY, analysis: { ...ANALYSIS, analyzed_at: '2026-10-03T10:00:00Z' } }
    let state = READY
    const calls = mockApi({
      ...PROJECT_ROUTES,
      [`GET ${BASE}/analysis`]: () => json(state),
      [`GET ${BASE}/graph`]: () => json(GRAPH),
    })
    renderProject()
    await userEvent.click(await screen.findByRole('tab', { name: 'Knowledge Graph' }))
    await screen.findByText(/Complete graph/)

    // A new analysis finished (another analyzed_at): the page is opened again.
    state = later
    cleanup()
    renderProject()
    await userEvent.click(await screen.findByRole('tab', { name: 'Knowledge Graph' }))

    await waitFor(() => expect(calls.filter((call) => call.path === `${BASE}/graph`)).toHaveLength(2))
  })

  it('opens the insights only when asked', async () => {
    const calls = mockApi({
      ...PROJECT_ROUTES,
      [`GET ${BASE}/analysis`]: () => json(READY),
      [`GET ${BASE}/analysis/dependencies`]: () => json(DEPENDENCIES),
    })
    renderProject()
    await screen.findByText('Ready')
    expect(calls.some((call) => call.path.endsWith('/dependencies'))).toBe(false)

    await userEvent.click(screen.getByRole('tab', { name: 'Insights' }))

    expect(await screen.findByRole('heading', { name: 'Dependency analysis' })).toBeInTheDocument()
    expect(calls.filter((call) => call.path.endsWith('/dependencies'))).toHaveLength(1)
    expect(calls.some((call) => call.path.endsWith('/architecture'))).toBe(false) // no LLM call unasked
  })
})
