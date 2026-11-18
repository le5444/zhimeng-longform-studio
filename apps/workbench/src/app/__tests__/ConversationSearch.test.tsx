import { act, cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { afterAll, afterEach, beforeAll, beforeEach, expect, it, vi } from 'vitest'
import { ConversationSearch } from '../ConversationSearch'
import { RuntimeApiError } from '../../api/runtime'
import type { SessionSearchPage } from '../../api/sessionSearch'

const api = vi.hoisted(() => ({ search: vi.fn(), source: vi.fn() }))
vi.mock('../../api/sessionSearch', () => ({ searchSessionMessages: api.search }))
vi.mock('../../api/sessionSource', () => ({ readSessionSource: api.source }))

function page(content = '预算确定为 730 元', id = 'm1'): SessionSearchPage {
  return { query: '预算', scope: 'all', project_id: null, has_more: false, next_before_message_id: null,
    matches: [{ session_id: 'past', session_title: '采购记录', project_id: null, project_name: null,
      message_id: id, ordinal: 1, role: 'user', created_at: '2026-11-07T00:00:00Z', run_id: 'r1',
      run_status: 'succeeded', origin_kind: 'user', content, start_offset: 0, end_offset: content.length,
      total_characters: content.length, next_start_offset: null, masked_secrets: 0, offset_basis: 'masked_text' }] }
}
function view(query = '预算', scope: 'all' | 'project' = 'all', projectId: string | null = null) {
  return <><textarea aria-label="当前草稿" defaultValue="正在写的新问题" /><ConversationSearch query={query} scope={scope} projectId={projectId} /></>
}
function pending<T>() { let resolve!: (value: T) => void; const promise = new Promise<T>(r => { resolve = r }); return { promise, resolve } }

beforeAll(() => {
  Object.defineProperty(HTMLDialogElement.prototype, 'showModal', { configurable: true, value() { this.open = true; this.querySelector('button')?.focus() } })
  Object.defineProperty(HTMLDialogElement.prototype, 'close', { configurable: true, value() { this.open = false } })
})
afterAll(() => { Reflect.deleteProperty(HTMLDialogElement.prototype, 'showModal'); Reflect.deleteProperty(HTMLDialogElement.prototype, 'close') })
beforeEach(() => { api.search.mockReset(); api.source.mockReset() })
afterEach(cleanup)

it('shows body hits with source and opens the exact original without changing the current draft', async () => {
  api.search.mockResolvedValue(page())
  api.source.mockResolvedValue({ message: page('原文：预算确定为 730 元').matches[0], content_hash: 'a'.repeat(64), previous_message_id: null, next_message_id: null })
  render(view())
  expect(screen.queryByText('没有包含这些文字的消息。')).toBeNull()
  const hit = await screen.findByRole('button', { name: /采购记录.*730/ })
  hit.focus(); fireEvent.click(hit)
  expect(await screen.findByText('原文：预算确定为 730 元')).toBeVisible()
  expect(api.source).toHaveBeenCalledWith({ sessionId: 'past', messageId: 'm1' }, 0, undefined, expect.any(Object))
  fireEvent.click(screen.getByRole('button', { name: '关闭原文' }))
  expect(hit).toHaveFocus()
  expect(screen.getByLabelText('当前草稿')).toHaveValue('正在写的新问题')
})

it('aborts and discards a late response when query or project changes, even if transport ignores abort', async () => {
  const old = pending<SessionSearchPage>()
  api.search.mockReturnValueOnce(old.promise).mockResolvedValueOnce(page('新的结果'))
  const rendered = render(view())
  await waitFor(() => expect(api.search).toHaveBeenCalledTimes(1))
  const signal = api.search.mock.lastCall?.[4].signal
  rendered.rerender(view('新问题', 'project', 'p2'))
  expect(signal.aborted).toBe(true)
  await screen.findByText('新的结果')
  await act(async () => { old.resolve(page('旧结果')); await old.promise })
  expect(screen.queryByText('旧结果')).toBeNull()
  expect(api.search.mock.lastCall?.slice(0, 4)).toEqual(['新问题', 'project', 'p2', undefined])
})

it('keeps loaded hits on pagination failure and retries the same cursor without duplicates', async () => {
  const first = { ...page(), has_more: true, next_before_message_id: 'm1' }
  api.search.mockResolvedValueOnce(first).mockRejectedValueOnce(new Error('网络暂时不可用'))
    .mockResolvedValueOnce({ ...page('第二条', 'm2'), matches: [...first.matches, ...page('第二条', 'm2').matches] })
  render(view())
  fireEvent.click(await screen.findByRole('button', { name: '加载更多消息' }))
  expect(await screen.findByRole('alert')).toHaveTextContent('网络暂时不可用')
  expect(screen.getByText('预算确定为 730 元')).toBeVisible()
  fireEvent.click(screen.getByRole('button', { name: '重试正文搜索' }))
  await screen.findByText('第二条')
  expect(api.search.mock.lastCall?.[3]).toBe('m1')
  expect(screen.getAllByText('预算确定为 730 元')).toHaveLength(1)
  expect(screen.queryByRole('button', { name: '加载更多消息' })).toBeNull()
})

it('allows restarting after the cursor conversation was deleted', async () => {
  api.search.mockResolvedValueOnce({ ...page(), has_more: true, next_before_message_id: 'm1' })
    .mockRejectedValueOnce(new RuntimeApiError(400, { error: { message: '搜索位置已失效' } }))
    .mockResolvedValueOnce(page('仍然存在的消息', 'm2'))
  render(view())
  fireEvent.click(await screen.findByRole('button', { name: '加载更多消息' }))
  fireEvent.click(await screen.findByRole('button', { name: '重新搜索' }))
  await screen.findByText('仍然存在的消息')
  expect(screen.queryByText('预算确定为 730 元')).toBeNull()
  expect(api.search.mock.lastCall?.[3]).toBeUndefined()
})

it('never describes unavailable history as no matches', async () => {
  api.search.mockRejectedValueOnce(new Error('历史库读取失败')).mockResolvedValueOnce({ ...page(), matches: [] })
  render(view())
  expect(await screen.findByRole('alert')).toHaveTextContent('历史库读取失败')
  expect(screen.queryByText('没有包含这些文字的消息。')).toBeNull()
  fireEvent.click(screen.getByRole('button', { name: '重试正文搜索' }))
  expect(await screen.findByText('没有包含这些文字的消息。')).toBeVisible()
})

it('refreshes saved history explicitly and removes results that no longer exist', async () => {
  api.search.mockResolvedValueOnce(page()).mockResolvedValueOnce({ ...page(), matches: [] })
  render(view())
  await screen.findByText('预算确定为 730 元')
  fireEvent.click(screen.getByRole('button', { name: '刷新搜索' }))
  await screen.findByText('没有包含这些文字的消息。')
  expect(screen.queryByText('预算确定为 730 元')).toBeNull()
  expect(api.search.mock.lastCall?.[3]).toBeUndefined()
})

it('closes an open source when the search changes and labels partial excerpts', async () => {
  const first = page()
  first.matches[0].start_offset = 100
  api.search.mockResolvedValue(first)
  api.source.mockResolvedValue({ message: first.matches[0], content_hash: 'a'.repeat(64), previous_message_id: null, next_message_id: null })
  const rendered = render(view())
  fireEvent.click(await screen.findByRole('button', { name: /查看原消息 · 部分原文/ }))
  await waitFor(() => expect(within(screen.getByRole('dialog')).getByText('预算确定为 730 元')).toBeVisible())
  rendered.rerender(view('下一次搜索'))
  expect(screen.queryByRole('dialog')).toBeNull()
  expect(screen.getByLabelText('当前草稿')).toHaveValue('正在写的新问题')
})
