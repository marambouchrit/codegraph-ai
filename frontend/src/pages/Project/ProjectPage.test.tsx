import { render, screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { MemoryRouter, Route, Routes } from 'react-router'
import { describe, expect, it } from 'vitest'
import type { AnalysisResponse, ChatResponse } from '../../types/api'
import { ANALYSIS, CHAT, GRAPH, json, mockApi, NOT_ANALYZED, PROJECT, READY } from '../../test/mockApi'
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

describe('Analysis', () => {
  it('runs once, shows a loading state, then the real statistics', async () => {
    const pending = deferred()
    const calls = mockApi({
      ...PROJECT_ROUTES,
      [`POST ${BASE}/analyze`]: () => pending.promise,
    })
    renderProject()
    const button = await screen.findByRole('button', { name: 'Analyze Project' })

    await userEvent.click(button)
    expect(screen.getByText(/Analyzing codebase/)).toBeInTheDocument()
    expect(screen.getByRole('button', { name: /Analyzing/ })).toBeDisabled()
    await userEvent.click(screen.getByRole('button', { name: /Analyzing/ })) // ignored
    expect(calls.filter((call) => call.path === `${BASE}/analyze`)).toHaveLength(1)

    pending.resolve(json(ANALYSIS))
    expect(await screen.findByText(/in 6\.9s\. The project is ready for chat/)).toBeInTheDocument()
    expect(screen.getByText('Ready')).toBeInTheDocument()
    const stats = screen.getByText('Entities').closest('dl') as HTMLElement
    expect(within(stats).getByText('17')).toBeInTheDocument()
    expect(within(stats).getByText('26')).toBeInTheDocument()
    expect(within(stats).getByText('15')).toBeInTheDocument()
    expect(screen.getByText('CALLS · 8')).toBeInTheDocument()
    expect(screen.getByText('BAAI/bge-m3')).toBeInTheDocument()
  })

  it('shows the warnings returned by the backend', async () => {
    const result: AnalysisResponse = { ...ANALYSIS, failed_files: 1, warnings: ['1 file(s) could not be parsed and were skipped.'] }
    mockApi({ ...PROJECT_ROUTES, [`POST ${BASE}/analyze`]: () => json(result) })
    renderProject()

    await userEvent.click(await screen.findByRole('button', { name: 'Analyze Project' }))

    expect(await screen.findByText('1 file(s) could not be parsed and were skipped.')).toBeInTheDocument()
  })

  it('shows a failure, never "Ready"', async () => {
    mockApi({
      ...PROJECT_ROUTES,
      [`POST ${BASE}/analyze`]: () => json({ detail: 'Qdrant is not reachable.' }, 503),
    })
    renderProject()

    await userEvent.click(await screen.findByRole('button', { name: 'Analyze Project' }))

    expect(await screen.findByRole('alert')).toHaveTextContent('Analysis failed: Qdrant is not reachable.')
    expect(screen.queryByText('Ready')).not.toBeInTheDocument()
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

describe('Persisted analysis state', () => {
  it('restores "Ready" and the real counts after a reload, without analyzing again', async () => {
    const calls = mockApi({ ...PROJECT_ROUTES, [`GET ${BASE}/analysis`]: () => json(READY) })
    renderProject()

    expect(await screen.findByText('Ready')).toBeInTheDocument()
    const stats = screen.getByText('Entities').closest('dl') as HTMLElement
    expect(within(stats).getByText('17')).toBeInTheDocument()
    expect(within(stats).getByText('26')).toBeInTheDocument()
    expect(within(stats).getByText('15')).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Analyze again' })).toBeEnabled()
    expect(calls.some((call) => call.method === 'POST')).toBe(false)
  })

  it('shows "Checking…" until the state is known', async () => {
    const pending = deferred()
    mockApi({ ...PROJECT_ROUTES, [`GET ${BASE}/analysis`]: () => pending.promise })
    renderProject()

    expect(await screen.findByText('Checking…')).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: /Analyze/ })).not.toBeInTheDocument()
    pending.resolve(json(NOT_ANALYZED))
    expect(await screen.findByRole('button', { name: 'Analyze Project' })).toBeInTheDocument()
  })

  it('says when the state cannot be read', async () => {
    mockApi({ ...PROJECT_ROUTES, [`GET ${BASE}/analysis`]: () => json({ detail: 'x' }, 500) })
    renderProject()

    expect(await screen.findByRole('alert')).toHaveTextContent('Could not read the analysis state.')
  })

  it('reports "Not analyzed" after a failed re-analysis, as the backend does', async () => {
    mockApi({
      ...PROJECT_ROUTES,
      [`GET ${BASE}/analysis`]: () => json(READY),
      [`POST ${BASE}/analyze`]: () => json({ detail: 'Neo4j is not reachable.' }, 503),
    })
    renderProject()

    await userEvent.click(await screen.findByRole('button', { name: 'Analyze again' }))

    expect(await screen.findByRole('alert')).toHaveTextContent('Neo4j is not reachable. The project is not ready')
    expect(screen.getByText('Not analyzed')).toBeInTheDocument()
    expect(screen.queryByText('Ready')).not.toBeInTheDocument()
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
    const calls = mockApi({
      ...PROJECT_ROUTES,
      [`GET ${BASE}/graph`]: () => json(GRAPH),
      [`POST ${BASE}/analyze`]: () => json(ANALYSIS),
    })
    renderProject()
    await userEvent.click(await screen.findByRole('tab', { name: 'Knowledge Graph' }))
    await screen.findByText(/Complete graph/)

    await userEvent.click(screen.getByRole('button', { name: 'Analyze Project' }))
    await screen.findByText('Ready')

    await waitFor(() => expect(calls.filter((call) => call.path === `${BASE}/graph`)).toHaveLength(2))
  })
})
