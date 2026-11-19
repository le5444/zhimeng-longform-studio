import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import { BrowserPanel } from '../panel/Browser'
import type { SharedBrowserState } from '../../api/sharedBrowser'

beforeEach(() => { vi.stubGlobal('ResizeObserver', class { observe() {} disconnect() {} }) })
afterEach(() => { cleanup(); delete window.zhimengBrowser; vi.restoreAllMocks(); vi.unstubAllGlobals() })

const first: SharedBrowserState = { active: 'page-a', error: null, downloads: [], tabs: [
  { id: 'page-a', session_id: 'chat-a', title: '资料 A', url: 'https://example.com/a', mode: 'agent', loading: false, error: null, restored: false, can_back: false, can_forward: false },
  { id: 'page-b', session_id: 'chat-b', title: '资料 B', url: 'https://example.com/b', mode: 'user', loading: false, error: null, restored: true, can_back: false, can_forward: false },
] }

it('switches session ownership without closing its retained page; late replies cannot replace current errors', async () => {
  let notify!: (state: SharedBrowserState) => void
  let rejectOld!: (error: Error) => void
  const command = vi.fn(async (request: Record<string, unknown>) => {
    if (request.action === 'navigate') return new Promise((_resolve, reject) => { rejectOld = reject })
  })
  window.zhimengBrowser = { state: async () => first, command, onChange: callback => { notify = callback; return () => {} }, onLayout: () => () => {} }
  const view = render(<BrowserPanel sessionId="chat-a" />)
  await screen.findByRole('button', { name: '资料 A' })
  expect(screen.queryByText('资料 B')).not.toBeInTheDocument()
  fireEvent.change(screen.getByLabelText('网页地址'), { target: { value: 'https://example.com/new' } })
  fireEvent.submit(screen.getByLabelText('网页地址').closest('form')!)
  view.rerender(<BrowserPanel sessionId="chat-b" />)
  await screen.findByRole('button', { name: '资料 B' })
  expect(command).toHaveBeenCalledWith({ action: 'select', id: 'page-b', session_id: 'chat-b' })
  await act(async () => { rejectOld(new Error('旧对话失败')); notify({ ...first, active: 'page-b' }) })
  expect(screen.queryByText('旧对话失败')).not.toBeInTheDocument()
  expect(screen.getByLabelText('网页地址')).toHaveValue('https://example.com/b')
  expect(screen.getByText(/未提交表单没有恢复/)).toBeInTheDocument()
  view.unmount()
  expect(command).toHaveBeenCalledWith({ action: 'layout', bounds: null })
  expect(command.mock.calls.some(([request]) => request.action === 'close')).toBe(false)
})

it('preserves an address being edited when page state changes, and transfers control explicitly', async () => {
  let notify!: (state: SharedBrowserState) => void
  const command = vi.fn().mockResolvedValue(undefined)
  window.zhimengBrowser = { state: async () => first, command, onChange: callback => { notify = callback; return () => {} }, onLayout: () => () => {} }
  render(<BrowserPanel sessionId="chat-a" />)
  const input = await screen.findByLabelText('网页地址')
  await waitFor(() => expect(input).toHaveValue('https://example.com/a'))
  fireEvent.focus(input); fireEvent.change(input, { target: { value: 'https://draft.example/' } })
  act(() => notify({ ...first, tabs: first.tabs.map(tab => ({ ...tab, url: 'https://example.com/changed' })) }))
  expect(input).toHaveValue('https://draft.example/')
  fireEvent.click(screen.getByRole('button', { name: '我来操作' }))
  expect(command).toHaveBeenCalledWith({ action: 'control', id: 'page-a', mode: 'user', session_id: 'chat-a' })
})
