import { useEffect, useRef, useState } from 'react'
import type { Core } from 'cytoscape'
import type { ProjectGraph } from '../../types/api'
import { graphStylesheet, toElements } from './graphStyle'

interface Props {
  graph: ProjectGraph
  hiddenRelationships: ReadonlySet<string>
  selectedId: string | null
  // IDs of the entities affected by a change of the selected node (impact analysis),
  // or null when no impact analysis is shown.
  impacted: ReadonlySet<string> | null
  onSelect: (nodeId: string | null) => void
}

/**
 * Draws the graph with Cytoscape.js: zoom (wheel, pinch, buttons), pan (drag the
 * background), select a node (click). Cytoscape is loaded on first use, so the other
 * pages don't download it. This component owns the Cytoscape instance; React owns
 * the data (graph, selection, filters) and passes it down.
 */
export default function GraphCanvas({ graph, hiddenRelationships, selectedId, impacted, onSelect }: Props) {
  const containerRef = useRef<HTMLDivElement>(null)
  const cyRef = useRef<Core | null>(null)
  const onSelectRef = useRef(onSelect)
  const tappedRef = useRef(false) // the selection came from a click on the canvas
  const [status, setStatus] = useState<'loading' | 'ready' | 'error'>('loading')

  useEffect(() => {
    onSelectRef.current = onSelect
  }, [onSelect])

  // Create the graph once per API graph; destroy it when leaving.
  useEffect(() => {
    let cancelled = false
    let resizeObserver: ResizeObserver | undefined
    import('cytoscape')
      .then(({ default: cytoscape }) => {
        if (cancelled || !containerRef.current) return
        const css = getComputedStyle(document.documentElement)
        const cy = cytoscape({
          container: containerRef.current,
          elements: toElements(graph),
          style: graphStylesheet(
            css.getPropertyValue('--text').trim() || '#0f172a',
            css.getPropertyValue('--surface').trim() || '#ffffff',
          ),
          // Force-directed layout, no animation, not random: the same graph looks the same.
          layout: { name: 'cose', animate: false, randomize: false, nodeDimensionsIncludeLabels: true },
          minZoom: 0.05,
          maxZoom: 4,
        })
        cy.on('tap', 'node', (event) => {
          tappedRef.current = true
          onSelectRef.current(event.target.id())
        })
        cy.on('tap', (event) => {
          if (event.target === cy) onSelectRef.current(null) // click on the background
        })
        cyRef.current = cy
        // The container changes size (details panel opened, window resized): redraw to fit it.
        resizeObserver = new ResizeObserver(() => cy.resize())
        resizeObserver.observe(containerRef.current)
        setStatus('ready')
      })
      .catch(() => !cancelled && setStatus('error'))
    return () => {
      cancelled = true
      resizeObserver?.disconnect()
      cyRef.current?.destroy()
      cyRef.current = null
    }
  }, [graph])

  // Show or hide relationship types.
  useEffect(() => {
    cyRef.current?.edges().forEach((edge) => {
      edge.style('display', hiddenRelationships.has(edge.data('type')) ? 'none' : 'element')
    })
  }, [hiddenRelationships, status])

  // Highlight the selected node with its relationships, or with the entities its change
  // would affect (impact analysis); center it if it was chosen outside the canvas.
  useEffect(() => {
    const cy = cyRef.current
    if (!cy) return
    cy.elements().removeClass('faded highlighted impacted').unselect()
    if (!selectedId) return
    const node = cy.getElementById(selectedId)
    if (node.empty()) return
    node.select()
    if (impacted) {
      const affected = cy.nodes().filter((other) => impacted.has(other.id()))
      affected.addClass('impacted')
      const shown = affected.union(node)
      cy.elements().not(shown.union(shown.edgesWith(shown))).addClass('faded')
    } else {
      cy.elements().not(node.closedNeighborhood()).addClass('faded')
      node.connectedEdges().addClass('highlighted')
    }
    if (!tappedRef.current) {
      cy.animate({ center: { eles: node }, zoom: Math.max(cy.zoom(), 1) }, { duration: 300 })
    }
    tappedRef.current = false
  }, [selectedId, impacted, status])

  function zoomBy(factor: number) {
    const cy = cyRef.current
    if (!cy) return
    const center = { x: cy.width() / 2, y: cy.height() / 2 }
    cy.zoom({ level: cy.zoom() * factor, renderedPosition: center })
  }

  return (
    <div className="graph-canvas-wrap">
      <div ref={containerRef} className="graph-canvas" role="img" aria-label="Knowledge graph" />
      {status === 'loading' && <p className="graph-canvas-status muted">Drawing the graph…</p>}
      {status === 'error' && (
        <p className="graph-canvas-status alert error" role="alert">
          The graph viewer could not be loaded. Reload the page to try again.
        </p>
      )}
      <div className="graph-controls" aria-label="Graph controls">
        <button type="button" className="button secondary" onClick={() => zoomBy(1.25)} aria-label="Zoom in">
          +
        </button>
        <button type="button" className="button secondary" onClick={() => zoomBy(0.8)} aria-label="Zoom out">
          −
        </button>
        <button type="button" className="button secondary" onClick={() => cyRef.current?.fit(undefined, 30)}>
          Fit
        </button>
      </div>
    </div>
  )
}
