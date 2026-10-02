import { render, screen, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { describe, expect, it, vi } from 'vitest'
import type { ProjectGraph } from '../../types/api'
import { GRAPH, json, mockApi, PROJECT } from '../../test/mockApi'
import GraphPanel from './GraphPanel'

// jsdom has no canvas: replace the Cytoscape view by a list of the nodes it receives,
// with a button per node to simulate a click on it.
vi.mock('./GraphCanvas', () => ({
  default: ({ graph, hiddenRelationships, selectedId, onSelect }: {
    graph: ProjectGraph
    hiddenRelationships: ReadonlySet<string>
    selectedId: string | null
    onSelect: (id: string | null) => void
  }) => (
    <div data-testid="canvas" data-selected={selectedId ?? ''} data-hidden={[...hiddenRelationships].join(',')}>
      {graph.nodes.map((node) => (
        <button key={node.id} type="button" onClick={() => onSelect(node.id)}>
          node:{node.name}
        </button>
      ))}
      <span>edges:{graph.edges.length}</span>
    </div>
  ),
}))

const PATH = `/projects/${PROJECT.id}/graph`

function renderPanel(analyzed: boolean | null = true) {
  render(<GraphPanel projectId={PROJECT.id} analyzed={analyzed} analyzedAt="2026-10-02T18:30:00Z" />)
}

describe('GraphPanel', () => {
  it('draws exactly the nodes and edges returned by the API', async () => {
    const calls = mockApi({ [`GET ${PATH}`]: () => json(GRAPH) })
    renderPanel()

    expect(screen.getByText('Loading the knowledge graph…')).toBeInTheDocument()
    const canvas = await screen.findByTestId('canvas')
    expect(within(canvas).getAllByRole('button').map((b) => b.textContent)).toEqual([
      'node:service.py', 'node:AuthService', 'node:login', 'node:find_user',
    ])
    expect(within(canvas).getByText('edges:3')).toBeInTheDocument()
    expect(screen.getByText('Complete graph: 4 nodes, 3 relationships.')).toBeInTheDocument()
    expect(calls[0].path).toBe(PATH)
    expect(String(vi.mocked(fetch).mock.calls[0][0])).toContain('/graph?limit=150')
  })

  it('shows the legend and relationship types present in the graph only', async () => {
    mockApi({ [`GET ${PATH}`]: () => json(GRAPH) })
    renderPanel()
    await screen.findByTestId('canvas')

    const legend = screen.getByLabelText('Entity types')
    expect(legend).toHaveTextContent('File · 1')
    expect(legend).toHaveTextContent('Class · 1')
    expect(legend).toHaveTextContent('Method · 2')
    expect(legend).not.toHaveTextContent('Interface')
    expect(screen.getByLabelText('CONTAINS · 2')).toBeChecked()
    expect(screen.getByLabelText('CALLS · 1')).toBeChecked()
    expect(screen.queryByLabelText(/IMPORTS/)).not.toBeInTheDocument()
  })

  it('hides a relationship type when unchecked', async () => {
    mockApi({ [`GET ${PATH}`]: () => json(GRAPH) })
    renderPanel()
    await screen.findByTestId('canvas')

    await userEvent.click(screen.getByLabelText('CONTAINS · 2'))

    expect(screen.getByTestId('canvas')).toHaveAttribute('data-hidden', 'CONTAINS')
  })

  it('shows the real metadata and relationships of a selected node', async () => {
    mockApi({ [`GET ${PATH}`]: () => json(GRAPH) })
    renderPanel()
    await userEvent.click(await screen.findByRole('button', { name: 'node:login' }))

    const details = screen.getByRole('complementary')
    expect(within(details).getByRole('heading', { name: 'login' })).toBeInTheDocument()
    expect(details).toHaveTextContent('TypeMethod')
    expect(details).toHaveTextContent('Qualified nameAuthService.login')
    expect(details).toHaveTextContent('Fileauth/service.py')
    expect(details).toHaveTextContent('Lines14–20')
    expect(details).toHaveTextContent('Languagepython')
    expect(details).toHaveTextContent('Outgoing (1)CALLSUserRepository.find_user')
    expect(details).toHaveTextContent('Incoming (1)CONTAINSAuthService')

    // Follow a relationship to its node.
    await userEvent.click(within(details).getByRole('button', { name: 'UserRepository.find_user' }))
    expect(within(screen.getByRole('complementary')).getByRole('heading', { name: 'find_user' })).toBeInTheDocument()
    expect(screen.getByTestId('canvas').dataset.selected).toContain('find_user')

    await userEvent.click(screen.getByRole('button', { name: 'Close details' }))
    expect(screen.queryByRole('complementary')).not.toBeInTheDocument()
  })

  it('finds a node by name', async () => {
    mockApi({ [`GET ${PATH}`]: () => json(GRAPH) })
    renderPanel()
    await screen.findByTestId('canvas')

    await userEvent.type(screen.getByLabelText('Find a node'), 'AuthService.login{Enter}')
    expect(within(screen.getByRole('complementary')).getByRole('heading', { name: 'login' })).toBeInTheDocument()

    await userEvent.clear(screen.getByLabelText('Find a node'))
    await userEvent.type(screen.getByLabelText('Find a node'), 'Nothing{Enter}')
    expect(screen.getByRole('alert')).toHaveTextContent('No node named “Nothing” in the displayed graph.')
  })

  it('says clearly when the graph is truncated', async () => {
    const partial: ProjectGraph = { ...GRAPH, truncated: true, total_nodes: 1200, total_edges: 3400 }
    mockApi({ [`GET ${PATH}`]: () => json(partial) })
    renderPanel()

    // Any thousands separator: numbers use the user's locale (1,200 / 1 200 / 1.200).
    expect(
      await screen.findByText(/Partial graph: showing 4 of 1\D?200 nodes and 3 of 3\D?400 relationships\./),
    ).toBeInTheDocument()
  })

  it('asks the backend again with a new node limit', async () => {
    const calls = mockApi({ [`GET ${PATH}`]: () => json(GRAPH) })
    renderPanel()
    await screen.findByTestId('canvas')

    await userEvent.selectOptions(screen.getByLabelText('Nodes'), '500')

    await screen.findByTestId('canvas')
    expect(calls.map((call) => call.init)).toHaveLength(2)
    const fetchMock = vi.mocked(fetch)
    expect(String(fetchMock.mock.calls.at(-1)?.[0])).toContain('/graph?limit=500')
  })

  it('explains an empty graph of a project that is not analyzed', async () => {
    mockApi({ [`GET ${PATH}`]: () => json({ ...GRAPH, nodes: [], edges: [], total_nodes: 0, total_edges: 0 }) })
    renderPanel(false)

    expect(await screen.findByText('No knowledge graph yet.')).toBeInTheDocument()
    expect(screen.getByText('Analyze the project to build its knowledge graph.')).toBeInTheDocument()
    expect(screen.queryByTestId('canvas')).not.toBeInTheDocument()
  })

  it('shows the real graph of an unfinished analysis, and says chat is not ready', async () => {
    mockApi({ [`GET ${PATH}`]: () => json(GRAPH) })
    renderPanel(false)

    expect(await screen.findByTestId('canvas')).toBeInTheDocument()
    expect(screen.getByText(/analysis that did not complete/)).toBeInTheDocument()
  })

  it('has no such notice once the project is ready', async () => {
    mockApi({ [`GET ${PATH}`]: () => json(GRAPH) })
    renderPanel(true)

    await screen.findByTestId('canvas')
    expect(screen.queryByText(/analysis that did not complete/)).not.toBeInTheDocument()
  })

  it('explains an empty graph of an analyzed project', async () => {
    mockApi({ [`GET ${PATH}`]: () => json({ ...GRAPH, nodes: [], edges: [], total_nodes: 0, total_edges: 0 }) })
    renderPanel(true)

    expect(await screen.findByText('The knowledge graph is empty.')).toBeInTheDocument()
  })

  it('shows a useful message when Neo4j is unavailable', async () => {
    mockApi({ [`GET ${PATH}`]: () => json({ detail: 'Neo4j is not reachable at bolt://localhost:7687.' }, 503) })
    renderPanel()

    expect(await screen.findByRole('alert')).toHaveTextContent(
      'The knowledge graph could not be loaded. Neo4j is not reachable',
    )
  })

  it('waits for the analysis state before loading', () => {
    const calls = mockApi({})
    renderPanel(null)

    expect(screen.getByText('Checking the analysis state…')).toBeInTheDocument()
    expect(calls).toHaveLength(0)
  })
})
