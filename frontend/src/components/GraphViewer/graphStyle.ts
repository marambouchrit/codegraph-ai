// How the knowledge graph looks: one color/shape per entity type, one color per
// relationship type, and the conversion of the API graph into Cytoscape elements.
// Pure functions and constants: easy to test, no Cytoscape needed.

import type { ElementDefinition, StylesheetJson } from 'cytoscape'
import type { ProjectGraph } from '../../types/api'

// The backend's entity types (EntityType) and relationship types (RelationshipType +
// CONTAINS). A type not listed here is still drawn, in a neutral color.
export const ENTITY_STYLES: Record<string, { color: string; shape: string; label: string }> = {
  file: { color: '#64748b', shape: 'round-rectangle', label: 'File' },
  class: { color: '#6366f1', shape: 'ellipse', label: 'Class' },
  interface: { color: '#0891b2', shape: 'diamond', label: 'Interface' },
  function: { color: '#16a34a', shape: 'ellipse', label: 'Function' },
  method: { color: '#d97706', shape: 'ellipse', label: 'Method' },
}

export const RELATIONSHIP_COLORS: Record<string, string> = {
  CONTAINS: '#94a3b8',
  CALLS: '#e11d48',
  IMPORTS: '#2563eb',
  DEPENDS_ON: '#7c3aed',
  INHERITS: '#059669',
  IMPLEMENTS: '#0891b2',
  USES: '#d97706',
}

const NEUTRAL = '#9ca3af'
export const IMPACT_COLOR = '#e11d48'

export function entityColor(entityType: string): string {
  return ENTITY_STYLES[entityType]?.color ?? NEUTRAL
}

export function relationshipColor(type: string): string {
  return RELATIONSHIP_COLORS[type] ?? NEUTRAL
}

/** The API graph as Cytoscape elements: real IDs, names and types only. */
export function toElements(graph: ProjectGraph): ElementDefinition[] {
  const ids = new Set(graph.nodes.map((node) => node.id))
  const nodes: ElementDefinition[] = graph.nodes.map((node) => ({
    group: 'nodes',
    data: {
      id: node.id,
      label: node.name,
      entityType: node.entity_type,
      color: entityColor(node.entity_type),
      shape: ENTITY_STYLES[node.entity_type]?.shape ?? 'ellipse',
    },
  }))
  const edges: ElementDefinition[] = graph.edges
    // The API only returns edges between returned nodes; skip anything else defensively.
    .filter((edge) => ids.has(edge.source) && ids.has(edge.target))
    .map((edge) => ({
      group: 'edges',
      data: {
        id: edge.id,
        source: edge.source,
        target: edge.target,
        type: edge.relationship_type,
        color: relationshipColor(edge.relationship_type),
      },
    }))
  return [...nodes, ...edges]
}

/** Cytoscape styles; `text` and `background` come from the page's CSS tokens (light/dark). */
export function graphStylesheet(text: string, background: string): StylesheetJson {
  return [
    {
      selector: 'node',
      style: {
        'background-color': 'data(color)',
        shape: 'data(shape)' as never, // a valid shape name from ENTITY_STYLES
        label: 'data(label)',
        color: text,
        'font-size': 10,
        'text-valign': 'bottom',
        'text-margin-y': 4,
        'text-outline-color': background,
        'text-outline-width': 2,
        width: 18,
        height: 18,
      },
    },
    { selector: 'node[entityType = "file"]', style: { width: 26, height: 20, 'font-weight': 'bold' } },
    {
      selector: 'edge',
      style: {
        width: 1.4,
        'line-color': 'data(color)',
        'target-arrow-color': 'data(color)',
        'target-arrow-shape': 'triangle',
        'arrow-scale': 0.8,
        'curve-style': 'bezier',
        opacity: 0.75,
      },
    },
    { selector: 'edge[type = "CONTAINS"]', style: { 'line-style': 'dashed', opacity: 0.5 } },
    { selector: '.faded', style: { opacity: 0.12 } },
    // Affected by a change of the selected node (impact analysis).
    { selector: 'node.impacted', style: { 'border-width': 3, 'border-color': IMPACT_COLOR, width: 22, height: 22 } },
    {
      selector: 'node:selected',
      style: { 'border-width': 3, 'border-color': text, width: 26, height: 26, 'font-size': 12 },
    },
    { selector: 'edge.highlighted', style: { width: 2.6, opacity: 1, label: 'data(type)', 'font-size': 8, color: text,
      'text-outline-color': background, 'text-outline-width': 2, 'text-rotation': 'autorotate' } }, // prettier-ignore
  ]
}
