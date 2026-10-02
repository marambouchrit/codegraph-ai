// A remark plugin turning the answer's citations, "[1]" or "[1, 3]", into links to its sources.
//
// Same pattern as the backend (app/llm/generator.py CITATION). Only numbers that match a
// source returned by the API become links (href "#cite-<id>", rendered as buttons by
// MarkdownAnswer); any other number stays plain text, so no citation is ever invented.
// Text inside code (inline or blocks) is never touched: those are not "text" nodes.

const CITATION = /\[(\d+(?:\s*,\s*\d+)*)\]/g

// The few mdast fields this plugin reads and writes.
interface MdNode {
  type: string
  value?: string
  url?: string
  children?: MdNode[]
}

export default function remarkCitations(sourceIds: readonly number[]) {
  const known = new Set(sourceIds)
  return (tree: MdNode) => {
    visit(tree, known)
  }
}

function visit(node: MdNode, known: Set<number>): void {
  if (!node.children || node.type === 'link' || node.type === 'linkReference') {
    return
  }
  node.children = node.children.flatMap((child) =>
    child.type === 'text' && child.value ? splitCitations(child.value, known) : [child],
  )
  for (const child of node.children) {
    visit(child, known)
  }
}

function splitCitations(text: string, known: Set<number>): MdNode[] {
  const nodes: MdNode[] = []
  let last = 0
  for (const match of text.matchAll(CITATION)) {
    const ids = match[1].split(',').map((id) => Number(id.trim()))
    if (!ids.every((id) => known.has(id))) {
      continue // not (only) real sources: leave the text as written
    }
    const start = match.index ?? 0
    if (start > last) {
      nodes.push({ type: 'text', value: text.slice(last, start) })
    }
    for (const id of ids) {
      nodes.push({ type: 'link', url: `#cite-${id}`, children: [{ type: 'text', value: String(id) }] })
    }
    last = start + match[0].length
  }
  if (last === 0) {
    return [{ type: 'text', value: text }]
  }
  if (last < text.length) {
    nodes.push({ type: 'text', value: text.slice(last) })
  }
  return nodes
}
