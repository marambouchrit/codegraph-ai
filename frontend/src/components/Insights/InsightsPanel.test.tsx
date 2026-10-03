import { render, screen, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { describe, expect, it } from 'vitest'
import type { ArchitectureOverview, DependencyAnalysis } from '../../types/api'
import { ARCHITECTURE, DEPENDENCIES, json, mockApi, PROJECT } from '../../test/mockApi'
import InsightsPanel from './InsightsPanel'

const BASE = `/projects/${PROJECT.id}/analysis`
const DEPS = `GET ${BASE}/dependencies`
const ARCH = `GET ${BASE}/architecture`

function renderPanel(analyzed: boolean | null = true) {
  render(<InsightsPanel projectId={PROJECT.id} analyzed={analyzed} analyzedAt="2026-10-03T09:00:00Z" />)
}

describe('Dependency analysis', () => {
  it('shows the counts, the circular dependencies and the hubs returned by the API', async () => {
    mockApi({ [DEPS]: () => json(DEPENDENCIES) })
    renderPanel()

    expect(screen.getByText('Analyzing dependencies…')).toBeInTheDocument()
    const section = (await screen.findByRole('heading', { name: 'Dependency analysis' })).closest('section') as HTMLElement
    expect(await within(section).findByText(/file-to-file dependencies/)).toHaveTextContent(
      '4 files · 3 file-to-file dependencies · 1 circular dependency',
    )
    // The cycle as a readable path that closes on itself.
    expect(section.querySelector('.cycles li')).toHaveTextContent('a.py → b.py → a.py')
    // Hubs with their real counts.
    const hubs = section.querySelectorAll('.hubs')
    expect(hubs[0]).toHaveTextContent('3auth/service.py')
    expect(hubs[1]).toHaveTextContent('2UserRepository.find_user repository/user.py')
  })

  it('never calls unreferenced entities dead code', async () => {
    mockApi({ [DEPS]: () => json(DEPENDENCIES) })
    renderPanel()

    const summary = await screen.findByText(/No detected references · 1 entity/)
    const details = summary.closest('details') as HTMLElement
    expect(details).toHaveTextContent(DEPENDENCIES.unreferenced_note)
    expect(details).toHaveTextContent('AuthService.login method · auth/service.py:14')
    const page = document.body.textContent ?? ''
    expect(page.replace(DEPENDENCIES.unreferenced_note, '')).not.toMatch(/dead code|unused code/i)
  })

  it('lists files nothing depends on, and every dependency with its types', async () => {
    mockApi({ [DEPS]: () => json(DEPENDENCIES) })
    renderPanel()

    const none = (await screen.findByText(/Files no other file depends on · 1/)).closest('details') as HTMLElement
    expect(none).toHaveTextContent('the graph cannot tell which')
    const all = screen.getByText(/All file dependencies · 3/).closest('details') as HTMLElement
    expect(all).toHaveTextContent('a.py → b.py DEPENDS_ON, IMPORTS')
    expect(all).toHaveTextContent('auth/service.py → repository/user.py DEPENDS_ON')
  })

  it('says when there is no circular dependency, and when the analysis is partial', async () => {
    const partial: DependencyAnalysis = { ...DEPENDENCIES, cycles: [], partial: true, dependencies_truncated: true }
    mockApi({ [DEPS]: () => json(partial) })
    renderPanel()

    expect(await screen.findByText('No circular dependency between files was found.')).toBeInTheDocument()
    expect(screen.getByText(/these results cover part of it/)).toBeInTheDocument()
    expect(screen.getByText(/first 3 shown/)).toBeInTheDocument()
  })

  it('explains an unanalyzed project, and an error', async () => {
    let fail = false
    mockApi({
      [DEPS]: () =>
        fail
          ? json({ detail: 'Neo4j is not reachable.' }, 503)
          : json({ ...DEPENDENCIES, files: 0, dependency_count: 0, dependencies: [], cycles: [] }),
    })
    renderPanel(false)
    expect(await screen.findByText('Analyze the project to see its dependencies.')).toBeInTheDocument()

    fail = true
    document.body.innerHTML = ''
    renderPanel()
    expect(await screen.findByRole('alert')).toHaveTextContent(
      'The dependency analysis could not be loaded. Neo4j is not reachable.',
    )
  })

  it('waits for the analysis state before loading', () => {
    const calls = mockApi({})
    renderPanel(null)

    expect(screen.getByText('Checking the analysis state…')).toBeInTheDocument()
    expect(calls).toHaveLength(0)
  })
})

describe('Architecture summary', () => {
  it('is generated only when asked (it calls the LLM)', async () => {
    const calls = mockApi({ [DEPS]: () => json(DEPENDENCIES), [ARCH]: () => json(ARCHITECTURE) })
    renderPanel()
    await screen.findByText(/file-to-file dependencies/)
    expect(calls.some((call) => call.path.endsWith('/architecture'))).toBe(false)

    await userEvent.click(screen.getByRole('button', { name: 'Generate architecture summary' }))

    expect(await screen.findByText('Facts from the graph')).toBeInTheDocument()
    expect(calls.filter((call) => call.path.endsWith('/architecture'))).toHaveLength(1)
  })

  it('shows the summary as safe Markdown, the model and the numbered facts', async () => {
    mockApi({ [DEPS]: () => json(DEPENDENCIES), [ARCH]: () => json(ARCHITECTURE) })
    renderPanel()
    await userEvent.click(await screen.findByRole('button', { name: 'Generate architecture summary' }))

    expect(await screen.findByText('gemini-flash-lite-latest')).toBeInTheDocument()
    expect(screen.getByText('hub').tagName).toBe('STRONG') // Markdown rendered
    const facts = document.querySelectorAll('.fact')
    expect([...facts].map((fact) => fact.textContent)).toEqual(ARCHITECTURE.facts.map((fact) => fact.text))
    expect(facts[1]).toHaveAttribute('value', '2')
  })

  it('links each citation of the summary to its fact', async () => {
    mockApi({ [DEPS]: () => json(DEPENDENCIES), [ARCH]: () => json(ARCHITECTURE) })
    renderPanel()
    await userEvent.click(await screen.findByRole('button', { name: 'Generate architecture summary' }))

    await userEvent.click(await screen.findByRole('button', { name: 'Show fact 2' }))

    expect(document.getElementById('fact-2')).toHaveClass('active')
    expect(document.getElementById('fact-1')).not.toHaveClass('active')
  })

  it('shows the facts with a warning when the LLM was unavailable', async () => {
    const withoutSummary: ArchitectureOverview = {
      ...ARCHITECTURE, summary: null, cited: [], model: null,
      warnings: ['No summary could be generated: The LLM provider is unavailable.'],
    } // prettier-ignore
    mockApi({ [DEPS]: () => json(DEPENDENCIES), [ARCH]: () => json(withoutSummary) })
    renderPanel()
    await userEvent.click(await screen.findByRole('button', { name: 'Generate architecture summary' }))

    expect(await screen.findByText(/No summary could be generated/)).toBeInTheDocument()
    expect(document.querySelectorAll('.fact')).toHaveLength(2)
    expect(document.querySelector('.architecture-summary')).toBeNull()
  })

  it('shows a warning and no fact for an unanalyzed project, and an error if the request fails', async () => {
    let fail = false
    mockApi({
      [DEPS]: () => json(DEPENDENCIES),
      [ARCH]: () =>
        fail
          ? json({ detail: 'x' }, 500)
          : json({ ...ARCHITECTURE, facts: [], summary: null, cited: [], model: null,
              warnings: ['This project has no knowledge graph yet: analyze it first.'] }), // prettier-ignore
    })
    renderPanel()
    await userEvent.click(await screen.findByRole('button', { name: 'Generate architecture summary' }))
    expect(await screen.findByText(/has no knowledge graph yet/)).toBeInTheDocument()
    expect(screen.queryByText('Facts from the graph')).not.toBeInTheDocument()

    fail = true
    await userEvent.click(screen.getByRole('button', { name: 'Generate again' }))
    expect(await screen.findByRole('alert')).toHaveTextContent('The architecture summary could not be generated.')
  })
})
