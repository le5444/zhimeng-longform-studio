import { act, cleanup, renderHook, waitFor } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import type { AgentRun, AgentRunContinuation, AgentSession } from '../api/agent'
import { useAgentConversation } from './useAgentConversation'

const api = vi.hoisted(() => ({
  status: vi.fn(), sessions: vi.fn(), messages: vi.fn(), runs: vi.fn(), activities: vi.fn(),
  continuation: vi.fn(), createRun: vi.fn(), checkpoint: vi.fn(), admission: vi.fn(),
}))
vi.mock('../api/agent', async (original) => ({ ...await original<typeof import('../api/agent')>(),
  getAgentStatus: api.status, listAgentSessions: api.sessions, listUnboundAgentSessions: api.sessions,
  listAgentMessages: api.messages, listAgentRuns: api.runs, listAgentActivities: api.activities,
  getAgentRunContinuation: api.continuation, createAgentRun: api.createRun,
  getLatestSessionCheckpoint: api.checkpoint,
  getAgentRunAdmission: api.admission,
}))

const projectId = 'project-0123456789abcdef01234567'
const sessionId = 'session-0123456789abcdef01234567'
const fingerprint = 'd'.repeat(64)
const source = { run_id: 'run-source', session_id: sessionId, status: 'failed', objective: '完成全部资料整理',
  project_revision: { project_id: projectId, revision: 1, fingerprint }, ordinal: 1 } as AgentRun
const child = { ...source, run_id: 'run-child', status: 'succeeded' } as AgentRun
const session = { session_id: sessionId, project_id: projectId, title: '资料整理', status: 'idle', executor: {},
  updated_at: '2026-09-16T00:00:00Z' } as AgentSession
const state: AgentRunContinuation = { schema: 'zhimeng.agent-run-continuation.v1', run_id: source.run_id,
  can_continue: true, reason_code: 'interrupted', reason: '可以继续', stop_reason: null, objective: source.objective,
  continued_by_run_id: null, completed_tool_count: 3, uncertain_tool_count: 0 }
const options = { enabled: true, projectId, projectRevision: 1, projectFingerprint: fingerprint, openFilePath: null }
function deferred<T>() { let resolve!: (value: T) => void; const promise = new Promise<T>((done) => { resolve = done }); return { promise, resolve } }

beforeEach(() => {
  vi.resetAllMocks()
  localStorage.clear()
  api.admission.mockResolvedValue(null)
  api.status.mockResolvedValue({ available: true, detail: '', executor: {} })
  api.sessions.mockResolvedValue([session])
  api.messages.mockResolvedValue({ messages: [], last_ordinal: 0 })
  api.runs.mockResolvedValue([source])
  api.activities.mockImplementation(async (runId) => ({ run_id: runId, activities: [], last_sequence: 0 }))
  api.continuation.mockImplementation(async (runId) => ({ ...state, run_id: runId, can_continue: runId === source.run_id }))
  api.checkpoint.mockResolvedValue(null)
  api.createRun.mockResolvedValue({ run: child, user_message: { message_id: 'message-new', session_id: sessionId,
    run_id: child.run_id, ordinal: 1, role: 'user', content: source.objective } })
})
afterEach(cleanup)

async function ready() {
  const hook = renderHook(() => useAgentConversation(options))
  await waitFor(() => expect(hook.result.current.continuation?.can_continue).toBe(true))
  return hook
}

describe('普通 Agent 继续任务', () => {
  it('复核请求保留来源和只读挡位，消息刷新恢复来源标记', async () => {
    const review = { source_run_id: source.run_id, source_run_ordinal: 1, source_message_id: 'message-original' }
    api.messages.mockResolvedValue({ messages: [], last_ordinal: 0, review_sources: { [child.run_id]: review } })
    const { result } = await ready()
    expect(result.current.reviewSources?.[child.run_id]).toEqual(review)
    await act(() => result.current.send({ text: 'Review this task.', reviewSourceRunId: source.run_id, writerMode: 'repair_only' }))
    expect(api.createRun.mock.calls[0][1]).toMatchObject({ review_source_run_id: source.run_id, writer_mode: 'repair_only' })
    expect(api.createRun.mock.calls[0][1]).not.toHaveProperty('continuation_source_run_id')
  })

  it('把来源任务与原目标交给新 Run；不再先编资料包（纠偏 3.9）', async () => {
    const { result } = await ready()
    let continuation!: AgentRunContinuation | null
    await act(async () => { continuation = await result.current.prepareContinuation() })
    await act(() => result.current.send({ text: continuation!.objective, continuationSourceRunId: continuation!.run_id,
      writerMode: 'author_review_change_proposal' }))
    expect(api.createRun).toHaveBeenCalledWith(sessionId, expect.objectContaining({
      continuation_source_run_id: source.run_id, message: source.objective,
      writer_mode: 'author_review_change_proposal',
    }), `continue:${source.run_id}`, expect.any(Object), expect.any(AbortSignal))
    expect(api.createRun.mock.calls[0][1]).not.toHaveProperty('context_pack_id')
    expect(result.current.activeRun?.run_id).toBe(child.run_id)
    // 只创建新 Run；前端没有重放 source 的写工具或历史操作。
    expect(api.createRun).toHaveBeenCalledTimes(1)
  })

  it('独立对话继续也携带 source，双击发送只创建一次', async () => {
    api.runs.mockResolvedValue([{ ...source, project_revision: null }])
    const hook = renderHook(() => useAgentConversation({ ...options, projectId: null, projectRevision: null, projectFingerprint: null }))
    await waitFor(() => expect(hook.result.current.continuation?.can_continue).toBe(true))
    const pending = deferred<unknown>()
    api.createRun.mockReturnValue(pending.promise)
    let first!: Promise<void>
    act(() => { first = hook.result.current.send({ text: source.objective, continuationSourceRunId: source.run_id }) })
    await expect(hook.result.current.send({ text: source.objective, continuationSourceRunId: source.run_id })).rejects.toThrow('正在处理')
    await act(async () => { pending.resolve({ run: { ...child, project_revision: null }, user_message: { message_id: 'new', ordinal: 1 } }); await first })
    expect(api.createRun).toHaveBeenCalledTimes(1)
    expect(api.createRun.mock.calls[0][1]).toMatchObject({ continuation_source_run_id: source.run_id, message: source.objective })
  })

  it('接受请求但响应丢失后查询已创建 child，不重发创建请求', async () => {
    const { result } = await ready()
    api.createRun.mockImplementation(async () => {
      api.continuation.mockResolvedValue({ ...state, continued_by_run_id: child.run_id, can_continue: false, reason_code: 'already_continued' })
      api.runs.mockResolvedValue([source, child])
      throw new TypeError('Failed to fetch')
    })
    await act(() => result.current.send({ text: source.objective, continuationSourceRunId: source.run_id }))
    expect(result.current.activeRun?.run_id).toBe(child.run_id)
    expect(api.createRun).toHaveBeenCalledTimes(1)
    expect(result.current.error).toBeNull()
  })

  it('再次继续发现后台已有 child 时直接恢复，连新资料包都不再创建', async () => {
    const { result } = await ready()
    api.continuation.mockResolvedValue({ ...state, continued_by_run_id: child.run_id, can_continue: false, reason_code: 'already_continued' })
    api.runs.mockResolvedValue([source, child])
    await act(async () => { expect(await result.current.prepareContinuation()).toBeNull() })
    expect(result.current.activeRun?.run_id).toBe(child.run_id)
    expect(api.createRun).not.toHaveBeenCalled()
  })

  it('继续状态请求未返回就换书，旧返回不能改新对话或创建任务', async () => {
    const { result, rerender } = renderHook(({ id }) => useAgentConversation({ ...options, projectId: id }), { initialProps: { id: projectId } })
    await waitFor(() => expect(result.current.continuation?.can_continue).toBe(true))
    const slow = deferred<AgentRunContinuation>()
    api.continuation.mockReturnValue(slow.promise)
    let pending!: Promise<unknown>
    act(() => { pending = result.current.prepareContinuation().catch(() => undefined) })
    api.sessions.mockResolvedValue([{ ...session, session_id: 'session-other', project_id: 'project-other' }])
    api.runs.mockResolvedValue([])
    rerender({ id: 'project-other' })
    await waitFor(() => expect(result.current.session?.session_id).toBe('session-other'))
    await act(async () => { slow.resolve(state); await pending })
    expect(result.current.continuation).toBeNull()
    expect(result.current.contextState).toBe('idle')
    expect(api.createRun).not.toHaveBeenCalled()
  })

  it.each(['session_closed', 'project_rebound'])('已有 child 但 %s 时不能跳转或接续', async (reason_code) => {
    const { result } = await ready()
    api.continuation.mockResolvedValue({ ...state, reason_code, reason: '原任务现在不能继续', can_continue: false,
      continued_by_run_id: child.run_id })
    await act(async () => { await expect(result.current.prepareContinuation()).rejects.toThrow('原任务现在不能继续') })
    expect(result.current.activeRun?.run_id).toBe(source.run_id)
    expect(api.createRun).not.toHaveBeenCalled()
  })
})
