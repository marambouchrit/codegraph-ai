// The analysis panel of the project page: a background job on the server, followed by polling.
import { act, render, screen, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { MemoryRouter, Route, Routes } from 'react-router'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { POLL_INTERVAL_MS } from '../../hooks/useAnalysis'
import {
  ANALYSIS, INCREMENTAL, json, mockApi, NOT_ANALYZED, PROJECT, QUEUED, READY, sequence, status,
} from '../../test/mockApi' // prettier-ignore
import type { Call } from '../../test/mockApi'
import ProjectPage from './ProjectPage'

const BASE = `/projects/${PROJECT.id}`
const STATE = `GET ${BASE}/analysis`
const START = `POST ${BASE}/analyze`

function renderProject() {
  render(
    <MemoryRouter initialEntries={[BASE]}>
      <Routes>
        <Route path="/projects/:projectId" element={<ProjectPage />} />
      </Routes>
    </MemoryRouter>,
  )
}

/** Only the polling timer is faked: the test decides when the next poll happens. */
function controlPolling() {
  vi.useFakeTimers({ toFake: ['setInterval', 'clearInterval'] })
}

async function nextPoll() {
  await act(async () => {
    await vi.advanceTimersByTimeAsync(POLL_INTERVAL_MS)
  })
}

const reads = (calls: Call[]) => calls.filter((call) => call.method === 'GET' && call.path === `${BASE}/analysis`)
const starts = (calls: Call[]) => calls.filter((call) => call.method === 'POST')

afterEach(() => {
  vi.useRealTimers()
})

describe('Starting an analysis', () => {
  it('starts a background job and follows it until it is ready', async () => {
    controlPolling()
    const running = status('running', { mode: 'full', phase: 'embedding', completed: 5, total: 15, unit: 'chunks' })
    const calls = mockApi({
      [`GET ${BASE}`]: () => json(PROJECT),
      [STATE]: sequence(NOT_ANALYZED, running, READY),
      [START]: () => json(QUEUED, 202),
    })
    renderProject()

    await userEvent.click(await screen.findByRole('button', { name: 'Analyze Project' }))

    // The request returned at once: the job is queued on the server.
    expect(await screen.findByText('Queued')).toBeInTheDocument()
    expect(screen.getByText('Waiting for the analysis worker…')).toBeInTheDocument()
    expect(screen.getByRole('button', { name: /Analyzing/ })).toBeDisabled()
    expect(starts(calls)).toHaveLength(1)

    await nextPoll()
    expect(screen.getByText('Analyzing')).toBeInTheDocument()
    expect(screen.getByText('Analysis running')).toBeInTheDocument()

    await nextPoll()
    expect(screen.getByText('Ready')).toBeInTheDocument()
    const stats = screen.getByText('Entities').closest('dl') as HTMLElement
    expect(within(stats).getByText('17')).toBeInTheDocument()
    expect(within(stats).getByText('26')).toBeInTheDocument()
    expect(within(stats).getByText('15')).toBeInTheDocument()
    expect(screen.getByText('CALLS · 8')).toBeInTheDocument()
    expect(screen.getByText('BAAI/bge-m3')).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Analyze again' })).toBeEnabled()
  })

  it('stops polling once the job has finished', async () => {
    controlPolling()
    const calls = mockApi({
      [`GET ${BASE}`]: () => json(PROJECT),
      [STATE]: sequence(NOT_ANALYZED, READY),
      [START]: () => json(QUEUED, 202),
    })
    renderProject()
    await userEvent.click(await screen.findByRole('button', { name: 'Analyze Project' }))
    await screen.findByText('Queued')
    await nextPoll()
    expect(screen.getByText('Ready')).toBeInTheDocument()
    const count = reads(calls).length

    await nextPoll()
    await nextPoll()

    expect(reads(calls)).toHaveLength(count) // no request after "ready"
  })

  it('ignores a second click while the job is active', async () => {
    controlPolling()
    const calls = mockApi({
      [`GET ${BASE}`]: () => json(PROJECT),
      [STATE]: sequence(NOT_ANALYZED, QUEUED),
      [START]: () => json(QUEUED, 202),
    })
    renderProject()
    await userEvent.click(await screen.findByRole('button', { name: 'Analyze Project' }))
    await screen.findByText('Queued')

    await userEvent.click(screen.getByRole('button', { name: /Analyzing/ }))

    expect(starts(calls)).toHaveLength(1)
  })

  it('shows why an analysis was refused, and what the server says now', async () => {
    const running = status('running', { phase: 'parsing', completed: 1, total: 4, unit: 'files' })
    mockApi({
      [`GET ${BASE}`]: () => json(PROJECT),
      [STATE]: sequence(NOT_ANALYZED, running),
      [START]: () => json({ detail: 'This project is already being analyzed. Wait for it to finish.' }, 409),
    })
    renderProject()

    await userEvent.click(await screen.findByRole('button', { name: 'Analyze Project' }))

    expect(await screen.findByRole('alert')).toHaveTextContent(
      'The analysis could not be started: This project is already being analyzed.',
    )
    expect(screen.getByText('Analyzing')).toBeInTheDocument() // the job that is really running
  })

  it('can force a full re-analysis', async () => {
    const calls = mockApi({
      [`GET ${BASE}`]: () => json(PROJECT),
      [STATE]: () => json(READY),
      [START]: () => json(QUEUED, 202),
    })
    renderProject()

    await userEvent.click(await screen.findByRole('button', { name: 'Full re-analysis' }))

    await screen.findByText('Queued')
    expect(String(vi.mocked(fetch).mock.calls.at(-1)?.[0])).toContain('/analyze?full=true')
    expect(starts(calls)).toHaveLength(1)
  })
})

describe('Real progress', () => {
  it('shows the phase and the real counts of the running job', async () => {
    const running = status('running', { mode: 'incremental', phase: 'embedding', completed: 5, total: 7, unit: 'chunks' }, ANALYSIS)
    mockApi({ [`GET ${BASE}`]: () => json(PROJECT), [STATE]: () => json(running) })
    renderProject()

    const current = await screen.findByText(/Embedding changed code/)
    expect(current.closest('li')).toHaveAttribute('aria-current', 'step')
    expect(current.closest('li')).toHaveTextContent('5 / 7 chunks')
    expect(screen.getByText('Incremental')).toBeInTheDocument()
    const bar = screen.getByRole('progressbar')
    expect(bar).toHaveAttribute('value', '5')
    expect(bar).toHaveAttribute('max', '7')
    // Earlier phases are done, later ones are not.
    expect(screen.getByText(/Parsing changed files/).closest('li')).toHaveClass('done')
    expect(screen.getByText(/Finalizing/).closest('li')).not.toHaveClass('done')
    // The previous report stays visible, not presented as the new result.
    expect(screen.getByText(/Last analysis:/)).toBeInTheDocument()
    expect(screen.queryByText(/The project is ready for chat/)).not.toBeInTheDocument()
  })

  it('shows files while parsing', async () => {
    const running = status('running', { mode: 'full', phase: 'parsing', completed: 12, total: 42, unit: 'files' })
    mockApi({ [`GET ${BASE}`]: () => json(PROJECT), [STATE]: () => json(running) })
    renderProject()

    expect((await screen.findByText(/Parsing changed files/)).closest('li')).toHaveTextContent('12 / 42 files')
    expect(screen.getByText('Full analysis')).toBeInTheDocument()
  })

  it('shows no numbers and no bar for a phase with nothing to count', async () => {
    const running = status('running', { mode: 'full', phase: 'resolving' })
    mockApi({ [`GET ${BASE}`]: () => json(PROJECT), [STATE]: () => json(running) })
    renderProject()

    const current = await screen.findByText(/Resolving relationships/)
    expect(current.closest('li')).toHaveAttribute('aria-current', 'step')
    expect(screen.queryByRole('progressbar')).not.toBeInTheDocument()
    expect(document.querySelector('.step-count')).toBeNull()
    expect(document.body.textContent).not.toMatch(/\d+\s*%/) // never a percentage
  })

  it('resumes following a job that was running when the page was reloaded', async () => {
    controlPolling()
    const running = status('running', { mode: 'full', phase: 'embedding', completed: 32, total: 378, unit: 'chunks' })
    const later = status('running', { mode: 'full', phase: 'embedding', completed: 64, total: 378, unit: 'chunks' })
    const calls = mockApi({ [`GET ${BASE}`]: () => json(PROJECT), [STATE]: sequence(running, later) })
    renderProject()

    expect((await screen.findByText(/Embedding changed code/)).closest('li')).toHaveTextContent('32 / 378 chunks')
    expect(starts(calls)).toHaveLength(0) // nothing was started: the job was already there

    await nextPoll()
    expect(screen.getByText(/Embedding changed code/).closest('li')).toHaveTextContent('64 / 378 chunks')
  })

  it('keeps polling after a failed refresh', async () => {
    controlPolling()
    const running = status('running', { phase: 'parsing', completed: 1, total: 4, unit: 'files' })
    let attempt = 0
    mockApi({
      [`GET ${BASE}`]: () => json(PROJECT),
      [STATE]: () => (++attempt === 2 ? json({ detail: 'x' }, 500) : json(attempt > 2 ? READY : running)),
    })
    renderProject()
    await screen.findByText('Analyzing')

    await nextPoll()
    expect(screen.getByText(/Could not refresh the progress/)).toBeInTheDocument()
    await nextPoll()
    expect(screen.getByText('Ready')).toBeInTheDocument()
  })
})

describe('Results and failures', () => {
  it('restores "Ready" and the real counts after a reload, without analyzing again', async () => {
    const calls = mockApi({ [`GET ${BASE}`]: () => json(PROJECT), [STATE]: () => json(READY) })
    renderProject()

    expect(await screen.findByText('Ready')).toBeInTheDocument()
    const stats = screen.getByText('Entities').closest('dl') as HTMLElement
    expect(within(stats).getByText('17')).toBeInTheDocument()
    expect(screen.getByText(/in 6\.9s\. The project is ready for chat/)).toBeInTheDocument()
    expect(screen.getByText(/Every file was processed: 4 files parsed, 15 chunks embedded/)).toBeInTheDocument()
    expect(starts(calls)).toHaveLength(0)
  })

  it('says what an incremental analysis really processed', async () => {
    const ready = status('ready', { status: 'ready', mode: 'incremental' }, INCREMENTAL)
    mockApi({ [`GET ${BASE}`]: () => json(PROJECT), [STATE]: () => json(ready) })
    renderProject()

    expect(await screen.findByText('Incremental analysis')).toBeInTheDocument()
    expect(
      screen.getByText(/1 of 4 files parsed \(0 added, 1 modified, 0 deleted, 3 unchanged\); 1 chunks embedded, 14 reused\./),
    ).toBeInTheDocument()
  })

  it('shows the warnings returned by the backend', async () => {
    const result = { ...ANALYSIS, failed_files: 1, warnings: ['1 file(s) could not be parsed and were skipped.'] }
    mockApi({ [`GET ${BASE}`]: () => json(PROJECT), [STATE]: () => json(status('ready', { status: 'ready' }, result)) })
    renderProject()

    expect(await screen.findByText('1 file(s) could not be parsed and were skipped.')).toBeInTheDocument()
  })

  it('shows a failed job with no result as not ready', async () => {
    const failed = status('failed', { status: 'failed', phase: 'graph', error: 'Neo4j is not reachable.' })
    mockApi({ [`GET ${BASE}`]: () => json(PROJECT), [STATE]: () => json(failed) })
    renderProject()

    expect(await screen.findByRole('alert')).toHaveTextContent(
      'Analysis failed: Neo4j is not reachable. The project is not ready: analyze it again.',
    )
    expect(screen.getByText('Failed')).toBeInTheDocument()
    expect(screen.queryByText('Ready')).not.toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Analyze Project' })).toBeEnabled()
  })

  it('keeps the previous analysis when a job failed before writing anything', async () => {
    const failed = status('failed', { status: 'failed', phase: 'embedding',
      error: 'The embedding model failed to embed the text.' }, ANALYSIS) // prettier-ignore
    mockApi({ [`GET ${BASE}`]: () => json(PROJECT), [STATE]: () => json(failed) })
    renderProject()

    expect(await screen.findByRole('alert')).toHaveTextContent(
      'the previous analysis below is still valid and chat keeps using it',
    )
    const stats = screen.getByText('Entities').closest('dl') as HTMLElement
    expect(within(stats).getByText('17')).toBeInTheDocument()
    expect(screen.queryByText(/The project is ready for chat/)).not.toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Analyze again' })).toBeEnabled()
  })

  it('shows "Checking…" until the state is known, and says when it cannot be read', async () => {
    mockApi({ [`GET ${BASE}`]: () => json(PROJECT), [STATE]: () => json({ detail: 'x' }, 500) })
    renderProject()

    expect(await screen.findByRole('alert')).toHaveTextContent('Could not read the analysis state.')
    expect(screen.getByText('Unknown')).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: /Analyze/ })).not.toBeInTheDocument()
  })
})
