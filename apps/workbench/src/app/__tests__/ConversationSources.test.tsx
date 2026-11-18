import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterAll, afterEach, beforeAll, beforeEach, expect, it, vi } from 'vitest'
import { ConversationSources, SourceLink } from '../chat/ConversationSources'
import { StepList } from '../chat/Steps'
import { readSessionSource, type SessionSourcePage } from '../../api/sessionSource'
import { RuntimeApiError } from '../../api/runtime'

vi.mock('../../api/sessionSource', () => ({ readSessionSource: vi.fn() }))
const read = vi.mocked(readSessionSource)
const target = { sessionId: 'original', messageId: 'm1' }
const copy = vi.fn().mockResolvedValue(undefined)
const page = (content = 'Original request', extra: Partial<SessionSourcePage['message']> = {}): SessionSourcePage => ({
  message: { session_id: 'original', message_id: 'm1', session_title: 'Purchase notes', project_id: null,
    project_name: null, ordinal: 1, role: 'user', created_at: '2026-10-17T10:00:00Z', run_id: 'r1',
    run_status: 'succeeded', origin_kind: 'user', content, start_offset: 0, end_offset: content.length,
    total_characters: content.length, next_start_offset: null, masked_secrets: 0, offset_basis: 'masked_text', ...extra },
  content_hash: 'a'.repeat(64), previous_message_id: null, next_message_id: 'm2',
})
const view = (scope = 'current') => <ConversationSources scope={scope}><textarea aria-label="Draft" defaultValue="Unsaved draft" /><SourceLink target={target} /></ConversationSources>
const open = () => { const link = screen.getByRole('button', { name: '查看原消息' }); link.focus(); fireEvent.click(link); return link }

beforeAll(() => {
  Object.defineProperty(HTMLDialogElement.prototype, 'showModal', { configurable: true, value() { this.open = true; this.querySelector('button')?.focus() } })
  Object.defineProperty(HTMLDialogElement.prototype, 'close', { configurable: true, value() { this.open = false } })
})
afterAll(() => { Reflect.deleteProperty(HTMLDialogElement.prototype, 'showModal'); Reflect.deleteProperty(HTMLDialogElement.prototype, 'close') })
beforeEach(() => { read.mockReset(); copy.mockClear(); Object.defineProperty(navigator, 'clipboard', { configurable: true, value: { writeText: copy } }) })
afterEach(cleanup)

it('reads exact source, navigates adjacent messages and closes without losing focus or draft', async () => {
  read.mockResolvedValueOnce(page()).mockResolvedValueOnce({ ...page('Assistant suggestion', { message_id: 'm2', ordinal: 2, role: 'assistant' }), previous_message_id: 'm1', next_message_id: null })
  render(view())
  const link = open()
  expect(await screen.findByText('Original request')).toBeInTheDocument()
  expect(read).toHaveBeenLastCalledWith(target, 0, undefined, expect.objectContaining({ signal: expect.any(AbortSignal) }))
  fireEvent.click(screen.getByRole('button', { name: '下一条消息' }))
  expect(await screen.findByText('Assistant suggestion')).toBeInTheDocument()
  expect(read.mock.lastCall?.[0]).toEqual({ sessionId: 'original', messageId: 'm2' })
  expect(screen.getByRole('button', { name: '下一条消息' })).toBeDisabled()
  fireEvent.click(screen.getByRole('button', { name: '关闭原文' }))
  expect(screen.queryByRole('dialog')).toBeNull()
  expect(link).toHaveFocus()
  expect(screen.getByRole('textbox', { name: 'Draft' })).toHaveValue('Unsaved draft')
})

it('copies only loaded text, continues by server character offset and then copies full text', async () => {
  read.mockResolvedValueOnce(page('A\u{1f9ea}', { end_offset: 2, total_characters: 4, next_start_offset: 2 }))
    .mockResolvedValueOnce(page('BC', { start_offset: 2, end_offset: 4, total_characters: 4 }))
  render(view()); open()
  fireEvent.click(await screen.findByRole('button', { name: '复制已加载原文' }))
  await waitFor(() => expect(copy).toHaveBeenCalledWith('A\u{1f9ea}'))
  fireEvent.click(screen.getByRole('button', { name: '继续加载原文' }))
  expect(await screen.findByText('A\u{1f9ea}BC')).toBeInTheDocument()
  expect(read.mock.lastCall?.slice(1, 3)).toEqual([2, 'a'.repeat(64)])
  expect(screen.getByText('已加载 4 / 4 字符')).toBeInTheDocument()
  fireEvent.click(screen.getByRole('button', { name: '复制原文' }))
  await waitFor(() => expect(copy).toHaveBeenLastCalledWith('A\u{1f9ea}BC'))
})

it('scope changes cancel loading, ignore late replies and never reopen the old source', async () => {
  let finish!: (value: SessionSourcePage) => void
  read.mockImplementationOnce(() => new Promise(resolve => { finish = resolve }))
  const rendered = render(view()); open()
  const signal = read.mock.lastCall?.[3]?.signal
  rendered.rerender(view('other'))
  expect(signal?.aborted).toBe(true)
  await act(async () => { finish(page('Late reply')) })
  rendered.rerender(view())
  expect(screen.queryByRole('dialog')).toBeNull()
  expect(screen.queryByText('Late reply')).toBeNull()
  expect(screen.getByRole('textbox', { name: 'Draft' })).toHaveValue('Unsaved draft')
})

it.each([404, 409])('clears stale text after continuation fails with %s, and can reload', async status => {
  read.mockResolvedValueOnce(page('Old content', { total_characters: 30, next_start_offset: 11 }))
    .mockRejectedValueOnce(new RuntimeApiError(status, { error: { message: '原消息已更新，请重新读取。', code: 'session_source_changed' } }))
    .mockResolvedValueOnce(page('Fresh content'))
  render(view()); open()
  fireEvent.click(await screen.findByRole('button', { name: '继续加载原文' }))
  expect(await screen.findByRole('alert')).toBeInTheDocument()
  expect(screen.queryByText('Old content')).toBeNull()
  expect(screen.getByRole('button', { name: '复制原文' })).toBeDisabled()
  fireEvent.click(screen.getByRole('button', { name: '重新读取原文' }))
  expect(await screen.findByText('Fresh content')).toBeInTheDocument()
})

it('network failure preserves loaded text and retries the same continuation', async () => {
  read.mockResolvedValueOnce(page('First', { total_characters: 9, next_start_offset: 5 }))
    .mockRejectedValueOnce(new Error('Network unavailable')).mockResolvedValueOnce(page('Last', { start_offset: 5, end_offset: 9, total_characters: 9 }))
  render(view()); open()
  fireEvent.click(await screen.findByRole('button', { name: '继续加载原文' }))
  await screen.findByRole('alert')
  expect(screen.getByText('First')).toBeInTheDocument()
  fireEvent.click(screen.getByRole('button', { name: '继续加载原文' }))
  expect(await screen.findByText('FirstLast')).toBeInTheDocument()
  expect(read.mock.calls.slice(1).map(call => call[1])).toEqual([5, 5])
})

it('source-only steps can expand and open the original without a summary string', async () => {
  read.mockResolvedValueOnce(page())
  render(<ConversationSources scope="current"><StepList rows={[{ key: 'read', kind: 'read', text: '读取了 1 条历史消息', state: 'succeeded', sources: [{ ...target, label: 'Purchase notes · 用户', content: 'Excerpt' }] }]} /></ConversationSources>)
  fireEvent.click(screen.getByRole('button', { name: '读取了 1 条历史消息' }))
  expect(screen.getByText('Excerpt')).toBeInTheDocument()
  open()
  expect(await screen.findByText('Original request')).toBeInTheDocument()
})
