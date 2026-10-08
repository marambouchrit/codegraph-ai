import Markdown from 'react-markdown'
import remarkGfm from 'remark-gfm'
import remarkCitations from './remarkCitations'

interface Props {
  markdown: string
  sourceIds: readonly number[]
  onCite: (sourceId: number) => void
  citeLabel?: string // what a citation points to: a "source" (chat) or a "fact" (architecture)
}

/**
 * Render the LLM's Markdown answer safely.
 *
 * LLM output is untrusted: react-markdown builds React elements (no innerHTML), raw HTML
 * in the answer is dropped (`skipHtml`), images are not rendered (no remote loads from
 * model output), and unsafe URLs (javascript:...) are removed by its default urlTransform.
 * Citations become buttons that point at the matching source below the answer.
 */
export default function MarkdownAnswer({ markdown, sourceIds, onCite, citeLabel = 'source' }: Props) {
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
                  className="mx-px inline-flex min-w-[1.5em] cursor-pointer items-center justify-center rounded-md border border-transparent bg-accent-soft px-1.5 align-[0.1em] text-[0.78em] leading-normal font-bold text-accent hover:border-accent focus-visible:border-accent"
                  onClick={() => onCite(id)}
                  aria-label={`Show ${citeLabel} ${id}`}
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
