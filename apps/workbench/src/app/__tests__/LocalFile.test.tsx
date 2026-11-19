import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import { readLocalFile, revealLocalFile, type LocalFilePreview } from '../../api/fs'
import { LocalFilePanel, type LocalFileTarget } from '../panel/LocalFile'

vi.mock('../../api/fs', () => ({ readLocalFile: vi.fn(), revealLocalFile: vi.fn() }))
beforeEach(() => vi.clearAllMocks())
afterEach(cleanup)
const target: LocalFileTarget = { projectId: null, sessionId: 'chat-a', path: 'C:/outside/report.md#L2', request: 1, returnPath: null }
const file: LocalFilePreview = { path: 'C:/outside/report.md', kind: 'text', content: '# 报告\n核对49.50\n', size_bytes: 32,
  sha256: 'a', encoding: 'utf-8', format: null, truncated: false, warnings: [] }

it('opens external artifacts with line position, copies actual text, quotes to the current free chat and follows relative links', async () => {
  vi.mocked(readLocalFile).mockResolvedValue(file)
  const onQuote = vi.fn(), onOpenFile = vi.fn(), copy = vi.fn().mockResolvedValue(undefined)
  const onBack = vi.fn()
  Object.defineProperty(navigator, 'clipboard', { configurable: true, value: { writeText: copy } })
  render(<LocalFilePanel target={{ ...target, returnPath: null }} refreshKey="1" onBack={onBack} onQuote={onQuote} onOpenFile={onOpenFile} notify={vi.fn()} />)
  await screen.findByText('核对49.50', { selector: 'mark' })
  expect(readLocalFile).toHaveBeenCalledWith(['C:/outside/report.md#L2', 'C:/outside/report.md'], expect.anything())
  fireEvent.click(screen.getByRole('button', { name: '复制文件内容' }))
  await waitFor(() => expect(copy).toHaveBeenCalledWith(file.content))
  fireEvent.click(screen.getByRole('button', { name: '引用文件到对话' }))
  expect(onQuote).toHaveBeenCalledWith({ projectId: null, path: file.path })
  vi.mocked(readLocalFile).mockResolvedValue({ ...file, content: '[明细](data/report.csv)' })
  fireEvent.click(screen.getByRole('button', { name: '重新读取文件' }))
  await waitFor(() => expect(readLocalFile).toHaveBeenCalledTimes(2))
  fireEvent.click(screen.getByRole('button', { name: /^预览$/ }))
  fireEvent.click(await screen.findByRole('button', { name: '明细' }))
  expect(onOpenFile).toHaveBeenCalledWith('C:/outside/data/report.csv')
  fireEvent.click(screen.getByRole('button', { name: '返回文件' }))
  expect(onBack).toHaveBeenCalledOnce()
})

it('late reads cannot replace a different conversation and undo refresh shows the missing path', async () => {
  let finish!: (value: LocalFilePreview) => void
  vi.mocked(readLocalFile).mockImplementationOnce(() => new Promise(resolve => { finish = resolve }))
  vi.mocked(readLocalFile).mockResolvedValue({ ...file, path: 'C:/outside/second.csv', content: '第二场内容' })
  const props = { refreshKey: '1', onOpenFile: vi.fn(), onBack: vi.fn(), notify: vi.fn() }
  const view = render(<LocalFilePanel {...props} target={target} />)
  const signal = vi.mocked(readLocalFile).mock.calls[0][1]!.signal!
  view.rerender(<LocalFilePanel {...props} target={{ ...target, sessionId: 'chat-b', path: 'C:/outside/second.csv' }} />)
  await screen.findByText('第二场内容')
  expect(signal.aborted).toBe(true)
  await act(async () => finish(file))
  expect(screen.queryByText('核对49.50')).toBeNull()
  vi.mocked(readLocalFile).mockRejectedValue(new Error('文件不存在：C:/outside/second.csv'))
  view.rerender(<LocalFilePanel {...props} refreshKey="undone" target={{ ...target, sessionId: 'chat-b', path: 'C:/outside/second.csv' }} />)
  expect(await screen.findByRole('alert')).toHaveTextContent('文件不存在')
  expect(screen.queryByText('第二场内容')).toBeNull()
  fireEvent.click(screen.getByRole('button', { name: '打开所在文件夹' }))
  expect(revealLocalFile).toHaveBeenCalledWith('C:/outside/second.csv')
})

it('Office preview shows extraction scope and a literal #L filename is not treated as a position', async () => {
  vi.mocked(readLocalFile).mockResolvedValue({ ...file, kind: 'office', path: target.path, format: 'docx', content: '文字内容', warnings: ['图片和完整排版不包含在内。'] })
  render(<LocalFilePanel target={{ ...target, returnPath: null }} refreshKey="1" onBack={vi.fn()} onOpenFile={vi.fn()} notify={vi.fn()} />)
  await screen.findByText('文字内容')
  expect(screen.getByText('文字提取 · 只读')).toBeInTheDocument()
  expect(screen.getByRole('note')).toHaveTextContent('图片和完整排版')
  expect(document.querySelector('mark')).toBeNull()
})
