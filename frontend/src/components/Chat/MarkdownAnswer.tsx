import Markdown from 'react-markdown'
import remarkGfm from 'remark-gfm'
import remarkCitations from './remarkCitations'

interface Props {
  markdown: string
  sourceIds: readonly number[]
  onCite: (sourceId: number) => void
}

/**
 * Render the LLM's Markdown answer safely.
 *
 * LLM output is untrusted: react-markdown builds React elements (no innerHTML), raw HTML
 * in the answer is dropped (`skipHtml`), images are not rendered (no remote loads from
 * model output), and unsafe URLs (javascript:...) are removed by its default urlTransform.
 * Citations become buttons that point at the matching source below the answer.
 */
export default function MarkdownAnswer({ markdown, sourceIds, onCite }: Props) {
  return (
    <div className="markdown">
      <Markdown
        remarkPlugins={[remarkGfm, [remarkCitations, sourceIds]]}
        skipHtml
        disallowedElements={['img']}
        components={{
          a({ href, children }) {
            if (href?.startsWith('#cite-')) {
              const id = Number(href.slice('#cite-'.length))
              return (
                <button
                  type="button"
                  className="cite"
                  onClick={() => onCite(id)}
                  aria-label={`Show source ${id}`}
                >
                  {children}
                </button>
              )
            }
            return (
              <a href={href} target="_blank" rel="noopener noreferrer nofollow">
                {children}
              </a>
            )
          },
        }}
      >
        {markdown}
      </Markdown>
    </div>
  )
}
