import { cleanup, renderHook } from '@testing-library/react'
import { afterEach, expect, it } from 'vitest'
import type { AgentActivity } from '../../api/agent'
import { processActivities, useProcessActivities } from '../chat/useProcessActivities'
import { stepRows, thoughtBlocksOf, turnTailActivities } from '../chat/stepRows'

afterEach(cleanup)

const message = (sequence: number, phase = 'delta', role = 'assistant'): AgentActivity => ({
  activity_id: `a-${sequence}`, sequence, run_id: 'r1', session_id: 's1', kind: 'message',
  payload: { role, phase, delta: '采购明细', message_id: phase === 'completed' ? `m-${sequence}` : null },
}) as unknown as AgentActivity
const call = (sequence: number, id: string): AgentActivity => ({
  activity_id: `a-${sequence}`, sequence, run_id: 'r1', session_id: 's1', kind: 'tool_call',
  payload: { tool_call_id: id, tool_name: 'read_file', arguments: { path: 'purchases.csv' } },
}) as unknown as AgentActivity
const result = (sequence: number, id: string, status = 'succeeded'): AgentActivity => ({
  activity_id: `a-${sequence}`, sequence, run_id: 'r1', session_id: 's1', kind: 'tool_result',
  payload: { tool_call_id: id, status, output: { content: 'item,amount' },
    error: status === 'failed' ? { error: { message: '读取失败' } } : null },
}) as unknown as AgentActivity

it('正文追加复用过程引用，但保留完整原始历史与非正文变化', () => {
  const source = [call(1, 'read'), result(2, 'read', 'failed'), message(3)]
  const view = renderHook(({ activities }) => useProcessActivities(activities), { initialProps: { activities: source } })
  const first = view.result.current
  const appended = [...source, ...Array.from({ length: 1000 }, (_, i) => message(i + 4))]
  view.rerender({ activities: appended })
  expect(view.result.current).toBe(first)
  expect(appended).toHaveLength(1003)
  expect(appended.at(-1)?.sequence).toBe(1003)
  // Same length and endpoints, but a repaired middle result must be observed.
  const repaired = [...appended]
  repaired[1] = result(2, 'read')
  view.rerender({ activities: repaired })
  expect(view.result.current).not.toBe(first)
  expect(stepRows(view.result.current, null)[0].state).toBe('succeeded')
  const complete = message(1004, 'completed')
  view.rerender({ activities: [...repaired, complete] })
  expect(view.result.current.at(-1)).toBe(complete)
  expect(turnTailActivities(view.result.current, 'r1')).toEqual([])
})

it('保留正文隔开的重试、最早失败、思考、用户插话和回复边界', () => {
  const thoughts = [1, 2].map((n): AgentActivity => ({ activity_id: `t-${n}`, sequence: n + 9,
    run_id: 'r1', session_id: 's1', kind: 'reasoning', payload: { summary: '思考', block_id: 'thought',
      phase: n === 1 ? 'delta' : 'completed', delta: '核对', content: n === 2 ? '核对完成' : null, source: 'model' },
  }) as unknown as AgentActivity)
  const original = [call(1, 'first'), result(2, 'first', 'failed'), message(3), message(4),
    call(5, 'second'), result(6, 'second'), message(7, 'completed'), message(8, 'completed', 'user'),
    message(9), ...thoughts, call(12, 'third'), result(13, 'third'), message(14), message(15)]
  const projected = processActivities(original)
  expect(stepRows(projected, null)).toEqual(stepRows(original, null))
  expect(stepRows(projected, null)[0].state).toBe('failed')
  expect(thoughtBlocksOf(projected, 'r1')).toEqual(thoughtBlocksOf(original, 'r1'))
  expect(projected.filter(item => item.kind === 'message' && item.payload.phase === 'completed')).toEqual([original[6], original[7]])
  expect(processActivities([...original].reverse())).toEqual(projected)
})

it('替换或切换会话后不复用另一份过程，旧数组重新读取仍准确', () => {
  const a = [message(1), message(2)]
  const b = a.map(item => ({ ...item, session_id: 's2', run_id: 'r2' }))
  const view = renderHook(({ activities }) => useProcessActivities(activities), { initialProps: { activities: a } })
  view.rerender({ activities: b })
  expect(view.result.current).toEqual([b[0]])
  view.rerender({ activities: a })
  expect(view.result.current).toEqual([a[0]])
  view.rerender({ activities: [] })
  expect(view.result.current).toEqual([])
})
