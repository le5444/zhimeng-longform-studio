import { cleanup, fireEvent, render, screen } from '@testing-library/react'
import { afterEach, expect, it, vi } from 'vitest'
import { Markdown } from '../chat/Markdown'

afterEach(cleanup)

it('keeps unit prices as text while explicit numeric filenames remain clickable', () => {
  const open = vi.fn()
  const source = 'Notebook 2 件 @19.50、Cable 3 件 @12.00、Adapter 1 件 @65.20）。文件 @"19.50" 和 @2026.csv。'
  const { container } = render(<Markdown text={source} onOpenFile={open} />)
  expect(container).toHaveTextContent('Notebook 2 件 @19.50、Cable 3 件 @12.00、Adapter 1 件 @65.20）。')
  expect(screen.getAllByRole('button')).toHaveLength(2)
  fireEvent.click(screen.getByRole('button', { name: '@"19.50"' }))
  expect(open).toHaveBeenCalledWith('19.50')
})

it('keeps loose ordered lists and their paragraphs in one list', () => {
  const { container } = render(<Markdown text={'1. First step\n\n   Supporting detail.\n\n2. Second step'} />)
  expect(container.querySelectorAll('ol')).toHaveLength(1)
  expect(container.querySelectorAll('ol > li')).toHaveLength(2)
  expect(container.querySelector('ol > li p:last-child')).toHaveTextContent('Supporting detail.')
})

it('resolves reference links defined after a blank line', () => {
  const open = vi.fn()
  render(<Markdown text={'Read [the report][report].\n\n[report]: reports/result.csv'} onOpenFile={open} />)
  fireEvent.click(screen.getByRole('button', { name: 'the report' }))
  expect(open).toHaveBeenCalledWith('reports/result.csv')
})

it('keeps blank lines and shorter backtick fences inside a longer code fence', () => {
  const source = '````md\n# Example\n\n```js\nrun()\n```\n\nThe rest of the example.\n````'
  const { container } = render(<Markdown text={source} streaming />)
  expect(container.querySelectorAll('pre')).toHaveLength(1)
  expect(container.querySelector('pre')?.textContent).toBe('# Example\n\n```js\nrun()\n```\n\nThe rest of the example.\n')
  expect(container.querySelector('h1')).toBeNull()
})

it('keeps blank lines inside tilde code fences', () => {
  const { container } = render(<Markdown text={'~~~txt\nalpha\n\nbeta\n~~~'} />)
  expect(container.querySelector('pre')?.textContent).toBe('alpha\n\nbeta\n')
  expect(container.querySelectorAll('pre')).toHaveLength(1)
})

it('keeps file mentions literal inside a longer fence containing shorter fences', () => {
  const content = '```md\n@example.py\n```\n\n@second.py\n'
  const { container } = render(<Markdown text={'````txt\n' + content + '````'} onOpenFile={vi.fn()} />)
  expect(container.querySelector('pre')?.textContent).toBe(content)
  expect(container.querySelector('.zm-file-link')).toBeNull()
})

it('preserves code block DOM and selection target as streamed text grows', () => {
  const view = render(<Markdown text={'```txt\nalpha'} streaming />)
  const code = view.container.querySelector('code')
  view.rerender(<Markdown text={'```txt\nalpha beta'} streaming />)
  expect(view.container.querySelector('code')).toBe(code)
  expect(code?.textContent).toBe('alpha beta\n')
})

it('preserves a focused file link as streamed text grows', () => {
  const open = vi.fn()
  const view = render(<Markdown text={'Read @report.csv.\n\nMore'} streaming onOpenFile={open} />)
  const link = screen.getByRole('button', { name: '@report.csv' })
  link.focus()
  view.rerender(<Markdown text={'Read @report.csv.\n\nMore details.'} streaming onOpenFile={open} />)
  expect(screen.getByRole('button', { name: '@report.csv' })).toBe(link)
  expect(link).toHaveFocus()
  fireEvent.click(link)
  expect(open).toHaveBeenCalledWith('report.csv')
})
