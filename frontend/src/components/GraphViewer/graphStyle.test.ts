import { describe, expect, it } from 'vitest'
import { GRAPH } from '../../test/mockApi'
import { entityColor, relationshipColor, toElements } from './graphStyle'

describe('toElements', () => {
  it('turns the API graph into Cytoscape elements, with nothing added', () => {
    const elements = toElements(GRAPH)

    const nodes = elements.filter((element) => element.group === 'nodes')
    const edges = elements.filter((element) => element.group === 'edges')
    expect(nodes.map((node) => node.data.id)).toEqual(GRAPH.nodes.map((node) => node.id))
    expect(nodes.map((node) => node.data.label)).toEqual(['service.py', 'AuthService', 'login', 'find_user'])
    expect(edges.map((edge) => [edge.data.source, edge.data.type, edge.data.target])).toEqual(
      GRAPH.edges.map((edge) => [edge.source, edge.relationship_type, edge.target]),
    )
  })

  it('drops an edge whose node is not in the graph', () => {
    const graph = { ...GRAPH, edges: [...GRAPH.edges, { id: 'x', source: GRAPH.nodes[0].id, target: 'missing', relationship_type: 'CALLS' }] }

    expect(toElements(graph).filter((element) => element.group === 'edges')).toHaveLength(GRAPH.edges.length)
  })

  it('uses distinct colors per type, and a neutral one for an unknown type', () => {
    expect(new Set(['file', 'class', 'interface', 'function', 'method'].map(entityColor)).size).toBe(5)
    expect(relationshipColor('CALLS')).not.toBe(relationshipColor('IMPORTS'))
    expect(entityColor('something-new')).toBe(relationshipColor('SOMETHING_NEW'))
  })
})
