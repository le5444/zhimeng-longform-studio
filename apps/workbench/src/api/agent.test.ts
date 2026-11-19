import { describe, expect, it, vi } from 'vitest'
import { getAgentRunAdmission, listAgentActivities, withdrawAgentRunAdmission, type AgentActivity } from './agent'

function jsonResponse(body: unknown): Response {
  return new Response(JSON.stringify(body), {
    status: 200,
    headers: { 'Content-Type': 'application/json' },
  })
}

const activity: AgentActivity = {
  schema: 'zhimeng.activity-item.v1',
  activity_id: 'activity-00000005',
  session_id: 'session-one',
  run_id: 'run/a b?',
  sequence: 5,
  created_at: '2026-07-20T05:00:00Z',
  project_revision: {
    project_id: 'project-one',
    revision: 3,
    fingerprint: 'a'.repeat(64),
    observed_at: '2026-07-20T05:00:00Z',
  },
  correlation_id: 'correlation-one',
  causation_id: null,
  kind: 'tool_call',
  payload: {
    tool_call_id: 'tool-call-one',
    tool_name: 'project.read',
    arguments: { path: '05_正文内容/CH001.md' },
  },
}

describe('agent API client', () => {
  it('后续页失败时不交付半份过程，重试从原位置读取', async () => {
    const first = { run_id: activity.run_id, activities: [activity], last_sequence: 6, last_event_sequence: 12, next_after_sequence: 5 }
    const last = { ...first, activities: [{ ...activity, activity_id: 'a6', sequence: 6 }], next_after_sequence: null }
    const fetcher = vi.fn<typeof fetch>().mockResolvedValueOnce(jsonResponse(first))
      .mockRejectedValueOnce(new Error('记录连接中断'))
      .mockResolvedValueOnce(jsonResponse(first)).mockResolvedValueOnce(jsonResponse(last))
    await expect(listAgentActivities(activity.run_id, 4, { fetcher })).rejects.toThrow('记录连接中断')
    const recovered = await listAgentActivities(activity.run_id, 4, { fetcher })
    expect(recovered.activities.map(item => item.sequence)).toEqual([5, 6])
    expect(fetcher.mock.calls[2][0]).toContain('after_sequence=4&limit=250')
  })

  it.each(['wrong-run', 'stalled', 'changed-boundary'])('拒绝不属于本次读取的分页：%s', async (reason) => {
    const first = { run_id: activity.run_id, activities: [activity], last_sequence: 6, last_event_sequence: 12, next_after_sequence: 5 }
    const last = { ...first, activities: [{ ...activity, activity_id: 'a6', sequence: 6 }], next_after_sequence: null }
    const fetcher = vi.fn<typeof fetch>().mockResolvedValueOnce(jsonResponse(first))
      .mockResolvedValueOnce(jsonResponse(reason === 'wrong-run' ? { ...last, run_id: 'another-run' }
        : reason === 'stalled' ? first : { ...last, last_sequence: 7 }))
    await expect(listAgentActivities(activity.run_id, 4, { fetcher })).rejects.toThrow('处理记录')
    expect(fetcher).toHaveBeenCalledTimes(2)
  })

  it('切走取消后不再请求下一页', async () => {
    const controller = new AbortController()
    const fetcher = vi.fn<typeof fetch>().mockImplementation(async () => {
      controller.abort()
      return jsonResponse({ run_id: activity.run_id, activities: [activity], last_sequence: 6, next_after_sequence: 5 })
    })
    await expect(listAgentActivities(activity.run_id, 4, { fetcher }, controller.signal)).rejects.toThrow()
    expect(fetcher).toHaveBeenCalledTimes(1)
  })

  it('按固定活动边界补齐多页，返回独立事件游标用于接回实时流', async () => {
    const fetcher = vi.fn<typeof fetch>()
      .mockResolvedValueOnce(jsonResponse({ run_id: activity.run_id, activities: [activity], last_sequence: 6, last_event_sequence: 12, next_after_sequence: 5 }))
      .mockResolvedValueOnce(jsonResponse({ run_id: activity.run_id, activities: [{ ...activity, activity_id: 'a6', sequence: 6 }], last_sequence: 6, last_event_sequence: 12, next_after_sequence: null }))
    const page = await listAgentActivities(activity.run_id, 4, { fetcher })
    expect(page.activities.map(item => item.sequence)).toEqual([5, 6])
    expect(page.last_event_sequence).toBe(12)
    expect(fetcher.mock.calls[1][0]).toContain('after_sequence=5&limit=250&through_sequence=6')
  })

  it('原子取回使用同一 key，准确区分撤销成功和已受理', async () => {
    const fetcher = vi.fn<typeof fetch>().mockResolvedValueOnce(jsonResponse({ state: 'withdrawn', accepted: null }))
    await expect(withdrawAgentRunAdmission('s1', 'turn-1', { fetcher })).resolves.toEqual({ state: 'withdrawn', accepted: null })
    expect(fetcher.mock.calls[0][0]).toContain('/sessions/s1/runs/admission/withdraw')
    expect(fetcher.mock.calls[0][1]?.method).toBe('POST')
    expect(new Headers(fetcher.mock.calls[0][1]?.headers).get('Idempotency-Key')).toBe('turn-1')
    const accepted = { run: { run_id: 'r1', session_id: 's1' }, user_message: { run_id: 'r1', session_id: 's1' } }
    fetcher.mockResolvedValueOnce(jsonResponse({ state: 'accepted', accepted }))
    await expect(withdrawAgentRunAdmission('s1', 'turn-1', { fetcher })).resolves.toEqual({ state: 'accepted', accepted })
    fetcher.mockResolvedValueOnce(jsonResponse({ state: 'accepted', accepted: { ...accepted, run: { ...accepted.run, session_id: 'other' } } }))
    await expect(withdrawAgentRunAdmission('s1', 'turn-1', { fetcher })).rejects.toThrow('取回发送的结果与当前对话不一致')
  })

  it('按会话和 Idempotency-Key 核对受理，不把不同 key 的并发查询合并', async () => {
    const fetcher = vi.fn<typeof fetch>().mockImplementation(async () => jsonResponse({
      run: { run_id: 'r1', session_id: 'session/one' }, user_message: { run_id: 'r1', session_id: 'session/one' },
    }))
    await Promise.all(['turn-a', 'turn-b'].map((key) => getAgentRunAdmission('session/one', key, { fetcher })))
    expect(fetcher).toHaveBeenCalledTimes(2)
    expect(fetcher.mock.calls[0][0]).toContain('/sessions/session%2Fone/runs/admission')
    expect(new Headers(fetcher.mock.calls[0][1]?.headers).get('Idempotency-Key')).toBe('turn-a')
    expect(new Headers(fetcher.mock.calls[1][1]?.headers).get('Idempotency-Key')).toBe('turn-b')
  })

  it('只把明确 admission-not-found 当作未查到，其他 404 和错误继续报告', async () => {
    const fetcher = vi.fn<typeof fetch>().mockImplementation(async () => new Response(JSON.stringify({ error: { code: 'agent_admission_not_found' } }), { status: 404 }))
    await expect(getAgentRunAdmission('s1', 'turn-a', { fetcher })).resolves.toBeNull()
    fetcher.mockImplementation(async () => new Response(JSON.stringify({ error: { code: 'agent_resource_not_found' } }), { status: 404 }))
    await expect(getAgentRunAdmission('s1', 'turn-a', { fetcher })).rejects.toThrow('HTTP 404')
  })

  it('拒绝错会话或用户消息归属不同 Run 的受理回执', async () => {
    const fetcher = vi.fn<typeof fetch>().mockResolvedValue(jsonResponse({ run: { run_id: 'r1', session_id: 's1' }, user_message: { run_id: 'r2', session_id: 's1' } }))
    await expect(getAgentRunAdmission('s1', 'turn-a', { fetcher })).rejects.toThrow('发送结果与当前对话不一致')
  })

  it('lists typed Run activities from the existing paged endpoint', async () => {
    const fetcher = vi.fn<typeof fetch>().mockResolvedValue(jsonResponse({
      schema: 'zhimeng.agent-activity-list.v1',
      run_id: 'run/a b?',
      activities: [activity],
      last_sequence: 5,
    }))

    const page = await listAgentActivities(
      'run/a b?',
      4,
      { baseUrl: 'http://127.0.0.1:8787', fetcher },
    )

    expect(page.activities).toEqual([activity])
    expect(page.last_sequence).toBe(5)
    expect(fetcher.mock.calls[0]?.[0]).toBe(
      'http://127.0.0.1:8787/api/v1/agent/runs/run%2Fa%20b%3F/activities?after_sequence=4&limit=250',
    )
    expect(fetcher.mock.calls[0]?.[1]?.method).toBeUndefined()
  })
})
