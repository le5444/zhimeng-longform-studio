import { cleanup, fireEvent, render, screen } from '@testing-library/react'
import { afterEach, expect, it, vi } from 'vitest'
import { appendFileQuotation, selectionLocation } from '../lib/fileQuotation'
import { fileTextSelection } from '../lib/filePosition'
import { mentionedFileIds } from '../chat/composerLogic'
import { UserMessage } from '../chat/Message'
import { makeFile } from './fixtures'

afterEach(cleanup)

it('keeps a complete quoted code block and a file with spaces through Markdown and attachment parsing', () => {
  const file = makeFile('docs/review (final).md')
  const other = makeFile('private.txt')
  const excerpt = '  preserve indentation\n@private.txt\n\n```ts\nconst value = 42\n```\n' + 'detail '.repeat(700)
  const draft = appendFileQuotation('原来的问题\n', { projectId: 'p1', path: file.relative_path, text: excerpt, source: 'draft' })
  expect(draft.startsWith('原来的问题\n\n\n')).toBe(true)
  expect(draft).toContain('未保存草稿：')
  expect(mentionedFileIds(draft, [file, other])).toEqual([file.file_id])
  const onCopy = vi.fn(), onEdit = vi.fn()
  const view = render(<UserMessage message={{ content: draft } as never} isLatest actions={{ onCopy, onEdit }} onOpenFile={() => {}} />)
  expect(view.container.querySelector('pre code')?.textContent).toBe(excerpt + '\n')
  expect(screen.queryByRole('button', { name: '@private.txt' })).not.toBeInTheDocument()
  fireEvent.click(screen.getByRole('button', { name: '复制' }))
  fireEvent.click(screen.getByRole('button', { name: '编辑' }))
  expect(onCopy).toHaveBeenCalledWith(draft)
  expect(onEdit).toHaveBeenCalledWith(draft)
})

it('maps source and normalized editor selections back to the same characters', () => {
  for (const text of ['first\r\n\tsecond line\r\nthird', 'first\n\tsecond line\nthird']) {
    const start = text.indexOf('second')
    const end = text.indexOf('third') + 3
    const point = selectionLocation(text, start, end)
    const restored = fileTextSelection(text, point)!
    expect(text.slice(restored.start, restored.end)).toBe(text.slice(start, end))
  }
})
