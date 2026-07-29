import { cleanup, render, screen } from '@testing-library/react'
import { afterEach, describe, expect, it } from 'vitest'
import { SafeMarkdown } from './SafeMarkdown'

describe('SafeMarkdown', () => {
  afterEach(cleanup)

  it('renders structured Markdown without enabling raw HTML or dangerous links', () => {
    const { container } = render(
      <SafeMarkdown content={'## 修改建议\n\n- 保留线索\n\n```ts\nconst safe = true\n```\n\n<script>alert(1)</script>\n\n[危险链接](javascript:alert(1))'} />,
    )

    expect(screen.getByRole('heading', { name: '修改建议' })).toBeInTheDocument()
    expect(screen.getByText('保留线索')).toBeInTheDocument()
    expect(screen.getByText('const safe = true')).toBeInTheDocument()
    expect(container.querySelector('script')).toBeNull()
    const link = screen.getByText('危险链接')
    expect(link.getAttribute('href') ?? '').not.toMatch(/^javascript:/i)
  })
})
