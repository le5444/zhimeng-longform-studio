import { expect, it, vi } from 'vitest'
import { getAgentRun } from './agent'

it('来源读取保留任务编号并传递取消信号', async () => {
  const run = { schema: 'zhimeng.execution-run.v1', run_id: 'run/a', session_id: 'chat-a', objective: '整理会议记录' }
  const fetcher = vi.fn<typeof fetch>().mockResolvedValue(new Response(JSON.stringify({ run })))
  const controller = new AbortController()
  expect(await getAgentRun('run/a', { fetcher, signal: controller.signal })).toEqual(run)
  expect(fetcher.mock.calls[0][0]).toContain('/runs/run%2Fa')
  expect(fetcher.mock.calls[0][1]?.signal).toBeInstanceOf(AbortSignal)
})

it.each([
  { schema: 'zhimeng.execution-run.v1', run_id: 'other', session_id: 'chat-a', objective: '其他要求' },
  { schema: 'zhimeng.execution-run.v1', run_id: 'run-a', session_id: 'chat-a', objective: null },
  null,
])('不接受缺失或不匹配的来源任务 %#', async (run) => {
  const fetcher = vi.fn<typeof fetch>().mockResolvedValue(new Response(JSON.stringify({ run })))
  await expect(getAgentRun('run-a', { fetcher })).rejects.toThrow('来源任务与这条修改记录不一致')
})
