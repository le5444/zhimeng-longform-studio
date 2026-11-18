import { afterEach, describe, expect, it, vi } from 'vitest'
import { AgentContractError, createAgentRun, getAgentRunContinuation } from './agent'

afterEach(() => vi.unstubAllGlobals())
const state = { schema: 'zhimeng.agent-run-continuation.v1', run_id: 'run/a', can_continue: true,
  reason_code: 'ready', reason: '可以继续', stop_reason: 'budget', objective: '完成全部资料整理',
  continued_by_run_id: null, completed_tool_count: 3, uncertain_tool_count: 1 }
function response(value: unknown) { return new Response(JSON.stringify(value), { status: 200, headers: { 'Content-Type': 'application/json' } }) }

describe('普通任务接续 API', () => {
  it('读取指定 Run 的恢复状态，拒绝其他 Run 的响应', async () => {
    const fetcher = vi.fn().mockResolvedValueOnce(response(state)).mockResolvedValueOnce(response({ ...state, run_id: 'other' }))
    vi.stubGlobal('fetch', fetcher)
    expect(await getAgentRunContinuation('run/a')).toMatchObject({ completed_tool_count: 3 })
    expect(String(fetcher.mock.calls[0][0])).toContain('/runs/run%2Fa/continuation')
    await expect(getAgentRunContinuation('run/a')).rejects.toBeInstanceOf(AgentContractError)
  })

  it('创建新 Run 时发送原任务来源和新资料包，不调用旧 resume', async () => {
    const fetcher = vi.fn().mockResolvedValue(response({ run: { run_id: 'child' }, user_message: {} }))
    vi.stubGlobal('fetch', fetcher)
    await createAgentRun('session', { message: state.objective,
      writer_mode: 'author_review_change_proposal', continuation_source_run_id: state.run_id,
      context_pack_id: 'fresh-pack', expected_context_pack_hash: 'fresh-hash' }, 'continue-stable-key')
    expect(String(fetcher.mock.calls[0][0])).toContain('/sessions/session/runs')
    const init = fetcher.mock.calls[0][1] as RequestInit
    expect(JSON.parse(String(init.body))).toMatchObject({ continuation_source_run_id: 'run/a', context_pack_id: 'fresh-pack' })
    expect(new Headers(init.headers).get('Idempotency-Key')).toBe('continue-stable-key')
  })
})
