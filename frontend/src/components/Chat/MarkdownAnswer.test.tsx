import { render, screen } from '@testing-library/react'
import { describe, expect, it, vi } from 'vitest'
import MarkdownAnswer from './MarkdownAnswer'

function renderAnswer(markdown: string, sourceIds: number[] = [1, 2, 3]) {
  const onCite = vi.fn()
  const { container } = render(<MarkdownAnswer markdown={markdown} sourceIds={sourceIds} onCite={onCite} />)
  return { container, onCite }
}

describe('MarkdownAnswer', () => {
  it('renders lists, code blocks and inline code', () => {
    const { container } = renderAnswer('- one\n- two\n\n```python\nprint("hi")\n```\n\nUse `login()`.')

    expect(container.querySelectorAll('li')).toHaveLength(2)
    expect(container.querySelector('pre code')).toHaveTextContent('print("hi")')
    expect(screen.getByText('login()').tagName).toBe('CODE')
  })

  it('never renders raw HTML, images or unsafe links from the model', () => {
    const { container } = renderAnswer(
      'Hi <script>alert(1)</script><b onclick="x()">bold</b>\n\n![x](https://evil.example/p.png)\n\n[click](javascript:alert(1))',
    )

    expect(container.querySelector('script, b, img')).toBeNull()
    const link = screen.getByText('click').closest('a')
    expect(link?.getAttribute('href') ?? '').not.toContain('javascript')
  })

  it('links only citations that match a returned source', () => {
    const { onCite } = renderAnswer('Login [1] and [2, 3]; not [9]; and `code [1]`.')

    const buttons = screen.getAllByRole('button')
    expect(buttons.map((button) => button.textContent)).toEqual(['1', '2', '3'])
    expect(screen.getByText(/not \[9\]/)).toBeInTheDocument()
    expect(screen.getByText('code [1]').tagName).toBe('CODE')
    buttons[1].click()
    expect(onCite).toHaveBeenCalledWith(2)
  })
})
