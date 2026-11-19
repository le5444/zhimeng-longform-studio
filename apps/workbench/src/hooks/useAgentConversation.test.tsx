import { act, cleanup, renderHook, waitFor } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import type { AgentActivity, AgentMessage, AgentRun, AgentRunEvent, AgentSession } from '../api/agent'
import { AgentSendUnconfirmedError, useAgentConversation } from './useAgentConversation'
import { RuntimeApiError } from '../api/runtime'
import { readPendingAgentSend, savePendingAgentSend } from './agentSendReceipt'
import { useConversationImages } from '../app/lib/useConversationImages'
import { useConversationSkills } from '../app/lib/useConversationSkills'

const api = vi.hoisted(() => ({
  status: vi.fn(), sessions: vi.fn(), unboundSessions: vi.fn(), messages: vi.fn(), runs: vi.fn(), activities: vi.fn(),
  continuation: vi.fn(), checkpoint: vi.fn(), createRun: vi.fn(), createSession: vi.fn(), createUnbound: vi.fn(),
  updateSession: vi.fn(), deleteSession: vi.fn(), getSession: vi.fn(), stream: vi.fn(), cancel: vi.fn(),
  fetch: vi.fn(), admission: vi.fn(), withdraw: vi.fn(), inspect: vi.fn(),
}))
vi.mock('../api/projects', () => ({ inspectBoundProject: api.inspect }))
vi.mock('../api/agent', async (original) => ({ ...await original<typeof import('../api/agent')>(),
  getAgentStatus: api.status, listAgentSessions: api.sessions, listUnboundAgentSessions: api.unboundSessions,
  listAgentMessages: api.messages, listAgentRuns: api.runs, listAgentActivities: api.activities,
  getAgentRunContinuation: api.continuation, getLatestSessionCheckpoint: api.checkpoint, createAgentRun: api.createRun,
  createAgentSession: api.createSession, createUnboundAgentSession: api.createUnbound, updateAgentSession: api.updateSession,
  deleteAgentSession: api.deleteSession, getAgentSession: api.getSession, streamAgentRunEvents: api.stream,
  cancelAgentRun: api.cancel,
  getAgentRunAdmission: api.admission,
  withdrawAgentRunAdmission: api.withdraw,
}))

const projectId = 'project-0123456789abcdef01234567'
const fingerprint = 'd'.repeat(64)
const bookOptions = { enabled: true, projectId, projectRevision: 3, projectFingerprint: fingerprint, openFilePath: '05_正文内容/004_玉佩上的青云.md' }
const casualOptions = { enabled: true, projectId: null, projectRevision: null, projectFingerprint: null, openFilePath: null }

function session(id: string, extra: Partial<AgentSession> = {}): AgentSession {
  return { session_id: id, title: id, status: 'idle', project_id: projectId, executor: {}, capabilities: {},
    updated_at: '2026-09-20T00:00:00Z', ...extra } as unknown as AgentSession
}
function run(id: string, status: string, extra: Partial<AgentRun> = {}): AgentRun {
  return { run_id: id, session_id: 's1', status, objective: '改开头', ordinal: 1, error: null,
    created_at: '2026-09-20T00:00:00Z', updated_at: '2026-09-20T00:00:00Z', ...extra } as unknown as AgentRun
}
function userMessage(id: string, runId: string, content: string, ordinal = 1): AgentMessage {
  return { message_id: id, session_id: 's1', run_id: runId, role: 'user', content, ordinal } as unknown as AgentMessage
}
function deferred<T>() {
  let resolve!: (value: T) => void
  let reject!: (reason: unknown) => void
  const promise = new Promise<T>((done, fail) => { resolve = done; reject = fail })
  return { promise, resolve, reject }
}

beforeEach(() => {
  vi.resetAllMocks()
  localStorage.clear()
  api.admission.mockResolvedValue(null)
  api.withdraw.mockResolvedValue({ state: 'withdrawn', accepted: null })
  api.inspect.mockResolvedValue({ project: { project_id: projectId, revision: 3, fingerprint } })
  api.status.mockResolvedValue({ available: true, detail: '', executor: {} })
  api.sessions.mockResolvedValue([session('s1')])
  api.unboundSessions.mockResolvedValue([])
  api.messages.mockResolvedValue({ messages: [], last_ordinal: 0 })
  api.runs.mockResolvedValue([])
  api.activities.mockImplementation(async (runId: string) => ({ run_id: runId, activities: [], last_sequence: 0 }))
  api.continuation.mockResolvedValue({ run_id: 'x', can_continue: false, reason_code: 'already_completed', reason: '', continued_by_run_id: null })
  api.checkpoint.mockResolvedValue(null)
  api.createRun.mockImplementation(async (_sessionId: string, body: { message: string }) => ({
    run: run('r-new', 'queued', { objective: body.message }),
    user_message: userMessage('m-new', 'r-new', body.message, 5),
  }))
  // 默认：事件流一直挂着，直到测试自己结束它。
  api.stream.mockImplementation((_runId: string, { signal }: { signal: AbortSignal }) => new Promise<void>((resolve) => signal.addEventListener('abort', () => resolve())))
})
afterEach(cleanup)

type HookOptions = Parameters<typeof useAgentConversation>[0]

async function openBook(options: HookOptions = bookOptions) {
  const hook = renderHook((props: HookOptions) => useAgentConversation(props), { initialProps: options })
  await waitFor(() => expect(hook.result.current.contextState).toBe('idle'))
  return hook
}

it('持续目标身份随发送回执持久保存，重试保留同一目标和版本', async () => {
  const hook = await openBook()
  const goal = { action: 'resume' as const, goal_id: 'goal-test-123', expected_revision: 4 }
  api.createRun.mockRejectedValueOnce(new Error('lost response'))
  await act(async () => {
    await expect(hook.result.current.send({ text: '继续目标', goal })).rejects.toThrow()
  })
  expect(readPendingAgentSend('s1')?.request.goal).toEqual(goal)
  const key = api.createRun.mock.calls[0][2]
  await act(async () => { await hook.result.current.retryPendingSend?.() })
  expect(api.createRun.mock.calls[1][1].goal).toEqual(goal)
  expect(api.createRun.mock.calls[1][2]).toBe(key)
})

it('按消息页接入过程摘要，加载更早记录合并，切会话清除旧摘要', async () => {
  api.getSession.mockResolvedValue(session('s2'))
  api.sessions.mockResolvedValue([session('s1'), session('s2')])
  api.messages.mockImplementation(async (sid, _options, _signal, before) => sid === 's2'
    ? { messages: [userMessage('s2m', 'r2', 'another session')], run_process_summaries: { r2: { has_process: false, tool_count: 0 } } }
    : before ? { messages: [userMessage('old', 'rold', 'earlier', 1)], has_earlier: false, run_process_summaries: { rold: { has_process: true, tool_count: 3 } } }
      : { messages: [userMessage('latest', 'r1', 'hello', 5)], has_earlier: true, run_process_summaries: { r1: { has_process: false, tool_count: 0 } } })
  const { result } = await openBook()
  expect(result.current.runProcessSummaries?.r1).toEqual({ has_process: false, tool_count: 0 })
  await act(() => result.current.loadEarlierMessages())
  expect(result.current.runProcessSummaries?.rold).toEqual({ has_process: true, tool_count: 3 })
  expect(result.current.runProcessSummaries?.r1.has_process).toBe(false)
  await act(() => result.current.selectConversation('s2'))
  expect(result.current.runProcessSummaries).toEqual({ r2: { has_process: false, tool_count: 0 } })
})

describe('useAgentConversation', () => {
  it('离线保留原会话、已读历史和流尾段，恢复补齐缺口且不重新执行任务', async () => {
    const page = (start: number, end: number) => ({ messages: Array.from({ length: end - start + 1 }, (_, i) => userMessage(`m${start+i}`, 'r1', `采购记录 ${start+i}`, start+i)), has_earlier: start > 1 })
    api.messages.mockResolvedValueOnce(page(1, 2))
    api.runs.mockResolvedValue([run('r1', 'running')])
    const hook = await openBook()
    await waitFor(() => expect(api.stream).toHaveBeenCalledOnce())
    const stream = api.stream.mock.calls[0][1]
    act(() => stream.onEvent({ event_id: 'r1:1', event_type: 'activity', data: { activity: {
      activity_id: 'partial', run_id: 'r1', session_id: 's1', sequence: 1, kind: 'message',
      payload: { role: 'assistant', message_id: 'answer', phase: 'delta', delta: '已核对源文件' },
    } } }))
    hook.rerender({ ...bookOptions, enabled: false })
    expect(hook.result.current.session?.session_id).toBe('s1')
    expect(hook.result.current.messages.map(m => m.ordinal)).toEqual([1, 2])
    expect(hook.result.current.streamingContent).toBe('已核对源文件')
    expect(stream.signal.aborted).toBe(true)
    expect(api.cancel).not.toHaveBeenCalled()
    api.messages.mockImplementation(async (_sid, _options, _signal, before) => !before ? page(503, 1002) : page(3, 502))
    api.runs.mockResolvedValue([run('r1', 'succeeded')])
    hook.rerender(bookOptions)
    expect(hook.result.current.messages).toHaveLength(2)
    await waitFor(() => expect(hook.result.current.contextState).toBe('idle'))
    expect(hook.result.current.messages.map(m => m.ordinal)).toEqual(Array.from({ length: 1002 }, (_, i) => i+1))
    expect(hook.result.current.activeRun?.status).toBe('succeeded')
    expect(hook.result.current.streamingContent).toBe('')
    expect(api.createRun).not.toHaveBeenCalled()
  })

  it('重连读取失败保留历史，换项目才清理旧会话', async () => {
    api.messages.mockResolvedValue({ messages: [userMessage('original', 'r1', '核对采购记录')] })
    const hook = await openBook()
    hook.rerender({ ...bookOptions, enabled: false })
    api.sessions.mockRejectedValueOnce(new Error('服务尚在恢复'))
    hook.rerender(bookOptions)
    await waitFor(() => expect(hook.result.current.error).toBe('服务尚在恢复'))
    expect(hook.result.current.session?.session_id).toBe('s1')
    expect(hook.result.current.messages[0]?.content).toBe('核对采购记录')
    expect(hook.result.current.availability).toBe('error')
    act(() => hook.result.current.refresh())
    expect(hook.result.current.messages[0]?.content).toBe('核对采购记录')
    await waitFor(() => expect(hook.result.current.contextState).toBe('idle'))
    expect(hook.result.current.error).toBeNull()
    expect(hook.result.current.availability).toBe('available')
    expect(hook.result.current.session?.session_id).toBe('s1')
    expect(api.createRun).not.toHaveBeenCalled()
    hook.rerender({ ...bookOptions, enabled: false, projectId: 'another-project' })
    expect(hook.result.current.session).toBeNull()
    expect(hook.result.current.messages).toEqual([])
  })
  it('突发回放去重后保留全部过程，done立即交付尾段', async () => {
    api.runs.mockResolvedValue([run('r1', 'running')])
    const { result } = await openBook()
    await waitFor(() => expect(api.stream).toHaveBeenCalledOnce())
    const stream = api.stream.mock.calls[0][1]
    const events = Array.from({ length: 753 }, (_, index) => ({
      event_id: `r1:${index + 1}`, event_type: 'activity', data: { activity: {
        activity_id: `replay-${index}`, session_id: 's1', run_id: 'r1', sequence: index + 1,
        kind: 'tool_result', payload: { tool_call_id: `t${index}`, tool_name: 'read_file', status: index === 0 ? 'failed' : 'succeeded' },
      } },
    }))
    act(() => { for (const event of events) { stream.onEvent(event); stream.onEvent(event) } })
    // 最后不足一批的记录还在等待短刷新；done必须立刻交付。
    act(() => stream.onEvent({ event_id: 'r1:754', event_type: 'done', data: { run: run('r1', 'succeeded') } }))
    expect(result.current.activities.map(item => item.activity_id)).toEqual(events.map(event => event.data.activity.activity_id))
    expect(result.current.activities[0].kind === 'tool_result' && result.current.activities[0].payload.status).toBe('failed')
  })

  it('断流前收到的尾段立即保留，重连从最后事件继续且不重复追加', async () => {
    api.runs.mockResolvedValue([run('r1', 'running')])
    const broken = deferred<void>()
    const done = deferred<void>()
    api.stream.mockReturnValueOnce(broken.promise).mockReturnValueOnce(done.promise)
    const { result } = await openBook()
    await waitFor(() => expect(api.stream).toHaveBeenCalledOnce())
    const event = (n: number) => ({ event_id: `r1:${n}`, event_type: 'activity', data: { activity: {
      activity_id: `a${n}`, run_id: 'r1', session_id: 's1', sequence: n, kind: 'tool_call', payload: { tool_call_id: `t${n}`, tool_name: 'read_file', arguments: {} },
    } } })
    await act(async () => {
      api.stream.mock.calls[0][1].onEvent(event(1))
      api.stream.mock.calls[0][1].onEvent(event(2))
      broken.reject(new Error('回放连接断开'))
    })
    expect(result.current.activities.map(item => item.activity_id)).toEqual(['a1', 'a2'])
    await waitFor(() => expect(api.stream).toHaveBeenCalledTimes(2))
    const next = api.stream.mock.calls[1][1]
    expect(next.lastEventId).toBe('r1:2')
    await act(async () => {
      next.onEvent(event(2)); next.onEvent(event(3))
      next.onEvent({ event_id: 'r1:4', event_type: 'done', data: { run: run('r1', 'succeeded') } })
      done.resolve()
    })
    await waitFor(() => expect(result.current.activeRun?.status).toBe('succeeded'))
    expect(result.current.activities.map(item => item.activity_id)).toEqual(['a1', 'a2', 'a3'])
    expect(api.createRun).not.toHaveBeenCalled()
  })

  it('切会话前收到但尚未刷新的尾段不会通过旧计时器串入新会话', async () => {
    api.sessions.mockResolvedValue([session('s1'), session('s2')])
    api.getSession.mockResolvedValue(session('s2'))
    api.runs.mockImplementation(async sid => sid === 's1' ? [run('r1', 'running')] : [])
    const { result } = await openBook()
    await waitFor(() => expect(api.stream).toHaveBeenCalledOnce())
    const stream = api.stream.mock.calls[0][1]
    act(() => { for (let i = 0; i < 3; i++) stream.onEvent({ event_id: `r1:${i + 1}`, event_type: 'activity', data: { activity: {
      activity_id: `pending-${i}`, run_id: 'r1', session_id: 's1', sequence: i + 1, kind: 'tool_call', payload: { tool_call_id: `t${i}`, tool_name: 'read_file', arguments: {} },
    } } }) })
    await act(() => result.current.selectConversation('s2'))
    await act(() => new Promise(resolve => setTimeout(resolve, 45)))
    expect(stream.signal.aborted).toBe(true)
    expect(result.current.session?.session_id).toBe('s2')
    expect(result.current.activities).toEqual([])
  })

  it.each(['restore', 'background'])('%s 用事件游标接回SSE，不把活动序号当作事件位置', async (kind) => {
    api.runs.mockResolvedValue([run('r1', kind === 'restore' ? 'running' : 'succeeded')])
    api.activities.mockImplementation(async (rid) => ({ run_id: rid, activities: [], last_sequence: 4, last_event_sequence: 19 }))
    const { result } = await openBook()
    if (kind === 'background') await act(() => result.current.observeBackgroundRun!(run('r2', 'running', { ordinal: 2 })))
    await waitFor(() => expect(api.stream).toHaveBeenCalledWith(kind === 'restore' ? 'r1' : 'r2', expect.objectContaining({ lastEventId: `${kind === 'restore' ? 'r1' : 'r2'}:19` })))
  })

  it('断线期间超过一页的消息全部补齐，发送回执不能掩盖中间缺口', async () => {
    const page = (start: number, end: number) => ({
      messages: Array.from({ length: end - start + 1 }, (_, i) => userMessage(`m${start + i}`, `r${start + i}`, `记录 ${start + i}`, start + i)),
      last_ordinal: 1202, has_earlier: start > 1,
    })
    api.messages.mockResolvedValueOnce(page(1, 2))
    const { result } = await openBook()
    api.createRun.mockResolvedValue({ run: run('r-new', 'queued'), user_message: userMessage('m1202', 'r-new', '继续核对', 1202) })
    await act(() => result.current.send({ text: '继续核对' }))
    api.messages.mockImplementation(async (_sid, _options, _signal, before) => !before ? page(703, 1202)
      : before === 703 ? page(203, 702) : page(1, 202))
    await act(() => result.current.refreshMessages!())
    expect(result.current.messages.map((message) => message.ordinal)).toEqual(Array.from({ length: 1202 }, (_, i) => i + 1))
    expect(api.messages).toHaveBeenCalledTimes(4)
  })

  it('补页失败保留已读历史，重试读记录不重发任务', async () => {
    api.messages.mockResolvedValueOnce({ messages: [userMessage('m1', 'r1', '原记录', 1)], last_ordinal: 1 })
    const { result } = await openBook()
    api.messages.mockResolvedValueOnce({ messages: [userMessage('m4', 'r4', '最新记录', 4)], has_earlier: true })
    api.messages.mockRejectedValueOnce(new Error('连接断开'))
    let loaded = true
    await act(async () => { loaded = await result.current.refreshMessages!() })
    expect(loaded).toBe(false)
    expect(result.current.messageSyncError).toBe('连接断开')
    expect(result.current.syncingMessages).toBe(false)
    expect(result.current.messages.map((message) => message.ordinal)).toEqual([1])
    api.messages.mockResolvedValueOnce({ messages: [userMessage('m4', 'r4', '最新记录', 4)], has_earlier: true })
    api.messages.mockResolvedValueOnce({ messages: [userMessage('m2', 'r2', '二', 2), userMessage('m3', 'r3', '三', 3)], has_earlier: true })
    await act(async () => { loaded = await result.current.refreshMessages!() })
    expect(loaded).toBe(true)
    expect(result.current.messageSyncError).toBeNull()
    expect(result.current.messages.map((message) => message.ordinal)).toEqual([1, 2, 3, 4])
    expect(api.createRun).not.toHaveBeenCalled()
  })

  it('补页游标不推进时停止，不能把缺页当成恢复成功', async () => {
    api.messages.mockResolvedValueOnce({ messages: [userMessage('m1', 'r1', '原记录', 1)], last_ordinal: 1 })
    const { result } = await openBook()
    api.messages.mockResolvedValue({ messages: [userMessage('m4', 'r4', '最新记录', 4)], has_earlier: true })
    await act(() => result.current.refreshMessages!())
    expect(result.current.messageSyncError).toContain('分页没有向前推进')
    expect(result.current.messages.map((message) => message.ordinal)).toEqual([1])
    expect(api.messages).toHaveBeenCalledTimes(3)
  })

  it('补页期间切换对话，迟到结果和错误不污染新对话', async () => {
    api.sessions.mockResolvedValue([session('s1'), session('s2')])
    api.getSession.mockResolvedValue(session('s2'))
    api.messages.mockResolvedValueOnce({ messages: [userMessage('m1', 'r1', '原记录', 1)], last_ordinal: 1 })
    const { result } = await openBook()
    const missing = deferred<never>()
    api.messages.mockResolvedValueOnce({ messages: [userMessage('m4', 'r4', '最新记录', 4)], has_earlier: true })
    api.messages.mockReturnValueOnce(missing.promise)
    let loading!: Promise<boolean>
    act(() => { loading = result.current.refreshMessages!() })
    await waitFor(() => expect(api.messages).toHaveBeenCalledTimes(3))
    await act(() => result.current.selectConversation('s2'))
    await act(async () => { missing.reject(new Error('A 的旧连接')); await loading })
    expect(result.current.session?.session_id).toBe('s2')
    expect(result.current.messages).toEqual([])
    expect(result.current.messageSyncError).toBeNull()
    expect(result.current.syncingMessages).toBe(false)
  })

  it.each(['background', 'terminal'])('%s 恢复也补齐跨页记录和每轮元数据', async (kind) => {
    api.runs.mockResolvedValue([run('r1', kind === 'terminal' ? 'running' : 'succeeded')])
    let finish!: () => void
    let emit!: (event: AgentRunEvent) => void
    api.stream.mockImplementation((_rid, { onEvent }) => new Promise<void>((resolve) => { emit = onEvent; finish = resolve }))
    api.messages.mockResolvedValueOnce({ messages: [userMessage('m1', 'r1', '原记录', 1)], last_ordinal: 1 })
    const { result } = await openBook()
    api.messages.mockResolvedValueOnce({ messages: [userMessage('m4', 'r4', '最新记录', 4)], has_earlier: true, run_process_summaries: { r4: { has_process: true, tool_count: 2 } } })
    api.messages.mockResolvedValueOnce({ messages: [userMessage('m2', 'r2', '二', 2), userMessage('m3', 'r3', '三', 3)], has_earlier: true, run_origins: { r2: 'command_completion' } })
    if (kind === 'background') await act(() => result.current.observeBackgroundRun!(run('r4', 'succeeded', { ordinal: 4 })))
    else {
      await waitFor(() => expect(api.stream).toHaveBeenCalled())
      await act(async () => {
        emit({ schema: 'zhimeng.run-event.v1', event_id: 'r1:2', event_type: 'done', emitted_at: '', data: { run: run('r1', 'succeeded') } })
        finish()
      })
    }
    await waitFor(() => expect(result.current.messages.map((message) => message.ordinal)).toEqual([1, 2, 3, 4]))
    expect(result.current.runOrigins?.r2).toBe('command_completion')
    expect(result.current.runProcessSummaries?.r4.tool_count).toBe(2)
    expect(result.current.hasEarlierMessages).toBe(false)
  })

  it.each(['保留新技能', '同名重新附加'])('受理未知时刷新后，已提交技能标签只归原回执：%s', async (scenario) => {
    api.createRun.mockReturnValue(new Promise(() => undefined))
    const useCombined = () => {
      const conversation = useAgentConversation(bookOptions)
      const [skills, setSkills, controls] = useConversationSkills(projectId, conversation.session?.session_id ?? null, conversation.contextState === 'restoring')
      return { conversation, skills, setSkills, controls }
    }
    const first = renderHook(useCombined)
    await waitFor(() => expect(first.result.current.conversation.contextState).toBe('idle'))
    act(() => first.result.current.setSkills(['文档整理']))
    const submission = first.result.current.controls.captureSubmission()
    const request = { text: '按技能整理文档', skillNames: submission.names, skillDraftIdentity: submission.draftIdentity }
    act(() => { void first.result.current.conversation.send(request) })
    await waitFor(() => expect(readPendingAgentSend('s1')).not.toBeNull())
    act(() => first.result.current.setSkills(['文档整理', '表格核对']))
    if (scenario === '同名重新附加') {
      act(() => first.result.current.setSkills(['表格核对']))
      act(() => first.result.current.setSkills(['文档整理', '表格核对']))
    }
    first.unmount()
    const restored = renderHook(useCombined)
    await waitFor(() => expect(restored.result.current.conversation.contextState).toBe('idle'))
    expect(restored.result.current.conversation.pendingSend?.item.request.manual_skill_names).toEqual(['文档整理'])
    expect(restored.result.current.skills).toEqual(scenario === '同名重新附加' ? ['文档整理', '表格核对'] : ['表格核对'])
  })
  it('idle conversation adopts a background-origin run without resetting session, injecting user text or losing old messages', async () => {
    api.runs.mockResolvedValue([run('r1', 'succeeded')])
    const oldMessage = userMessage('original-user', 'r1', '运行统计脚本', 1)
    api.messages.mockResolvedValue({ messages: [oldMessage], run_origins: { r1: 'user' } })
    const hook = await openBook()
    const background = run('r2', 'running', { ordinal: 2, origin_kind: 'command_completion' })
    const progress = { ...oldMessage, message_id: 'internal-progress', run_id: 'r2', role: 'assistant', content: '命令已退出，正在核对结果。', ordinal: 2 }
    api.messages.mockResolvedValue({ messages: [oldMessage, progress], run_origins: { r1: 'user', r2: 'command_completion' } })
    await act(() => hook.result.current.observeBackgroundRun!(background))
    expect(hook.result.current.session?.session_id).toBe('s1')
    expect(hook.result.current.messages.filter((message) => message.role === 'user')).toEqual([oldMessage])
    expect(hook.result.current.messages.at(-1)?.content).toBe(progress.content)
    expect(hook.result.current.runOrigins?.r2).toBe('command_completion')
    expect(hook.result.current.contextState).toBe('idle')
    expect(hook.result.current.switchingSession).toBe(false)
    expect(hook.result.current.running).toBe(true)
    await waitFor(() => expect(api.stream).toHaveBeenCalledWith('r2', expect.any(Object)))
    expect(api.createRun).not.toHaveBeenCalled()
  })

  it('does not replace an in-flight SSE tail or regress to an older background result', async () => {
    api.runs.mockResolvedValue([run('r1', 'running')])
    const hook = await openBook()
    const before = api.messages.mock.calls.length
    await act(() => hook.result.current.observeBackgroundRun!(run('r2', 'running', { ordinal: 2, origin_kind: 'command_completion' })))
    expect(hook.result.current.activeRun?.run_id).toBe('r1')
    expect(api.messages).toHaveBeenCalledTimes(before)
    hook.unmount()
    api.runs.mockResolvedValue([run('r3', 'succeeded', { ordinal: 3 })])
    const finished = await openBook()
    await act(() => finished.result.current.observeBackgroundRun!(run('r2', 'succeeded', { ordinal: 2, origin_kind: 'command_completion' })))
    expect(finished.result.current.activeRun?.run_id).toBe('r3')
  })

  it('late background history for A cannot overwrite B after a session switch', async () => {
    api.sessions.mockResolvedValue([session('s1'), session('s2')])
    api.getSession.mockResolvedValue(session('s2'))
    api.runs.mockResolvedValue([run('r1', 'succeeded')])
    const hook = await openBook()
    const waiting = deferred<{ messages: AgentMessage[] }>()
    api.messages.mockReturnValueOnce(waiting.promise)
    let loading!: Promise<void>
    act(() => { loading = hook.result.current.observeBackgroundRun!(run('r2', 'running', { ordinal: 2, origin_kind: 'command_completion' })) })
    api.runs.mockResolvedValue([])
    await act(() => hook.result.current.selectConversation('s2'))
    await act(async () => { waiting.resolve({ messages: [userMessage('a-late', 'r2', 'A-only')] }); await loading })
    expect(hook.result.current.session?.session_id).toBe('s2')
    expect(hook.result.current.messages).toEqual([])
    expect(hook.result.current.activeRun).toBeNull()
  })

  it('a fast already-finished internal run restores its answer without an unnecessary event stream', async () => {
    api.runs.mockResolvedValue([run('r1', 'succeeded')])
    const hook = await openBook()
    api.messages.mockResolvedValue({ messages: [{ ...userMessage('finished-bg', 'r2', '结果核对完成。', 2), role: 'assistant' }], run_origins: { r2: 'command_completion' } })
    await act(() => hook.result.current.observeBackgroundRun!(run('r2', 'succeeded', { ordinal: 2, origin_kind: 'command_completion' })))
    expect(hook.result.current.messages.at(-1)?.content).toBe('结果核对完成。')
    expect(hook.result.current.running).toBe(false)
    expect(api.stream).not.toHaveBeenCalled()
  })
  it.each(['unknown', 'accepted'])('图片发送受理%s且立刻刷新后，附件只属于原回执，不混入下一条草稿', async (admission) => {
    const image = { image_id: 'image-reload', name: 'synthetic.png', mime_type: 'image/png', width: 2, height: 2, url: '/api/v1/agent/images/image-reload' }
    api.createRun.mockReturnValue(new Promise(() => undefined))
    const useCombined = () => {
      const conversation = useAgentConversation(bookOptions)
      const images = useConversationImages(projectId, conversation.session?.session_id ?? null, conversation.contextState === 'restoring')
      return { conversation, images }
    }
    const first = renderHook(useCombined)
    await waitFor(() => expect(first.result.current.conversation.contextState).toBe('idle'))
    act(() => first.result.current.images.restore([image]))
    const submission = first.result.current.images.captureSubmission()
    const request = { text: '读取这张图片', imageIds: submission.imageIds, images: submission.images, imageDraftIdentity: submission.draftIdentity }
    act(() => { void first.result.current.conversation.send(request) })
    await waitFor(() => expect(readPendingAgentSend('s1')?.images).toEqual([image]))
    first.unmount()
    if (admission === 'accepted') api.admission.mockResolvedValue({ run: run('image-original', 'succeeded'), user_message: userMessage('image-message', 'image-original', request.text) })
    const restored = renderHook(useCombined)
    await waitFor(() => expect(restored.result.current.conversation.contextState).toBe('idle'))
    if (admission === 'unknown') expect(restored.result.current.conversation.pendingSend?.item.images).toEqual([image])
    else expect(restored.result.current.conversation.pendingSend).toBeNull()
    expect(restored.result.current.images.images).toEqual([])
  })

  it('刷新清理原图时保留后来附加的新图，以及用户重新附加的同一个imageId', async () => {
    const old = { image_id: 'same-image', name: 'same.png', mime_type: 'image/png', width: 2, height: 2, url: '/api/v1/agent/images/same-image' }
    const later = { ...old, image_id: 'new-image', name: 'new.png', url: '/api/v1/agent/images/new-image' }
    api.createRun.mockReturnValue(new Promise(() => undefined))
    const useCombined = () => {
      const conversation = useAgentConversation(bookOptions)
      const images = useConversationImages(projectId, conversation.session?.session_id ?? null, conversation.contextState === 'restoring')
      return { conversation, images }
    }
    const first = renderHook(useCombined)
    await waitFor(() => expect(first.result.current.conversation.contextState).toBe('idle'))
    act(() => first.result.current.images.restore([old]))
    const submission = first.result.current.images.captureSubmission()
    act(() => { void first.result.current.conversation.send({ text: '原图片任务', imageIds: submission.imageIds, images: submission.images, imageDraftIdentity: submission.draftIdentity }) })
    await waitFor(() => expect(readPendingAgentSend('s1')).not.toBeNull())
    act(() => {
      first.result.current.images.remove(submission.draftIdentity.clientIds[0])
      first.result.current.images.restore([old, later])
    })
    const laterClientIds = first.result.current.images.images.map((image) => image.clientId)
    expect(laterClientIds).not.toContain(submission.draftIdentity.clientIds[0])
    first.unmount()
    const restored = renderHook(useCombined)
    await waitFor(() => expect(restored.result.current.conversation.contextState).toBe('idle'))
    expect(restored.result.current.images.images.map((image) => image.clientId)).toEqual(laterClientIds)
    expect(restored.result.current.images.images.map((image) => image.image?.image_id)).toEqual(['same-image', 'new-image'])
  })

  it('首次发送创建的会话恢复时清理源草稿，后来附加的新图保留在新会话', async () => {
    api.createUnbound.mockResolvedValue(session('first-session', { project_id: null }))
    api.createRun.mockReturnValue(new Promise(() => undefined))
    const image = { image_id: 'first-image', name: 'first.png', mime_type: 'image/png', width: 2, height: 2, url: '/api/v1/agent/images/first-image' }
    const later = { ...image, image_id: 'later-image', name: 'later.png', url: '/api/v1/agent/images/later-image' }
    const useCombined = () => {
      const conversation = useAgentConversation(casualOptions)
      const images = useConversationImages(null, conversation.session?.session_id ?? null, conversation.contextState === 'restoring')
      return { conversation, images }
    }
    const first = renderHook(useCombined)
    await waitFor(() => expect(first.result.current.conversation.contextState).toBe('idle'))
    act(() => first.result.current.images.restore([image]))
    const submission = first.result.current.images.captureSubmission()
    expect(submission.draftIdentity.sessionId).toBeNull()
    act(() => { void first.result.current.conversation.send({ text: '', imageIds: submission.imageIds, images: submission.images, imageDraftIdentity: submission.draftIdentity }) })
    await waitFor(() => expect(readPendingAgentSend('first-session')?.images).toEqual([image]))
    await waitFor(() => expect(first.result.current.conversation.session?.session_id).toBe('first-session'))
    act(() => first.result.current.images.restore([later]))
    first.unmount()
    api.unboundSessions.mockResolvedValue([session('first-session', { project_id: null })])
    const restored = renderHook(useCombined)
    await waitFor(() => expect(restored.result.current.conversation.contextState).toBe('idle'))
    expect(restored.result.current.images.images.map((entry) => entry.image?.image_id)).toEqual(['later-image'])
    expect(localStorage.getItem('zhimeng.desk.composer-images:' + JSON.stringify([null, null]))).toBeNull()
    expect(restored.result.current.conversation.pendingSend?.item.images).toEqual([image])
  })

  it('选中较早的A后刷新和重挂仍回A，而不是更新时间较新的B', async () => {
    const old = session('s-old', { updated_at: '2026-09-01T00:00:00Z' })
    api.sessions.mockResolvedValue([old, session('s1')])
    api.getSession.mockResolvedValue(old)
    const first = await openBook()
    expect(first.result.current.session?.session_id).toBe('s1')
    await act(() => first.result.current.selectConversation('s-old'))
    act(() => first.result.current.refresh())
    await waitFor(() => expect(first.result.current.contextState).toBe('idle'))
    expect(first.result.current.session?.session_id).toBe('s-old')
    first.unmount()
    const second = await openBook()
    expect(second.result.current.session?.session_id).toBe('s-old')
  })

  it('自由对话和不同项目各恢复自己的选择，显式跨项目导航优先一次', async () => {
    const key = (id: string | null) => 'zhimeng.agent.selected-session:' + JSON.stringify(id)
    localStorage.setItem(key(projectId), 'project-old')
    localStorage.setItem(key(null), 'loose-old')
    api.sessions.mockImplementation(async (id) => id === projectId ? [session('project-old'), session('project-new')] : [session('other-one', { project_id: id })])
    api.unboundSessions.mockResolvedValue([session('loose-old', { project_id: null }), session('loose-new', { project_id: null })])
    const hook = await openBook()
    expect(hook.result.current.session?.session_id).toBe('project-old')
    hook.rerender(casualOptions)
    await waitFor(() => expect(hook.result.current.session?.session_id).toBe('loose-old'))
    hook.rerender({ ...bookOptions, preferredSessionId: 'project-new' })
    await waitFor(() => expect(hook.result.current.session?.session_id).toBe('project-new'))
    api.getSession.mockResolvedValue(session('project-old'))
    await act(() => hook.result.current.selectConversation('project-old'))
    act(() => hook.result.current.refresh())
    await waitFor(() => expect(hook.result.current.contextState).toBe('idle'))
    expect(hook.result.current.session?.session_id).toBe('project-old')
    expect(localStorage.getItem(key(null))).toBe('loose-old')
  })

  it.each(['missing', 'closed'])('记住的会话已%s时回退有效对话并更新记录，不留空白', async (missing) => {
    localStorage.setItem('zhimeng.agent.selected-session:' + JSON.stringify(projectId), 'invalid')
    api.sessions.mockResolvedValue([session('s1'), ...(missing === 'closed' ? [session('invalid', { status: 'closed' })] : [])])
    const hook = await openBook()
    expect(hook.result.current.session?.session_id).toBe('s1')
    expect(localStorage.getItem('zhimeng.agent.selected-session:' + JSON.stringify(projectId))).toBe('s1')
  })

  it('纯图片消息可创建真实Run，传持久附件ID而非把图片当文字提示', async () => {
    const { result } = await openBook()
    await act(() => result.current.send({ text: '', imageIds: ['image-1', 'image-1'] }))
    expect(api.createRun.mock.calls[0][1]).toMatchObject({ message: '', image_ids: ['image-1'] })
    expect(api.createRun).toHaveBeenCalledOnce()
  })

  it('含图片发送丢失回执后保留附件身份和metadata，重试仍是原请求', async () => {
    const image = { image_id: 'image-1', name: 'chart.png', mime_type: 'image/png', width: 2, height: 2, url: '/api/v1/agent/images/image-1' }
    api.createRun.mockRejectedValueOnce(new Error('断线'))
    const { result } = await openBook()
    await act(async () => { await expect(result.current.send({ text: '读图', imageIds: [image.image_id], images: [image] })).rejects.toBeInstanceOf(AgentSendUnconfirmedError) })
    const pending = readPendingAgentSend('s1')!
    expect(pending.request.image_ids).toEqual(['image-1'])
    expect(pending.images).toEqual([image])
    expect(JSON.stringify(pending)).not.toContain('data:')
    await act(() => result.current.retryPendingSend!())
    expect(api.createRun.mock.calls[1].slice(0, 3)).toEqual(api.createRun.mock.calls[0].slice(0, 3))
  })

  it.each([['move_file', 'succeeded'], ['computer_run_command', 'cancelled']])('%s结束后马上发送先等待真实快照，新请求使用新版本而不是旧界面版本', async (tool, status) => {
    api.runs.mockResolvedValue([run('previous', status)])
    api.activities.mockResolvedValue({ run_id: 'previous', last_sequence: 1, activities: [
      { activity_id: 'file-op', session_id: 's1', run_id: 'previous', sequence: 1, kind: 'tool_call', payload: { tool_call_id: 'operation', tool_name: tool, arguments: {} } },
    ] })
    const fresh = deferred<unknown>()
    api.inspect.mockReturnValue(fresh.promise)
    const { result } = await openBook()
    let sending!: Promise<void>
    act(() => { sending = result.current.send({ text: '现在检查文件' }) })
    await waitFor(() => expect(api.inspect).toHaveBeenCalledWith(projectId, expect.objectContaining({ signal: expect.any(AbortSignal) })))
    expect(api.createRun).not.toHaveBeenCalled()
    await act(async () => { fresh.resolve({ project: { project_id: projectId, revision: 5, fingerprint: 'new-fingerprint' } }); await sending })
    expect(api.createRun.mock.calls[0][1]).toMatchObject({ expected_project_revision: 5, expected_project_fingerprint: 'new-fingerprint' })
  })

  it('未确认期间切过模型，明确重试仍恢复原发送的模型快照和权限', async () => {
    const withModel = (model: string) => session('s1', {
      executor: { instance_id: model }, capabilities: { extensions: { 'x-zhimeng.provider-preference': { config: {
        provider_id: 'saved-provider', model, reasoning_effort: 'high',
      } } } },
    })
    api.sessions.mockResolvedValue([withModel('first-model')])
    api.updateSession.mockImplementation(async (_id, change) => withModel(change.model))
    api.createRun.mockRejectedValueOnce(new Error('断线'))
    const { result } = await openBook()
    await act(async () => { await expect(result.current.send({ text: '普通文档任务', writerMode: 'repair_only' })).rejects.toThrow('原内容已保留') })
    expect(readPendingAgentSend('s1')?.provider?.model).toBe('first-model')
    await act(() => result.current.rebindProvider({ provider_id: 'saved-provider', model: 'second-model', reasoning_effort: 'high' }))
    await act(() => result.current.retryPendingSend!())
    expect(api.updateSession).toHaveBeenLastCalledWith('s1', expect.objectContaining({ model: 'first-model', provider_id: 'saved-provider' }), expect.any(Object), expect.any(AbortSignal))
    expect(api.createRun.mock.calls[1][1].writer_mode).toBe('repair_only')
    expect(api.createRun.mock.calls[1][2]).toBe(api.createRun.mock.calls[0][2])
  })

  it('未受理的消息经后台原子撤销后才能取回原文，后续发送用新 key', async () => {
    api.createRun.mockRejectedValueOnce(new Error('网络中断'))
    const { result } = await openBook()
    await act(async () => { await expect(result.current.send({ text: '原文任务' })).rejects.toBeInstanceOf(AgentSendUnconfirmedError) })
    const key = api.createRun.mock.calls[0][2]
    let original: string | null = null
    await act(async () => { original = await result.current.withdrawPendingSend!() })
    expect(api.withdraw).toHaveBeenCalledWith('s1', key, expect.any(Object), expect.any(AbortSignal))
    expect(original).toBe('原文任务')
    expect(result.current.pendingSend).toBeNull()
    await act(() => result.current.send({ text: '重新编辑后的任务' }))
    expect(api.createRun.mock.calls[1][2]).not.toBe(key)
  })

  it('取回时后台已经受理就恢复原 Run，不把已发送的原文当草稿重发', async () => {
    api.createRun.mockRejectedValueOnce(new Error('网络中断'))
    const { result } = await openBook()
    await act(async () => { await expect(result.current.send({ text: '原文任务' })).rejects.toThrow('原内容已保留') })
    const accepted = { run: run('old', 'running'), user_message: userMessage('old-msg', 'old', '原文任务') }
    api.withdraw.mockResolvedValue({ state: 'accepted', accepted })
    api.runs.mockResolvedValue([accepted.run])
    api.messages.mockResolvedValue({ messages: [accepted.user_message] })
    let original: string | null = '未清空'
    await act(async () => { original = await result.current.withdrawPendingSend!() })
    expect(original).toBeNull()
    expect(result.current.activeRun?.run_id).toBe('old')
    expect(api.createRun).toHaveBeenCalledOnce()
  })

  it('撤销回执丢失后刷新识别 410，保留可取回原文，未声称仍在发送', async () => {
    savePendingAgentSend({ version: 1, sessionId: 's1', key: 'withdrawn-key', provider: null, createdAt: '', request: { message: '已取回的任务', writer_mode: 'repair_only' } })
    api.admission.mockRejectedValue(new RuntimeApiError(410, { error: { code: 'agent_admission_withdrawn' } }))
    const { result } = await openBook()
    expect(result.current.pendingSend?.status).toBe('withdrawn')
    await act(() => result.current.withdrawPendingSend!())
    expect(readPendingAgentSend('s1')).toBeNull()
    expect(api.createRun).not.toHaveBeenCalled()
  })

  it('浏览器存储写入和删除不可用时同页仍沿原身份重试，成功后不挡下一条', async () => {
    const storageWrite = vi.spyOn(Storage.prototype, 'setItem').mockImplementation(() => { throw new Error('quota') })
    api.createRun.mockRejectedValueOnce(new Error('断线'))
    const { result } = await openBook()
    try {
      await act(async () => { await expect(result.current.send({ text: '第一条' })).rejects.toBeInstanceOf(AgentSendUnconfirmedError) })
      expect(result.current.pendingSend?.item.persistent).toBe(false)
      api.createRun.mockImplementation(async (_id, body) => ({ run: run('done', 'succeeded'), user_message: userMessage('done-msg', 'done', body.message) }))
      await act(() => result.current.retryPendingSend!())
      expect(api.createRun.mock.calls[1][2]).toBe(api.createRun.mock.calls[0][2])
      expect(readPendingAgentSend('s1')).toBeNull()
      storageWrite.mockRestore()
      const storageRemove = vi.spyOn(Storage.prototype, 'removeItem').mockImplementation(() => { throw new Error('blocked') })
      try {
        await act(() => result.current.send({ text: '第二条' }))
        expect(readPendingAgentSend('s1')).toBeNull()
        await act(() => result.current.send({ text: '第三条' }))
      } finally { storageRemove.mockRestore() }
    } finally { storageWrite.mockRestore() }
  })

  it('POST 回包丢失先用原身份核对，已受理就接回同一 Run，不再 POST', async () => {
    api.createRun.mockRejectedValueOnce(new TypeError('Failed to fetch'))
    api.admission.mockResolvedValue({ run: run('accepted', 'running'), user_message: userMessage('sent', 'accepted', '检查这份表格') })
    const { result } = await openBook()
    await act(() => result.current.send({ text: '检查这份表格' }))
    expect(api.createRun).toHaveBeenCalledOnce()
    expect(api.admission).toHaveBeenCalledWith('s1', api.createRun.mock.calls[0][2], expect.any(Object), expect.any(AbortSignal))
    expect(result.current.activeRun?.run_id).toBe('accepted')
    expect(result.current.pendingSend).toBeNull()
    expect(result.current.messages.map((message) => message.content)).toEqual(['检查这份表格'])
    expect(readPendingAgentSend('s1')).toBeNull()
    expect(api.inspect).not.toHaveBeenCalled()
  })

  it('ACK 丢失后任务已经完成，也补读完整回答而不只留下用户消息', async () => {
    api.createRun.mockRejectedValueOnce(new Error('回包丢失'))
    api.admission.mockResolvedValue({ run: run('accepted', 'succeeded'), user_message: userMessage('sent', 'accepted', '检查表格') })
    const { result } = await openBook()
    api.messages.mockResolvedValue({ messages: [userMessage('sent', 'accepted', '检查表格'), { ...userMessage('answer', 'accepted', '检查完成', 2), role: 'assistant' }], last_ordinal: 2 })
    await act(() => result.current.send({ text: '检查表格' }))
    expect(result.current.messages.at(-1)?.content).toBe('检查完成')
    expect(api.createRun).toHaveBeenCalledOnce()
    expect(api.stream).not.toHaveBeenCalled()
  })

  it('查询未找到也保留精确 payload；明确重试沿用 key、附件、技能、权限和当时项目版本', async () => {
    api.createRun.mockRejectedValueOnce(new Error('网络断开'))
    const hook = await openBook()
    await act(async () => {
      await expect(hook.result.current.send({ text: '整理文件', manualFileIds: ['file-a'], skillNames: ['表格技能'], writerMode: 'author_review_change_proposal' })).rejects.toBeInstanceOf(AgentSendUnconfirmedError)
    })
    const original = api.createRun.mock.calls[0]
    expect(hook.result.current.pendingSend?.status).toBe('not_found')
    expect(readPendingAgentSend('s1')?.key).toBe(original[2])
    hook.rerender({ ...bookOptions, projectRevision: 10, openFilePath: '改开的文件.md' })
    await act(async () => { await expect(hook.result.current.send({ text: '新的草稿' })).rejects.toThrow('还没确认') })
    expect(api.createRun).toHaveBeenCalledOnce()
    await act(() => hook.result.current.retryPendingSend!())
    expect(api.createRun.mock.calls[1].slice(0, 3)).toEqual(original.slice(0, 3))
    expect(hook.result.current.pendingSend).toBeNull()
  })

  it('对账接口也离线时保留 unknown，重试先核对且不会盲发第二个请求', async () => {
    api.createRun.mockRejectedValueOnce(new TypeError('offline'))
    api.admission.mockRejectedValue(new TypeError('offline'))
    const { result } = await openBook()
    await act(async () => { await expect(result.current.send({ text: '核对一份文档' })).rejects.toThrow('尚未确认') })
    expect(result.current.pendingSend?.status).toBe('unknown')
    await act(() => result.current.retryPendingSend!())
    expect(api.createRun).toHaveBeenCalledOnce()
    expect(readPendingAgentSend('s1')).not.toBeNull()
  })

  it('刷新恢复只按 key 查询；晚到的受理接回旧 Run，不触发 POST', async () => {
    api.createRun.mockRejectedValueOnce(new Error('断线'))
    const first = await openBook()
    await act(async () => { await expect(first.result.current.send({ text: '原消息' })).rejects.toThrow('原内容已保留') })
    const key = readPendingAgentSend('s1')!.key
    first.unmount()
    api.admission.mockResolvedValue({ run: run('late', 'running'), user_message: userMessage('late-message', 'late', '原消息') })
    api.runs.mockResolvedValue([run('late', 'running')])
    const second = await openBook()
    expect(api.admission).toHaveBeenLastCalledWith('s1', key, expect.any(Object), expect.any(AbortSignal))
    expect(api.createRun).toHaveBeenCalledOnce()
    expect(second.result.current.activeRun?.run_id).toBe('late')
    expect(second.result.current.pendingSend).toBeNull()
  })

  it('刷新查不到未受理消息时只展示原文并等主动重试，不自动发送', async () => {
    savePendingAgentSend({ version: 1, sessionId: 's1', key: 'turn-saved', createdAt: '', provider: null,
      request: { message: '重启前的消息', writer_mode: 'repair_only', manual_skill_names: ['文档技能'] } })
    const { result } = await openBook()
    expect(result.current.pendingSend?.item.request.message).toBe('重启前的消息')
    expect(result.current.pendingSend?.status).toBe('not_found')
    expect(api.createRun).not.toHaveBeenCalled()
  })

  it('前一条已受理后用户确实再次发送相同文字，仍是新的发送身份', async () => {
    api.createRun.mockImplementation(async (_sid, body) => ({ run: run('done', 'succeeded'), user_message: userMessage('done-msg', 'done', body.message) }))
    const { result } = await openBook()
    await act(() => result.current.send({ text: '继续' }))
    await act(() => result.current.send({ text: '继续' }))
    expect(api.createRun).toHaveBeenCalledTimes(2)
    expect(api.createRun.mock.calls[1][2]).not.toBe(api.createRun.mock.calls[0][2])
    expect(api.admission).not.toHaveBeenCalled()
  })

  it('切到 B 后 A 迟到的已受理结果不污染 B', async () => {
    const waiting = deferred<unknown>()
    api.createRun.mockReturnValueOnce(waiting.promise)
    const hook = await openBook()
    let sending!: Promise<void>
    act(() => { sending = hook.result.current.send({ text: 'A 的任务' }) })
    await waitFor(() => expect(api.createRun).toHaveBeenCalledOnce())
    api.sessions.mockResolvedValue([session('s2', { project_id: 'project-b' })])
    hook.rerender({ ...bookOptions, projectId: 'project-b' })
    await waitFor(() => expect(hook.result.current.session?.session_id).toBe('s2'))
    await act(async () => {
      waiting.resolve({ run: run('r-a', 'running'), user_message: userMessage('m-a', 'r-a', 'A 的任务') })
      await sending
    })
    expect(hook.result.current.messages).toEqual([])
    expect(hook.result.current.activeRun).toBeNull()
    expect(readPendingAgentSend('s1')).toBeNull()
  })

  it('重试被拒绝仍保留原身份，不能把更早可能晚到的发送认作未受理', async () => {
    api.createRun.mockRejectedValueOnce(new Error('断线'))
    const { result } = await openBook()
    await act(async () => { await expect(result.current.send({ text: '修改原始文件' })).rejects.toThrow('原内容已保留') })
    api.createRun.mockRejectedValueOnce(new RuntimeApiError(409, { error: { message: '文件夹版本已变化' } }))
    await act(() => result.current.retryPendingSend!())
    expect(result.current.pendingSend?.status).toBe('not_found')
    expect(result.current.error).toBe('文件夹版本已变化')
    expect(readPendingAgentSend('s1')?.request.message).toBe('修改原始文件')
    expect(api.createRun.mock.calls[1][2]).toBe(api.createRun.mock.calls[0][2])
  })

  it('工具前阶段说明刷新入历史，下一段单独流出；迟到的刷新不抹掉新段', async () => {
    api.unboundSessions.mockResolvedValue([session('s1', { project_id: null } as never)])
    api.runs.mockResolvedValue([run('r1', 'running')])
    let emit!: (event: AgentRunEvent) => void
    api.stream.mockImplementation((_runId: string, { onEvent, signal }: { onEvent: (event: AgentRunEvent) => void; signal: AbortSignal }) => new Promise<void>((resolve) => {
      emit = onEvent
      signal.addEventListener('abort', () => resolve())
    }))
    const { result } = await openBook(casualOptions)
    await waitFor(() => expect(api.stream).toHaveBeenCalledOnce())
    const event = (sequence: number, payload: object) => ({ event_id: `r1:${sequence}`, event_type: 'activity', data: {
      activity: { activity_id: `a${sequence}`, run_id: 'r1', sequence, kind: 'message', payload },
    } }) as AgentRunEvent
    act(() => emit(event(1, { role: 'assistant', message_id: 'm1', phase: 'delta', delta: '先检查配置', content: null })))
    expect(result.current.streamingContent).toBe('先检查配置')
    const waiting = deferred<{ messages: AgentMessage[]; last_ordinal: number }>()
    api.messages.mockReturnValueOnce(waiting.promise)
    act(() => emit(event(2, { role: 'assistant', message_id: 'm1', phase: 'completed', content: '先检查配置', finish_reason: 'tool_calls' })))
    act(() => emit(event(3, { role: 'assistant', message_id: 'm2', phase: 'delta', delta: '配置正确', content: null })))
    expect(result.current.streamingContent).toBe('配置正确')
    await act(async () => waiting.resolve({ messages: [{ ...userMessage('m1', 'r1', '先检查配置'), role: 'assistant' }], last_ordinal: 1 }))
    expect(result.current.messages.map((message) => message.content)).toEqual(['先检查配置'])
    expect(result.current.streamingContent).toBe('配置正确')
    expect(api.stream).toHaveBeenCalledOnce()
  })

  it('阶段消息刷新失败保留已收到的文字；工具阶段重连不重复已保存说明', async () => {
    api.unboundSessions.mockResolvedValue([session('s1', { project_id: null } as never)])
    api.runs.mockResolvedValue([run('r1', 'running')])
    const first = { ...userMessage('m1', 'r1', '先检查配置'), role: 'assistant' }
    api.messages.mockResolvedValue({ messages: [first], last_ordinal: 1 })
    api.activities.mockResolvedValue({ run_id: 'r1', last_sequence: 2, activities: [
      { activity_id: 'a1', run_id: 'r1', sequence: 1, kind: 'message', payload: { role: 'assistant', message_id: 'm1', phase: 'completed', content: '先检查配置' } },
      { activity_id: 'a2', run_id: 'r1', sequence: 2, kind: 'message', payload: { role: 'assistant', message_id: 'm2', phase: 'delta', delta: '读取', content: null } },
    ] })
    let emit!: (event: AgentRunEvent) => void
    api.stream.mockImplementation((_runId: string, { onEvent, signal }: { onEvent: (event: AgentRunEvent) => void; signal: AbortSignal }) => new Promise<void>((resolve) => {
      emit = onEvent; signal.addEventListener('abort', () => resolve())
    }))
    const { result } = await openBook(casualOptions)
    await waitFor(() => expect(api.stream).toHaveBeenCalledOnce())
    expect(result.current.streamingContent).toBe('读取')
    act(() => emit({ event_id: 'r1:3', event_type: 'activity', data: { activity: { activity_id: 'a3', run_id: 'r1', sequence: 3, kind: 'message',
      payload: { role: 'assistant', message_id: 'm2', phase: 'delta', delta: '完成', content: null } } } } as AgentRunEvent))
    expect(result.current.streamingContent).toBe('读取完成')
    api.messages.mockRejectedValueOnce(new Error('历史暂时不可用'))
    act(() => emit({ event_id: 'r1:4', event_type: 'activity', data: { activity: { activity_id: 'a4', run_id: 'r1', sequence: 4, kind: 'message',
      payload: { role: 'assistant', message_id: 'm2', phase: 'completed', content: '读取完成', finish_reason: 'tool_calls' } } } } as AgentRunEvent))
    await waitFor(() => expect(result.current.messageSyncError).toBe('历史暂时不可用'))
    expect(result.current.streamingContent).toBe('读取完成')
    expect(result.current.messages.map((message) => message.content)).toEqual(['先检查配置'])
    api.messages.mockResolvedValueOnce({ messages: [first, { ...userMessage('m2', 'r1', '读取完成', 2), role: 'assistant' }], last_ordinal: 2 })
    await act(() => result.current.refreshMessages!())
    expect(result.current.streamingContent).toBe('')
    expect(result.current.messageSyncError).toBeNull()
    expect(result.current.messages.map((message) => message.content)).toEqual(['先检查配置', '读取完成'])
  })

  it('恢复运行中的插话边界，只展示新段流式内容，前段回答仍在消息历史中', async () => {
    api.unboundSessions.mockResolvedValue([session('s1', { project_id: null } as never)])
    api.runs.mockResolvedValue([run('r1', 'running')])
    const first = { ...userMessage('m1', 'r1', '原始结果', 1), role: 'assistant' }
    api.messages.mockResolvedValue({ messages: [first, userMessage('m2', 'r1', '改成表格', 2)], last_ordinal: 2 })
    api.activities.mockResolvedValue({ run_id: 'r1', last_sequence: 3, activities: [
      { activity_id: 'a1', run_id: 'r1', sequence: 1, kind: 'message', payload: { role: 'assistant', phase: 'completed', content: '原始结果' } },
      { activity_id: 'a2', run_id: 'r1', sequence: 2, kind: 'message', payload: { role: 'user', phase: 'completed', content: '改成表格' } },
      { activity_id: 'a3', run_id: 'r1', sequence: 3, kind: 'message', payload: { role: 'assistant', phase: 'delta', delta: '新表格' } },
    ] })
    const { result } = await openBook(casualOptions)
    expect(result.current.streamingContent).toBe('新表格')
    expect(result.current.messages.map((message) => message.content)).toEqual(['原始结果', '改成表格'])
  })

  it('本轮插话落盘后轻刷消息，保留当前流连接和同一run，不重复旧回答', async () => {
    api.unboundSessions.mockResolvedValue([session('s1', { project_id: null } as never)])
    api.runs.mockResolvedValue([run('r1', 'running')])
    let emit!: (event: AgentRunEvent) => void
    api.stream.mockImplementation((_runId: string, { onEvent, signal }: { onEvent: (event: AgentRunEvent) => void; signal: AbortSignal }) => new Promise<void>((resolve) => {
      emit = onEvent
      signal.addEventListener('abort', () => resolve())
    }))
    const { result } = await openBook(casualOptions)
    await waitFor(() => expect(api.stream).toHaveBeenCalledOnce())
    const messageEvent = (sequence: number, payload: object) => ({ schema: 'zhimeng.run-event.v1', event_id: `r1:${sequence}`, event_type: 'activity', emitted_at: '',
      data: { activity: { activity_id: `a${sequence}`, session_id: 's1', run_id: 'r1', sequence, kind: 'message', payload } } }) as AgentRunEvent
    act(() => emit(messageEvent(1, { role: 'assistant', phase: 'delta', delta: '原始结果', content: null })))
    const updated = [{ ...userMessage('m1', 'r1', '原始结果', 1), role: 'assistant' }, userMessage('m2', 'r1', '改成表格', 2)]
    api.messages.mockResolvedValue({ messages: updated, last_ordinal: 2 })
    act(() => emit(messageEvent(2, { role: 'user', phase: 'completed', content: '改成表格', message_id: 'm2' })))
    await waitFor(() => expect(result.current.messages).toHaveLength(2))
    expect(result.current.streamingContent).toBe('')
    act(() => emit(messageEvent(3, { role: 'assistant', phase: 'delta', delta: '新表格', content: null })))
    await act(async () => result.current.refreshMessages?.())
    expect(result.current.streamingContent).toBe('新表格')
    expect(result.current.activeRun?.run_id).toBe('r1')
    expect(result.current.contextState).toBe('idle')
    expect(result.current.messages).toHaveLength(2)
    expect(api.stream).toHaveBeenCalledOnce()
    expect(api.status).toHaveBeenCalledOnce()
    expect(api.createRun).not.toHaveBeenCalled()
  })

  it('恢复最近在用的那条对话（置顶的旧对话不抢），连同上一轮没收尾时流出的回答', async () => {
    api.sessions.mockResolvedValue([
      session('s-old', { pinned: true, updated_at: '2026-09-01T00:00:00Z' }),
      session('s1', { updated_at: '2026-09-20T00:00:00Z' }),
    ])
    api.runs.mockResolvedValue([run('r1', 'failed')])
    api.activities.mockResolvedValue({ run_id: 'r1', last_sequence: 2, activities: [
      { activity_id: 'a1', run_id: 'r1', sequence: 1, kind: 'message', payload: { role: 'assistant', phase: 'delta', delta: '写到一半', content: null } },
      { activity_id: 'a2', run_id: 'r1', sequence: 2, kind: 'message', payload: { role: 'assistant', phase: 'delta', delta: '的回答', content: null } },
    ] })
    const { result } = await openBook()
    expect(result.current.session?.session_id).toBe('s1')
    expect(result.current.sessions[0].session_id).toBe('s-old')
    expect(result.current.activeRun?.run_id).toBe('r1')
    expect(result.current.streamingContent).toBe('写到一半的回答')
  })

  it('开了书发一句话就一个请求：不先编资料包，只带开着哪一章和亲手附上的文件（纠偏 3.9）', async () => {
    const { result } = await openBook()
    await act(() => result.current.send({ text: '把开头改得更有画面感', writerMode: 'author_review_change_proposal',
      manualFileIds: ['file-b', 'file-a', 'file-b'], skillNames: ['网文知识库'] }))

    expect(api.createRun).toHaveBeenCalledTimes(1)
    expect(api.createRun.mock.calls[0][0]).toBe('s1')
    expect(api.createRun.mock.calls[0][1]).toEqual({
      message: '把开头改得更有画面感', expected_project_revision: 3, expected_project_fingerprint: fingerprint,
      open_file_path: '05_正文内容/004_玉佩上的青云.md', manual_file_ids: ['file-a', 'file-b'], writer_mode: 'author_review_change_proposal',
      manual_skill_names: ['网文知识库'],
    })
    expect(result.current.activeRun?.run_id).toBe('r-new')
    expect(result.current.messages.at(-1)?.content).toBe('把开头改得更有画面感')
    expect(result.current.running).toBe(true)
    expect(result.current.contextState).toBe('idle')
  })

  it('右栏没开着文件：这一轮不带 open_file_path，也不再带章号', async () => {
    const { result } = await openBook({ ...bookOptions, openFilePath: null })
    await act(() => result.current.send({ text: '看看第 5 章' }))
    const body = api.createRun.mock.calls[0][1]
    expect(body).toMatchObject({ writer_mode: 'repair_only' })
    expect(body).not.toHaveProperty('open_file_path')
    expect(body).not.toHaveProperty('active_chapter_id')
  })

  it('这本书还没有对话：发第一句时现开一条，只开一次', async () => {
    api.sessions.mockResolvedValue([])
    api.createSession.mockResolvedValue(session('s-created'))
    const { result } = await openBook()
    expect(result.current.session).toBeNull()
    await act(() => result.current.send({ text: '接着写第 6 章' }))
    expect(api.createSession).toHaveBeenCalledTimes(1)
    expect(api.createRun.mock.calls[0][0]).toBe('s-created')
    expect(result.current.session?.session_id).toBe('s-created')
  })

  it('后台明确拒绝发送不会把对话卡死：说清原因，下一句照样能发', async () => {
    const { result } = await openBook()
    api.createRun.mockRejectedValueOnce(new RuntimeApiError(400, { error: { message: '本机服务没接上' } }))
    await act(async () => {
      await expect(result.current.send({ text: '第一句' })).rejects.toThrow('本机服务没接上')
    })
    expect(result.current.error).toBe('本机服务没接上')
    expect(result.current.contextState).toBe('idle')
    expect(result.current.busy).toBe(false)
    await act(() => result.current.send({ text: '第二句' }))
    expect(api.createRun).toHaveBeenCalledTimes(2)
    expect(result.current.activeRun?.run_id).toBe('r-new')
    expect(result.current.error).toBeNull()
  })

  it('正在发的时候再发一句会被挡回，不会发出两轮', async () => {
    const { result } = await openBook()
    const pending = deferred<{ run: AgentRun; user_message: AgentMessage }>()
    api.createRun.mockReturnValueOnce(pending.promise)
    let first!: Promise<void>
    act(() => { first = result.current.send({ text: '第一句' }) })
    await waitFor(() => expect(result.current.contextState).toBe('sending'))
    await expect(result.current.send({ text: '第二句' })).rejects.toThrow('正在处理')
    await act(async () => { pending.resolve({ run: run('r-new', 'queued'), user_message: userMessage('m-new', 'r-new', '第一句') }); await first })
    expect(api.createRun).toHaveBeenCalledTimes(1)
  })

  it('没开书就是随便聊：现开一条不绑书的对话，直接开工，不建资料包', async () => {
    api.createUnbound.mockResolvedValue(session('s-casual', { project_id: null } as never))
    const { result } = await openBook(casualOptions)
    await act(() => result.current.send({ text: '帮我起五个书名', skillNames: ['网文知识库'], writerMode: 'author_review_change_proposal' }))
    expect(api.createUnbound).toHaveBeenCalledTimes(1)
    expect(api.createUnbound.mock.calls[0][0]).toBe('新对话')
    expect(api.createRun.mock.calls[0][0]).toBe('s-casual')
    // 不绑书也按作者选的挡位发（以前写死只读、后台又不认，命令照样能跑）。
    expect(api.createRun.mock.calls[0][1]).toEqual({
      message: '帮我起五个书名', writer_mode: 'author_review_change_proposal', manual_skill_names: ['网文知识库'],
    })
  })

  it('没开书选了只读：按只读发', async () => {
    api.createUnbound.mockResolvedValue(session('s-casual', { project_id: null } as never))
    const { result } = await openBook(casualOptions)
    await act(() => result.current.send({ text: '只看看', writerMode: 'repair_only' }))
    expect(api.createRun.mock.calls[0][1]).toMatchObject({ message: '只看看', writer_mode: 'repair_only' })
  })

  it.each([['项目对话', bookOptions], ['无项目对话', casualOptions]] as const)('%s 按自己保存的模型恢复并发请求，不跟随全局新模型', async (_label, options) => {
    const preferred = session('s1', {
      executor: { instance_id: 'provider-old' },
      capabilities: { extensions: { 'x-zhimeng.provider-preference': { config: { model: 'grok-4.6', provider_id: 'p-main', reasoning_effort: 'high' } } } },
    } as never)
    api.sessions.mockResolvedValue([preferred])
    api.unboundSessions.mockResolvedValue([preferred])
    api.updateSession.mockResolvedValue(session('s1', { executor: { instance_id: 'provider-new' } } as never))
    const { result } = await openBook(options)
    await act(() => result.current.send({ text: '接着写' }))
    expect(api.updateSession).toHaveBeenCalledWith('s1', { rebind_provider: true, provider_id: 'p-main', model: 'grok-4.6', reasoning_effort: 'high' },
      expect.any(Object), expect.any(AbortSignal))
    expect(api.updateSession.mock.invocationCallOrder[0]).toBeLessThan(api.createRun.mock.invocationCallOrder[0])
  })

  it('同一对话连续发送前刷新原服务身份，修改认证头后沿用精确模型选择和历史', async () => {
    const preference = { provider_id: 'session-service', model: 'session-model', reasoning_effort: 'high' }
    let effectiveIdentity = 'before-settings-change'
    let boundIdentity = effectiveIdentity
    const selected = () => session('s1', {
      executor: { instance_id: boundIdentity },
      capabilities: { extensions: { 'x-zhimeng.provider-preference': { config: preference } } },
    } as never)
    api.sessions.mockResolvedValue([selected()])
    api.updateSession.mockImplementation(async (_id, request) => {
      expect(request).toEqual({ rebind_provider: true, ...preference })
      boundIdentity = effectiveIdentity
      return selected()
    })
    let ordinal = 0
    api.createRun.mockImplementation(async (_id, request) => {
      if (boundIdentity !== effectiveIdentity) throw new RuntimeApiError(409, { error: { code: 'workflow_executor_changed', message: 'Agent session executor configuration changed' } })
      ordinal += 1
      return { run: run(`r-${ordinal}`, 'succeeded'), user_message: userMessage(`m-${ordinal}`, `r-${ordinal}`, request.message, ordinal) }
    })
    const { result } = await openBook()
    await act(() => result.current.send({ text: '第一轮文件检查' }))
    expect(result.current.session?.executor.instance_id).toBe('before-settings-change')
    // 设置保存、外部编辑或另一浏览器更新了同一服务的凭据；全局默认与本会话无关。
    effectiveIdentity = 'after-settings-change'
    await act(() => result.current.send({ text: '第二轮文件检查' }))
    expect(api.updateSession).toHaveBeenCalledTimes(2)
    expect(result.current.session?.executor.instance_id).toBe('after-settings-change')
    expect(result.current.messages.map((message) => message.content)).toEqual(['第一轮文件检查', '第二轮文件检查'])
    expect(api.createRun).toHaveBeenCalledTimes(2)
  })

  it('旧服务已删除仍能打开和刷新历史，发送明确失败且保留原模型，手动重选可恢复', async () => {
    const old = session('s-old', { updated_at: '2026-09-01T00:00:00Z', executor: { instance_id: 'deleted-instance' },
      capabilities: { extensions: { 'x-zhimeng.provider-preference': {
        config: { provider_id: 'deleted-service', model: 'old-model', reasoning_effort: 'high' },
      } } } } as never)
    const unavailable = new RuntimeApiError(503, { error: { code: 'agent_unavailable', message: '原模型服务不可用' } })
    api.sessions.mockResolvedValue([session('s1'), old])
    api.getSession.mockResolvedValue(old)
    api.updateSession.mockRejectedValue(unavailable)
    api.messages.mockImplementation(async (sid) => ({ messages: sid === 's-old' ? [userMessage('old-message', 'old-run', '保留的历史内容')] : [] }))
    const { result } = await openBook()
    await act(() => result.current.selectConversation('s-old'))
    expect(result.current.session).toEqual(old)
    expect(result.current.messages[0]?.content).toBe('保留的历史内容')
    expect(api.updateSession).not.toHaveBeenCalled()
    expect(result.current.error).toBeNull()
    await act(async () => { await expect(result.current.send({ text: '继续检查文件' })).rejects.toThrow('原模型服务不可用') })
    expect(api.createRun).not.toHaveBeenCalled()
    expect(result.current.session).toEqual(old)
    expect(api.updateSession.mock.calls.every(([, request]) => request.provider_id === 'deleted-service')).toBe(true)
    act(() => result.current.refresh())
    await waitFor(() => expect(result.current.contextState).toBe('idle'))
    expect(result.current.session).toEqual(old)
    expect(result.current.messages[0]?.content).toBe('保留的历史内容')
    const rebound = session('s-old', { capabilities: { extensions: { 'x-zhimeng.provider-preference': {
      config: { provider_id: 'replacement', model: 'new-model', reasoning_effort: 'high' },
    } } } } as never)
    api.updateSession.mockResolvedValueOnce(rebound)
    await act(() => result.current.rebindProvider({ provider_id: 'replacement', model: 'new-model', reasoning_effort: 'high' }))
    expect(result.current.session).toEqual(rebound)
    expect(result.current.messages[0]?.content).toBe('保留的历史内容')
  })

  it('恢复历史的读取错误不伪装成功', async () => {
    const other = session('s-other', { updated_at: '2026-09-01T00:00:00Z',
      capabilities: { extensions: { 'x-zhimeng.provider-preference': { config: { provider_id: 'old', model: 'old-model', reasoning_effort: 'auto' } } } } } as never)
    api.sessions.mockResolvedValue([session('s1'), other])
    api.getSession.mockRejectedValue(new RuntimeApiError(503, { error: { code: 'backend_not_ready', message: '服务正在重启' } }))
    const { result } = await openBook()
    await act(async () => { await expect(result.current.selectConversation('s-other')).rejects.toThrow('服务正在重启') })
    expect(result.current.session?.session_id).toBe('s1')
  })

  it('只切当前对话的模型，其他对话保持；切换未完成时不发送，失败保留旧模型并抛错', async () => {
    const original = session('s1', { capabilities: { extensions: { 'x-zhimeng.provider-preference': {
      config: { provider_id: 'p-main', model: 'old-model', reasoning_effort: 'high' },
    } } } })
    const other = session('s2', { updated_at: '2026-09-01T00:00:00Z' })
    api.sessions.mockResolvedValue([original, other])
    const { result } = await openBook()
    const pending = deferred<AgentSession>()
    api.updateSession.mockReturnValueOnce(pending.promise)
    let switching!: Promise<void>
    act(() => { switching = result.current.rebindProvider({ provider_id: 'p-main', model: 'new-model', reasoning_effort: 'ultra' }) })
    await waitFor(() => expect(result.current.busy).toBe(true))
    await expect(result.current.send({ text: '检查文件' })).rejects.toThrow('正在处理')
    await act(async () => {
      pending.reject(new Error('切换被拒绝'))
      await expect(switching).rejects.toThrow('切换被拒绝')
    })
    expect(result.current.session).toEqual(original)
    expect(result.current.sessions.find((item) => item.session_id === 's2')).toEqual(other)
    expect(result.current.busy).toBe(false)
    expect(result.current.error).toBe('切换被拒绝')
    expect(api.createRun).not.toHaveBeenCalled()

    const changed = { ...original, capabilities: { extensions: { 'x-zhimeng.provider-preference': {
      config: { provider_id: 'p-main', model: 'new-model', reasoning_effort: 'ultra' },
    } } } }
    api.updateSession.mockResolvedValueOnce(changed)
    await act(() => result.current.rebindProvider({ provider_id: 'p-main', model: 'new-model', reasoning_effort: 'ultra' }))
    expect(result.current.session).toEqual(changed)
    expect(result.current.sessions.find((item) => item.session_id === 's2')).toEqual(other)
  })

  it('流式：第一段字一来就显示；收尾后换成存下的正式回答', async () => {
    api.runs.mockResolvedValue([run('r1', 'running')])
    let emit!: (event: AgentRunEvent) => void
    let finish!: () => void
    api.stream.mockImplementation((_runId: string, { onEvent }: { onEvent: (event: AgentRunEvent) => void }) => new Promise<void>((resolve) => {
      emit = onEvent
      finish = resolve
    }))
    const { result } = await openBook()
    await waitFor(() => expect(api.stream).toHaveBeenCalled())
    const delta = { activity_id: 'a1', session_id: 's1', run_id: 'r1', sequence: 1, kind: 'message',
      payload: { role: 'assistant', phase: 'delta', delta: '柳青烟', content: null } } as unknown as AgentActivity
    act(() => emit({ schema: 'zhimeng.run-event.v1', event_id: 'r1:1', event_type: 'activity', emitted_at: '', data: { activity: delta } }))
    expect(result.current.streamingContent).toBe('柳青烟')
    api.messages.mockResolvedValue({ messages: [userMessage('m1', 'r1', '改一下'), { message_id: 'm2', session_id: 's1', run_id: 'r1',
      role: 'assistant', content: '柳青烟的手抖了一下。', ordinal: 2 }], last_ordinal: 2 })
    await act(async () => {
      emit({ schema: 'zhimeng.run-event.v1', event_id: 'r1:2', event_type: 'done', emitted_at: '', data: { run: run('r1', 'succeeded') } })
      finish()
    })
    await waitFor(() => expect(result.current.activeRun?.status).toBe('succeeded'))
    expect(result.current.streamingContent).toBe('')
    expect(result.current.messages.at(-1)?.content).toBe('柳青烟的手抖了一下。')
    expect(result.current.running).toBe(false)
  })

  it('长对话打开时取最新一页；「显示更早的消息」往前补；一轮结束刷新时补出来的不丢', async () => {
    const say = (id: string, role: 'user' | 'assistant', content: string, ordinal: number) =>
      ({ message_id: id, session_id: 's1', run_id: `r${ordinal}`, role, content, ordinal }) as unknown as AgentMessage
    api.runs.mockResolvedValue([run('r1', 'running')])
    let finish!: () => void
    let emit!: (event: AgentRunEvent) => void
    api.stream.mockImplementation((_runId: string, { onEvent }: { onEvent: (event: AgentRunEvent) => void }) => new Promise<void>((resolve) => {
      emit = onEvent
      finish = resolve
    }))
    api.messages.mockResolvedValueOnce({ messages: [say('m3', 'user', '三', 3), say('m4', 'assistant', '四', 4)], last_ordinal: 4, has_earlier: true })
    const { result } = await openBook()
    expect(api.messages).toHaveBeenCalledWith('s1', expect.any(Object), expect.any(AbortSignal))
    expect(result.current.hasEarlierMessages).toBe(true)

    api.messages.mockResolvedValueOnce({ messages: [say('m1', 'user', '一', 1), say('m2', 'assistant', '二', 2)], last_ordinal: 4, has_earlier: false })
    await act(() => result.current.loadEarlierMessages())
    expect(api.messages).toHaveBeenLastCalledWith('s1', expect.any(Object), expect.any(AbortSignal), 3)
    expect(result.current.messages.map((m) => m.content)).toEqual(['一', '二', '三', '四'])
    expect(result.current.hasEarlierMessages).toBe(false)

    // 这一轮结束，刷新拿到的仍是「最新一页」（这里从第 3 条起）：前面补出来的两条留着，不重复。
    await waitFor(() => expect(api.stream).toHaveBeenCalled())
    api.messages.mockResolvedValueOnce({ messages: [say('m3', 'user', '三', 3), say('m4', 'assistant', '四', 4), say('m5', 'assistant', '五', 5)], last_ordinal: 5, has_earlier: true })
    await act(async () => {
      emit({ schema: 'zhimeng.run-event.v1', event_id: 'r1:2', event_type: 'done', emitted_at: '', data: { run: run('r1', 'succeeded') } })
      finish()
    })
    await waitFor(() => expect(result.current.messages.map((m) => m.content)).toEqual(['一', '二', '三', '四', '五']))
    expect(result.current.hasEarlierMessages).toBe(false)
  })

  it('发下一句时上一轮的步骤挪进「前几轮」，不跟着清掉；旧的一轮要看时再读', async () => {
    api.runs.mockResolvedValue([run('r1', 'succeeded')])
    const read = { activity_id: 'a1', session_id: 's1', run_id: 'r1', sequence: 1, kind: 'tool_call',
      payload: { tool_call_id: 'c1', tool_name: 'project_read_file', arguments: {} } } as unknown as AgentActivity
    api.activities.mockImplementation(async (runId: string) => ({ run_id: runId, activities: runId === 'r1' ? [read] : [], last_sequence: 1 }))
    const { result } = await openBook()
    expect(result.current.activities).toEqual([read])
    expect(result.current.pastActivities).toEqual({})

    await act(() => result.current.send({ text: '接着改' }))
    expect(result.current.activeRun?.run_id).toBe('r-new')
    expect(result.current.activities).toEqual([])
    expect(result.current.pastActivities).toEqual({ r1: [read] })

    await act(() => result.current.loadRunActivities('r0'))
    expect(api.activities).toHaveBeenLastCalledWith('r0', 0, expect.any(Object), expect.any(AbortSignal))
    expect(result.current.pastActivities).toEqual({ r1: [read], r0: [] })
  })

  it('换书时断开这一轮的事件流', async () => {
    api.runs.mockResolvedValue([run('r1', 'running')])
    let streamSignal!: AbortSignal
    api.stream.mockImplementation((_runId: string, { signal }: { signal: AbortSignal }) => {
      streamSignal = signal
      return new Promise<void>((resolve) => signal.addEventListener('abort', () => resolve()))
    })
    const hook = await openBook()
    await waitFor(() => expect(api.stream).toHaveBeenCalled())
    hook.rerender({ ...bookOptions, projectId: 'project-other-0000000000000000' })
    expect(streamSignal.aborted).toBe(true)
  })

  it('重复停止的接口先回终态仍等SSE尾部工具结果和done，文件版本不会被提前断流丢掉', async () => {
    api.runs.mockResolvedValue([run('r1', 'running')])
    api.cancel.mockResolvedValueOnce(run('r1', 'cancelling')).mockResolvedValueOnce(run('r1', 'cancelled'))
    let emit!: (event: AgentRunEvent) => void
    let finish!: () => void
    let streamSignal!: AbortSignal
    api.stream.mockImplementation((_id, { onEvent, signal }) => new Promise<void>((resolve) => {
      emit = onEvent; finish = resolve; streamSignal = signal
      signal.addEventListener('abort', resolve)
    }))
    const { result } = await openBook()
    await waitFor(() => expect(api.stream).toHaveBeenCalledOnce())
    await act(() => result.current.cancel())
    expect(api.cancel).toHaveBeenCalledWith('r1', expect.any(Object), expect.any(AbortSignal))
    expect(result.current.activeRun?.status).toBe('cancelling')
    await act(() => result.current.cancel())
    expect(result.current.activeRun?.status).toBe('cancelling')
    expect(streamSignal.aborted).toBe(false)
    const observed = { activity_id: 'mcp-observed', session_id: 's1', run_id: 'r1', sequence: 7, kind: 'tool_result',
      payload: { tool_call_id: 'mcp-write', tool_name: 'mcp__write', status: 'cancelled', output: { source_mcp: true, read_only: false, result_revision: 8, result_fingerprint: 'e'.repeat(64) } } } as unknown as AgentActivity
    act(() => emit({ schema: 'zhimeng.run-event.v1', event_id: 'r1:7', event_type: 'activity', emitted_at: '', data: { activity: observed } }))
    expect(result.current.activities).toContainEqual(observed)
    expect(streamSignal.aborted).toBe(false)
    await act(async () => {
      emit({ schema: 'zhimeng.run-event.v1', event_id: 'r1:8', event_type: 'done', emitted_at: '', data: { run: run('r1', 'cancelled') } })
      finish()
    })
    await waitFor(() => expect(result.current.activeRun?.status).toBe('cancelled'))
    expect(result.current.activities).toContainEqual(observed)
    expect(streamSignal.aborted).toBe(true)
    expect(api.stream).toHaveBeenCalledOnce()
  })

  it('迟到的停止中响应不能把已经完成SSE的任务重新激活', async () => {
    api.runs.mockResolvedValue([run('r1', 'running')])
    const response = deferred<AgentRun>()
    api.cancel.mockReturnValue(response.promise)
    let emit!: (event: AgentRunEvent) => void
    let finish!: () => void
    api.stream.mockImplementation((_id, { onEvent, signal }) => new Promise<void>((resolve) => {
      emit = onEvent; finish = resolve; signal.addEventListener('abort', resolve)
    }))
    const { result } = await openBook()
    await waitFor(() => expect(api.stream).toHaveBeenCalledOnce())
    let pending!: Promise<void>
    act(() => { pending = result.current.cancel() })
    await act(async () => {
      emit({ schema: 'zhimeng.run-event.v1', event_id: 'r1:8', event_type: 'done', emitted_at: '', data: { run: run('r1', 'cancelled') } })
      finish()
    })
    await waitFor(() => expect(result.current.activeRun?.status).toBe('cancelled'))
    await act(async () => { response.resolve(run('r1', 'cancelling')); await pending })
    expect(result.current.activeRun?.status).toBe('cancelled')
    expect(api.stream).toHaveBeenCalledOnce()
  })

  it.each(['同会话新任务', '另一会话', '另一项目'])('旧停止响应晚于%s时不会覆盖新任务或挂上旧错误', async (target) => {
    api.runs.mockResolvedValue([run('r1', 'running')])
    api.sessions.mockResolvedValue([session('s1'), session('s2')])
    api.getSession.mockResolvedValue(session('s2'))
    const firstResponse = deferred<AgentRun>()
    const secondResponse = deferred<AgentRun>()
    api.cancel.mockReturnValueOnce(firstResponse.promise).mockReturnValueOnce(secondResponse.promise)
    let emit!: (event: AgentRunEvent) => void
    let finish!: () => void
    api.stream.mockImplementation((_id, { onEvent, signal }) => new Promise<void>((resolve) => {
      emit = onEvent; finish = resolve; signal.addEventListener('abort', resolve)
    }))
    const hook = await openBook()
    await waitFor(() => expect(api.stream).toHaveBeenCalledOnce())
    let first!: Promise<void>, second!: Promise<void>
    act(() => { first = hook.result.current.cancel(); second = hook.result.current.cancel() })
    await act(async () => {
      emit({ schema: 'zhimeng.run-event.v1', event_id: 'r1:8', event_type: 'done', emitted_at: '', data: { run: run('r1', 'cancelled') } })
      finish()
    })
    await waitFor(() => expect(hook.result.current.activeRun?.status).toBe('cancelled'))
    if (target === '同会话新任务') await act(() => hook.result.current.send({ text: '开始新任务' }))
    else {
      api.runs.mockResolvedValue([run('r-next', 'running', { session_id: 's2' })])
      if (target === '另一会话') await act(() => hook.result.current.selectConversation('s2'))
      else {
        api.sessions.mockResolvedValue([session('s2', { project_id: 'project-other' })])
        hook.rerender({ ...bookOptions, projectId: 'project-other' })
        await waitFor(() => expect(hook.result.current.contextState).toBe('idle'))
      }
    }
    const expectedRunId = target === '同会话新任务' ? 'r-new' : 'r-next'
    await waitFor(() => expect(hook.result.current.activeRun?.run_id).toBe(expectedRunId))
    await act(async () => {
      firstResponse.resolve(run('r1', 'cancelling')); secondResponse.reject(new Error('上一轮停止请求迟到失败'))
      await Promise.all([first, second])
    })
    expect(hook.result.current.activeRun?.run_id).toBe(expectedRunId)
    expect(hook.result.current.error).toBeNull()
  })

  it('改名、置顶、删除都走后台的对话记录', async () => {
    api.sessions.mockResolvedValue([session('s1'), session('s2', { updated_at: '2026-09-10T00:00:00Z' })])
    api.updateSession.mockImplementation(async (id: string, patch: Record<string, unknown>) => session(id, patch as never))
    api.deleteSession.mockResolvedValue(undefined)
    const { result } = await openBook()
    await act(() => result.current.renameConversation('s1', '  第五章返工  '))
    expect(api.updateSession).toHaveBeenLastCalledWith('s1', { title: '第五章返工' }, expect.any(Object), expect.any(AbortSignal))
    expect(result.current.session?.title).toBe('第五章返工')
    await act(() => result.current.setConversationPinned('s2', true))
    expect(result.current.sessions[0].session_id).toBe('s2')
    await act(() => result.current.deleteConversation('s2'))
    expect(result.current.sessions.map((item) => item.session_id)).toEqual(['s1'])
    await expect(result.current.renameConversation('s1', '   ')).rejects.toThrow('不能为空')
  })

  it('运行时可切换、发送另一任务并返回原流，停止只作用于当前任务', async () => {
    api.sessions.mockResolvedValue([session('s1'), session('s2')])
    api.getSession.mockImplementation(async (id) => session(id))
    const runs = new Map([['s1', run('r1', 'running')]])
    api.runs.mockImplementation(async (id) => runs.has(id) ? [runs.get(id)!] : [])
    api.createRun.mockImplementation(async (id, body) => {
      const created = run('r2', 'running', { session_id: id })
      runs.set(id, created)
      return { run: created, user_message: { ...userMessage('m2', 'r2', body.message), session_id: id } }
    })
    const { result } = await openBook()
    await waitFor(() => expect(api.stream).toHaveBeenCalledWith('r1', expect.any(Object)))
    const oldStream = api.stream.mock.calls[0][1]
    await act(() => result.current.selectConversation('s2'))
    expect(oldStream.signal.aborted).toBe(true)
    expect(result.current.running).toBe(false)
    expect(api.updateSession).not.toHaveBeenCalled()
    await act(() => result.current.send({ text: '计算 17 + 25' }))
    expect(result.current.activeRun?.run_id).toBe('r2')
    await act(() => result.current.selectConversation('s1'))
    expect(result.current.activeRun?.run_id).toBe('r1')
    expect(result.current.running).toBe(true)
    await waitFor(() => expect(api.stream.mock.calls.filter(([id]) => id === 'r1')).toHaveLength(2))
    expect(api.cancel).not.toHaveBeenCalled()
    await act(() => result.current.selectConversation('s2'))
    api.cancel.mockResolvedValue(run('r2', 'cancelling', { session_id: 's2' }))
    await act(() => result.current.cancel())
    expect(api.cancel).toHaveBeenCalledExactlyOnceWith('r2', expect.any(Object), expect.any(AbortSignal))
  })

  it('切走后迟到的流事件不能把旧回答、步骤或终态写入新对话', async () => {
    api.sessions.mockResolvedValue([session('s1'), session('s2')])
    api.getSession.mockResolvedValue(session('s2'))
    api.runs.mockImplementation(async (id) => id === 's1' ? [run('r1', 'running')] : [])
    const { result } = await openBook()
    await waitFor(() => expect(api.stream).toHaveBeenCalled())
    const oldStream = api.stream.mock.calls[0][1]
    await act(() => result.current.selectConversation('s2'))
    act(() => oldStream.onEvent({ event_id: 'r1:99', event_type: 'activity', data: { activity: {
      activity_id: 'late', run_id: 'r1', session_id: 's1', kind: 'message', sequence: 99,
      payload: { role: 'assistant', phase: 'delta', delta: 'A-only', message_id: 'old-message' },
    } } }))
    expect(result.current.session?.session_id).toBe('s2')
    expect(result.current.streamingContent).toBe('')
    expect(result.current.activities).toEqual([])
    expect(result.current.activeRun).toBeNull()
  })

  it('快速连续导航以最后一次选择为准，旧加载不能覆盖或解锁新加载', async () => {
    api.sessions.mockResolvedValue([session('s1'), session('s2'), session('s3')])
    const second = deferred<AgentSession>()
    const third = deferred<AgentSession>()
    api.getSession.mockImplementation((id) => id === 's2' ? second.promise : third.promise)
    const { result } = await openBook()
    let b!: Promise<void>
    let c!: Promise<void>
    act(() => { b = result.current.selectConversation('s2') })
    act(() => { c = result.current.selectConversation('s3') })
    await act(async () => { second.resolve(session('s2')); await b })
    expect(result.current.switchingSession).toBe(true)
    await act(async () => { third.resolve(session('s3')); await c })
    expect(result.current.session?.session_id).toBe('s3')
    expect(result.current.switchingSession).toBe(false)
    expect(result.current.error).toBeNull()
  })

  it('发送确认尚未返回也能切走，迟到的受理归原会话，返回恢复原任务', async () => {
    api.sessions.mockResolvedValue([session('s1'), session('s2')])
    api.getSession.mockImplementation(async (id) => session(id))
    const accepted = deferred<{ run: AgentRun; user_message: AgentMessage }>()
    api.createRun.mockReturnValue(accepted.promise)
    const { result } = await openBook()
    let sending!: Promise<void>
    act(() => { sending = result.current.send({ text: '整理普通文档' }) })
    await waitFor(() => expect(api.createRun).toHaveBeenCalled())
    await act(() => result.current.selectConversation('s2'))
    await act(async () => { accepted.resolve({ run: run('r1', 'running'), user_message: userMessage('m1', 'r1', '整理普通文档') }); await sending })
    expect(result.current.session?.session_id).toBe('s2')
    expect(result.current.messages).toEqual([])
    expect(result.current.busy).toBe(false)
    api.runs.mockImplementation(async (id) => id === 's1' ? [run('r1', 'running')] : [])
    await act(() => result.current.selectConversation('s1'))
    expect(result.current.activeRun?.run_id).toBe('r1')
    expect(api.createRun).toHaveBeenCalledTimes(1)
  })

  it.each([bookOptions, casualOptions])('运行时可以新建对话，原任务不取消：$projectId', async (options) => {
    api.sessions.mockResolvedValue([session('s1')])
    api.unboundSessions.mockResolvedValue([session('s1', { project_id: null })])
    api.runs.mockImplementation(async (id) => id === 's1' ? [run('r1', 'running')] : [])
    api.createSession.mockResolvedValue(session('s-new'))
    api.createUnbound.mockResolvedValue(session('s-new', { project_id: null }))
    const { result } = await openBook(options)
    await act(() => result.current.createConversation())
    expect(result.current.session?.session_id).toBe('s-new')
    expect(result.current.running).toBe(false)
    expect(api.cancel).not.toHaveBeenCalled()
  })

  it('导航读取失败后重新接回原任务，仍可继续接收回答', async () => {
    api.runs.mockResolvedValue([run('r1', 'running')])
    const { result } = await openBook()
    await waitFor(() => expect(api.stream).toHaveBeenCalledTimes(1))
    api.getSession.mockRejectedValue(new Error('历史暂时不可读'))
    await act(async () => { await expect(result.current.selectConversation('s2')).rejects.toThrow('历史暂时不可读') })
    await waitFor(() => expect(api.stream).toHaveBeenCalledTimes(2))
    expect(result.current.session?.session_id).toBe('s1')
    expect(api.stream.mock.calls[1][1].signal.aborted).toBe(false)
    expect(result.current.running).toBe(true)
  })

  it('切回后台已完成的任务直接恢复保存的回答，不重新提交', async () => {
    api.sessions.mockResolvedValue([session('s1'), session('s2')])
    api.getSession.mockImplementation(async (id) => session(id))
    api.runs.mockImplementation(async (id) => id === 's1' ? [run('r1', 'running')] : [])
    const { result } = await openBook()
    await act(() => result.current.selectConversation('s2'))
    const answer = { ...userMessage('answer', 'r1', '合计 42'), role: 'assistant' } as AgentMessage
    api.runs.mockImplementation(async (id) => id === 's1' ? [run('r1', 'succeeded')] : [])
    api.messages.mockImplementation(async (id) => ({ messages: id === 's1' ? [answer] : [] }))
    await act(() => result.current.selectConversation('s1'))
    expect(result.current.messages).toEqual([answer])
    expect(result.current.running).toBe(false)
    expect(api.createRun).not.toHaveBeenCalled()
    expect(api.cancel).not.toHaveBeenCalled()
  })

  it('同项目导航的在途读取也会在卸载时中止', async () => {
    const waiting = deferred<AgentSession>()
    api.getSession.mockReturnValue(waiting.promise)
    const hook = await openBook()
    let pending!: Promise<void>
    act(() => { pending = hook.result.current.selectConversation('s2') })
    const signal = api.getSession.mock.calls[0][2]
    hook.unmount()
    expect(signal.aborted).toBe(true)
    await act(async () => { waiting.resolve(session('s2')); await pending })
  })

  it('运行中当前对话的压缩仍由真实任务状态约束', async () => {
    api.sessions.mockResolvedValue([session('s1'), session('s2', { updated_at: '2026-09-10T00:00:00Z' })])
    api.runs.mockResolvedValue([run('r1', 'running')])
    const { result } = await openBook()
    await expect(result.current.compactConversation()).rejects.toThrow('尚未结束')
  })
})
