import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import Root from '../Root'
import { makeFile, makeSnapshot } from './fixtures'
import type { ProjectInspection } from '../../api/projects'
import { readChangeHistory, readHistoryDetail, undoHistoryChange, type ChangeHistoryDetail } from '../../api/changeHistory'
import type { FileFocus } from '../lib/filePosition'
import type { FileLocation } from '../lib/fileReferences'

vi.mock('../../api/changeHistory', () => ({ readChangeHistory: vi.fn(), readHistoryDetail: vi.fn(), undoHistoryChange: vi.fn() }))

const state = vi.hoisted(() => ({ sid: 'chat-a', refresh: vi.fn(), unmounted: vi.fn(), cancel: vi.fn(), snapshot: null as ProjectInspection | null, openFilePath: null as string | null }))
const historyActions = vi.hoisted(() => vi.fn())
vi.mock('../../hooks/useRuntimeStatus', () => ({ useRuntimeStatus: () => ({ runtime: { ready: true, connection: 'online' } }) }))
vi.mock('../../hooks/useProjectWorkspace', () => ({ useProjectWorkspace: () => ({ selectedProjectId: state.snapshot?.project.project_id ?? null, snapshot: state.snapshot, bindings: [], refresh: state.refresh }) }))
vi.mock('../../hooks/useChangeSets', () => ({ useChangeSets: () => ({ refresh: state.refresh }) }))
vi.mock('../lib/useProviderSettings', () => ({ useProviderSettings: () => ({ reload: state.refresh }) }))
vi.mock('../lib/useConversations', () => ({ useConversations: () => ({ projects: [], refresh: state.refresh }) }))
vi.mock('../../hooks/useAgentConversation', () => ({ AgentBusyError: class extends Error {}, useAgentConversation: ({ openFilePath }: { openFilePath: string | null }) => {
  state.openFilePath = openFilePath
  return {
  session: { session_id: state.sid, title: state.sid }, sessions: [], activities: [], messages: [], activeRun: null, cancel: state.cancel,
} } }))
vi.mock('../Sidebar', () => ({ Sidebar: () => null }))
vi.mock('../chat/ChatView', () => ({ ChatView: ({ onShowCommands, onShowChange, onOpenFile, onOpenLocalFile, onOpenSettings, onOpenMemorySettings, openFileLabel }: { onShowCommands: (id?: string) => void; onShowChange: (id: string) => void; onOpenFile: (path: string, location?: FileLocation) => void; onOpenLocalFile: (path: string) => void; onOpenSettings: () => void; onOpenMemorySettings: () => void; openFileLabel: string | null }) => {
  historyActions({ onShowCommands, onOpenMemorySettings })
  return <>
  <textarea className="zm-composer-input" defaultValue="待继续的草稿" aria-label="消息草稿" />
  <button onClick={() => onShowCommands()}>查看命令会话</button>
  <button onClick={() => onShowCommands('cmd-a')}>定位指定命令</button>
  <button onClick={() => onShowChange('filechange-1')}>打开修改测试</button>
  <button onClick={() => onOpenFile('notes.txt')}>打开测试文件</button>
  <button onClick={() => onOpenLocalFile('C:/outside/report.md')}>打开外部文件</button>
  <a href="https://example.com/report" target="_blank" rel="noreferrer">资料网页</a>
  <button onClick={() => onOpenFile('notes.txt', { line: 80, column: 4 })}>定位测试文件</button>
  <button onClick={onOpenSettings}>测试设置入口</button>
  <output data-testid="open-file-label">{openFileLabel}</output>
</> } }))
vi.mock('../panel/Files', () => ({ FilesPanel: ({ focus }: { focus?: FileFocus }) => <div>文件预览<output data-testid="file-focus">{JSON.stringify(focus)}</output></div> }))
vi.mock('../panel/LocalFile', () => ({ LocalFilePanel: ({ target }: { target: { path: string } }) => <div>外部预览 {target.path}</div> }))
vi.mock('../settings/Settings', () => ({ SettingsView: ({ onClose }: { onClose: () => void }) => <button onClick={onClose}>关闭测试设置</button> }))
vi.mock('../panel/Commands', async () => {
  const { useEffect } = await import('react')
  return { CommandsPanel: ({ sessionId, focusId, onChanged }: { sessionId: string; focusId: string | null; onChanged: () => void }) => {
    useEffect(() => () => state.unmounted(), [])
    return <div aria-label="命令面板测试">{sessionId}<output data-testid="command-focus">{focusId}</output><button onClick={onChanged}>刷新文件结果</button></div>
  } }
})

beforeEach(() => { vi.clearAllMocks(); localStorage.clear(); state.sid = 'chat-a'; state.snapshot = null; state.openFilePath = null })
afterEach(() => { cleanup(); delete window.zhimengBrowser; vi.unstubAllGlobals() })

it('opens outside artifacts without a project and drops the preview in a different conversation', () => {
  const view = render(<Root />)
  fireEvent.click(screen.getByRole('button', { name: '打开外部文件' }))
  expect(screen.getByText('外部预览 C:/outside/report.md')).toBeInTheDocument()
  expect(screen.getByRole('tab', { name: '文件' })).toHaveAttribute('aria-selected', 'true')
  expect(state.openFilePath).toBeNull()
  state.sid = 'chat-b'
  view.rerender(<Root />)
  expect(screen.queryByText('外部预览 C:/outside/report.md')).toBeNull()
  expect(screen.getByRole('textbox', { name: '消息草稿' })).toHaveValue('待继续的草稿')
})

it('desktop links open a shared page belonging to the active conversation and keep its draft', async () => {
  vi.stubGlobal('ResizeObserver', class { observe() {} disconnect() {} })
  const command = vi.fn().mockResolvedValue(undefined)
  window.zhimengBrowser = { command, state: async () => ({ tabs: [], active: null, downloads: [], error: null }), onChange: () => () => {}, onLayout: () => () => {} }
  render(<Root />)
  fireEvent.click(screen.getByRole('link', { name: '资料网页' }))
  expect(command).toHaveBeenCalledWith({ action: 'create', session_id: 'chat-a', url: 'https://example.com/report' })
  expect(screen.getByRole('tab', { name: '浏览器' })).toHaveAttribute('aria-selected', 'true')
  expect(screen.getByRole('textbox', { name: '消息草稿' })).toHaveValue('待继续的草稿')
  await act(async () => {})
})

it('根页面更新保持历史导航回调，换会话后的命令入口使用新会话', () => {
  const view = render(<Root />)
  const original = historyActions.mock.lastCall![0]
  view.rerender(<Root />)
  expect(historyActions.mock.lastCall![0].onShowCommands).toBe(original.onShowCommands)
  expect(historyActions.mock.lastCall![0].onOpenMemorySettings).toBe(original.onOpenMemorySettings)
  state.sid = 'chat-b'
  view.rerender(<Root />)
  expect(historyActions.mock.lastCall![0].onShowCommands).not.toBe(original.onShowCommands)
  fireEvent.click(screen.getByRole('button', { name: '定位指定命令' }))
  expect(screen.getByLabelText('命令面板测试')).toHaveTextContent('chat-b')
  expect(screen.getByTestId('command-focus')).toHaveTextContent('cmd-a')
})

it('文件定位独立传到右栏，重复点击产生新请求，上下文保留纯路径', () => {
  state.snapshot = makeSnapshot({ files: [makeFile('notes.txt')], chapters: [], volumes: [] })
  render(<Root />)
  fireEvent.click(screen.getByRole('button', { name: '定位测试文件' }))
  const first = JSON.parse(screen.getByTestId('file-focus').textContent!)
  expect(first.location).toEqual({ line: 80, column: 4 })
  expect(state.openFilePath).toBe('notes.txt')
  fireEvent.click(screen.getByRole('button', { name: '定位测试文件' }))
  expect(JSON.parse(screen.getByTestId('file-focus').textContent!).request).toBe(first.request + 1)
  expect(screen.getByRole('textbox', { name: '消息草稿' })).toHaveValue('待继续的草稿')
})

it('撤回面板卸载并切到另一个对话后，真实 Hook 仍通知当前文件工作区刷新', async () => {
  const changed = { id: 'filechange-1', source: 'file_change', session_id: 'chat-a', project_id: null, run_id: null,
    title: '保存报告', created_at: '2026-11-19T10:00:00Z', files: [], previews: [], status: 'applied', undo_available: true, undo_reason: null, review_revision: null } satisfies ChangeHistoryDetail
  let finish!: () => void
  vi.mocked(undoHistoryChange).mockReturnValueOnce(new Promise(resolve => { finish = () => resolve({} as never) }))
  vi.mocked(readChangeHistory).mockResolvedValue({ session_id: 'chat-a', scope: 'session', project_id: null, revision: '1', total: 0, changes: [], next_offset: null })
  vi.mocked(readHistoryDetail).mockResolvedValue(changed)
  const view = render(<Root />)
  fireEvent.click(screen.getByRole('button', { name: '打开修改测试' }))
  fireEvent.click(await screen.findByRole('button', { name: '撤回这次修改' }))
  expect(undoHistoryChange).toHaveBeenCalledWith(changed)
  fireEvent.click(screen.getByRole('button', { name: '返回对话' }))
  state.sid = 'chat-b'
  state.snapshot = makeSnapshot({ files: [makeFile('notes.txt')], chapters: [], volumes: [] })
  view.rerender(<Root />)
  fireEvent.click(screen.getByRole('button', { name: '打开测试文件' }))
  expect(screen.getByText('文件预览')).toBeInTheDocument()
  state.refresh.mockClear()
  await act(async () => finish())
  expect(state.refresh).toHaveBeenCalledOnce()
  expect(screen.getByText('文件预览')).toBeInTheDocument()
  expect(screen.getByRole('textbox', { name: '消息草稿' })).toHaveValue('待继续的草稿')
})

it('文件被设置页遮住时界面与请求都清除当前文件，返回后恢复', async () => {
  state.snapshot = makeSnapshot({ files: [makeFile('notes.txt')], chapters: [], volumes: [] })
  render(<Root />)
  fireEvent.click(screen.getByRole('button', { name: '打开测试文件' }))
  expect(state.openFilePath).toBe('notes.txt')
  expect(screen.getByTestId('open-file-label')).toHaveTextContent('notes.txt')
  fireEvent.click(screen.getByRole('button', { name: '测试设置入口' }))
  expect(state.openFilePath).toBeNull()
  expect(screen.getByTestId('open-file-label')).toBeEmptyDOMElement()
  fireEvent.click(await screen.findByRole('button', { name: '关闭测试设置' }))
  expect(state.openFilePath).toBe('notes.txt')
  fireEvent.click(screen.getByRole('button', { name: '返回对话' }))
  expect(state.openFilePath).toBeNull()
})

it('无项目对话可以打开命令页，切对话不沿用前一场编号，返回时卸载轮询并保留草稿', async () => {
  const view = render(<Root />)
  fireEvent.click(screen.getByRole('button', { name: '定位指定命令' }))
  expect(screen.getByTestId('command-focus')).toHaveTextContent('cmd-a')
  expect(screen.getByRole('tab', { name: '命令' })).toHaveAttribute('aria-selected', 'true')
  expect(screen.getByLabelText('命令面板测试')).toHaveTextContent('chat-a')
  state.refresh.mockClear()
  fireEvent.click(screen.getByRole('button', { name: '刷新文件结果' }))
  expect(state.refresh).toHaveBeenCalled()
  state.sid = 'chat-b'
  view.rerender(<Root />)
  expect(screen.getByLabelText('命令面板测试')).toHaveTextContent('chat-b')
  expect(screen.getByLabelText('命令面板测试')).not.toHaveTextContent('chat-a')
  expect(screen.getByTestId('command-focus')).toBeEmptyDOMElement()
  fireEvent.click(screen.getByRole('button', { name: '返回对话' }))
  expect(screen.queryByLabelText('命令面板测试')).toBeNull()
  expect(state.unmounted).toHaveBeenCalledOnce()
  expect(screen.getByRole('textbox', { name: '消息草稿' })).toHaveValue('待继续的草稿')
  await waitFor(() => expect(screen.getByRole('textbox', { name: '消息草稿' })).toHaveFocus())
  expect(state.cancel).not.toHaveBeenCalled()
})
