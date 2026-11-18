/**
 * 中间的对话（9-29 回炉重造，从旧 AgentColumn 的测试搬过来、按新界面改）：发送 / 排队 / 停止 / 接着做 / 重发 / 分叉 / 压缩，
 * @ 文件、/ 命令和技能、快捷键、修改卡（默认直接保存、能撤回）、失败卡（说原因、测哪些模型能用）、这一轮记住了什么。
 * 接口全部 mock，不启动服务。
 */
import { act, cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { createRef } from 'react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { AgentSendUnconfirmedError, type AgentConversationState } from '../../hooks/useAgentConversation'
import { effortOptionLabel } from '../../lib/effortOptions'
import { ChatView, type ChatViewProps, type ChatComposerHandle } from '../chat/ChatView'
import { makeFile, makeSnapshot } from './fixtures'
import { T } from '../strings'
import { A } from '../text'
import * as messagePresentation from '../../lib/humanizeAiText'
import type { AgentActivity } from '../../api/agent'
import type { QueueState, QueueInput, QueueUpdate } from '../../api/agentQueue'
import type { MemoryChangesView } from '../../api/memoryChanges'
import { readRunArtifacts } from '../../api/runArtifacts'
import { readSessionContextState } from '../../api/contextState'
import { readSessionGoal } from '../../api/sessionGoal'

vi.mock('../../api/runArtifacts', () => ({ readRunArtifacts: vi.fn() }))

const queues = vi.hoisted(() => new Map<string, QueueState>())
const queueApi = vi.hoisted(() => ({ readMessageQueue: vi.fn(), enqueueMessage: vi.fn(), steerQueuedMessage: vi.fn(), updateMessageQueue: vi.fn(), dispatchQueuedMessage: vi.fn() }))
vi.mock('../../api/agentQueue', () => queueApi)
vi.mock('../../api/sessionPlan', () => ({ readSessionPlan: vi.fn(async (sessionId: string) => ({ session_id: sessionId, plan: null })) }))
vi.mock('../../api/sessionGoal', () => ({ readSessionGoal: vi.fn(async (sessionId: string) => ({ session_id: sessionId, goal: null })), endSessionGoal: vi.fn() }))
vi.mock('../../api/contextState', () => ({ readSessionContextState: vi.fn(async (sessionId: string) => ({
  session_id: sessionId, message_count: 0, recent_message_count: 0, compacted_message_count: 0,
  compacting: false, background_pending: false, latest_checkpoint: null,
})) }))

afterEach(() => {
  cleanup()
  vi.useRealTimers()
})

const changeSetMock = vi.hoisted(() => ({
  getChangeSet: vi.fn(async (id: string) => ({
    change_set_id: id,
    title: '把第 4 章开头改得更有画面感',
    summary: '',
    origin: 'agent',
    status: 'reviewing',
    review_revision: 1,
    base_revision: 3,
    changes: [{ relative_path: '05_正文内容/第一卷文件夹/001-050章/004_玉佩上的青云.md' }],
    writeback: null,
  })),
  applyChangeSetDirect: vi.fn(async () => ({ writeback: { result_revision: 4 } })),
  discardChangeSet: vi.fn(async () => ({})),
  rollbackChangeSet: vi.fn(async () => ({})),
  listProjectChangeSets: vi.fn(async () => []),
}))
vi.mock('../../api/changeSets', () => changeSetMock)

const userSkillsMock = vi.hoisted(() => ({
  readUserSkills: vi.fn(async () => ({
    schema: 'zhimeng.user-skills.v1',
    skills_root: 'C:/skills',
    exists: true,
    total: 1,
    enabled: 1,
    skills: [
      { name: '网文知识库', description: '网文写作机制卡库', folder: '网文知识库', has_scripts: false, enabled: true, origin: 'project' },
    ],
  })),
}))
vi.mock('../../api/userSkills', async (importActual) => ({
  ...(await importActual<object>()),
  readUserSkills: userSkillsMock.readUserSkills,
}))

const memoryMock = vi.hoisted(() => ({
  readMemoryChanges: vi.fn(async (sessionId: string): Promise<MemoryChangesView> => ({ session_id: sessionId, changes: {}, pending: false })),
}))
vi.mock('../../api/memoryChanges', () => memoryMock)

function conversation(overrides: Partial<AgentConversationState> = {}): AgentConversationState {
  return {
    availability: 'available',
    detail: '',
    sessions: [],
    session: { session_id: 's1', title: '写作对话', updated_at: '2026-09-13T01:00:00Z' } as never,
    messages: [],
    hasEarlierMessages: false,
    loadEarlierMessages: vi.fn(async () => undefined),
    streamingContent: '',
    activeRun: null,
    activities: [],
    pastActivities: {},
    loadRunActivities: vi.fn(async () => undefined),
    contextState: 'idle',
    running: false,
    switchingSession: false,
    busy: false,
    error: null,
    send: vi.fn(async () => undefined),
    cancel: vi.fn(async () => undefined),
    rebindProvider: vi.fn(async () => undefined),
    createConversation: vi.fn(async () => undefined),
    forkConversation: vi.fn(async () => ({ session_id: 's-fork' } as never)),
    compactConversation: vi.fn(async () => ({ checkpoint_id: 'cp-1' } as never)),
    latestCheckpoint: null,
    continuation: null,
    prepareContinuation: vi.fn().mockResolvedValue(null),
    selectConversation: vi.fn(async () => undefined),
    renameConversation: vi.fn(async () => undefined),
    setConversationPinned: vi.fn(async () => undefined),
    deleteConversation: vi.fn(async () => undefined),
    refresh: vi.fn(),
    ...overrides,
  }
}

const CHAPTER_4 = '05_正文内容/第一卷文件夹/001-050章/004_玉佩上的青云.md'
const snapshot = {
  project: { project_id: 'p1', display_name: '宗门七骗子', revision: 3, fingerprint: 'f'.repeat(64), root_path: 'D:\\书', format: '01-09' },
  chapters: [
    { chapter_id: 'VOL001-CH004', number: 4, volume_id: 'VOL001', file_ids: ['file-4'], prose_file_ids: ['file-4'] },
    { chapter_id: 'VOL001-CH005', number: 5, volume_id: 'VOL001', file_ids: ['file-5'], prose_file_ids: ['file-5'] },
  ],
  files: [
    { file_id: 'file-4', relative_path: CHAPTER_4, kind: 'prose', chapter_id: 'VOL001-CH004', size_bytes: 12000 },
    { file_id: 'file-5', relative_path: '05_正文内容/第一卷文件夹/001-050章/005_厨子的药膳.md', kind: 'prose', chapter_id: 'VOL001-CH005', size_bytes: 9000 },
  ],
  volumes: [],
  sections: [],
} as never

const provider = { settings: { configured: true, model: '[opencode]deepseek-v4-pro' }, state: 'ready', reload: vi.fn() } as never
const runtime = { connection: 'online', ready: true, serviceStatus: 'ready', runStatus: null, version: null, detail: '', checkedAt: '' } as const
const NO_FOLDER = { projectId: null, snapshot: null } as const

function renderChat(conv: AgentConversationState, over: Partial<ChatViewProps> = {}) {
  const props: ChatViewProps = {
    conversation: conv,
    projectId: 'p1',
    snapshot,
    projectState: 'ready',
    projectError: null,
    openFileLabel: null,
    onProjectChanged: vi.fn(),
    onChangesChanged: vi.fn(),
    provider,
    onProviderChanged: vi.fn(async () => undefined),
    runtime,
    projects: [],
    onRebindProject: vi.fn(async () => undefined),
    onOpenFolder: vi.fn(),
    onNewFolder: vi.fn(),
    onOpenFile: vi.fn(),
    onShowChange: vi.fn(),
    onShowChanges: vi.fn(),
    onOpenSettings: vi.fn(),
    onOpenMemorySettings: vi.fn(),
    panelOpen: false,
    onTogglePanel: vi.fn(),
    sidebarCollapsed: false,
    onExpandSidebar: vi.fn(),
    onNewConversation: vi.fn(),
    notify: vi.fn(),
    askConfirm: vi.fn(),
    ...over,
  }
  const view = render(<ChatView {...props} />)
  return { ...view, props, rerenderWith: (next: Partial<ChatViewProps>) => view.rerender(<ChatView {...props} {...next} />) }
}

const msg = (id: string, runId: string, role: 'user' | 'assistant', content: string, ordinal: number, extra: Record<string, unknown> = {}) =>
  ({ message_id: id, run_id: runId, role, content, ordinal, ...extra }) as never

it('明确启动持续目标携带原文，普通发送保持普通任务', async () => {
  const conv = conversation()
  renderChat(conv)
  fireEvent.change(screen.getByPlaceholderText(T.agent.placeholder), { target: { value: '整理采购数据并完成验收' } })
  await waitFor(() => expect(screen.getByRole('button', { name: '将输入作为持续目标' })).toBeEnabled())
  fireEvent.click(screen.getByRole('button', { name: '将输入作为持续目标' }))
  await waitFor(() => expect(conv.send).toHaveBeenCalledWith(expect.objectContaining({ text: '整理采购数据并完成验收', goal: { action: 'start' } })))
})

it('继续目标保留未发送草稿，并传递当前目标版本', async () => {
  vi.mocked(readSessionGoal).mockResolvedValueOnce({ session_id: 's1', goal: {
    goal_id: 'goal-existing', session_id: 's1', run_id: 'run-old', objective: '完成采购核对', status: 'paused', revision: 7,
    stage_count: 1, progress: '来源已读', evidence: '', requested_status: null, pause_reason: '用户暂停',
    created_at: '', updated_at: '', status_source: 'user',
  } })
  const conv = conversation()
  renderChat(conv)
  fireEvent.change(screen.getByPlaceholderText(T.agent.placeholder), { target: { value: '这份草稿先不要发' } })
  fireEvent.click(await screen.findByRole('button', { name: '继续目标' }))
  await waitFor(() => expect(conv.send).toHaveBeenCalledWith(expect.objectContaining({ goal: { action: 'resume', goal_id: 'goal-existing', expected_revision: 7 } })))
  expect(screen.getByPlaceholderText(T.agent.placeholder)).toHaveValue('这份草稿先不要发')
  expect(vi.mocked(conv.send).mock.calls[0][0].text).not.toContain('这份草稿')
})

it('整理期间失联时输入区显示离线而不是继续声称正在整理', async () => {
  vi.mocked(readSessionContextState).mockResolvedValueOnce({
    schema: 'zhimeng.session-context-state.v1', session_id: 's1', message_count: 1,
    recent_message_count: 1, compacted_message_count: 0, compacting: true,
    background_pending: true, latest_checkpoint: null,
    run_compaction: { run_id: 'r1', state: 'running', folded_results: 0, tokens_before: 75000, tokens_after: null },
  })
  const conv = conversation({ running: true, activeRun: { run_id: 'r1', status: 'running' } as never })
  const view = renderChat(conv, NO_FOLDER)
  await screen.findByText('正在整理工具结果…')
  fireEvent.change(screen.getByRole('textbox'), { target: { value: '仍未发送的补充' } })
  view.rerenderWith({ runtime: { ...runtime, ready: false, connection: 'offline' } })
  expect(screen.queryByText('正在整理工具结果…')).toBeNull()
  expect(screen.getByRole('textbox')).toHaveValue('仍未发送的补充')
  expect(conv.send).not.toHaveBeenCalled()
})

it('任务失败也展示记录同步重试，重试保留草稿且不重发任务', () => {
  const conv = conversation({ messageSyncError: '连接断开', refreshMessages: vi.fn(async () => true),
    activeRun: { run_id: 'r1', session_id: 's1', status: 'failed', error: { message: '模型连接断开' } } as never })
  const view = renderChat(conv, NO_FOLDER)
  fireEvent.change(screen.getByRole('textbox'), { target: { value: '还没发送的草稿' } })
  fireEvent.click(screen.getByRole('button', { name: '重试同步记录' }))
  expect(conv.refreshMessages).toHaveBeenCalledOnce()
  expect(conv.send).not.toHaveBeenCalled()
  expect(screen.getByRole('textbox')).toHaveValue('还没发送的草稿')
  view.rerenderWith({ conversation: { ...conv, syncingMessages: true } })
  expect(screen.getByRole('button', { name: '正在同步记录…' })).toBeDisabled()
  view.rerenderWith({ conversation: { ...conv, messageSyncError: null } })
  expect(screen.queryByRole('button', { name: '重试同步记录' })).not.toBeInTheDocument()
})

it('本轮文件核对只出现一次，按需读真实轮次并保留草稿，项目外走原修改预览', async () => {
  vi.mocked(readRunArtifacts).mockResolvedValueOnce({ session_id: 's1', run_id: 'r1', run_status: 'succeeded',
    revision: 'v1', observed_at: '2026-11-08T10:00:00Z', total_paths: 1, next_offset: null, scope: 'run_receipt_paths_only',
    observations: [{ receipt_id: 'filechange-result', source: 'file_change', record_state: 'applied', operation: 'create',
      path: 'C:/outside/report.csv', expected_state: 'file', expected_sha256: 'a', matches_recorded_state: true,
      current: { state: 'file', sha256: 'a', bytes: 42, reason: null } }],
  })
  const conv = conversation({ messages: [
    msg('u1', 'r1', 'user', '整理采购表', 1, { session_id: 's1' }),
    msg('a1', 'r1', 'assistant', '开始读取表格。', 2, { session_id: 's1' }),
    msg('a2', 'r1', 'assistant', '采购表已经整理。', 3, { session_id: 's1' }),
  ] })
  const { props } = renderChat(conv, NO_FOLDER)
  expect(screen.getAllByRole('button', { name: '核对本轮文件' })).toHaveLength(1)
  expect(readRunArtifacts).not.toHaveBeenCalled()
  fireEvent.change(screen.getByRole('textbox'), { target: { value: '下一条草稿' } })
  fireEvent.click(screen.getByRole('button', { name: '核对本轮文件' }))
  await screen.findByText('与记录一致')
  expect(readRunArtifacts).toHaveBeenCalledWith('s1', 'r1', 0, undefined, expect.objectContaining({ signal: expect.any(AbortSignal) }))
  expect(screen.queryByRole('button', { name: '打开文件' })).toBeNull()
  fireEvent.click(screen.getByRole('button', { name: '查看修改与撤回' }))
  expect(props.onShowChange).toHaveBeenCalledWith('filechange-result')
  expect(screen.getByRole('textbox')).toHaveValue('下一条草稿')
  expect(conv.send).not.toHaveBeenCalled()
})

it('独立复核从最后回答发起只读任务，保留草稿和全局访问模式', async () => {
  const conv = conversation({ messages: [
    msg('u1', 'r1', 'user', '整理采购表', 1, { session_id: 's1' }),
    msg('a1', 'r1', 'assistant', '采购表已经整理。', 2, { session_id: 's1' }),
  ] })
  renderChat(conv, NO_FOLDER)
  fireEvent.change(screen.getByRole('textbox'), { target: { value: '还没发出的文字' } })
  fireEvent.click(screen.getByRole('button', { name: '独立复核结果（只读）' }))
  await waitFor(() => expect(conv.send).toHaveBeenCalledWith(expect.objectContaining({ reviewSourceRunId: 'r1', writerMode: 'repair_only' })))
  expect(screen.getByRole('textbox')).toHaveValue('还没发出的文字')
  fireEvent.submit(screen.getByRole('textbox').closest('form')!)
  await waitFor(() => expect(conv.send).toHaveBeenLastCalledWith(expect.objectContaining({ text: '还没发出的文字', reviewSourceRunId: undefined, writerMode: 'author_review_change_proposal' })))
})

it('复核回答显示原始来源，重新生成仍复核原任务', async () => {
  const conv = conversation({ messages: [
    msg('u2', 'r2', 'user', '复核采购结果', 3, { session_id: 's1' }),
    msg('a2', 'r2', 'assistant', '产物缺少明细。', 4, { session_id: 's1' }),
  ], reviewSources: { r2: { source_run_id: 'r1', source_run_ordinal: 1, source_message_id: 'u1' } } })
  renderChat(conv, NO_FOLDER)
  expect(screen.getByText('独立复核 · 第 1 轮')).toBeInTheDocument()
  expect(screen.getByRole('button', { name: '原始要求' })).toBeInTheDocument()
  expect(screen.queryByRole('button', { name: '独立复核结果（只读）' })).not.toBeInTheDocument()
  fireEvent.click(screen.getByRole('button', { name: '重新回答' }))
  await waitFor(() => expect(conv.send).toHaveBeenCalledWith(expect.objectContaining({ reviewSourceRunId: 'r1', writerMode: 'repair_only' })))
})

it.each(['running', 'busy', 'pending'])('复核操作在 %s 时禁用，跨会话旧消息不能触发', (state) => {
  const conv = conversation({ messages: [msg('a1', 'r1', 'assistant', '已完成', 1, { session_id: 's1' })],
    running: state === 'running', busy: state === 'busy',
    ...(state === 'pending' ? { pendingSend: { status: 'unknown' as const, item: { version: 1 as const,
      sessionId: 's1', key: 'review-pending', createdAt: '', provider: null,
      request: { message: 'Review', writer_mode: 'repair_only' as const } } } } : {}),
  })
  const view = renderChat(conv, NO_FOLDER)
  expect(screen.getByRole('button', { name: '独立复核结果（只读）' })).toBeDisabled()
  const other = conversation({ ...conv, running: false, busy: false, pendingSend: null, session: { ...conv.session!, session_id: 's2' } })
  view.rerenderWith({ ...NO_FOLDER, conversation: other })
  expect(screen.queryByRole('button', { name: '核对本轮文件' })).toBeNull()
  fireEvent.click(screen.getByRole('button', { name: '独立复核结果（只读）' }))
  expect(other.send).not.toHaveBeenCalled()
})

it('文件引用追加到原草稿，刷新保留，真正发送时携带文件编号和完整选段', async () => {
  const file = makeFile('docs/review (final).md')
  const docs = makeSnapshot({ files: [file], chapters: [], volumes: [] })
  const composerRef = createRef<ChatComposerHandle>()
  const conv = conversation()
  const over = { projectId: docs.project.project_id, snapshot: docs, composerRef }
  const first = renderChat(conv, over)
  const input = screen.getByRole('textbox')
  fireEvent.change(input, { target: { value: '请把这里改得更清楚。' } })
  const excerpt = '完整内容\n'.repeat(800)
  act(() => composerRef.current!.quoteFile({ projectId: docs.project.project_id, path: file.relative_path, text: excerpt, source: 'original' }))
  const expected = (input as HTMLTextAreaElement).value
  expect(expected).toContain('请把这里改得更清楚。')
  expect(expected).toContain(excerpt)
  expect(conv.send).not.toHaveBeenCalled()
  first.unmount()
  renderChat(conv, over)
  const restored = screen.getByRole('textbox') as HTMLTextAreaElement
  expect(restored).toHaveValue(expected)
  fireEvent.submit(restored.closest('form')!)
  await waitFor(() => expect(conv.send).toHaveBeenCalledWith(expect.objectContaining({ text: expected.trim(), manualFileIds: [file.file_id] })))
})

it('文件引用按会话保留，旧句柄和其他项目不能把选段写进新对话', () => {
  const composerRef = createRef<ChatComposerHandle>()
  const conv = conversation()
  const view = renderChat(conv, { composerRef })
  fireEvent.change(screen.getByRole('textbox'), { target: { value: 'A 草稿' } })
  const previous = composerRef.current!
  const b = conversation({ session: { ...conv.session!, session_id: 's2' } })
  view.rerenderWith({ composerRef, conversation: b })
  fireEvent.change(screen.getByRole('textbox'), { target: { value: 'B 草稿' } })
  act(() => previous.quoteFile({ projectId: 'p1', path: 'old.txt', text: '旧选段' }))
  act(() => composerRef.current!.quoteFile({ projectId: 'another-project', path: 'wrong.txt' }))
  expect(screen.getByRole('textbox')).toHaveValue('B 草稿')
  act(() => composerRef.current!.quoteFile({ projectId: 'p1', path: 'current.txt' }))
  expect(screen.getByRole('textbox')).toHaveValue('B 草稿\n\n@current.txt\n\n')
  view.rerenderWith({ composerRef, conversation: conv })
  expect(screen.getByRole('textbox')).toHaveValue('A 草稿')
})
const toolCall = (runId: string, sequence: number, id: string, tool: string, args: Record<string, unknown> = {}) =>
  ({ activity_id: `${runId}-a${sequence}`, run_id: runId, sequence, kind: 'tool_call', payload: { tool_call_id: id, tool_name: tool, arguments: args } })
const toolResult = (runId: string, sequence: number, id: string) =>
  ({ activity_id: `${runId}-a${sequence}`, run_id: runId, sequence, kind: 'tool_result', payload: { tool_call_id: id, status: 'succeeded' } })
const agentBubble = (text: string) => screen.getByText(text).closest('.zm-msg') as HTMLElement
const processFor = (text: string) => agentBubble(text).closest('.zm-turn')!.querySelector('.zm-process-group') as HTMLDetailsElement
function openProcess(details: HTMLDetailsElement) {
  fireEvent.click(details.querySelector('summary')!)
  fireEvent(details, new Event('toggle'))
}

beforeEach(() => {
  window.localStorage.clear()
  window.sessionStorage.clear()
  vi.clearAllMocks()
  memoryMock.readMemoryChanges.mockImplementation(async (sessionId: string) => ({ session_id: sessionId, changes: {}, pending: false }))
  queues.clear()
  const read = (sid: string): QueueState => structuredClone(queues.get(sid) ?? { session_id: sid, revision: 0, pause_reason: null, active_run_id: null, items: [] })
  queueApi.readMessageQueue.mockImplementation(async (sid: string) => read(sid))
  queueApi.enqueueMessage.mockImplementation(async (sid: string, input: QueueInput) => {
    const state = read(sid)
    state.items.push({ ...input, sending: false })
    state.revision++
    queues.set(sid, state)
    return structuredClone(state)
  })
  queueApi.updateMessageQueue.mockImplementation(async (sid: string, _revision: number, change: QueueUpdate) => {
    const state = read(sid)
    if (change.action === 'pause' || change.action === 'resume') state.pause_reason = change.action === 'pause' ? 'user' : null
    if (change.action === 'remove') state.items = state.items.filter((item) => item.item_id !== change.item_id)
    if (change.action === 'edit') state.items = state.items.map((item) => item.item_id === change.item_id ? { ...item, message: change.message! } : item)
    state.revision++
    queues.set(sid, state)
    return structuredClone(state)
  })
  queueApi.steerQueuedMessage.mockImplementation(async (sid: string, itemId: string, runId: string, input?: QueueInput) => {
    const state = read(sid)
    if (input && !state.items.some((item) => item.item_id === itemId)) state.items.push({ ...input, sending: false })
    state.items = state.items.map((item) => item.item_id === itemId ? { ...item, delivery: 'steer_pending', steering_run_id: runId } : item)
    state.revision++
    queues.set(sid, state)
    return { queue: structuredClone(state), delivery: { item_id: itemId, run_id: runId, status: 'pending' } }
  })
  queueApi.dispatchQueuedMessage.mockImplementation(async (sid: string) => {
    const state = read(sid)
    state.items.shift()
    state.active_run_id = 'queued-run'
    state.revision++
    queues.set(sid, state)
    return { queue: structuredClone(state), accepted: { run: { run_id: 'queued-run' } } }
  })
})

describe('对话', () => {
  it('恢复读取失败可重试原对话，保留草稿且不重发失败任务', () => {
    const conv = conversation({ availability: 'error', error: '对话记录暂时不可读',
      activeRun: { run_id: 'r1', session_id: 's1', status: 'failed', error: { message: '上一轮执行失败' } } as never,
      messages: [msg('a', 'r1', 'assistant', '已保存的部分结果', 1)] })
    const view = renderChat(conv, NO_FOLDER)
    fireEvent.change(screen.getByRole('textbox'), { target: { value: '重试期间保留的草稿' } })
    fireEvent.click(screen.getByRole('button', { name: '重新读取对话' }))
    expect(conv.refresh).toHaveBeenCalledOnce()
    expect(conv.send).not.toHaveBeenCalled()
    expect(screen.getByRole('textbox')).toHaveValue('重试期间保留的草稿')
    expect(screen.getByText('已保存的部分结果')).toBeInTheDocument()
    view.rerenderWith({ runtime: { ...runtime, ready: false, connection: 'offline' } })
    expect(screen.getByRole('button', { name: '重新读取对话' })).toBeDisabled()
    view.rerenderWith({ conversation: { ...conv, availability: 'available', error: null } })
    expect(screen.queryByRole('button', { name: '重新读取对话' })).not.toBeInTheDocument()
  })

  it('离线草稿不发送到正在运行的任务，恢复后可引导原任务', async () => {
    const conv = conversation({ running: true, activeRun: { run_id: 'r1', status: 'running', error: null } as never })
    const view = renderChat(conv, { ...NO_FOLDER, runtime: { ...runtime, ready: false, connection: 'offline' } })
    const input = screen.getByRole('textbox')
    fireEvent.change(input, { target: { value: '补充核对单位' } })
    expect(screen.getByRole('button', { name: '引导这一轮' })).toBeDisabled()
    fireEvent.click(screen.getByRole('button', { name: '引导这一轮' }))
    fireEvent.keyDown(input, { key: 'Enter' })
    expect(queueApi.steerQueuedMessage).not.toHaveBeenCalled()
    expect(queueApi.enqueueMessage).not.toHaveBeenCalled()
    expect(input).toHaveValue('补充核对单位')
    view.rerenderWith({ runtime })
    fireEvent.click(screen.getByRole('button', { name: '引导这一轮' }))
    await waitFor(() => expect(queueApi.steerQueuedMessage).toHaveBeenCalledOnce())
    expect(queueApi.steerQueuedMessage.mock.calls[0][2]).toBe('r1')
    expect(conv.send).not.toHaveBeenCalled()
  })

  it('本机服务离线仍可阅读和编辑草稿，恢复前不发送', async () => {
    const conv = conversation({ messages: [msg('a', 'r', 'assistant', '已保存的核对结果', 1)] })
    const view = renderChat(conv, NO_FOLDER)
    const input = screen.getByRole('textbox')
    fireEvent.change(input, { target: { value: '下一步核对' } })
    view.rerenderWith({ conversation: { ...conv, availability: 'checking' }, runtime: { ...runtime, ready: false, connection: 'offline' } })
    expect(screen.getByText('已保存的核对结果')).toBeInTheDocument()
    expect(input).not.toBeDisabled()
    fireEvent.change(input, { target: { value: '离线期间继续整理要求' } })
    fireEvent.keyDown(input, { key: 'Enter' })
    expect(conv.send).not.toHaveBeenCalled()
    expect(input).toHaveValue('离线期间继续整理要求')
    view.rerenderWith({ conversation: conv, runtime })
    expect(input).toHaveValue('离线期间继续整理要求')
  })
  it('经空会话返回长历史，刻度和更早入口等待这一次显示完成', async () => {
    vi.useFakeTimers()
    try {
      const messages = Array.from({ length: 12 }, (_, index) => [
        msg(`u-${index}`, `run-${index}`, 'user', `问题 ${index}`, index * 2 + 1),
        msg(`a-${index}`, `run-${index}`, 'assistant', `回答 ${index}`, index * 2 + 2),
      ]).flat()
      const conv = conversation({ messages, hasEarlierMessages: true })
      const view = renderChat(conv, NO_FOLDER)
      for (let i = 0; i < 5; i++) await act(async () => { vi.advanceTimersByTime(100) })
      expect(screen.getByRole('navigation', { name: '对话历史定位' })).toBeInTheDocument()
      view.rerenderWith({ conversation: { ...conv, session: { ...conv.session!, session_id: 'empty' }, messages: [], hasEarlierMessages: false } })
      view.rerenderWith({ conversation: conv })
      expect(screen.queryByRole('navigation', { name: '对话历史定位' })).not.toBeInTheDocument()
      expect(screen.getByRole('button', { name: T.agent.showEarlierMessages })).toBeDisabled()
      for (let i = 0; i < 5; i++) await act(async () => { vi.advanceTimersByTime(100) })
      expect(screen.getByRole('navigation', { name: '对话历史定位' })).toBeInTheDocument()
      expect(screen.getByText('回答 0')).toBeInTheDocument()
    } finally { vi.useRealTimers() }
  })

  it('当前回复逐字增长不重新处理旧消息，阶段尾段和后来修正仍更新', async () => {
    vi.useFakeTimers()
    const presentation = vi.spyOn(messagePresentation, 'presentAiText')
    try {
      const messages = Array.from({ length: 30 }, (_, index) => [
        msg(`u-${index}`, `r-${index}`, 'user', `检查采购表 ${index}`, index * 2 + 1),
        msg(`a-${index}`, `r-${index}`, 'assistant', `旧结果 ${index} 保留原文。`, index * 2 + 2),
      ]).flat()
      messages.push(msg('progress', 'active', 'assistant', '先核对采购明细。', 61))
      const base = { activity_id: 'delta-1', run_id: 'active', session_id: 's1', sequence: 2, kind: 'message',
        payload: { role: 'assistant', phase: 'delta', delta: '开始' } } as AgentActivity
      const conv = conversation({ messages, running: true, streamingContent: '开始',
        activeRun: { run_id: 'active', session_id: 's1', status: 'running' } as never, activities: [base],
      })
      const view = renderChat(conv, NO_FOLDER)
      for (let i = 0; i < 10; i++) await act(async () => { vi.advanceTimersByTime(100) })
      expect(screen.queryByText('正在显示历史记录…')).not.toBeInTheDocument()
      presentation.mockClear()
      view.rerenderWith({ conversation: { ...conv, streamingContent: '开始核对，表格内容完整。',
        activities: [...conv.activities, { ...base, activity_id: 'delta-2', sequence: 3 } as never] } })
      expect(screen.getByText('开始核对，表格内容完整。')).toBeInTheDocument()
      expect(presentation.mock.calls.filter(([text]) => text.startsWith('旧结果'))).toHaveLength(0)
      expect(screen.getByText('旧结果 0 保留原文。')).toBeInTheDocument()
      view.rerenderWith({ conversation: { ...conv, running: false, streamingContent: '',
        activeRun: { ...conv.activeRun!, status: 'succeeded' }, messages: [
          ...messages.slice(0, 1), msg('a-0', 'r-0', 'assistant', '首条结果已修正。', 2),
          ...messages.slice(2), msg('final', 'active', 'assistant', '最终核对完成。', 62),
        ] } })
      expect(screen.getByText('首条结果已修正。')).toBeInTheDocument()
      expect(screen.getAllByText('最终核对完成。')).toHaveLength(1)
      expect(screen.queryByText('开始核对，表格内容完整。')).not.toBeInTheDocument()
    } finally { presentation.mockRestore(); vi.useRealTimers() }
  })

  it('输入草稿不重新处理历史，收到更新仍显示新回答', async () => {
    vi.useFakeTimers()
    const presentation = vi.spyOn(messagePresentation, 'presentAiText')
    try {
      const messages = Array.from({ length: 12 }, (_, index) => [
        msg(`u-${index}`, `run-${index}`, 'user', `Check input ${index}`, index * 2 + 1),
        msg(`a-${index}`, `run-${index}`, 'assistant', `Report ${index}: preserved result.`, index * 2 + 2),
      ]).flat()
      const conv = conversation({ messages })
      const page = renderChat(conv, NO_FOLDER)
      for (let i = 0; i < 10; i++) await act(async () => { vi.advanceTimersByTime(100) })
      expect(screen.queryByText('正在显示历史记录…')).not.toBeInTheDocument()
      presentation.mockClear()
      fireEvent.change(screen.getByRole('textbox'), { target: { value: 'Continue with the next input.' } })
      expect(presentation).not.toHaveBeenCalled()
      expect(screen.getByText('Report 0: preserved result.')).toBeInTheDocument()
      page.rerenderWith({ conversation: { ...conv, messages: [...messages.slice(0, -1), msg('a-11', 'run-11', 'assistant', 'Updated result.', 24)] } })
      expect(screen.getByText('Updated result.')).toBeInTheDocument()
      expect(screen.getByRole('textbox')).toHaveValue('Continue with the next input.')
    } finally { presentation.mockRestore(); vi.useRealTimers() }
  })

  it('当前对话可打开命令面板，草稿和原对话不变', async () => {
    const onShowCommands = vi.fn()
    const conv = conversation()
    renderChat(conv, { ...NO_FOLDER, onShowCommands })
    const user = userEvent.setup()
    await user.type(screen.getByRole('textbox'), '等会继续这件事')
    await user.click(screen.getByRole('button', { name: '查看命令会话' }))
    expect(onShowCommands).toHaveBeenCalledOnce()
    expect(screen.getByRole('textbox')).toHaveValue('等会继续这件事')
    expect(conv.send).not.toHaveBeenCalled()
  })

  it('后台明确无过程的历史回复不再出现查询入口，有过程的单回复直接提供折叠入口', () => {
    const conv = conversation({ activeRun: { run_id: 'new', status: 'succeeded' } as never,
      messages: [msg('a1', 'plain', 'assistant', '你好。', 1), msg('a2', 'tools', 'assistant', '文件已检查。', 2)],
      runProcessSummaries: { plain: { has_process: false, tool_count: 0 }, tools: { has_process: true, tool_count: 1 } } })
    renderChat(conv, NO_FOLDER)
    expect(agentBubble('你好。').closest('.zm-turn')?.querySelector('.zm-process-group')).toBeNull()
    expect(within(agentBubble('你好。')).queryByRole('button', { name: '查看处理记录' })).toBeNull()
    expect(processFor('文件已检查。')).not.toBeNull()
    expect(conv.loadRunActivities).not.toHaveBeenCalled()
  })

  it('运行时参数说明留在请求详情，不把普通聊天包装成工具处理过程', () => {
    const conv = conversation({ activeRun: { run_id: 'r1', status: 'succeeded' } as never,
      messages: [msg('a1', 'r1', 'assistant', '你好。', 1)],
      runProcessSummaries: { r1: { has_process: false, tool_count: 0 } },
      activities: [{ activity_id: 'params', run_id: 'r1', sequence: 1, kind: 'reasoning', payload: { source: 'runtime', summary: '本次使用自动强度', content: '这描述的是发送参数，不是模型思考。' } }] as never })
    const view = renderChat(conv, NO_FOLDER)
    expect(view.container.querySelector('.zm-process-group')).toBeNull()
    expect(view.container.querySelector('.zm-request-details')).toHaveTextContent('这描述的是发送参数')
  })

  it('已绑定项目的工作区标签始终打开项目选择，不再切换文件面板', async () => {
    const { props } = renderChat(conversation())
    await userEvent.setup().click(screen.getByRole('button', { name: '切换工作区' }))
    expect(screen.getByRole('menuitem', { name: /不关联文件夹/ })).toBeInTheDocument()
    expect(props.onTogglePanel).not.toHaveBeenCalled()
  })

  it('侧栏收起时正在运行的对话也能从顶栏新建对话', async () => {
    const { props } = renderChat(conversation({ running: true, busy: true, activeRun: { run_id: 'r1', status: 'running' } as never }),
      { ...NO_FOLDER, sidebarCollapsed: true })
    const button = screen.getByRole('button', { name: T.agent.newConversation })
    expect(button).toBeEnabled()
    await userEvent.setup().click(button)
    expect(props.onNewConversation).toHaveBeenCalledOnce()
  })

  it.each([true, false])('过程落盘和任务结束保持用户选择%s，插话新段默认展开且互不影响', async (chosen) => {
    const first = msg('u1', 'r1', 'user', '检查配置', 1)
    const activities = [toolCall('r1', 1, 'read', 'read_file', { path: 'config.json' })] as AgentConversationState['activities']
    const conv = conversation({ messages: [first], activities, running: true, activeRun: { run_id: 'r1', status: 'running' } as never })
    const view = renderChat(conv, NO_FOLDER)
    const initial = view.container.querySelector('.zm-process-group') as HTMLDetailsElement
    expect(initial.open).toBe(true)
    openProcess(initial)
    if (chosen) openProcess(initial)
    const stage = msg('p1', 'r1', 'assistant', '读取后继续检查。', 2)
    view.rerenderWith({ ...NO_FOLDER, conversation: { ...conv, messages: [first, stage] } })
    expect((view.container.querySelector('.zm-process-group') as HTMLDetailsElement).open).toBe(chosen)
    const final = msg('a1', 'r1', 'assistant', '字段正确。', 3)
    view.rerenderWith({ ...NO_FOLDER, conversation: { ...conv, messages: [first, stage, final], running: false, activeRun: { run_id: 'r1', status: 'succeeded' } as never } })
    expect((view.container.querySelector('.zm-process-group') as HTMLDetailsElement).open).toBe(chosen)
    const steering = msg('u2', 'r1', 'user', '再看地址', 4)
    view.rerenderWith({ ...NO_FOLDER, conversation: { ...conv, messages: [first, stage, final, steering] } })
    const groups = view.container.querySelectorAll<HTMLDetailsElement>('.zm-process-group')
    expect(groups).toHaveLength(2)
    expect(groups[0].open).toBe(chosen)
    expect(groups[1].open).toBe(true)
  })

  it('历史处理记录读取失败后可直接重试，保留回答且不会自动重复请求', async () => {
    const load = vi.fn().mockRejectedValueOnce(new Error('暂时断线')).mockResolvedValue(undefined)
    const conv = conversation({ activeRun: { run_id: 'new', status: 'succeeded' } as never, loadRunActivities: load,
      messages: [msg('p1', 'old', 'assistant', '先检查文件。', 1), msg('a1', 'old', 'assistant', '文件无误。', 2)] })
    renderChat(conv, NO_FOLDER)
    openProcess(processFor('文件无误。'))
    const retry = await screen.findByRole('button', { name: '过程暂未读取，点击重试' })
    expect(load).toHaveBeenCalledTimes(1)
    expect(screen.getByText('文件无误。')).toBeInTheDocument()
    await userEvent.setup().click(retry)
    await waitFor(() => expect(load).toHaveBeenCalledTimes(2))
  })

  it('只有产物或技能事件也可展开查看，普通工具失败不占摘要或正文', () => {
    const activities = [{ activity_id: 'artifact', run_id: 'r1', sequence: 1, kind: 'artifact',
      payload: { artifact_id: 'report', artifact_type: 'document', name: '核对报告' } },
    { activity_id: 'skill', run_id: 'r1', sequence: 2, kind: 'skill_call', payload: { skill_call_id: 's1', skill_id: '文档核对', status: 'failed', output: '服务暂时离线' } }] as AgentConversationState['activities']
    const view = renderChat(conversation({ activities, activeRun: { run_id: 'r1', status: 'succeeded' } as never,
      messages: [msg('a1', 'r1', 'assistant', '已整理可用内容。', 1)] }), NO_FOLDER)
    const disclosure = processFor('已整理可用内容。')
    expect(disclosure.open).toBe(false)
    expect(disclosure.querySelector('summary')).toHaveTextContent('查看处理过程')
    expect(disclosure.querySelector('summary')).not.toHaveTextContent('失败')
    expect(agentBubble('已整理可用内容。').querySelector('.zm-step')).toBeNull()
    openProcess(disclosure)
    expect(view.container.querySelectorAll('.zm-step')).toHaveLength(2)
    expect(view.container.querySelector('.zm-step.is-failed')).toBeNull()
    expect(screen.queryByText(/共 \d+ 字|过程说明/)).toBeNull()
  })

  it('运行中的处理过程默认展开且只出现一份，流式正文继续可见', () => {
    const activities = [{ activity_id: 'a1', run_id: 'r1', sequence: 1, kind: 'message', payload: { message_id: 'p1', role: 'assistant', phase: 'completed', finish_reason: 'tool_calls' } },
      toolCall('r1', 2, 'read', 'read_file', { path: 'config.json' })] as AgentConversationState['activities']
    const view = renderChat(conversation({ activities, running: true, streamingContent: '配置已读取，正在核对。', activeRun: { run_id: 'r1', status: 'running' } as never,
      messages: [msg('p1', 'r1', 'assistant', '先读取文件。', 1)] }), NO_FOLDER)
    const disclosure = view.container.querySelector('.zm-process-group') as HTMLDetailsElement
    expect(view.container.querySelectorAll('.zm-process-group')).toHaveLength(1)
    expect(disclosure.open).toBe(true)
    expect(disclosure.querySelector('summary')).toHaveTextContent('config.json')
    expect(screen.getByText('先读取文件。').closest('.zm-process-content')).not.toBeNull()
    expect(screen.getByText('配置已读取，正在核对。').closest('.zm-process-content')).toBeNull()
  })

  it.each(['running', 'cancelled'])('插话后的%s尾段只显示后续过程，插话前工具仍保留且不重复', (status) => {
    const messages = [msg('u1', 'r1', 'user', '检查配置', 1), msg('p1', 'r1', 'assistant', '先读配置。', 2), msg('u2', 'r1', 'user', '补充检查地址', 3)]
    const activities = [
      { activity_id: 'm-p1', run_id: 'r1', sequence: 1, kind: 'message', payload: { role: 'assistant', message_id: 'p1', phase: 'completed', finish_reason: 'tool_calls' } },
      toolCall('r1', 2, 'read', 'read_file', { path: 'config.json' }), toolResult('r1', 3, 'read'),
      { activity_id: 'm-u2', run_id: 'r1', sequence: 4, kind: 'message', payload: { role: 'user', message_id: 'u2', phase: 'completed' } },
      { activity_id: 'new-thought', run_id: 'r1', sequence: 5, kind: 'reasoning', payload: { block_id: 'new-thought', phase: status === 'running' ? 'delta' : 'interrupted',
        delta: status === 'running' ? '正在核对地址' : null, content: status === 'cancelled' ? '正在核对地址' : null, summary: '正在核对地址', visibility: 'full' } },
    ] as AgentConversationState['activities']
    const view = renderChat(conversation({ messages, activities, running: status === 'running', activeRun: { run_id: 'r1', status, objective: '检查配置', error: null } as never }), NO_FOLDER)
    for (const details of view.container.querySelectorAll<HTMLDetailsElement>('.zm-process-group')) if (!details.open) openProcess(details)
    const boundary = screen.getByRole('group', { name: '补充指令前的处理过程' })
    expect(boundary.querySelectorAll('.zm-step')).toHaveLength(1)
    const tail = [...view.container.querySelectorAll('.zm-process-group')].at(-1)!
    expect(tail.querySelectorAll('.zm-steps')).toHaveLength(0)
    expect(tail.querySelector('.is-thinking')).not.toBeNull()
    expect(tail.compareDocumentPosition(screen.getByText('补充检查地址')) & Node.DOCUMENT_POSITION_PRECEDING).not.toBe(0)
  })

  it('同一轮用户插话前的工具留在插话前，之后的思考和回答不重复前段过程', () => {
    const messages: AgentConversationState['messages'] = [msg('u1', 'r1', 'user', '检查配置文件', 1), msg('p1', 'r1', 'assistant', '先读取配置。', 2),
      msg('u2', 'r1', 'user', '补充：只检查地址字段', 3), msg('a1', 'r1', 'assistant', '地址字段正确。', 4)]
    const completed = (id: string, role: 'user' | 'assistant', sequence: number, reason?: string) => ({ activity_id: `m-${id}`, run_id: 'r1', sequence, kind: 'message',
      payload: { message_id: id, role, phase: 'completed', finish_reason: reason, content: messages.find((message) => message.message_id === id)?.content } })
    const activities = [completed('u1', 'user', 1), completed('p1', 'assistant', 2, 'tool_calls'),
      toolCall('r1', 3, 'read-config', 'read_file', { path: 'config.json' }), toolResult('r1', 4, 'read-config'),
      completed('u2', 'user', 5), { activity_id: 'thought-new', run_id: 'r1', sequence: 6, kind: 'reasoning', payload: { block_id: 'after-steer', phase: 'completed', content: '现在核对地址字段', summary: '现在核对地址字段', visibility: 'full' } },
      completed('a1', 'assistant', 7, 'stop')] as AgentConversationState['activities']
    const view = renderChat(conversation({ messages, activities, activeRun: { run_id: 'r1', status: 'succeeded' } as never }), NO_FOLDER)
    for (const details of view.container.querySelectorAll<HTMLDetailsElement>('.zm-process-group')) openProcess(details)
    const boundary = screen.getByLabelText('补充指令前的处理过程')
    const user = screen.getByText('补充：只检查地址字段')
    expect(boundary.compareDocumentPosition(user) & Node.DOCUMENT_POSITION_FOLLOWING).not.toBe(0)
    expect(agentBubble('地址字段正确。').querySelector('.zm-steps')).toBeNull()
    expect(agentBubble('地址字段正确。').closest('.zm-turn')).not.toHaveClass('is-response-continuation')
    expect(boundary.querySelectorAll('.zm-step')).toHaveLength(1)
    expect(view.container.querySelectorAll('.zm-turn-process .is-thinking')).toHaveLength(1)
  })

  it('旧历史无逐消息事件边界时整轮工具只展示一次，分页首条可加载过程', () => {
    const messages = [msg('m20', 'old', 'assistant', '当前页第一段过程。', 20), msg('m21', 'old', 'assistant', '当前页回复。', 21)]
    const conv = conversation({ messages, hasEarlierMessages: true, activeRun: { run_id: 'new', status: 'succeeded' } as never })
    const view = renderChat(conv, NO_FOLDER)
    expect(view.container.querySelectorAll('.zm-process-group')).toHaveLength(1)
    view.rerenderWith({ ...NO_FOLDER, conversation: { ...conv, pastActivities: { old: [toolCall('old', 1, 'read', 'read_file', { path: 'README.md' }), toolResult('old', 2, 'read')] as never } } })
    openProcess(processFor('当前页回复。'))
    expect(view.container.querySelectorAll('.zm-step')).toHaveLength(1)
    expect(agentBubble('当前页回复。').querySelector('.zm-step')).toBeNull()
  })

  it('同一轮连续过程紧凑相连，历史步骤只加载一次且各段原文和动作仍在', async () => {
    const conv = conversation({ activeRun: { run_id: 'newer', status: 'succeeded' } as never, messages: [
      msg('u1', 'earlier', 'user', '检查配置', 1),
      msg('p1', 'earlier', 'assistant', '先看配置文件。', 2),
      msg('p2', 'earlier', 'assistant', '配置读到了，接着核对字段。', 3),
      msg('a1', 'earlier', 'assistant', '配置检查完成。', 4),
    ] })
    const view = renderChat(conv, NO_FOLDER)
    expect(view.container.querySelectorAll('.zm-process-group')).toHaveLength(1)
    const disclosure = processFor('配置检查完成。')
    expect(disclosure.open).toBe(false)
    expect(screen.queryByText('先看配置文件。')).toBeNull()
    openProcess(disclosure)
    const first = agentBubble('先看配置文件。'), second = agentBubble('配置读到了，接着核对字段。'), answer = agentBubble('配置检查完成。')
    expect(first).toHaveClass('is-progress')
    expect(second.closest('.zm-process-group')).toBe(disclosure)
    expect(answer.closest('.zm-process-group')).toBeNull()
    expect(first.querySelector('.zm-msg-foot')).toBeNull()
    expect(within(first).getByRole('button', { name: T.agent.copyReply })).toBeInTheDocument()
    expect(within(second).getByRole('button', { name: T.agent.forkConversationTitle })).toBeInTheDocument()
    expect(screen.queryByText('过程说明')).toBeNull()
    await waitFor(() => expect(conv.loadRunActivities).toHaveBeenCalledTimes(1))
    expect(conv.loadRunActivities).toHaveBeenCalledWith('earlier')
    const boundary = (id: string, sequence: number) => ({ activity_id: `phase-${id}`, run_id: 'earlier', sequence, kind: 'message',
      payload: { message_id: id, role: 'assistant', phase: 'completed' } })
    view.rerenderWith({ ...NO_FOLDER, conversation: { ...conv, pastActivities: { earlier: [boundary('p1', 1),
      toolCall('earlier', 2, 'read', 'read_file', { path: 'config.json' }), toolResult('earlier', 3, 'read'), boundary('p2', 4), boundary('a1', 5)] as never } } })
    expect(screen.queryByText(T.agent.runStepsNone)).toBeNull()
    expect(disclosure.querySelectorAll('.zm-step')).toHaveLength(1)
  })

  it('没有文字回复的旧任务仍能按需查看已取消的工具记录', async () => {
    const conv = conversation({ activeRun: { run_id: 'newer', status: 'succeeded' } as never, messages: [
      msg('u1', 'stopped', 'user', '运行后取消', 1),
      msg('u2', 'newer', 'user', '继续检查', 2),
      msg('a2', 'newer', 'assistant', '后续检查完成。', 3),
    ], runProcessSummaries: { stopped: { has_process: true, tool_count: 1 }, newer: { has_process: false, tool_count: 0 } } })
    const view = renderChat(conv, NO_FOLDER)
    const originalTurn = view.container.querySelector('[data-message-id="u1"]')!
    const disclosure = originalTurn.querySelector('.zm-process-group') as HTMLDetailsElement
    expect(disclosure).not.toBeNull()
    expect(disclosure.open).toBe(false)
    expect(conv.loadRunActivities).not.toHaveBeenCalled()
    openProcess(disclosure)
    await waitFor(() => expect(conv.loadRunActivities).toHaveBeenCalledWith('stopped'))
    view.rerenderWith({ conversation: { ...conv, pastActivities: { stopped: [
      toolCall('stopped', 2, 'command', 'computer_run_command', { command: 'echo heartbeat' }),
      { ...toolResult('stopped', 3, 'command'), payload: { tool_call_id: 'command', status: 'cancelled' } },
    ] as AgentConversationState['activities'] } } })
    expect(originalTurn.querySelectorAll('.zm-step')).toHaveLength(1)
    expect(originalTurn.querySelector('[data-outcome="cancelled"]')).not.toBeNull()
    expect(agentBubble('后续检查完成。').querySelector('.zm-step')).toBeNull()
  })

  it('同轮多段回答只显示一次合计，切会话不带走用量', () => {
    const usage = { scope: 'main_and_tool_compaction' as const,
      main: { requests: 1, completed: 1, usage: { input_tokens: 100, output_tokens: 10 } },
      compaction: { requests: 1, completed: 1, usage: { input_tokens: 20, output_tokens: 2 } },
      total: { input_tokens: 120, output_tokens: 12 } }
    const extra = { run_usage: usage }
    const messages = [msg('u1', 'r1', 'user', '检查文件', 1, extra), msg('a1', 'r1', 'assistant', '第一段', 2, extra),
      msg('u2', 'r1', 'user', '补充日期', 3, extra), msg('a2', 'r1', 'assistant', '完成', 4, extra)]
    const view = renderChat(conversation({ messages }), NO_FOLDER)
    expect(screen.getAllByText('输入 120 token')).toHaveLength(1)
    view.rerenderWith({ conversation: conversation({ session: { session_id: 's2' } as never, messages: [] }) })
    expect(screen.queryByText('输入 120 token')).toBeNull()
  })

  it('失败且没有最终回答时仍显示用量，历史无回复和阶段回复也能查看', () => {
    const usage = { scope: 'main_and_tool_compaction' as const,
      main: { requests: 1, completed: 1, usage: { input_tokens: 100 } },
      compaction: { requests: 1, completed: 0, usage: null },
      total: { input_tokens: 100, input_tokens_reported: false } }
    const user = msg('u1', 'r1', 'user', '检查文件', 1, { run_usage: usage })
    const conv = conversation({ messages: [user], activeRun: { run_id: 'r1', status: 'cancelled', objective: '检查文件' } as never })
    const view = renderChat(conv, NO_FOLDER)
    expect(screen.getAllByText('输入 100 token（部分上报）')).toHaveLength(1)
    view.rerenderWith({ conversation: { ...conv, activeRun: null } })
    expect(screen.getAllByText('输入 100 token（部分上报）')).toHaveLength(1)
    view.rerenderWith({ conversation: { ...conv,
      messages: [user, msg('a1', 'r1', 'assistant', '先读取文件。', 2, { run_usage: usage })],
      activities: [{ activity_id: 'act1', run_id: 'r1', sequence: 1, kind: 'message', payload: {
        role: 'assistant', phase: 'completed', message_id: 'a1', content: '先读取文件。', finish_reason: 'tool_calls',
      } }] as AgentConversationState['activities'],
    } })
    expect(screen.getAllByText('输入 100 token（部分上报）')).toHaveLength(1)
  })

  it('没有回复的旧任务修改卡只挂在原任务下，未加载原消息时仍可查看', async () => {
    localStorage.setItem('zhimeng.desk.change-cards:s1', JSON.stringify([
      { changeSetId: 'cs-stopped', runId: 'stopped', status: 'applied' },
    ]))
    await changeSetMock.getChangeSet.withImplementation(async (id) => ({
      change_set_id: id, title: '整理采购记录', summary: '', origin: 'agent', status: 'applied',
      review_revision: 1, base_revision: 3, changes: [{ relative_path: 'review.md' }], writeback: null,
    }), async () => {
      const originalMessages = [msg('u1', 'stopped', 'user', '整理采购记录', 1),
        msg('u2', 'stopped', 'user', '补充日期', 2)]
      const conv = conversation({ activeRun: { run_id: 'newer', status: 'succeeded' } as never,
        messages: [...originalMessages, msg('u3', 'newer', 'user', '继续检查', 3),
          msg('a3', 'newer', 'assistant', '后续检查完成。', 4)] })
      const view = renderChat(conv)
      await waitFor(() => expect(screen.getAllByRole('button', { name: T.agent.changeRollback })).toHaveLength(1))
      expect(view.container.querySelector('[data-message-id="u2"] .zm-change')).not.toBeNull()
      expect(view.container.querySelector('[data-message-id="u1"] .zm-change')).toBeNull()
      view.rerenderWith({ conversation: { ...conv, messages: conv.messages.slice(2), hasEarlierMessages: true } })
      expect(screen.getAllByRole('button', { name: T.agent.changeRollback })).toHaveLength(1)
      view.rerenderWith({ conversation: { ...conv, messages: originalMessages,
        activeRun: { run_id: 'stopped', status: 'cancelled' } as never } })
      expect(screen.getAllByRole('button', { name: T.agent.changeRollback })).toHaveLength(1)
    })
  })

  it('运行中的最后一段和停止前的阶段说明仍是过程，不标作最终结果', () => {
    const messages = [msg('u1', 'r1', 'user', '检查文件', 1), msg('p1', 'r1', 'assistant', '先读取文件。', 2)]
    const activities = [{ activity_id: 'a1', run_id: 'r1', sequence: 1, kind: 'message',
      payload: { role: 'assistant', phase: 'completed', message_id: 'p1', content: '先读取文件。', finish_reason: 'tool_calls' } }] as AgentConversationState['activities']
    const conv = conversation({ messages, activities, running: true, activeRun: { run_id: 'r1', status: 'running' } as never })
    const view = renderChat(conv, NO_FOLDER)
    expect(agentBubble('先读取文件。')).toHaveClass('is-progress')
    expect(screen.queryByText('回复')).toBeNull()
    view.rerenderWith({ ...NO_FOLDER, conversation: { ...conv, running: false, activeRun: { run_id: 'r1', status: 'cancelled', objective: '检查文件', error: null } as never } })
    expect(agentBubble('先读取文件。')).toHaveClass('is-progress')
    expect(screen.queryByText('回复')).toBeNull()
    expect(screen.queryByText('最终回答')).toBeNull()
  })

  it('后台正在准备队列下一项时按回车保留草稿，不抢先普通发送', async () => {
    queues.set('s1', { session_id: 's1', revision: 1, pause_reason: null, active_run_id: null,
      items: [{ item_id: 'queued-first', message: '先处理队列消息', writer_mode: 'repair_only', project_id: 'p1', sending: false }] } as QueueState)
    const conv = conversation({ activeRun: { run_id: 'previous', session_id: 's1', status: 'succeeded' } as never,
      activities: [{ activity_id: 'command', session_id: 's1', run_id: 'previous', kind: 'tool_call', sequence: 1,
        payload: { tool_call_id: 'cmd', tool_name: 'computer_run_command', arguments: {} } }] as never })
      renderChat(conv)
      await waitFor(() => expect(screen.getByText(T.agent.queue.count(1))).toBeInTheDocument())
      const user = userEvent.setup()
      const input = screen.getByPlaceholderText(T.agent.placeholder)
      await user.type(input, '等会再处理这份草稿{Enter}')
      expect(input).toHaveValue('等会再处理这份草稿')
      expect(conv.send).not.toHaveBeenCalled()
      expect(screen.getByRole('button', { name: T.agent.send })).toBeDisabled()
      expect(screen.getByRole('button', { name: T.agent.queue.pause })).toBeEnabled()
      expect(queueApi.dispatchQueuedMessage).not.toHaveBeenCalled()
      queues.set('s1', { session_id: 's1', revision: 2, pause_reason: null, active_run_id: 'queued-run', items: [] })
      act(() => window.dispatchEvent(new Event('focus')))
      await waitFor(() => expect(conv.refresh).toHaveBeenCalled())
      expect(input).toHaveValue('等会再处理这份草稿')
      expect(conv.send).not.toHaveBeenCalled()
  })

  it('历史查到运行中任务但发送回执仍未知时，仍可核对和原子取回，重试等当前任务结束', async () => {
    const conv = conversation({ running: true, busy: true, contextState: 'idle',
      activeRun: { run_id: 'running-task', status: 'running', session_id: 's1' } as never,
      checkPendingSend: vi.fn(async () => {}), withdrawPendingSend: vi.fn(async () => null),
      pendingSend: { status: 'unknown', item: { version: 1, sessionId: 's1', key: 'unknown-key', createdAt: '', provider: null,
        request: { message: '受理状态待核对的任务', writer_mode: 'repair_only' } } } })
    renderChat(conv, NO_FOLDER)
    const user = userEvent.setup()
    const pending = screen.getByRole('region', { name: '发送结果待确认' })
    expect(within(pending).getByRole('button', { name: '核对发送结果' })).toBeEnabled()
    expect(within(pending).getByRole('button', { name: '取回原文重新编辑' })).toBeEnabled()
    expect(within(pending).getByRole('button', { name: '重试这条' })).toBeDisabled()
    await user.click(within(pending).getByRole('button', { name: '核对发送结果' }))
    await user.click(within(pending).getByRole('button', { name: '取回原文重新编辑' }))
    expect(conv.checkPendingSend).toHaveBeenCalledOnce()
    expect(conv.withdrawPendingSend).toHaveBeenCalledOnce()
  })

  it('内部命令接续回答标真实来源，继续核对只带完成事实来源，不重发启动命令的旧用户话', async () => {
    const conv = conversation({ messages: [msg('u1', 'r1', 'user', '启动脚本处理表格', 1), msg('a1', 'r1', 'assistant', '脚本在后台运行。', 2), msg('a2', 'r2', 'assistant', '脚本结束，汇总已核对。', 3)],
      activeRun: { run_id: 'r2', session_id: 's1', status: 'succeeded', origin_kind: 'command_completion' } as never,
      runOrigins: { r1: 'user', r2: 'command_completion' },
      continuation: { run_id: 'r2', can_continue: true, objective: '启动脚本处理表格' } as never,
      prepareContinuation: vi.fn(async () => ({ run_id: 'r2', can_continue: true, objective: '启动脚本处理表格' } as never)) })
    renderChat(conv, NO_FOLDER)
    const user = userEvent.setup()
    await user.type(screen.getByPlaceholderText(T.agent.placeholderCasual), '留下的草稿')
    expect(screen.getByText('命令结束后继续')).toBeInTheDocument()
    expect(within(agentBubble('脚本结束，汇总已核对。')).queryByRole('button', { name: T.agent.regenerate })).toBeNull()
    await user.click(screen.getByRole('button', { name: '继续核对结果' }))
    await waitFor(() => expect(conv.send).toHaveBeenCalledWith(expect.objectContaining({ text: '继续核对这次命令的结果。', continuationSourceRunId: 'r2' })))
    expect(screen.getByPlaceholderText(T.agent.placeholderCasual)).toHaveValue('留下的草稿')
    expect(conv.messages.filter((message) => message.role === 'user')).toHaveLength(1)
  })

  it('发送身份未确认时原技能随原文留在发送记录，新附技能和新草稿保持', async () => {
    userSkillsMock.readUserSkills.mockResolvedValueOnce({ schema: 'zhimeng.user-skills.v1', skills_root: 'C:/skills', exists: true, total: 2, enabled: 2,
      skills: ['文档整理', '表格校验'].map((name) => ({ name, description: '', folder: name, has_scripts: false, enabled: true, origin: 'project' })) })
    let reject!: (error: Error) => void
    const conv = conversation({ send: vi.fn(() => new Promise<void>((_resolve, fail) => { reject = fail })) })
    renderChat(conv, NO_FOLDER)
    const user = userEvent.setup()
    await user.click(screen.getByRole('button', { name: '选择技能' }))
    await user.click(await screen.findByRole('button', { name: '文档整理' }))
    await user.type(screen.getByPlaceholderText(T.agent.placeholderCasual), '整理说明')
    await user.click(screen.getByRole('button', { name: T.agent.send }))
    await user.click(screen.getByRole('button', { name: '选择技能' }))
    await user.click(screen.getByRole('button', { name: '表格校验' }))
    await user.type(screen.getByPlaceholderText(T.agent.placeholderCasual), '接着核对表格')
    await act(async () => reject(new AgentSendUnconfirmedError('原消息及技能已保留')))
    expect(conv.send).toHaveBeenCalledWith(expect.objectContaining({ skillNames: ['文档整理'] }))
    expect(screen.queryByRole('button', { name: T.agent.skillTagRemove('文档整理') })).not.toBeInTheDocument()
    expect(screen.getByRole('button', { name: T.agent.skillTagRemove('表格校验') })).toBeInTheDocument()
    expect(screen.getByPlaceholderText(T.agent.placeholderCasual)).toHaveValue('接着核对表格')
    expect(screen.queryByRole('region', { name: '待恢复消息' })).toBeNull()
  })

  it('取回未受理消息先等后台确认，再追加到当前草稿，保留新输入', async () => {
    let finish!: (text: string | null) => void
    const conv = conversation({ withdrawPendingSend: vi.fn(() => new Promise<string | null>((resolve) => { finish = resolve })),
      pendingSend: { status: 'unknown', item: { version: 1, sessionId: 's1', key: 'retry-key', createdAt: '', provider: null,
        request: { message: '取回的原文', writer_mode: 'repair_only' } } } })
    renderChat(conv, NO_FOLDER)
    const user = userEvent.setup()
    const input = screen.getByPlaceholderText(T.agent.placeholderCasual)
    await user.type(input, '新草稿')
    await user.click(screen.getByRole('button', { name: '取回原文重新编辑' }))
    expect(input).toHaveValue('新草稿')
    await act(async () => finish('取回的原文'))
    expect(input).toHaveValue('新草稿\n\n取回的原文')
  })

  it('取回未受理技能消息同时恢复它的技能标签，不覆盖后来选择', async () => {
    userSkillsMock.readUserSkills.mockResolvedValueOnce({ schema: 'zhimeng.user-skills.v1', skills_root: 'C:/skills', exists: true, total: 2, enabled: 2,
      skills: ['文档整理', '表格核对'].map((name) => ({ name, description: '', folder: name, has_scripts: false, enabled: true, origin: 'project' })) })
    const conv = conversation({ withdrawPendingSend: vi.fn(async () => '原技能任务'), pendingSend: { status: 'not_found', item: { version: 1, sessionId: 's1', key: 'skill-pending', createdAt: '', provider: null, request: { message: '原技能任务', writer_mode: 'repair_only', manual_skill_names: ['文档整理'] } } } })
    renderChat(conv, NO_FOLDER)
    const user = userEvent.setup()
    await user.click(screen.getByRole('button', { name: '选择技能' }))
    await user.click(await screen.findByRole('button', { name: '表格核对' }))
    await user.click(screen.getByRole('button', { name: '取回原文重新编辑' }))
    expect(screen.getByRole('button', { name: T.agent.skillTagRemove('文档整理') })).toBeInTheDocument()
    expect(screen.getByRole('button', { name: T.agent.skillTagRemove('表格核对') })).toBeInTheDocument()
  })

  it('未确认发送由发送记录保管，保持新草稿且不另造待恢复消息', async () => {
    let reject!: (error: Error) => void
    const conv = conversation({ send: vi.fn(() => new Promise<void>((_resolve, fail) => { reject = fail })) })
    renderChat(conv, NO_FOLDER)
    const user = userEvent.setup()
    const input = screen.getByPlaceholderText(T.agent.placeholderCasual)
    await user.type(input, '原始任务{Enter}')
    await user.type(input, '我新写的草稿')
    await act(async () => reject(new AgentSendUnconfirmedError('发送结果尚未确认，原内容已保留')))
    expect(input).toHaveValue('我新写的草稿')
    expect(screen.queryByRole('region', { name: '待恢复消息' })).toBeNull()
  })

  it('未确认发送展示独立核对和重试，操作不占用当前草稿也不放行排队', async () => {
    queues.set('s1', { session_id: 's1', revision: 1, pause_reason: null, active_run_id: null,
      items: [{ item_id: 'later', message: '后一条队列消息', writer_mode: 'repair_only' }] } as never)
    const conv = conversation({ checkPendingSend: vi.fn(async () => {}), retryPendingSend: vi.fn(async () => {}),
      pendingSend: { status: 'not_found', item: { version: 1, sessionId: 's1', key: 'same-key', createdAt: '', provider: null,
        request: { message: '待确认原文', writer_mode: 'repair_only' } } } })
    renderChat(conv, NO_FOLDER)
    const user = userEvent.setup()
    const input = screen.getByPlaceholderText(T.agent.placeholderCasual)
    await user.type(input, '新的草稿')
    const pending = screen.getByRole('region', { name: '发送结果待确认' })
    expect(within(pending).getByText('待确认原文')).toBeInTheDocument()
    await user.click(within(pending).getByRole('button', { name: '核对发送结果' }))
    await user.click(within(pending).getByRole('button', { name: '重试这条' }))
    expect(conv.checkPendingSend).toHaveBeenCalledOnce()
    expect(conv.retryPendingSend).toHaveBeenCalledOnce()
    expect(input).toHaveValue('新的草稿')
    expect(queueApi.dispatchQueuedMessage).not.toHaveBeenCalled()
  })

  it('工具前进度已存成历史，停止后仍保留后续仅出了一半的正文', () => {
    renderChat(conversation({ messages: [msg('m1', 'r1', 'user', '整理资料', 1), msg('m2', 'r1', 'assistant', '我先读取资料', 2)],
      streamingContent: '读到的第一项是', activeRun: { run_id: 'r1', status: 'cancelled', objective: '整理资料', error: null } as never,
      activities: [{ activity_id: 'a1', run_id: 'r1', sequence: 1, kind: 'message', payload: { role: 'assistant', phase: 'completed', message_id: 'm2', content: '我先读取资料' } }] as never,
    }), NO_FOLDER)
    expect(screen.getAllByText('我先读取资料')).toHaveLength(1)
    expect(screen.getByText('读到的第一项是')).toBeInTheDocument()
    expect(screen.queryByText('等待模型回复…')).toBeNull()
  })

  it('保留挂载的聊天在技能库变更后更新技能选择，迟到的旧清单不能盖回', async () => {
    const fresh = {
      schema: 'zhimeng.user-skills.v1', skills_root: 'C:/skills', exists: true, total: 1, enabled: 1,
      skills: [{ name: '表格整理', description: '整理表格', folder: 'sheet', has_scripts: false, enabled: true, origin: 'project' }],
    }
    let finish!: (value: typeof fresh) => void
    userSkillsMock.readUserSkills.mockImplementationOnce(() => new Promise((resolve) => { finish = resolve }))
    renderChat(conversation(), NO_FOLDER)
    act(() => window.dispatchEvent(new CustomEvent('zhimeng:skills-changed', { detail: { view: fresh } })))
    fireEvent.change(screen.getByPlaceholderText(T.agent.placeholderCasual), { target: { value: '/' } })
    expect(await screen.findByRole('option', { name: /表格整理/ })).toBeInTheDocument()
    await act(async () => finish({ ...fresh, skills: [{ ...fresh.skills[0], name: '旧技能' }] }))
    expect(screen.queryByRole('option', { name: /旧技能/ })).not.toBeInTheDocument()
    expect(screen.getByRole('option', { name: /表格整理/ })).toBeInTheDocument()
  })

  it('技能库只通知已更改时，聊天重新读取可用技能', async () => {
    renderChat(conversation(), NO_FOLDER)
    await waitFor(() => expect(userSkillsMock.readUserSkills).toHaveBeenCalledTimes(1))
    userSkillsMock.readUserSkills.mockResolvedValueOnce({
      schema: 'zhimeng.user-skills.v1', skills_root: 'C:/skills', exists: true, total: 1, enabled: 1,
      skills: [{ name: '文档整理', description: '整理文档', folder: 'docs', has_scripts: false, enabled: true, origin: 'project' }],
    })
    act(() => window.dispatchEvent(new CustomEvent('zhimeng:skills-changed')))
    fireEvent.change(screen.getByPlaceholderText(T.agent.placeholderCasual), { target: { value: '/' } })
    expect(await screen.findByRole('option', { name: /文档整理/ })).toBeInTheDocument()
    expect(userSkillsMock.readUserSkills).toHaveBeenCalledTimes(2)
  })

  it('已附上的技能被停用后移除标签，发送时不再带旧技能', async () => {
    const conv = conversation()
    renderChat(conv, NO_FOLDER)
    const user = userEvent.setup()
    const textarea = screen.getByPlaceholderText(T.agent.placeholderCasual)
    await user.type(textarea, '/')
    await user.click(await screen.findByRole('option', { name: /网文知识库/ }))
    expect(screen.getByRole('button', { name: T.agent.skillTagRemove('网文知识库') })).toBeInTheDocument()
    act(() => window.dispatchEvent(new CustomEvent('zhimeng:skills-changed', { detail: { view: { skills: [] } } })))
    expect(screen.queryByRole('button', { name: T.agent.skillTagRemove('网文知识库') })).not.toBeInTheDocument()
    await user.type(textarea, '整理文档{Enter}')
    await waitFor(() => expect(conv.send).toHaveBeenCalledWith(expect.objectContaining({ text: '整理文档', skillNames: undefined })))
  })

  it('工作区入口固定在顶栏，当前模型和思考强度直接可见', async () => {
    renderChat(conversation(), { openFileLabel: CHAPTER_4 })
    expect(screen.getByTitle(T.agent.target.openFile('宗门七骗子', CHAPTER_4))).toBeInTheDocument()
    const model = screen.getByRole('combobox', { name: T.agent.modelPick })
    expect(model).toHaveTextContent('deepseek-v4-pro')
    expect(model.textContent).not.toContain('[')
    expect(screen.getByTitle(T.agent.effortPick)).toHaveTextContent(effortOptionLabel('auto'))
  })

  it('开着文件夹发消息：完全访问按能改文件发出去', async () => {
    const conv = conversation()
    renderChat(conv)
    const user = userEvent.setup()
    await user.type(screen.getByPlaceholderText(T.agent.placeholder), '把第 5 章开头改得更有画面感')
    await user.click(screen.getByRole('button', { name: T.agent.send }))
    await waitFor(() => expect(conv.send).toHaveBeenCalledWith(expect.objectContaining({ text: '把第 5 章开头改得更有画面感', writerMode: 'author_review_change_proposal' })))
  })

  it('输入区直接显示当前容量，不把配置容量当已用上下文', async () => {
    renderChat(conversation(), NO_FOLDER)
    expect(screen.getByText('256K')).toBeInTheDocument()
    expect(document.body.textContent).not.toContain('预估')
  })

  it('首页入口连接文件夹、记忆和技能，不发送预设任务', async () => {
    const conv = conversation()
    const onSkills = vi.fn()
    const { props } = renderChat(conv, { ...NO_FOLDER, onOpenSkillsSettings: onSkills })
    const user = userEvent.setup()
    await user.click(screen.getByRole('button', { name: A.home.open }))
    await user.click(screen.getByRole('button', { name: A.home.create }))
    await user.click(screen.getByRole('button', { name: A.home.memory }))
    await user.click(screen.getByRole('button', { name: A.home.skills }))
    expect(props.onOpenFolder).toHaveBeenCalledOnce()
    expect(props.onNewFolder).toHaveBeenCalledOnce()
    expect(props.onOpenMemorySettings).toHaveBeenCalledOnce()
    expect(onSkills).toHaveBeenCalledOnce()
    expect(conv.send).not.toHaveBeenCalled()
  })

  it('回车发送，Shift+Enter 换行，不提供应用级发送快捷键设置', async () => {
    const conv = conversation()
    renderChat(conv, NO_FOLDER)
    const user = userEvent.setup()
    await user.type(screen.getByRole('textbox'), '整理工作记录')
    await user.keyboard('{Shift>}{Enter}{/Shift}')
    expect(conv.send).not.toHaveBeenCalled()
    expect(screen.getByRole('textbox')).toHaveValue('整理工作记录\n')
    await user.keyboard('{Enter}')
    await waitFor(() => expect(conv.send).toHaveBeenCalledWith(expect.objectContaining({ text: '整理工作记录' })))
  })

  it.each([630, 0, 1000])('缓存用量%i直接显示真实比例，原数仍可查', (cached) => {
    renderChat(conversation({ messages: [msg('m1', 'r1', 'user', '问一句', 1), msg('m2', 'r1', 'assistant', '答一句。', 2, { usage: { input_tokens: 1000, cached_input_tokens: cached } })] }), NO_FOLDER)
    const details = screen.getByText('请求详情').closest('details')!
    expect(details.open).toBe(false)
    expect(screen.queryByText(/共 \d+ 字/)).toBeNull()
    expect(screen.getByLabelText('本轮模型用量')).toHaveTextContent(`缓存命中 ${cached / 10}%`)
    fireEvent.click(details.querySelector('summary')!)
    expect(within(details).getByText('输入 token')).toBeInTheDocument()
    expect([...details.querySelectorAll('dd')].map((node) => node.textContent)).toEqual(expect.arrayContaining(['1000', String(cached)]))
  })

  it.each([
    null,
    { input_tokens: 1000 },
    { input_tokens: 1000, cached_input_tokens: null },
    { input_tokens: 0, cached_input_tokens: 0 },
    { input_tokens: 1000, cached_input_tokens: 1001 },
    { input_tokens: 1000, cached_input_tokens: -1 },
  ])('没有报告或无效的缓存用量不伪装成零命中：%j', (usage) => {
    renderChat(conversation({ messages: [msg('m1', 'r1', 'user', '问一句', 1), msg('m2', 'r1', 'assistant', '答一句。', 2, { usage })] }), NO_FOLDER)
    expect(screen.queryByText(/缓存命中/)).toBeNull()
  })

  it('没打开文件夹时顶上明说，回车直接发；新对话中间只有一句话，没有预设话题', async () => {
    const conv = conversation()
    renderChat(conv, NO_FOLDER)
    expect(screen.getByRole('button', { name: '切换工作区' })).toHaveTextContent('自由对话')
    expect(screen.getByRole('heading', { name: A.chat.emptyTitle })).toBeInTheDocument()
    const user = userEvent.setup()
    await user.type(screen.getByPlaceholderText(T.agent.placeholderCasual), '帮我起五个书名')
    await user.keyboard('{Enter}')
    await waitFor(() => expect(conv.send).toHaveBeenCalledWith(expect.objectContaining({ text: '帮我起五个书名' })))
  })

  it('没打开文件夹时输入框下面那一格：挑打开过的项目 = 挪进去，另两项去选 / 建文件夹', async () => {
    const projects = [{ projectId: 'b1', name: '宗门', rootPath: 'D:\\宗门', conversations: [], latest: null }]
    const { props } = renderChat(conversation(), { ...NO_FOLDER, projects })
    const user = userEvent.setup()
    const chip = screen.getByRole('button', { name: '切换工作区' })
    await user.click(chip)
    await user.click(within(screen.getByRole('menu')).getByRole('menuitem', { name: '宗门' }))
    expect(props.onRebindProject).toHaveBeenCalledWith('b1')
    await user.click(chip)
    await user.click(within(screen.getByRole('menu')).getByRole('menuitem', { name: T.agent.openFolderPick }))
    expect(props.onOpenFolder).toHaveBeenCalledOnce()
    await user.click(chip)
    await user.click(within(screen.getByRole('menu')).getByRole('menuitem', { name: T.agent.newFolderPick }))
    expect(props.onNewFolder).toHaveBeenCalledOnce()
  })

  it('Agent 整理出一份修改后，默认直接保存，不用点；卡上是「已保存」和「撤回」', async () => {
    const runningRun = { session_id: 's1', run_id: 'r1', status: 'running', objective: '改开头', error: null }
    const view = renderChat(conversation({ activeRun: runningRun as never, running: true, messages: [msg('m1', 'r1', 'user', '改开头', 1)] }))
    view.rerenderWith({
      conversation: conversation({
        activeRun: { ...runningRun, status: 'succeeded' } as never,
        messages: [msg('m1', 'r1', 'user', '改开头', 1), msg('m2', 'r1', 'assistant', '已经整理好一份修改。', 2)],
        activities: [{ session_id: 's1', activity_id: 'a1', run_id: 'r1', sequence: 5, kind: 'change_proposal', payload: { proposal_id: 'cs-1', title: '', summary: '', files: [] } } as never],
      }),
    })
    await waitFor(() => expect(changeSetMock.applyChangeSetDirect).toHaveBeenCalledWith('cs-1', 1))
    await waitFor(() => expect(screen.getByText('已保存')).toBeInTheDocument())
    expect(screen.queryByRole('button', { name: T.agent.changeApply })).not.toBeInTheDocument()
    expect(screen.getByRole('button', { name: T.agent.changeRollback })).toBeInTheDocument()
    expect(view.props.onProjectChanged).toHaveBeenCalled()
  })

  it('右栏撤回后，无需切换会话，结果卡立即显示已撤回并移除撤回按钮', async () => {
    let status = 'applied'
    localStorage.setItem('zhimeng.desk.change-cards:s1', JSON.stringify([
      { changeSetId: 'cs-1', runId: 'r1', status },
    ]))
    await changeSetMock.getChangeSet.withImplementation(async (id) => ({
      change_set_id: id, title: '更新采购核对', summary: '', origin: 'agent', status,
      review_revision: status === 'applied' ? 1 : 2, base_revision: 3,
      changes: [{ relative_path: 'review.md' }], writeback: null,
    }), async () => {
      const view = renderChat(conversation({ messages: [
        msg('m1', 'r1', 'user', '核对采购', 1, { session_id: 's1' }),
        msg('m2', 'r1', 'assistant', '结果已经写入文件。', 2, { session_id: 's1' }),
      ] }), { changesRefreshKey: 'cs-1:1:applied' })
      await screen.findByText('已保存')
      expect(screen.getByRole('button', { name: T.agent.changeRollback })).toBeInTheDocument()
      status = 'rolled_back'
      view.rerenderWith({ changesRefreshKey: 'cs-1:2:rolled_back' })
      await screen.findByText('已撤回')
      expect(screen.queryByRole('button', { name: T.agent.changeRollback })).not.toBeInTheDocument()
      expect(screen.queryByText('已保存')).not.toBeInTheDocument()
    })
  })

  it('设置成「先给我看」：回答下面挂出带「保存 / 放弃」的修改卡，文件成果只保留一次，详情直接打开', async () => {
    window.localStorage.setItem('zhimeng.apply-policy.v1', 'review')
    const runningRun = { session_id: 's1', run_id: 'r1', status: 'running', objective: '改开头', error: null }
    const view = renderChat(conversation({ activeRun: runningRun as never, running: true, messages: [msg('m1', 'r1', 'user', '改开头', 1)] }))
    view.rerenderWith({
      conversation: conversation({
        activeRun: { ...runningRun, status: 'succeeded' } as never,
        messages: [msg('m1', 'r1', 'user', '改开头', 1), msg('m2', 'r1', 'assistant', '已经整理好一份修改。', 2)],
        activities: [{ session_id: 's1', activity_id: 'a1', run_id: 'r1', sequence: 5, kind: 'change_proposal', payload: { proposal_id: 'cs-1', title: '', summary: '', files: [] } } as never],
      }),
    })
    await waitFor(() => expect(screen.getByRole('button', { name: T.agent.changeApply })).toBeInTheDocument())
    expect(screen.getByText('1 个文件改动')).toBeInTheDocument()
    expect(screen.getByRole('button', { name: '查看改动' })).toBeInTheDocument()
    expect(screen.getByRole('button', { name: T.agent.changeDiscard })).toBeInTheDocument()

    await userEvent.setup().click(screen.getByRole('button', { name: T.agent.changeApply }))
    await waitFor(() => expect(changeSetMock.applyChangeSetDirect).toHaveBeenCalledWith('cs-1', 1))
    await waitFor(() => expect(screen.getByText('已保存')).toBeInTheDocument())
    expect(screen.getByRole('button', { name: T.agent.changeRollback })).toBeInTheDocument()
  })

  it('跑失败了：用人话说原因，给「重发这条」，并留着它这一趟做了什么', () => {
    renderChat(conversation({
      activeRun: {
        run_id: 'r9',
        status: 'failed',
        objective: '接着写第 6 章',
        error: { error: { code: 'agent_provider_incomplete_stream', message: 'Provider stream closed without a terminal event', retryable: true } },
      } as never,
      activities: [
        toolCall('r9', 1, 'c1', 'list_files'), toolResult('r9', 2, 'c1'),
        toolCall('r9', 3, 'c2', 'read_file', { path: CHAPTER_4 }), toolResult('r9', 4, 'c2'),
      ] as never,
    }))
    const card = screen.getByRole('alert')
    expect(within(card).getByText(T.agent.failedTitle)).toBeInTheDocument()
    expect(within(card).getByText(/模型通道把这次回答截断了/)).toBeInTheDocument()
    expect(within(card).getByRole('button', { name: T.agent.resend })).toBeInTheDocument()
    fireEvent.click(within(card).getByRole('button', { name: '展开错误详情' }))
    expect(within(card).getByText(T.agent.didSteps(2))).toBeInTheDocument()
    // 连着的看目录、读文件并成一行「已探索」
    expect(within(card).getByRole('button', { name: A.chat.exploreGroup({ read: 1, list: 1, search: 0 }, false) })).toBeInTheDocument()
  })

  it('旧的一轮：步骤没读回来给「查看这一轮的步骤」，点了去读；读回来的照样折成「执行了 N 步」', async () => {
    const listed = (runId: string) => [toolCall(runId, 1, `${runId}-c1`, 'list_files'), toolResult(runId, 2, `${runId}-c1`)]
    const messages = [msg('m1', 'r1', 'user', '先看目录', 1), msg('m2', 'r1', 'assistant', '目录看过了。', 2), msg('m3', 'r2', 'user', '再看一遍', 3), msg('m4', 'r2', 'assistant', '又看了一遍。', 4)]
    const activeRun = { run_id: 'r2', status: 'succeeded', objective: '再看一遍', error: null } as never
    const unloaded = conversation({ messages, activeRun, activities: listed('r2') as never })
    renderChat(unloaded)
    openProcess(processFor('又看了一遍。'))
    expect(processFor('又看了一遍。').querySelectorAll('.zm-step')).toHaveLength(1)
    expect(within(agentBubble('又看了一遍。')).queryByRole('button', { name: T.agent.showRunSteps })).toBeNull()
    expect(processFor('目录看过了。')).toBeNull()
    await userEvent.setup().click(within(agentBubble('目录看过了。')).getByRole('button', { name: '查看处理记录' }))
    await waitFor(() => expect(unloaded.loadRunActivities).toHaveBeenCalledWith('r1'))

    cleanup()
    renderChat(conversation({ messages, activeRun, activities: listed('r2') as never, pastActivities: { r1: listed('r1') as never } }))
    openProcess(processFor('目录看过了。'))
    expect(processFor('目录看过了。').querySelectorAll('.zm-step')).toHaveLength(1)
    expect(within(agentBubble('目录看过了。')).queryByRole('button', { name: T.agent.showRunSteps })).toBeNull()
  })

  it('点了「查看这一轮的步骤」读回来一步都没有（分叉抄过来的那几轮）：说一声，按钮不悄悄消失', async () => {
    const messages = [msg('m1', 'r-fork', 'user', '第 3 章讲了什么', 1), msg('m2', 'r-fork', 'assistant', '讲了借法入门。', 2), msg('m3', 'r2', 'user', '你是谁', 3), msg('m4', 'r2', 'assistant', '我是 Agent。', 4)]
    const activeRun = { run_id: 'r2', status: 'succeeded', objective: '你是谁', error: null } as never
    const conv = conversation({ messages, activeRun })
    const view = renderChat(conv)
    expect(processFor('讲了借法入门。')).toBeNull()
    await userEvent.setup().click(within(agentBubble('讲了借法入门。')).getByRole('button', { name: '查看处理记录' }))
    await waitFor(() => expect(conv.loadRunActivities).toHaveBeenCalledWith('r-fork'))
    view.rerenderWith({
      conversation: conversation({ messages, activeRun, pastActivities: { 'r-fork': [
        { activity_id: 'f1', run_id: 'r-fork', sequence: 1, kind: 'message', payload: { role: 'user', phase: 'completed', content: '第 3 章讲了什么' } },
      ] as never } }),
    })
    await waitFor(() => expect(processFor('讲了借法入门。')).toBeNull())
    expect(screen.getByText('没有额外处理记录')).toBeInTheDocument()
    expect(within(agentBubble('讲了借法入门。')).queryByRole('button', { name: T.agent.showRunSteps })).toBeNull()
  })

  it('跑完的一轮：过程默认折成一行「执行了 N 步」，点开才列出来', async () => {
    renderChat(conversation({
      messages: [msg('m1', 'r1', 'user', '看看第 4 章', 1), msg('m2', 'r1', 'assistant', '第 4 章没有问题。', 2)],
      activities: [toolCall('r1', 1, 'c1', 'list_files'), toolResult('r1', 2, 'c1'), toolCall('r1', 3, 'c2', 'read_file', { path: CHAPTER_4 }), toolResult('r1', 4, 'c2')] as never,
    }))
    const bubble = agentBubble('第 4 章没有问题。')
    const disclosure = processFor('第 4 章没有问题。')
    expect(disclosure.open).toBe(false)
    const group = A.chat.exploreGroup({ read: 1, list: 1, search: 0 }, false)
    expect(within(bubble).queryByRole('button', { name: group })).toBeNull()
    openProcess(disclosure)
    expect(within(disclosure).getByRole('button', { name: group })).toBeInTheDocument()
  })

  it('正在跑的时候：顶上状态滚动最新一句思路', () => {
    renderChat(conversation({
      running: true,
      activeRun: { run_id: 'r1', status: 'running', objective: '看看第 4 章', error: null } as never,
      activities: [{ activity_id: 'a1', run_id: 'r1', sequence: 1, kind: 'reasoning', payload: { block_id: 'thinking-1', phase: 'delta', summary: '', delta: '先读第 4 章，再对照人物表。\n第二行不看', visibility: 'full' } }] as never,
    }))
    expect(screen.getByText(T.agent.status.thinkingAbout('先读第 4 章，再对照人物表。'))).toBeInTheDocument()
  })

  it('思考完成后工具正在执行，顶栏显示真实执行状态而不是旧思路', () => {
    renderChat(conversation({ running: true, activeRun: { run_id: 'r1', status: 'running', objective: '整理文件', error: null } as never,
      activities: [
        { activity_id: 'a1', run_id: 'r1', sequence: 1, kind: 'reasoning', payload: { summary: '旧思路', content: '旧思路', phase: 'completed', visibility: 'full' } },
        toolCall('r1', 2, 'c1', 'read_file', { path: 'notes.txt' }),
      ] as never,
    }), NO_FOLDER)
    expect(screen.getByText('正在执行工具…')).toBeInTheDocument()
    expect(screen.queryByText(T.agent.status.thinkingAbout('旧思路'))).toBeNull()
  })

  it('输入框下面能切「完全访问 / 只读」，选择会记住；只读时按只读方式开工；跑着的时候不能切', async () => {
    const conv = conversation()
    renderChat(conv)
    const user = userEvent.setup()
    const toggle = screen.getByRole('button', { name: T.agent.access.full })
    expect(toggle).toHaveAttribute('aria-pressed', 'true')
    await user.click(toggle)
    expect(window.localStorage.getItem('zhimeng.desk.agent-access-mode.v1')).toBe('read_only')
    expect(screen.getByRole('button', { name: T.agent.access.readOnly })).toHaveAttribute('aria-pressed', 'false')
    await user.type(screen.getByPlaceholderText(T.agent.placeholder), '看看第 4 章')
    await user.click(screen.getByRole('button', { name: T.agent.send }))
    await waitFor(() => expect(conv.send).toHaveBeenCalledWith(expect.objectContaining({ text: '看看第 4 章', writerMode: 'repair_only' })))

    cleanup()
    renderChat(conversation({ running: true, activeRun: { run_id: 'r1', status: 'running', objective: '改开头', error: null } as never }))
    expect(screen.getByRole('button', { name: T.agent.access.readOnly })).toBeDisabled()
  })

  it('正在跑的时候能把下一句排队，后台派发后恢复新的任务', async () => {
    const running = conversation({ running: true, activeRun: { run_id: 'r1', status: 'running', objective: '改开头', error: null } as never })
    const view = renderChat(running)
    const user = userEvent.setup()
    await user.type(screen.getByPlaceholderText(T.agent.placeholder), '再润色一下')
    await user.click(screen.getByRole('button', { name: T.agent.queueSend }))
    await waitFor(() => expect(screen.getByText(T.agent.queue.count(1))).toBeInTheDocument())
    await waitFor(() => expect(screen.getByPlaceholderText(T.agent.placeholder)).toHaveValue(''))
    expect(running.send).not.toHaveBeenCalled()

    const idle = conversation()
    queues.set('s1', { session_id: 's1', revision: 2, pause_reason: null, active_run_id: 'queued-run', items: [] })
    view.rerenderWith({ conversation: idle })
    await waitFor(() => expect(idle.refresh).toHaveBeenCalled())
    expect(queueApi.dispatchQueuedMessage).not.toHaveBeenCalled()
    expect(queueApi.enqueueMessage).toHaveBeenCalledWith('s1', expect.objectContaining({ message: '再润色一下', project_id: 'p1' }))
    expect(idle.refresh).toHaveBeenCalled()
  })

  it('排队确认丢失后任务已结束，重试仍沿用原排队身份', async () => {
    const running = conversation({ running: true, activeRun: { run_id: 'r1', status: 'running', objective: '检查文件', error: null } as never })
    queueApi.enqueueMessage.mockRejectedValueOnce(new Error('lost acknowledgement'))
    const view = renderChat(running)
    const user = userEvent.setup()
    await user.type(screen.getByPlaceholderText(T.agent.placeholder), '检查配置文件')
    await user.click(screen.getByRole('button', { name: T.agent.queueSend }))
    await waitFor(() => expect(view.props.notify).toHaveBeenCalled())
    expect(screen.getByPlaceholderText(T.agent.placeholder)).toHaveValue('检查配置文件')
    const idle = conversation()
    view.rerenderWith({ conversation: idle })
    await user.click(screen.getByRole('button', { name: T.agent.send }))
    await waitFor(() => expect(queueApi.enqueueMessage).toHaveBeenCalledTimes(2))
    expect(queueApi.enqueueMessage.mock.calls[1][1]).toEqual(queueApi.enqueueMessage.mock.calls[0][1])
    expect(idle.send).not.toHaveBeenCalled()
    await waitFor(() => expect(screen.getByPlaceholderText(T.agent.placeholder)).toHaveValue(''))
  })

  it('普通对话输入原子保存并引导同一轮，受理不冒充已处理', async () => {
    const running = conversation({ running: true, activeRun: { run_id: 'r1', status: 'running', objective: '整理资料', error: null } as never })
    renderChat(running, NO_FOLDER)
    const user = userEvent.setup()
    await user.type(screen.getByPlaceholderText(T.agent.placeholderCasual), '补充：输出改成表格')
    await user.click(screen.getByRole('button', { name: '引导这一轮' }))
    await screen.findByText('待本轮接收')
    const input = queueApi.steerQueuedMessage.mock.calls[0][3] as QueueInput
    expect(input).toEqual(expect.objectContaining({ message: '补充：输出改成表格', project_id: null, manual_skill_names: [], open_file_path: null }))
    expect(queueApi.steerQueuedMessage).toHaveBeenCalledWith('s1', input.item_id, 'r1', input)
    expect(queueApi.enqueueMessage).not.toHaveBeenCalled()
    expect(queueApi.dispatchQueuedMessage).not.toHaveBeenCalled()
    expect(running.send).not.toHaveBeenCalled()
    expect(running.cancel).not.toHaveBeenCalled()
    expect(running.refresh).not.toHaveBeenCalled()
    expect(screen.getByPlaceholderText(T.agent.placeholderCasual)).toHaveValue('')
  })

  it('同一轮插话前后的思考和步骤各归自己的回复，不在每个气泡里重复', () => {
    const messages = [msg('m1', 'r1', 'user', '整理资料', 1), msg('m2', 'r1', 'assistant', '第一段结果', 2), msg('m3', 'r1', 'user', '改成表格', 3)]
    const activities = [
      { activity_id: 'a1', run_id: 'r1', sequence: 1, kind: 'reasoning', payload: { summary: '第一阶段思考' } },
      { activity_id: 'a2', run_id: 'r1', sequence: 2, kind: 'message', payload: { role: 'assistant', phase: 'completed', message_id: 'm2', content: '第一段结果' } },
      { activity_id: 'a3', run_id: 'r1', sequence: 3, kind: 'message', payload: { role: 'user', phase: 'completed', message_id: 'm3', content: '改成表格' } },
      { activity_id: 'a4', run_id: 'r1', sequence: 4, kind: 'reasoning', payload: { summary: '第二阶段思考' } },
    ] as never
    renderChat(conversation({ messages, activities, running: true, streamingContent: '正在制作表格',
      activeRun: { run_id: 'r1', status: 'running', objective: '整理资料', error: null } as never }), NO_FOLDER)
    screen.getAllByRole('button', { name: T.agent.thoughtsCount(1) }).forEach((button) => fireEvent.click(button))
    expect(screen.getAllByText('第一阶段思考')).toHaveLength(1)
    expect(screen.getAllByText('第二阶段思考')).toHaveLength(1)
    expect(screen.getByText('第一段结果')).toBeInTheDocument()
    expect(screen.getByText('正在制作表格')).toBeInTheDocument()
  })

  it('已经排队的内容可送入本轮，复用同一条消息身份', async () => {
    const running = conversation({ running: true, activeRun: { run_id: 'r1', status: 'running', objective: '整理资料', error: null } as never })
    renderChat(running, NO_FOLDER)
    const user = userEvent.setup()
    await user.type(screen.getByPlaceholderText(T.agent.placeholderCasual), '先整理来源链接')
    await user.click(screen.getByRole('button', { name: T.agent.queueSend }))
    await user.click(await screen.findByRole('button', { name: '送入本轮' }))
    await screen.findByText('待本轮接收')
    expect(queueApi.enqueueMessage).toHaveBeenCalledOnce()
    expect(queueApi.steerQueuedMessage).toHaveBeenCalledWith('s1', queueApi.enqueueMessage.mock.calls[0][1].item_id, 'r1')
    expect(queueApi.dispatchQueuedMessage).not.toHaveBeenCalled()
  })

  it('引导目标已经结束时保留已入队消息并清草稿，显示原因且不自动另发', async () => {
    queueApi.steerQueuedMessage.mockImplementationOnce(async (sid: string, _id: string, _run: string, input: QueueInput) => {
      queues.set(sid, { session_id: sid, revision: 1, pause_reason: 'user', active_run_id: null, items: [{ ...input, sending: false }] })
      throw new Error('这一轮已经结束，消息仍在队列里')
    })
    const running = conversation({ running: true, activeRun: { run_id: 'r1', status: 'running', objective: '整理资料', error: null } as never })
    const view = renderChat(running, NO_FOLDER)
    const user = userEvent.setup()
    await user.type(screen.getByPlaceholderText(T.agent.placeholderCasual), '补充来源')
    await user.click(screen.getByRole('button', { name: '引导这一轮' }))
    await screen.findByRole('alert')
    await waitFor(() => expect(screen.getByPlaceholderText(T.agent.placeholderCasual)).toHaveValue(''))
    expect(screen.getByText('补充来源')).toBeInTheDocument()
    view.rerenderWith({ ...NO_FOLDER, conversation: conversation() })
    expect(queueApi.dispatchQueuedMessage).not.toHaveBeenCalled()
  })

  it('原子引导未保存时保留原草稿，不另外入队', async () => {
    queueApi.steerQueuedMessage.mockRejectedValueOnce(new Error('保存队列失败'))
    const running = conversation({ running: true, activeRun: { run_id: 'r1', status: 'running', objective: '整理资料', error: null } as never })
    renderChat(running, NO_FOLDER)
    const user = userEvent.setup()
    await user.type(screen.getByPlaceholderText(T.agent.placeholderCasual), '补充来源')
    await user.click(screen.getByRole('button', { name: '引导这一轮' }))
    await screen.findByRole('alert')
    expect(screen.getByPlaceholderText(T.agent.placeholderCasual)).toHaveValue('补充来源')
    expect(queueApi.steerQueuedMessage).toHaveBeenCalledOnce()
    expect(queueApi.enqueueMessage).not.toHaveBeenCalled()
  })

  it('失败的一轮能「继续任务」：保留草稿、只请求一次，并把原任务来源交给新的一轮', async () => {
    const continuation = { schema: 'zhimeng.agent-run-continuation.v1', run_id: 'r1', can_continue: true,
      reason_code: 'budget', reason: '可继续', stop_reason: 'agent_provider_turn_limit', objective: '整理所有资料',
      continued_by_run_id: null, completed_tool_count: 5, uncertain_tool_count: 0 } as const
    let resolve!: (value: typeof continuation) => void
    const request = new Promise<typeof continuation>((done) => { resolve = done })
    const conv = conversation({ continuation, prepareContinuation: vi.fn().mockReturnValue(request),
      activeRun: { run_id: 'r1', session_id: 's1', status: 'failed', objective: continuation.objective, error: null } as never })
    renderChat(conv)
    const user = userEvent.setup()
    await user.type(screen.getByPlaceholderText(T.agent.placeholder), '稍后再检查人名')
    const button = screen.getByRole('button', { name: T.agent.continueTask })
    fireEvent.click(button)
    fireEvent.click(button)
    expect(conv.prepareContinuation).toHaveBeenCalledTimes(1)
    resolve(continuation)
    await waitFor(() => expect(conv.send).toHaveBeenCalledWith(expect.objectContaining({ text: '整理所有资料', continuationSourceRunId: 'r1' })))
    expect(screen.getByPlaceholderText(T.agent.placeholder)).toHaveValue('稍后再检查人名')
    expect(changeSetMock.applyChangeSetDirect).not.toHaveBeenCalled()
  })

  it('停止后保留并暂停原对话队列，点击继续才发送下一条', async () => {
    const running = conversation({ running: true, busy: true, activeRun: { run_id: 'r1', session_id: 's1', status: 'running', objective: '旧任务' } as never })
    const view = renderChat(running)
    const user = userEvent.setup()
    await user.type(screen.getByPlaceholderText(T.agent.placeholder), '接下来查错字')
    await user.click(screen.getByRole('button', { name: T.agent.queueSend }))
    await waitFor(() => expect(screen.getByText(T.agent.queue.count(1))).toBeInTheDocument())
    await user.click(screen.getByRole('button', { name: T.agent.stop }))
    await waitFor(() => expect(running.cancel).toHaveBeenCalledTimes(1))
    const stopped = conversation({ activeRun: { run_id: 'r1', session_id: 's1', status: 'cancelled', objective: '旧任务', error: null } as never })
    view.rerenderWith({ conversation: stopped })
    expect(queueApi.dispatchQueuedMessage).not.toHaveBeenCalled()
    await user.click(screen.getByRole('button', { name: T.agent.queue.resume }))
    await waitFor(() => expect(queueApi.updateMessageQueue).toHaveBeenCalledWith('s1', expect.any(Number), { action: 'resume' }))
    expect(queueApi.dispatchQueuedMessage).not.toHaveBeenCalled()
    expect(stopped.prepareContinuation).not.toHaveBeenCalled()
  })

  it('队列暂停请求还没返回时，也立即停止当前任务', async () => {
    const running = conversation({ running: true, busy: true })
    renderChat(running, NO_FOLDER)
    const user = userEvent.setup()
    await user.type(screen.getByPlaceholderText(T.agent.placeholderCasual), '稍后检查 README')
    await user.click(screen.getByRole('button', { name: T.agent.queueSend }))
    await screen.findByText(T.agent.queue.count(1))
    let complete!: (value: QueueState) => void
    queueApi.updateMessageQueue.mockReturnValueOnce(new Promise<QueueState>((resolve) => { complete = resolve }))
    await user.click(screen.getByRole('button', { name: T.agent.stop }))
    expect(running.cancel).toHaveBeenCalledTimes(1)
    await waitFor(() => expect(queueApi.updateMessageQueue).toHaveBeenCalled())
    await act(async () => complete({ session_id: 's1', revision: 2, pause_reason: 'user', active_run_id: null, items: [] }))
  })

  it('失败的一轮后台不允许继续时，说明具体原因', () => {
    renderChat(conversation({ activeRun: { run_id: 'r1', session_id: 's1', status: 'failed', error: null } as never,
      continuation: { schema: 'zhimeng.agent-run-continuation.v1', run_id: 'r1', can_continue: false,
        reason_code: 'handoff_too_large', reason: '此前操作记录过长，请明确下一步任务。', stop_reason: 'budget',
        objective: '整理资料', continued_by_run_id: null, completed_tool_count: 15, uncertain_tool_count: 0 } }))
    fireEvent.click(screen.getByRole('button', { name: '展开错误详情' }))
    expect(screen.getByText('此前操作记录过长，请明确下一步任务。')).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: T.agent.continueTask })).not.toBeInTheDocument()
  })

  it('切到 B 后 A 的继续请求结束，不能清掉 B 的等待状态或发送 A 的任务', async () => {
    const stateA = { schema: 'zhimeng.agent-run-continuation.v1', run_id: 'ra', can_continue: true,
      reason_code: 'interrupted', reason: '可继续', stop_reason: null, objective: 'A任务',
      continued_by_run_id: null, completed_tool_count: 0, uncertain_tool_count: 0 } as const
    const stateB = { ...stateA, run_id: 'rb', objective: 'B任务' }
    let resolveA!: (value: typeof stateA) => void
    let resolveB!: (value: typeof stateB) => void
    const a = conversation({ continuation: stateA, activeRun: { run_id: 'ra', session_id: 's1', status: 'failed', error: null } as never,
      prepareContinuation: vi.fn().mockReturnValue(new Promise((resolve) => { resolveA = resolve })) })
    const b = conversation({ session: { session_id: 's2' } as never, continuation: stateB,
      activeRun: { run_id: 'rb', session_id: 's2', status: 'failed', error: null } as never,
      prepareContinuation: vi.fn().mockReturnValue(new Promise((resolve) => { resolveB = resolve })) })
    const view = renderChat(a)
    fireEvent.click(screen.getByRole('button', { name: T.agent.continueTask }))
    view.rerenderWith({ conversation: b })
    expect(screen.getByRole('button', { name: T.agent.continueTask })).toBeEnabled()
    fireEvent.click(screen.getByRole('button', { name: T.agent.continueTask }))
    resolveA(stateA)
    await waitFor(() => expect(screen.getByRole('button', { name: T.agent.continueTask })).toBeDisabled())
    expect(a.send).not.toHaveBeenCalled()
    resolveB(stateB)
    await waitFor(() => expect(b.send).toHaveBeenCalledTimes(1))
  })

  it('换到别的对话不显示或编辑原对话队列，也不把它发给新对话', async () => {
    const running = conversation({ running: true, busy: true, activeRun: { run_id: 'r1', session_id: 's1', status: 'running', objective: '旧任务' } as never })
    const view = renderChat(running)
    const user = userEvent.setup()
    await user.type(screen.getByPlaceholderText(T.agent.placeholder), '原对话的下一条')
    await user.click(screen.getByRole('button', { name: T.agent.queueSend }))
    await waitFor(() => expect(screen.getByText(T.agent.queue.count(1))).toBeInTheDocument())
    const other = conversation({ session: { session_id: 's2', title: '另一条' } as never })
    view.rerenderWith({ conversation: other, projectId: 'p2' })
    expect(screen.queryByRole('button', { name: T.agent.queue.edit })).not.toBeInTheDocument()
    expect(other.send).not.toHaveBeenCalled()
    expect(screen.getByPlaceholderText(T.agent.placeholder)).toHaveValue('')
  })

  it('你发出的消息上有「复制」和「编辑」；点「编辑」装回输入框并聚焦', async () => {
    const writeText = vi.fn(async () => undefined)
    Object.defineProperty(navigator, 'clipboard', { value: { writeText }, configurable: true })
    renderChat(conversation({ messages: [msg('m1', 'r1', 'user', '写一段激烈的雨夜决斗', 1)] }))
    fireEvent.click(screen.getByRole('button', { name: T.agent.copyPrompt }))
    expect(writeText).toHaveBeenCalledWith('写一段激烈的雨夜决斗')
    const user = userEvent.setup()
    const textarea = screen.getByPlaceholderText(T.agent.placeholder)
    expect(textarea).toHaveValue('')
    await user.click(screen.getByRole('button', { name: T.agent.editPrompt }))
    expect(textarea).toHaveValue('写一段激烈的雨夜决斗')
    await waitFor(() => expect(textarea).toHaveFocus())
  })

  it('前面还有更早的消息时，顶上给「显示更早的消息」，点了往前补；补不出来说一句', async () => {
    const messages = [msg('m501', 'r9', 'user', '接着写', 501), msg('m502', 'r9', 'assistant', '写好了。', 502)]
    const conv = conversation({ messages, hasEarlierMessages: true })
    renderChat(conv)
    await userEvent.setup().click(screen.getByRole('button', { name: T.agent.showEarlierMessages }))
    expect(conv.loadEarlierMessages).toHaveBeenCalledTimes(1)
    expect(screen.queryByText(T.agent.earlierMessagesFailed)).not.toBeInTheDocument()

    cleanup()
    renderChat(conversation({ messages, hasEarlierMessages: true, loadEarlierMessages: vi.fn(async () => { throw new Error('断了') }) }))
    await userEvent.setup().click(screen.getByRole('button', { name: T.agent.showEarlierMessages }))
    expect(await screen.findByText(T.agent.earlierMessagesFailed)).toBeInTheDocument()

    cleanup()
    renderChat(conversation({ messages }))
    expect(screen.queryByRole('button', { name: T.agent.showEarlierMessages })).not.toBeInTheDocument()
  })

  it('回答上「重新回答」按上一句和最新访问模式开工，保留新草稿', async () => {
    const conv = conversation({ messages: [msg('m1', 'r1', 'user', '检查配置文件', 1), msg('m2', 'r1', 'assistant', '这是检查结果。', 2)] })
    renderChat(conv)
    const user = userEvent.setup()
    await user.type(screen.getByPlaceholderText(T.agent.placeholder), '这是下一句尚未发送的话')
    await user.click(screen.getByRole('button', { name: T.agent.access.full }))
    await user.click(screen.getByRole('button', { name: T.agent.regenerate }))
    await waitFor(() => expect(conv.send).toHaveBeenLastCalledWith(expect.objectContaining({ text: '检查配置文件', writerMode: 'repair_only' })))
    await user.click(screen.getByRole('button', { name: T.agent.access.readOnly }))
    await user.click(screen.getByRole('button', { name: T.agent.regenerate }))
    await waitFor(() => expect(conv.send).toHaveBeenLastCalledWith(expect.objectContaining({ text: '检查配置文件', writerMode: 'author_review_change_proposal' })))
    expect(conv.send).toHaveBeenCalledTimes(2)
    expect(screen.getByPlaceholderText(T.agent.placeholder)).toHaveValue('这是下一句尚未发送的话')
  })

  it('发送失败保留期间新写的草稿，旧原文在恢复区可明确追加', async () => {
    let reject!: (error: Error) => void
    const request = new Promise<void>((_resolve, fail) => { reject = fail })
    const conv = conversation({ send: vi.fn().mockReturnValue(request) })
    renderChat(conv, NO_FOLDER)
    const input = screen.getByPlaceholderText(T.agent.placeholderCasual)
    const user = userEvent.setup()
    await user.type(input, '先发送这句')
    await user.click(screen.getByRole('button', { name: T.agent.send }))
    await user.type(input, '等待期间写的新草稿')
    await act(async () => reject(new Error('发送没有确认成功')))
    expect(input).toHaveValue('等待期间写的新草稿')
    const recovery = screen.getByRole('region', { name: '待恢复消息' })
    expect(within(recovery).getByText('先发送这句')).toBeInTheDocument()
    await user.click(within(recovery).getByRole('button', { name: '追加到输入框' }))
    expect(input).toHaveValue('等待期间写的新草稿\n\n先发送这句')
    expect(screen.queryByRole('region', { name: '待恢复消息' })).not.toBeInTheDocument()
  })

  it('第一句创建会话后发送失败，输入原文恢复到这条新会话', async () => {
    let reject!: (error: Error) => void
    const request = new Promise<void>((_resolve, fail) => { reject = fail })
    const conv = conversation({ session: null, send: vi.fn().mockReturnValue(request) })
    const view = renderChat(conv, NO_FOLDER)
    const user = userEvent.setup()
    await user.type(screen.getByPlaceholderText(T.agent.placeholderCasual), '第一句也要保住')
    await user.click(screen.getByRole('button', { name: T.agent.send }))
    view.rerenderWith({ ...NO_FOLDER, conversation: { ...conv, session: { session_id: 'new-session', title: '新对话' } as never } })
    await act(async () => reject(new Error('建立运行失败')))
    expect(screen.getByPlaceholderText(T.agent.placeholderCasual)).toHaveValue('第一句也要保住')
    expect(localStorage.getItem('zhimeng.desk.composer:loose:new-session')).toBe('第一句也要保住')
  })

  it('发送成功只摘下本次技能，等待期间新加技能与草稿留给下一句', async () => {
    userSkillsMock.readUserSkills.mockResolvedValueOnce({
      schema: 'zhimeng.user-skills.v1', skills_root: 'C:/skills', exists: true, total: 2, enabled: 2,
      skills: ['文档整理', '表格校验'].map((name) => ({ name, description: name, folder: name, has_scripts: false, enabled: true, origin: 'project' })),
    })
    let finish!: () => void
    const request = new Promise<void>((resolve) => { finish = resolve })
    const conv = conversation({ send: vi.fn().mockReturnValue(request) })
    renderChat(conv, NO_FOLDER)
    const user = userEvent.setup()
    await user.click(screen.getByRole('button', { name: '选择技能' }))
    await user.click(await screen.findByRole('button', { name: '文档整理' }))
    await user.type(screen.getByPlaceholderText(T.agent.placeholderCasual), '整理说明')
    await user.click(screen.getByRole('button', { name: T.agent.send }))
    await user.click(screen.getByRole('button', { name: '选择技能' }))
    await user.click(screen.getByRole('button', { name: '表格校验' }))
    await user.type(screen.getByPlaceholderText(T.agent.placeholderCasual), '接着核对表格')
    await act(async () => finish())
    expect(conv.send).toHaveBeenCalledWith(expect.objectContaining({ skillNames: ['文档整理'] }))
    expect(screen.queryByRole('button', { name: T.agent.skillTagRemove('文档整理') })).not.toBeInTheDocument()
    expect(screen.getByRole('button', { name: T.agent.skillTagRemove('表格校验') })).toBeInTheDocument()
    expect(screen.getByPlaceholderText(T.agent.placeholderCasual)).toHaveValue('接着核对表格')
  })

  it.each([false, true])('重发失败任务保留当前新草稿，复核=%s', async (review) => {
    const conv = conversation({ activeRun: { run_id: 'r-failed', session_id: 's1', status: 'failed', objective: '检查旧任务', error: null } as never,
      ...(review ? { reviewSources: { 'r-failed': { source_run_id: 'r-original', source_run_ordinal: 1, source_message_id: 'u1' } } } : {}),
    })
    renderChat(conv, NO_FOLDER)
    const user = userEvent.setup()
    await user.type(screen.getByPlaceholderText(T.agent.placeholderCasual), '另一个新问题')
    await user.click(screen.getByRole('button', { name: T.agent.resend }))
    await waitFor(() => expect(conv.send).toHaveBeenCalledWith(expect.objectContaining({ text: '检查旧任务' })))
    expect(screen.getByPlaceholderText(T.agent.placeholderCasual)).toHaveValue('另一个新问题')
    if (review) expect(conv.send).toHaveBeenCalledWith(expect.objectContaining({ reviewSourceRunId: 'r-original', writerMode: 'repair_only' }))
  })

  it('向上阅读不被同轮流式回答拉走，切另一条长对话重新落到最新', async () => {
    const conv = conversation({ messages: [msg('a1', 'ra', 'assistant', 'A 的长回复', 1)] })
    const view = renderChat(conv, NO_FOLDER)
    const thread = view.container.querySelector('.zm-thread') as HTMLDivElement
    Object.defineProperties(thread, { scrollHeight: { configurable: true, value: 2000 }, clientHeight: { configurable: true, value: 400 } })
    thread.scrollTop = 200
    fireEvent.scroll(thread)
    view.rerenderWith({ ...NO_FOLDER, conversation: { ...conv, streamingContent: '继续输出新内容' } })
    expect(thread.scrollTop).toBe(200)
    const jump = screen.getByRole('button', { name: T.agent.jumpToLatest })
    expect(jump.closest('.zm-chat-foot-inner')).not.toBeNull()
    view.rerenderWith({ ...NO_FOLDER, conversation: { ...conv, session: { session_id: 'b', title: 'B' } as never,
      messages: [msg('b1', 'rb', 'assistant', 'B 的长回复', 1)] } })
    await waitFor(() => expect(thread.scrollTop).toBe(2000))
    expect(screen.queryByRole('button', { name: T.agent.jumpToLatest })).not.toBeInTheDocument()
  })

  it('多轮回答分别关联各自之前的用户消息，不串到最后一问', async () => {
    const conv = conversation({ messages: [
      msg('m0', 'r0', 'assistant', '一段更早的记录', 0),
      msg('m1', 'r1', 'user', '检查 README', 1),
      msg('m2', 'r1', 'assistant', '链接检查完成', 2),
      msg('m3', 'r2', 'user', '整理配置说明', 3),
      msg('m4', 'r2', 'assistant', '配置说明已整理', 4),
    ] })
    renderChat(conv, NO_FOLDER)
    expect(within(agentBubble('一段更早的记录')).queryByRole('button', { name: T.agent.regenerate })).not.toBeInTheDocument()
    await userEvent.setup().click(within(agentBubble('链接检查完成')).getByRole('button', { name: T.agent.regenerate }))
    await waitFor(() => expect(conv.send).toHaveBeenLastCalledWith(expect.objectContaining({ text: '检查 README' })))
    await userEvent.setup().click(within(agentBubble('配置说明已整理')).getByRole('button', { name: T.agent.regenerate }))
    await waitFor(() => expect(conv.send).toHaveBeenLastCalledWith(expect.objectContaining({ text: '整理配置说明' })))
  })

  it('输入框空着时按 ↑：把上一句发过的话调回来', async () => {
    renderChat(conversation({ messages: [msg('m1', 'r1', 'user', '给主角加一个心魔伏笔', 1), msg('m2', 'r1', 'assistant', '好的，已经安排。', 2)] }))
    const textarea = screen.getByPlaceholderText(T.agent.placeholder)
    const user = userEvent.setup()
    await user.click(textarea)
    await user.keyboard('{ArrowUp}')
    expect(textarea).toHaveValue('给主角加一个心魔伏笔')
  })

  it('顶上「压缩对话」直接压，不弹确认，正在写的草稿留着', async () => {
    const conv = conversation({ messages: [msg('m1', 'r1', 'user', '字'.repeat(8500), 1), msg('m2', 'r1', 'assistant', '回答', 2)] })
    const { props } = renderChat(conv)
    const user = userEvent.setup()
    await user.type(screen.getByPlaceholderText(T.agent.placeholder), '接下来继续检查')
    await user.click(screen.getByRole('button', { name: T.agent.compactAction }))
    await waitFor(() => expect(conv.compactConversation).toHaveBeenCalledTimes(1))
    expect(props.askConfirm).not.toHaveBeenCalled()
    expect(screen.getByPlaceholderText(T.agent.placeholder)).toHaveValue('接下来继续检查')
    expect(props.notify).toHaveBeenCalledWith('ok', T.agent.compactDone)
  })

  it('/compact 调真正的压缩；提到压缩的普通句子照常发给 Agent', async () => {
    const conv = conversation()
    renderChat(conv, NO_FOLDER)
    const user = userEvent.setup()
    const input = screen.getByPlaceholderText(T.agent.placeholderCasual)
    await user.type(input, '/compact')
    await user.keyboard('{Enter}')
    await waitFor(() => expect(conv.compactConversation).toHaveBeenCalledTimes(1))
    expect(conv.send).not.toHaveBeenCalled()
    await user.type(input, '不要做一次整理压缩，先解释这个词')
    await user.keyboard('{Enter}')
    await waitFor(() => expect(conv.send).toHaveBeenCalledWith(expect.objectContaining({ text: '不要做一次整理压缩，先解释这个词' })))
    expect(conv.compactConversation).toHaveBeenCalledTimes(1)
  })

  it('回答里的代码块带语言标和「复制代码」，点了放进剪贴板', async () => {
    const writeText = vi.fn(async () => undefined)
    Object.defineProperty(navigator, 'clipboard', { value: { writeText }, configurable: true })
    renderChat(conversation({ messages: [msg('m1', 'r1', 'assistant', '```json\n{"name": "林惊羽"}\n```', 1)] }))
    expect(screen.getByText('json')).toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: T.agent.codeBlock.copy }))
    expect(writeText).toHaveBeenCalledWith('{"name": "林惊羽"}')
    await waitFor(() => expect(screen.getByRole('button', { name: T.agent.codeBlock.copied })).toBeInTheDocument())
  })

  it('打 @ 弹出文件清单，回车插入路径，发的时候带上文件', async () => {
    const conv = conversation()
    renderChat(conv)
    const textarea = screen.getByPlaceholderText(T.agent.placeholder)
    const user = userEvent.setup()
    await user.type(textarea, '@')
    expect(screen.getByRole('listbox', { name: T.agent.mention.title })).toBeInTheDocument()
    await user.type(textarea, '004')
    expect(screen.getByText('004_玉佩上的青云.md')).toBeInTheDocument()
    await user.keyboard('{Enter}')
    expect(textarea).toHaveValue(`@${CHAPTER_4} `)
    await user.keyboard('{Enter}')
    await waitFor(() => expect(conv.send).toHaveBeenCalledWith(expect.objectContaining({ text: `@${CHAPTER_4}`, manualFileIds: ['file-4'] })))
  })

  it('/ 选技能变成输入框上方的标签：不改草稿，× 能摘掉；带着标签发出去，发完清空', async () => {
    const conv = conversation()
    renderChat(conv, NO_FOLDER)
    const user = userEvent.setup()
    const textarea = screen.getByPlaceholderText(T.agent.placeholderCasual)
    await user.type(textarea, '/')
    await user.click(await screen.findByRole('option', { name: /网文知识库/ }))
    expect(screen.getByText('网文知识库')).toBeInTheDocument()
    expect(textarea).toHaveValue('')
    await user.click(screen.getByRole('button', { name: T.agent.skillTagRemove('网文知识库') }))
    expect(screen.queryByText('网文知识库')).not.toBeInTheDocument()

    await user.type(textarea, '/')
    await user.click(await screen.findByRole('option', { name: /网文知识库/ }))
    await user.type(textarea, '玄幻开篇怎么写钩子')
    await user.keyboard('{Enter}')
    await waitFor(() => expect(conv.send).toHaveBeenCalledWith(expect.objectContaining({ text: '玄幻开篇怎么写钩子', skillNames: ['网文知识库'] })))
    await waitFor(() => expect(screen.queryByText('网文知识库')).not.toBeInTheDocument())
  })

  it('/ 里的真命令：新对话、修改记录、换模型、切换只读，各接已有功能', async () => {
    const { props } = renderChat(conversation())
    const user = userEvent.setup()
    const textarea = screen.getByPlaceholderText(T.agent.placeholder)

    await user.type(textarea, '/')
    await user.click(await screen.findByRole('option', { name: /^新对话/ }))
    expect(props.onNewConversation).toHaveBeenCalledTimes(1)
    expect(textarea).toHaveValue('')

    await user.type(textarea, '/')
    await user.click(await screen.findByRole('option', { name: /^修改记录/ }))
    expect(props.onShowChanges).toHaveBeenCalledTimes(1)

    await user.type(textarea, '/')
    await user.click(await screen.findByRole('option', { name: /^换模型/ }))
    expect(document.activeElement).toBe(screen.getByRole('combobox', { name: T.agent.modelPick }))

    expect(window.localStorage.getItem('zhimeng.desk.agent-access-mode.v1')).toBeNull()
    await user.type(textarea, '/')
    await user.click(await screen.findByRole('option', { name: /^切换只读/ }))
    expect(window.localStorage.getItem('zhimeng.desk.agent-access-mode.v1')).toBe('read_only')
    expect(props.notify).toHaveBeenCalledWith('ok', T.agent.access.switchedToReadOnly)
  })

  it('/停止：跑着时调 cancel，没在跑时说一句', async () => {
    const conv = conversation({ running: true, busy: true })
    renderChat(conv)
    const user = userEvent.setup()
    await user.type(screen.getByPlaceholderText(T.agent.placeholder), '/')
    await user.click(await screen.findByRole('option', { name: /^停止/ }))
    expect(conv.cancel).toHaveBeenCalledTimes(1)

    cleanup()
    const idle = conversation()
    const idleView = renderChat(idle)
    await userEvent.setup().type(screen.getByPlaceholderText(T.agent.placeholder), '/')
    await userEvent.setup().click(await screen.findByRole('option', { name: /^停止/ }))
    expect(idle.cancel).not.toHaveBeenCalled()
    expect(idleView.props.notify).toHaveBeenCalledWith('warn', T.agent.slash.stopIdle)
  })

  it('同一轮插话形成多条回复时，修改卡只出现一次', async () => {
    window.localStorage.setItem('zhimeng.desk.change-cards:s1', JSON.stringify([{ changeSetId: 'cs-1', runId: 'r1', status: 'applied' }]))
    changeSetMock.getChangeSet.mockResolvedValueOnce({ change_set_id: 'cs-1', title: '整理验收文档', summary: '', origin: 'agent', status: 'applied', review_revision: 2, base_revision: 3, changes: [], writeback: { result_revision: 4 } } as never)
    renderChat(conversation({ messages: [msg('m1', 'r1', 'user', '整理文档', 1), msg('m2', 'r1', 'assistant', '文件已整理', 2),
      msg('m3', 'r1', 'user', '补充说明', 3), msg('m4', 'r1', 'assistant', '补充完成', 4)] }))
    await waitFor(() => expect(screen.getAllByRole('button', { name: T.agent.changeRollback })).toHaveLength(1))
  })

  it('/撤回上一次修改：先问一句，确认后才撤；没有保存过的修改就说一句', async () => {
    window.localStorage.setItem('zhimeng.desk.change-cards:s1', JSON.stringify([{ changeSetId: 'cs-1', runId: 'r1', status: 'applied' }]))
    const applied = { change_set_id: 'cs-1', title: '改第 4 章', summary: '', origin: 'agent', status: 'applied', review_revision: 2, base_revision: 3, changes: [], writeback: { result_revision: 4 } } as never
    changeSetMock.getChangeSet.mockResolvedValueOnce(applied)
    const { props } = renderChat(conversation())
    const user = userEvent.setup()
    await user.type(screen.getByPlaceholderText(T.agent.placeholder), '/')
    await user.click(await screen.findByRole('option', { name: /^撤回上一次修改/ }))
    expect(props.askConfirm).toHaveBeenCalledTimes(1)
    const request = vi.mocked(props.askConfirm).mock.calls[0][0]
    expect(request.title).toBe(T.changes.rollbackConfirmTitle)
    expect(request.danger).toBe(true)
    expect(changeSetMock.rollbackChangeSet).not.toHaveBeenCalled()
    changeSetMock.getChangeSet.mockResolvedValueOnce(applied)
    await request.onConfirm()
    await waitFor(() => expect(changeSetMock.rollbackChangeSet).toHaveBeenCalledWith('cs-1', 2))

    cleanup()
    const empty = renderChat(conversation({ session: { session_id: 's2', title: '空对话', updated_at: '' } as never }))
    await userEvent.setup().type(screen.getByPlaceholderText(T.agent.placeholder), '/')
    await userEvent.setup().click(await screen.findByRole('option', { name: /^撤回上一次修改/ }))
    expect(empty.props.askConfirm).not.toHaveBeenCalled()
    expect(empty.props.notify).toHaveBeenCalledWith('warn', T.agent.slash.noChangeToRollback)
  })

  it('普通按键不会停止任务、新建对话或改写草稿', async () => {
    const conv = conversation({ running: true, busy: true })
    renderChat(conv)
    const user = userEvent.setup()
    const textarea = screen.getByPlaceholderText(T.agent.placeholder)
    await user.click(textarea)
    fireEvent.keyDown(textarea, { key: 'Escape' })
    expect(document.activeElement).toBe(textarea)
    expect(conv.cancel).not.toHaveBeenCalled()
    fireEvent.keyDown(document.body, { key: 'Escape' })
    expect(conv.cancel).not.toHaveBeenCalled()

    cleanup()
    const idleView = renderChat(conversation())
    const idleTextarea = screen.getByPlaceholderText(T.agent.placeholder)
    fireEvent.keyDown(document.body, { key: 'N', ctrlKey: true, shiftKey: true })
    expect(idleView.props.onNewConversation).not.toHaveBeenCalled()
    fireEvent.keyDown(document.body, { key: 'k', ctrlKey: true })
    expect(idleTextarea).toHaveValue('')
    expect(screen.queryByRole('listbox', { name: T.agent.slash.title })).toBeNull()

    cleanup()
    window.localStorage.clear() // 草稿按对话存在本机：上面那段留下的「/」别带过来
    renderChat(conversation())
    const draftBox = screen.getByPlaceholderText(T.agent.placeholder)
    await userEvent.setup().type(draftBox, '写到一半的话')
    fireEvent.keyDown(draftBox, { key: 'k', ctrlKey: true })
    expect(draftBox).toHaveValue('写到一半的话')
    // 焦点在别的编辑器里：Ctrl+K 不抢
    const editor = document.createElement('div')
    editor.contentEditable = 'true'
    document.body.appendChild(editor)
    fireEvent.keyDown(editor, { key: 'k', ctrlKey: true })
    expect(draftBox).toHaveValue('写到一半的话')
    editor.remove()
  })

  it('回答里带着思考过程：折成一行，点开看', async () => {
    renderChat(conversation({ messages: [msg('m1', 'r1', 'assistant', '<think>这是深层思考链</think>这是最终正文', 1)] }))
    expect(screen.getByText('这是最终正文')).toBeInTheDocument()
    openProcess(processFor('这是最终正文'))
    const head = screen.getByRole('button', { name: T.agent.thoughtsCount(1) })
    expect(screen.queryByText('这是深层思考链')).toBeNull()
    await userEvent.setup().click(head)
    expect(screen.getByText('这是深层思考链')).toBeInTheDocument()
  })

  it('消息上的分叉：从这一轮新开一条分支对话，提示用后台起的名字', async () => {
    const conv = conversation({
      messages: [msg('m-u1', 'r1', 'user', '把第 4 章的打斗写得更凶险一些', 1)],
      forkConversation: vi.fn(async () => ({ session_id: 's-fork', title: '第四章打斗（分支）' } as never)),
    })
    const { props } = renderChat(conv)
    await userEvent.setup().click(screen.getByRole('button', { name: T.agent.forkConversationTitle }))
    await waitFor(() => {
      expect(conv.forkConversation).toHaveBeenCalledWith('m-u1')
      expect(props.notify).toHaveBeenCalledWith('ok', T.agent.forkedNotice('第四章打斗（分支）'))
    })
  })

  it('消息里 @ 的文件是能点的，点了在右栏打开', async () => {
    const { props } = renderChat(conversation({ messages: [msg('m-u1', 'r1', 'user', '请阅读 @004_玉佩上的青云.md 这一章', 1)] }))
    await userEvent.setup().click(screen.getByTitle(T.agent.openFileHint('004_玉佩上的青云.md')))
    expect(props.onOpenFile).toHaveBeenCalledWith(CHAPTER_4)
  })

  it('打开历史对话就能看到该轮真实记忆更新及原话，管理入口仍可用', async () => {
    memoryMock.readMemoryChanges.mockResolvedValue({ session_id: 's1', pending: false, changes: { r1: [{ action: 'replace', old_text: '回答简短', text: '先给结论再展开理由', evidence: '以后请展开理由', source_message_id: 'm1', origin: 'automatic', created_at: '2026-10-17T10:00:00Z' }] } })
    const view = renderChat(conversation({ messages: [msg('m1', 'r1', 'user', '以后请展开理由', 1, { session_id: 's1' }), msg('m2', 'r1', 'assistant', '好的。', 2, { session_id: 's1' })] }), NO_FOLDER)
    fireEvent.click(await screen.findByText('记忆变更 · 1 项'))
    expect(screen.getByText('回答简短')).toBeInTheDocument()
    expect(screen.getByText('先给结论再展开理由')).toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: A.chat.memoryManage }))
    expect(view.props.onOpenMemorySettings).toHaveBeenCalledOnce()
  })
})

const providerMock = vi.hoisted(() => ({
  acceptReportedModel: vi.fn(async (..._args: unknown[]) => ({ already_accepted: false, reported_model: 'deepseek-v4-pro-0813' })),
  saveProviderSettings: vi.fn(async (..._args: unknown[]) => ({})),
  activateProviderProfile: vi.fn(async (..._args: unknown[]) => ({})),
  readProviderCatalog: vi.fn(async (..._args: unknown[]) => ({ schema: 'zhimeng.provider-catalog.v1', active_provider_id: null, providers: [] })),
  testProviderModelsLiveness: vi.fn(async (..._args: unknown[]) => ({
    schema: 'zhimeng.provider-model-liveness.v1', tested: 0, usable_count: 0, usable_models: [], fastest_model: null, results: [],
  })),
}))
vi.mock('../../api/providerSettings', async (importActual) => ({
  ...(await importActual<object>()),
  acceptReportedModel: (...args: unknown[]) => providerMock.acceptReportedModel(...args),
  saveProviderSettings: (...args: unknown[]) => providerMock.saveProviderSettings(...args),
  activateProviderProfile: (...args: unknown[]) => providerMock.activateProviderProfile(...args),
  readProviderCatalog: (...args: unknown[]) => providerMock.readProviderCatalog(...args),
  testProviderModelsLiveness: (...args: unknown[]) => providerMock.testProviderModelsLiveness(...args),
}))

describe('失败卡 · 模型身份核对', () => {
  it('关闭错误后重进仍收起，新一轮错误照常出现，历史详情可重新打开', async () => {
    const failed = (id: string) => conversation({ activeRun: { run_id: id, status: 'failed', objective: '你好',
      error: { error: { code: 'agent_provider_request_failed', message: 'Provider request failed: ', details: { network_error_type: 'ConnectError' } } } } as never })
    renderChat(failed('dismiss-a'))
    const user = userEvent.setup()
    expect(screen.getByText(/连接模型服务失败/)).toBeInTheDocument()
    expect(screen.queryByText(T.agent.failedBookUntouched)).toBeNull()
    await user.click(screen.getByRole('button', { name: '关闭提示' }))
    expect(screen.queryByRole('alert')).toBeNull()
    cleanup()
    renderChat(failed('dismiss-a'))
    expect(screen.queryByRole('alert')).toBeNull()
    await user.click(screen.getByRole('button', { name: '查看上次错误' }))
    expect(screen.getByRole('alert')).toBeInTheDocument()
    cleanup()
    renderChat(failed('dismiss-b'))
    expect(screen.getByRole('alert')).toBeInTheDocument()
  })

  it('上报名对不上时从详情进入模型设置，避免改错全局服务', async () => {
    const { props } = renderChat(conversation({
      activeRun: {
        run_id: 'r-mm',
        status: 'failed',
        objective: '接着写第 6 章',
        error: { error: { code: 'agent_provider_model_mismatch', message: 'Provider reported a model identity that was not explicitly accepted: "deepseek-v4-pro-0813"', retryable: false, details: { reported_model: 'deepseek-v4-pro-0813' } } },
      } as never,
    }))
    await userEvent.setup().click(screen.getByRole('button', { name: '展开错误详情' }))
    await userEvent.setup().click(screen.getByRole('button', { name: '模型设置' }))
    expect(props.onOpenSettings).toHaveBeenCalled()
    expect(providerMock.acceptReportedModel).not.toHaveBeenCalled()
    expect(screen.getByRole('button', { name: T.agent.resend })).toBeInTheDocument()
  })

  it('别的失败不出现「认可这个名字」', () => {
    renderChat(conversation({
      activeRun: { run_id: 'r-other', status: 'failed', objective: '接着写第 6 章', error: { error: { code: 'agent_provider_incomplete_stream', message: 'Provider stream closed', retryable: true } } } as never,
    }))
    expect(screen.queryByRole('button', { name: /认可「/ })).not.toBeInTheDocument()
  })
})

describe('失败卡 · 测一下哪些模型能用', () => {
  const failedConv = () => conversation({
    activeRun: { run_id: 'r-live', status: 'failed', objective: '接着写第 6 章', error: { error: { code: 'agent_provider_http_error', message: '404 model not found', retryable: true } } } as never,
  })
  const profile = (overrides: Record<string, unknown> = {}) => ({
    provider_id: 'p-main', name: '主力线路', base_url: 'https://relay.example/v1', model: 't5-没这个模型',
    selected_models: ['t5-没这个模型', 'deepseek-v4-pro', 'glm-5.2'], reasoning_effort: 'auto', wire_format: 'chat',
    accepted_reported_models: [], context_window: '256k', api_key_set: true, configured: true, active: true, ...overrides,
  })
  const liveness = (overrides: Record<string, unknown> = {}) => ({
    model: 'deepseek-v4-pro', status: 'ok', usable: true, label: '能用', note: '', first_byte_ms: 800, elapsed_ms: 1200, reply: '好', ...overrides,
  })

  it('两通一不通：三行两个「用这个重发」，点一个就换模型重发，不自己换', async () => {
    providerMock.readProviderCatalog.mockResolvedValue({ schema: 'zhimeng.provider-catalog.v1', active_provider_id: 'p-main', providers: [profile()] } as never)
    providerMock.testProviderModelsLiveness.mockResolvedValue({
      schema: 'zhimeng.provider-model-liveness.v1', tested: 3, usable_count: 2, usable_models: ['deepseek-v4-pro', 'glm-5.2'], fastest_model: 'deepseek-v4-pro',
      results: [
        liveness({ model: 't5-没这个模型', status: 'not_found', usable: false, label: '没有这个模型', first_byte_ms: null }),
        liveness({ model: 'deepseek-v4-pro' }),
        liveness({ model: 'glm-5.2', first_byte_ms: 1500 }),
      ],
    } as never)
    const conv = failedConv()
    const { props } = renderChat(conv)
    const user = userEvent.setup()
    const card = screen.getByRole('alert')
    await user.click(within(card).getByRole('button', { name: '展开错误详情' }))
    await user.click(within(card).getByRole('button', { name: T.agent.testModels }))
    expect(providerMock.testProviderModelsLiveness).toHaveBeenCalledWith(
      { providerId: 'p-main', baseUrl: 'https://relay.example/v1', models: ['t5-没这个模型', 'deepseek-v4-pro', 'glm-5.2'] },
      expect.objectContaining({ signal: expect.any(AbortSignal) }),
    )
    await within(card).findAllByRole('button', { name: T.agent.useThisResend })
    expect(within(card).getByText('t5-没这个模型')).toBeInTheDocument()
    expect(within(card).getByText(T.settings.testResult.not_found)).toBeInTheDocument()
    expect(within(card).getByText(T.agent.firstByte('0.8'))).toBeInTheDocument()
    expect(within(card).getAllByRole('button', { name: T.agent.useThisResend })).toHaveLength(2)
    expect(within(card).getByRole('button', { name: T.agent.useAnyResend })).toBeInTheDocument()

    await user.click(within(card).getAllByRole('button', { name: T.agent.useThisResend })[0])
    await waitFor(() => expect(conv.rebindProvider).toHaveBeenCalledWith(expect.objectContaining({ provider_id: 'p-main', model: 'deepseek-v4-pro' })))
    expect(providerMock.saveProviderSettings).not.toHaveBeenCalled()
    expect(providerMock.activateProviderProfile).not.toHaveBeenCalled()
    expect(props.onProviderChanged).not.toHaveBeenCalled()
    await waitFor(() => expect(conv.send).toHaveBeenCalledWith(expect.objectContaining({ text: '接着写第 6 章' })))
    expect(within(card).getByText(T.agent.resendingWith('deepseek-v4-pro'))).toBeInTheDocument()
    expect(document.body.textContent).not.toContain('自动切换')
  })

  it('挑别的线路的模型：按线路分组，先切到那条线路再换模型重发', async () => {
    providerMock.readProviderCatalog.mockResolvedValue({
      schema: 'zhimeng.provider-catalog.v1', active_provider_id: 'p-main',
      providers: [profile(), profile({ provider_id: 'p-spare', name: '硅基流动', model: 'sf-dead', selected_models: ['sf-dead', 'sf-live'], active: false })],
    } as never)
    providerMock.testProviderModelsLiveness.mockImplementation(((...args: unknown[]) => {
      const input = (args[0] ?? {}) as { providerId?: string }
      if (input.providerId === 'p-spare') {
        return Promise.resolve({ tested: 2, usable_count: 1, usable_models: ['sf-live'], fastest_model: 'sf-live',
          results: [liveness({ model: 'sf-dead', status: 'unreachable', usable: false, label: '连不上', first_byte_ms: null }), liveness({ model: 'sf-live' })] })
      }
      return Promise.resolve({ tested: 3, usable_count: 0, usable_models: [], fastest_model: null,
        results: [
          liveness({ model: 't5-没这个模型', status: 'not_found', usable: false, label: '没有这个模型', first_byte_ms: null }),
          liveness({ model: 'deepseek-v4-pro', status: 'timeout', usable: false, label: '超时', first_byte_ms: null }),
          liveness({ model: 'glm-5.2', status: 'auth', usable: false, label: '密钥被拒', first_byte_ms: null }),
        ] })
    }) as never)
    const conv = failedConv()
    renderChat(conv)
    const user = userEvent.setup()
    const card = screen.getByRole('alert')
    await user.click(within(card).getByRole('button', { name: '展开错误详情' }))
    await user.click(within(card).getByRole('button', { name: T.agent.testModels }))
    await within(card).findByText('硅基流动')
    expect(within(card).getByText(`主力线路（${T.settings.inUse}）`)).toBeInTheDocument()
    expect(within(card).getAllByRole('button', { name: T.agent.useThisResend })).toHaveLength(1)
    await user.click(within(card).getAllByRole('button', { name: T.agent.useThisResend })[0])
    await waitFor(() => expect(conv.rebindProvider).toHaveBeenCalledWith(expect.objectContaining({ provider_id: 'p-spare', model: 'sf-live' })))
    expect(providerMock.saveProviderSettings).not.toHaveBeenCalled()
    expect(providerMock.activateProviderProfile).not.toHaveBeenCalled()
    await waitFor(() => expect(conv.send).toHaveBeenCalledWith(expect.objectContaining({ text: '接着写第 6 章' })))
    expect(document.body.textContent).not.toContain('自动切换')
  })
})
