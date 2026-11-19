/**
 * 修改卡：默认「直接保存」——卡一挂出来就自动存进书里，不用作者逐张点头；
 * 设置成「先看修改」后保持待确认。被检查拦下的仍留在卡里等作者看。
 */
import { act, cleanup, renderHook, waitFor } from '@testing-library/react'
import { StrictMode } from 'react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import type { AgentActivity, AgentRun } from '../../api/agent'
import { useChangeCards, type UseChangeCardsOptions } from '../chat/useChangeCards'

afterEach(cleanup)

const api = vi.hoisted(() => ({
  getChangeSet: vi.fn(),
  applyChangeSetDirect: vi.fn(),
  discardChangeSet: vi.fn(),
  rollbackChangeSet: vi.fn(),
  agentRunChangeSetId: vi.fn(),
  listProjectChangeSets: vi.fn(),
}))

vi.mock('../../api/changeSets', () => ({
  getChangeSet: api.getChangeSet,
  applyChangeSetDirect: api.applyChangeSetDirect,
  discardChangeSet: api.discardChangeSet,
  rollbackChangeSet: api.rollbackChangeSet,
  listProjectChangeSets: api.listProjectChangeSets,
}))

vi.mock('../../lib/candidateChangeSet', () => ({
  agentRunChangeSetId: api.agentRunChangeSetId,
}))

function makeRun(status: string): AgentRun {
  return {
    run_id: 'run-1',
    session_id: 'session-1',
    status,
    ordinal: 1,
    objective: '改一下',
    created_at: '2026-09-15T00:00:00Z',
    updated_at: '2026-09-15T00:00:00Z',
  } as unknown as AgentRun
}

function renderCards(status: string) {
  const onBookChanged = vi.fn()
  const utils = renderHook(
    ({ run }: { run: AgentRun }) =>
      useChangeCards({
        sessionId: 'session-1',
        activeRun: run,
        activities: [],
        onBookChanged,
      }),
    { initialProps: { run: makeRun('running') } },
  )
  return { ...utils, onBookChanged, succeed: () => utils.rerender({ run: makeRun(status) }) }
}

describe('useChangeCards 保存方式', () => {
  beforeEach(() => {
    window.localStorage.clear()
    vi.resetAllMocks()
    api.agentRunChangeSetId.mockReturnValue('cs-1')
    api.getChangeSet.mockResolvedValue({ status: 'reviewing', review_revision: 0 })
    api.applyChangeSetDirect.mockResolvedValue({ writeback: { result_revision: 4 } })
    api.listProjectChangeSets.mockResolvedValue([])
  })

  it.each(['move-file', 'delete-file'])('%s 成功立即请求新快照，改动卡带实际版本；流重放不重复刷新', async (operation) => {
    const changed = vi.fn()
    api.agentRunChangeSetId.mockReturnValue(null)
    api.getChangeSet.mockResolvedValue({ status: 'applied', writeback: { result_revision: 5 } })
    const event = writeActivity()
    if (event.kind === 'tool_result') event.payload.output = { schema: `zhimeng.tool-result.project-${operation}.v1`, applied: true, change_set_id: 'cs-1', result_revision: 5 }
    const props = options({ projectId: 'ordinary-project', activeRun: makeRun('running'), activities: [event], onBookChanged: changed })
    const { result, rerender } = renderHook((input: UseChangeCardsOptions) => useChangeCards(input), { initialProps: props })
    await waitFor(() => expect(changed).toHaveBeenCalledOnce())
    await waitFor(() => expect(result.current.cards[0]).toMatchObject({ status: 'applied', resultRevision: 5 }))
    rerender({ ...props, activeRun: makeRun('succeeded'), activities: [...props.activities] })
    await act(async () => undefined)
    expect(changed).toHaveBeenCalledOnce()
  })

  it('无修改单的建目录与撤回文件日志也各刷新一次，旧会话结果不串进当前项目', async () => {
    api.agentRunChangeSetId.mockReturnValue(null)
    const changed = vi.fn()
    const mkdir = writeActivity()
    if (mkdir.kind === 'tool_result') mkdir.payload.output = { schema: 'zhimeng.tool-result.project-mkdir.v1', applied: true, result_revision: 5 }
    const props = options({ projectId: 'ordinary-project', activeRun: makeRun('running'), activities: [mkdir], onBookChanged: changed })
    const { result, rerender } = renderHook((input: UseChangeCardsOptions) => useChangeCards(input), { initialProps: props })
    await waitFor(() => expect(changed).toHaveBeenCalledOnce())
    const undo = writeActivity(); undo.activity_id = 'undo-2'; undo.sequence = 2
    if (undo.kind === 'tool_result') undo.payload.output = { schema: 'zhimeng.tool-result.project-undo-change.v1', change_id: 'fc-2', status: 'undone', result_revision: 6 }
    rerender({ ...props, activities: [mkdir, undo] })
    await waitFor(() => expect(changed).toHaveBeenCalledTimes(2))
    expect(result.current.cards).toEqual([])
    rerender({ ...props, sessionId: 'another-session', activities: [mkdir, undo] })
    await act(async () => undefined)
    expect(changed).toHaveBeenCalledTimes(2)
  })

  it.each([['computer_run_command', 'cancelled'], ['undo_change', 'failed']])('%s 以%s结束但未返回版本时仍刷新磁盘快照且重复状态只刷新一次', async (tool, terminalStatus) => {
    api.agentRunChangeSetId.mockReturnValue(null)
    const changed = vi.fn()
    const call = { ...writeActivity(), kind: 'tool_call', payload: { tool_call_id: 'cmd', tool_name: tool, arguments: {} } } as AgentActivity
    const props = options({ projectId: 'ordinary-project', activeRun: makeRun('running'), activities: [call], onBookChanged: changed })
    const { rerender } = renderHook((input: UseChangeCardsOptions) => useChangeCards(input), { initialProps: props })
    expect(changed).not.toHaveBeenCalled()
    rerender({ ...props, activeRun: makeRun(terminalStatus) })
    await waitFor(() => expect(changed).toHaveBeenCalledOnce())
    rerender({ ...props, activeRun: makeRun(terminalStatus), activities: [...props.activities] })
    await act(async () => undefined)
    expect(changed).toHaveBeenCalledOnce()
  })

  it('默认直接保存：卡挂出来就自动存，不用作者点', async () => {
    const { result, succeed, onBookChanged } = renderCards('succeeded')
    succeed()
    await waitFor(() => expect(api.applyChangeSetDirect).toHaveBeenCalledWith('cs-1', 0))
    await waitFor(() => expect(result.current.cards[0]?.status).toBe('applied'))
    expect(result.current.cards[0]?.resultRevision).toBe(4)
    expect(onBookChanged).toHaveBeenCalled()
  })

  it('先看修改：卡保持待确认，不自动存', async () => {
    window.localStorage.setItem('zhimeng.apply-policy.v1', 'review')
    const { result, succeed } = renderCards('succeeded')
    succeed()
    await waitFor(() => expect(result.current.cards[0]?.status).toBe('pending'))
    expect(api.applyChangeSetDirect).not.toHaveBeenCalled()
  })

  it('直接保存被写入前检查拦下：留在卡里等作者看', async () => {
    api.getChangeSet.mockResolvedValue({ status: 'reviewing', review_revision: 0 })
    api.applyChangeSetDirect.mockRejectedValue(
      new (await import('../../api/runtime')).RuntimeApiError(409, {
        error: { code: 'change_set_review_required', message: 'x' },
      }),
    )
    const { result, succeed } = renderCards('succeeded')
    succeed()
    await waitFor(() => expect(result.current.cards[0]?.status).toBe('attention'))
  })

  it('读旧卡时以后端的撤回状态为准，不因默认直接保存重新写入', async () => {
    window.localStorage.setItem('zhimeng.desk.change-cards:session-1', JSON.stringify([
      { changeSetId: 'cs-1', runId: 'run-1', status: 'busy' },
    ]))
    api.getChangeSet.mockResolvedValue({ status: 'rolled_back', review_revision: 4, rollback: { result_revision: 8 } })
    const { result } = renderHook(() => useChangeCards(options({ activeRun: makeRun('succeeded') })))
    await waitFor(() => expect(result.current.cards[0]?.status).toBe('rolled_back'))
    expect(result.current.cards[0]?.resultRevision).toBe(8)
    expect(api.applyChangeSetDirect).not.toHaveBeenCalled()
    // 初次挂载的空 state 不能先覆盖本机缓存，导致缓存核对跳过。
    expect(JSON.parse(localStorage.getItem('zhimeng.desk.change-cards:session-1')!)).toHaveLength(1)
  })

  it('切换时残留的上一条 run 和工具结果不会进入新对话', async () => {
    const { result, rerender } = renderHook((props: UseChangeCardsOptions) => useChangeCards(props), {
      initialProps: options({ activeRun: makeRun('running') }),
    })
    rerender(options({ sessionId: 'session-2', activeRun: makeRun('succeeded'), activities: [writeActivity()] }))
    await act(async () => undefined)
    expect(result.current.cards).toEqual([])
    expect(api.getChangeSet).not.toHaveBeenCalled()
    expect(api.applyChangeSetDirect).not.toHaveBeenCalled()
  })

  it('撤回成功后到达的旧列表不会把卡片改回已保存', async () => {
    const staleList = deferred<unknown[]>()
    api.listProjectChangeSets.mockReturnValue(staleList.promise)
    api.getChangeSet.mockResolvedValue({ status: 'applied', review_revision: 1 })
    const { result } = renderHook(() => useChangeCards(options({
      projectId: 'book-1', activeRun: makeRun('running'), activities: [writeActivity()],
    })))
    await waitFor(() => expect(result.current.cards[0]?.status).toBe('applied'))
    await act(() => result.current.rollback('cs-1'))
    expect(result.current.cards[0]?.status).toBe('rolled_back')
    await act(async () => { staleList.resolve([{ change_set_id: 'cs-1', source_run_id: 'run-1', status: 'applied' }]) })
    expect(result.current.cards[0]?.status).toBe('rolled_back')
  })

  it.each(['applied', 'rolled_back', 'discarded'])('右栏变更为 %s 后当前对话重新核对，旧详情不能盖回状态', async (status) => {
    const staleRead = deferred<unknown>()
    window.localStorage.setItem('zhimeng.desk.change-cards:session-1', JSON.stringify([
      { changeSetId: 'cs-1', runId: 'run-1', status: 'pending' },
    ]))
    api.getChangeSet.mockReturnValueOnce(staleRead.promise)
      .mockResolvedValue({ status, review_revision: 2 })
    const props = options({ refreshKey: 'before' })
    const { result, rerender } = renderHook((input: UseChangeCardsOptions) => useChangeCards(input), { initialProps: props })
    await waitFor(() => expect(result.current.cards).toHaveLength(1))
    rerender({ ...props, refreshKey: 'after' })
    await waitFor(() => expect(result.current.cards[0]?.status).toBe(status))
    await act(async () => { staleRead.resolve({ status: 'reviewing', review_revision: 0 }) })
    expect(result.current.cards[0]?.status).toBe(status)
    expect(api.applyChangeSetDirect).not.toHaveBeenCalled()
  })

  it('没有本机卡片缓存时，右栏撤回后也重新读取当前对话的修改记录', async () => {
    const records = [{ change_set_id: 'cs-1', source_run_id: 'run-1', status: 'applied' }]
    api.listProjectChangeSets.mockResolvedValue(records)
    const props = options({ projectId: 'ordinary-project', runIds: ['run-1'], refreshKey: 'before' })
    const { result, rerender } = renderHook((input: UseChangeCardsOptions) => useChangeCards(input), { initialProps: props })
    await waitFor(() => expect(result.current.cards[0]?.status).toBe('applied'))
    window.localStorage.clear()
    api.listProjectChangeSets.mockResolvedValue([{ ...records[0], status: 'rolled_back' }])
    rerender({ ...props, refreshKey: 'after' })
    await waitFor(() => expect(result.current.cards[0]?.status).toBe('rolled_back'))
    expect(api.listProjectChangeSets).toHaveBeenCalledTimes(2)
  })

  it('从对话放弃修改后通知右栏重新读取', async () => {
    window.localStorage.setItem('zhimeng.apply-policy.v1', 'review')
    const { result, succeed, onBookChanged } = renderCards('succeeded')
    succeed()
    await waitFor(() => expect(result.current.cards[0]?.status).toBe('pending'))
    await act(() => result.current.discard('cs-1'))
    expect(result.current.cards[0]?.status).toBe('discarded')
    expect(onBookChanged).toHaveBeenCalledOnce()
  })

  it('A → B → A 后第一次 A 的读取结果不会覆盖新读取结果', async () => {
    const firstRead = deferred<unknown>()
    window.localStorage.setItem('zhimeng.desk.change-cards:session-1', JSON.stringify([
      { changeSetId: 'cs-1', runId: 'run-1', status: 'applied' },
    ]))
    api.getChangeSet.mockReturnValueOnce(firstRead.promise).mockResolvedValue({ status: 'rolled_back', review_revision: 3 })
    const { result, rerender } = renderHook((props: UseChangeCardsOptions) => useChangeCards(props), { initialProps: options() })
    rerender(options({ sessionId: 'session-2' }))
    rerender(options())
    await waitFor(() => expect(result.current.cards[0]?.status).toBe('rolled_back'))
    await act(async () => { firstRead.resolve({ status: 'applied', review_revision: 1 }) })
    expect(result.current.cards[0]?.status).toBe('rolled_back')
  })

  it('保存前核对发现卡片已撤回时，直接采用真实状态，不重新 apply', async () => {
    window.localStorage.setItem('zhimeng.apply-policy.v1', 'review')
    const { result, succeed } = renderCards('succeeded')
    succeed()
    await waitFor(() => expect(result.current.cards[0]?.status).toBe('pending'))
    api.getChangeSet.mockResolvedValue({ status: 'rolled_back', review_revision: 2 })
    await act(() => result.current.approve('cs-1'))
    expect(result.current.cards[0]?.status).toBe('rolled_back')
    expect(api.applyChangeSetDirect).not.toHaveBeenCalled()
  })

  it('自动保存的核对尚未结束就离开对话，不把旧操作继续成写入', async () => {
    const pendingRead = deferred<unknown>()
    api.getChangeSet.mockReturnValue(pendingRead.promise)
    const changed = vi.fn()
    const { result, rerender } = renderHook((props: UseChangeCardsOptions) => useChangeCards(props), {
      initialProps: options({ activeRun: makeRun('running'), onBookChanged: changed }),
    })
    rerender(options({ activeRun: makeRun('succeeded'), onBookChanged: changed }))
    await waitFor(() => expect(api.getChangeSet).toHaveBeenCalled())
    rerender(options({ sessionId: 'session-2' }))
    await act(async () => { pendingRead.resolve({ status: 'reviewing', review_revision: 0 }) })
    expect(result.current.cards).toEqual([])
    expect(api.applyChangeSetDirect).not.toHaveBeenCalled()
    expect(changed).not.toHaveBeenCalled()
  })

  it('返回旧对话并更改保存方式，不自动保存之前留下的待确认修改', async () => {
    const { result, rerender } = renderHook((props: UseChangeCardsOptions) => useChangeCards(props), {
      initialProps: options({ activeRun: makeRun('running'), autoApply: false }),
    })
    rerender(options({ activeRun: makeRun('succeeded'), autoApply: false }))
    await waitFor(() => expect(result.current.cards[0]?.status).toBe('pending'))
    rerender(options({ sessionId: 'session-2', autoApply: true }))
    rerender(options({ activeRun: makeRun('succeeded'), autoApply: true }))
    await act(async () => undefined)
    expect(result.current.cards[0]?.status).toBe('pending')
    expect(api.applyChangeSetDirect).not.toHaveBeenCalled()
  })

  it('StrictMode 重跑挂载副作用后仍只自动保存一次', async () => {
    const { result, rerender } = renderHook((props: UseChangeCardsOptions) => useChangeCards(props), {
      initialProps: options({ activeRun: makeRun('running') }), wrapper: StrictMode,
    })
    rerender(options({ activeRun: makeRun('succeeded') }))
    await waitFor(() => expect(result.current.cards[0]?.status).toBe('applied'))
    expect(result.current.cards).toHaveLength(1)
    expect(api.applyChangeSetDirect).toHaveBeenCalledTimes(1)
  })

  it('撤回处理中出发的列表，也不能在撤回完成后覆盖新状态', async () => {
    const rollback = deferred<unknown>()
    const lateList = deferred<unknown[]>()
    api.listProjectChangeSets.mockResolvedValueOnce([]).mockReturnValueOnce(lateList.promise)
    api.getChangeSet.mockResolvedValue({ status: 'applied', review_revision: 1 })
    api.rollbackChangeSet.mockReturnValue(rollback.promise)
    const { result, rerender } = renderHook((props: UseChangeCardsOptions) => useChangeCards(props), {
      initialProps: options({ projectId: 'book-1', activeRun: makeRun('running'), activities: [writeActivity()] }),
    })
    await waitFor(() => expect(result.current.cards[0]?.status).toBe('applied'))
    let operation!: Promise<void>
    act(() => { operation = result.current.rollback('cs-1') })
    await waitFor(() => expect(api.rollbackChangeSet).toHaveBeenCalled())
    rerender(options({ projectId: 'book-1', activeRun: makeRun('failed'), activities: [writeActivity()] }))
    await waitFor(() => expect(api.listProjectChangeSets).toHaveBeenCalledTimes(2))
    await act(async () => { rollback.resolve(undefined); await operation })
    await act(async () => { lateList.resolve([{ change_set_id: 'cs-1', source_run_id: 'run-1', status: 'applied' }]) })
    expect(result.current.cards[0]?.status).toBe('rolled_back')
  })

  it('保存请求发出后切走又回来，完成时重新核对而不会一直显示处理中', async () => {
    const save = deferred<unknown>()
    api.applyChangeSetDirect.mockReturnValue(save.promise)
    const { result, rerender } = renderHook((props: UseChangeCardsOptions) => useChangeCards(props), {
      initialProps: options({ activeRun: makeRun('running') }),
    })
    rerender(options({ activeRun: makeRun('succeeded') }))
    await waitFor(() => expect(api.applyChangeSetDirect).toHaveBeenCalledTimes(1))
    rerender(options({ sessionId: 'session-2' }))
    rerender(options({ activeRun: makeRun('succeeded') }))
    api.getChangeSet.mockResolvedValue({ status: 'applied', review_revision: 1, writeback: { result_revision: 4 } })
    await act(async () => { save.resolve({ writeback: { result_revision: 4 } }) })
    await waitFor(() => expect(result.current.cards[0]?.status).toBe('applied'))
    expect(api.applyChangeSetDirect).toHaveBeenCalledTimes(1)
  })
})

function options(overrides: Partial<UseChangeCardsOptions> = {}): UseChangeCardsOptions {
  return { sessionId: 'session-1', activeRun: null, activities: [], onBookChanged: vi.fn(), ...overrides }
}

function deferred<T>() {
  let resolve!: (value: T) => void
  const promise = new Promise<T>((done) => { resolve = done })
  return { promise, resolve }
}

function writeActivity(): AgentActivity {
  return { schema: 'zhimeng.activity-item.v1', sequence: 1, created_at: '2026-09-16T10:00:00Z',
    project_revision: null, correlation_id: 'correlation-1', causation_id: null,
    activity_id: 'write-1', session_id: 'session-1', run_id: 'run-1', kind: 'tool_result',
    payload: { tool_call_id: 'tool-1', status: 'succeeded', error: null, duration_ms: 10, output: {
      schema: 'zhimeng.tool-result.project-write-file.v1', change_set_id: 'cs-1', applied: true, result_revision: 4,
    } } }
}
