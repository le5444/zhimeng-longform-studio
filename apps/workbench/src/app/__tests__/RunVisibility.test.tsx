import { cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'
import type { AgentMessage } from '../../api/agent'
import { AgentMessageView, ProcessGroup } from '../chat/Message'
import { StepList } from '../chat/Steps'
import type { StepRow } from '../chat/stepRows'
import { MessageDetails } from '../chat/MessageDetails'

afterEach(() => { cleanup(); window.localStorage.clear(); window.sessionStorage.clear() })

const baseMessage = { message_id: 'm1', run_id: 'r1', role: 'assistant', content: '检查完成。' } as AgentMessage
function answer(usage: AgentMessage['usage']) {
  return <AgentMessageView message={{ ...baseMessage, usage }} activities={[]} stepsUnloaded={false}
    onLoadSteps={async () => {}} snapshot={null} cards={null} actions={{ onCopy: () => {} }} isLatest />
}

describe('实际运行信息', () => {
  it('合计包含辅助整理，详情分列来源；刷新后仍使用已保存事件的合计', () => {
    const runUsage: NonNullable<AgentMessage['run_usage']> = {
      scope: 'main_and_tool_compaction',
      main: { requests: 5, completed: 5, usage: { input_tokens: 147647, output_tokens: 2493, cached_input_tokens: 1536 } },
      compaction: { requests: 9, completed: 9, usage: { input_tokens: 38136, output_tokens: 5090, cached_input_tokens: 2560 } },
      total: { input_tokens: 185783, output_tokens: 7583, cached_input_tokens: 4096 },
    }
    const element = <MessageDetails usage={runUsage.main.usage} runUsage={runUsage} parameterNotes={[]} />
    const view = render(element)
    expect(screen.getByText('输入 185,783 token')).toBeVisible()
    expect(screen.getByText('输出 7,583 token')).toBeVisible()
    expect(screen.getByText('本轮已报告（含工具整理）')).toBeVisible()
    fireEvent.click(screen.getByText('请求详情'))
    expect(within(screen.getByLabelText('主任务用量')).getByText('147647')).toBeVisible()
    expect(within(screen.getByLabelText('工具结果整理用量')).getByText('38136')).toBeVisible()
    expect(screen.getByText('9 次，9 次有完成回执')).toBeVisible()
    view.unmount()
    render(element)
    expect(screen.getByText('输入 185,783 token')).toBeVisible()
  })

  it('整理中断保留已上报部分，没有任何上报时不回退成旧主任务总数', () => {
    const runUsage: NonNullable<AgentMessage['run_usage']> = {
      scope: 'main_and_tool_compaction',
      main: { requests: 1, completed: 1, usage: { input_tokens: 100, output_tokens: 10 } },
      compaction: { requests: 1, completed: 0, usage: null },
      total: { input_tokens: 100, output_tokens: 10, cached_input_tokens: 0,
        input_tokens_reported: false, output_tokens_reported: false, cached_input_tokens_complete: false },
    }
    const view = render(<MessageDetails usage={runUsage.main.usage} runUsage={runUsage} parameterNotes={[]} />)
    expect(screen.getByText('输入 100 token（部分上报）')).toBeVisible()
    expect(screen.queryByText(/缓存命中.*%/)).toBeNull()
    fireEvent.click(screen.getByText('请求详情'))
    expect(screen.getByText('有请求尚无完成回执，用量可能不完整。')).toBeVisible()
    view.rerender(<MessageDetails usage={runUsage.main.usage} runUsage={{ ...runUsage, total: null }} parameterNotes={[]} />)
    expect(screen.getByText('本轮用量未报告')).toBeVisible()
    expect(screen.queryByLabelText('本轮模型用量')).toBeNull()
  })

  it.each([[630, '63%'], [0, '0%'], [1000, '100%']])('回答直接显示真实用量与缓存 %i，不用展开请求详情', (cached, ratio) => {
    const view = render(answer({ input_tokens: 1000, output_tokens: 123, cached_input_tokens: cached as number }))
    const usage = screen.getByLabelText('本轮模型用量')
    expect(within(usage).getByText('本轮已报告')).toBeVisible()
    expect(within(usage).getByText('输入 1,000 token')).toBeVisible()
    expect(within(usage).getByText('输出 123 token')).toBeVisible()
    expect(within(usage).getByText(`缓存命中 ${ratio}`)).toBeVisible()
    expect(view.container.querySelector('.zm-request-details')).not.toHaveAttribute('open')
    expect(screen.queryByText(/共 \d+ 字/)).toBeNull()
  })

  it('输出和缓存未报告不冒充零，真实输出零照常显示', () => {
    const view = render(answer({ input_tokens: 1000, cached_input_tokens: null }))
    expect(screen.getByText('输出 未报告')).toBeVisible()
    expect(screen.getByText('缓存未报告')).toBeVisible()
    expect(screen.queryByText('缓存命中 0%')).toBeNull()
    view.rerender(answer({ input_tokens: 1000, output_tokens: 0, cached_input_tokens: 0 }))
    expect(screen.getByText('输出 0 token')).toBeVisible()
    expect(screen.getByText('缓存命中 0%')).toBeVisible()
  })

  it('用量始终留在详情入口，更新用量时保留展开状态', () => {
    const view = render(answer({ input_tokens: 1000, output_tokens: 123, cached_input_tokens: 0 }))
    const usage = screen.getByLabelText('本轮模型用量')
    expect(usage.closest('summary')).toHaveTextContent('请求详情')
    expect(usage).toBeVisible()
    fireEvent.click(screen.getByText('请求详情'))
    expect(view.container.querySelector('.zm-request-details')).toHaveAttribute('open')
    view.rerender(answer({ input_tokens: 2000, output_tokens: 246, cached_input_tokens: 1000 }))
    expect(view.container.querySelector('.zm-request-details')).toHaveAttribute('open')
    expect(within(screen.getByLabelText('本轮模型用量')).getByText('缓存命中 50%')).toBeVisible()
    fireEvent.click(screen.getByText('请求详情'))
    expect(view.container.querySelector('.zm-request-details')).not.toHaveAttribute('open')
    expect(screen.getByLabelText('本轮模型用量')).toBeVisible()
  })

  it('缓存与输入输出只收到部分上报时保留原数并标清，不用累计输入推假缓存率', () => {
    const view = render(answer({ input_tokens: 1000, output_tokens: 123, cached_input_tokens: 630,
      input_tokens_reported: false, output_tokens_reported: false, cached_input_tokens_complete: false }))
    expect(screen.getByText('输入 1,000 token（部分上报）')).toBeVisible()
    expect(screen.getByText('输出 123 token（部分上报）')).toBeVisible()
    expect(screen.getByText('缓存 630 token（部分上报）')).toBeVisible()
    expect(screen.queryByText(/缓存命中.*%/)).toBeNull()
    fireEvent.click(screen.getByText('请求详情'))
    expect(view.container.querySelector('.zm-request-details')).toHaveTextContent('630（部分上报）')
  })

  it('未上报对应内部占位0不显示成真0', () => {
    render(answer({ input_tokens: 0, output_tokens: 0, cached_input_tokens: 0,
      input_tokens_reported: false, output_tokens_reported: false, cached_input_tokens_complete: false }))
    expect(screen.getByText('输入 未完整上报')).toBeVisible()
    expect(screen.getByText('输出 未完整上报')).toBeVisible()
    expect(screen.getByText('缓存未完整上报')).toBeVisible()
    expect(screen.queryByText(/0 token|缓存命中 0%/)).toBeNull()
  })

  it('明确完整的输出0和缓存0仍显示为零', () => {
    render(answer({ input_tokens: 1000, output_tokens: 0, cached_input_tokens: 0,
      input_tokens_reported: true, output_tokens_reported: true, cached_input_tokens_complete: true }))
    expect(screen.getByText('输出 0 token')).toBeVisible()
    expect(screen.getByText('缓存命中 0%')).toBeVisible()
  })

  it('即使缓存完整，分母输入未完整也不算命中率', () => {
    render(answer({ input_tokens: 1000, output_tokens: 123, cached_input_tokens: 630,
      input_tokens_reported: false, cached_input_tokens_complete: true }))
    expect(screen.getByText('缓存 630 token（比例未知）')).toBeVisible()
    expect(screen.queryByText(/缓存命中.*%/)).toBeNull()
  })

  it.each([undefined, null])('整轮没有用量 %s 不编数字', (usage) => {
    render(answer(usage))
    expect(screen.queryByLabelText('本轮模型用量')).toBeNull()
    expect(screen.queryByText(/缓存命中|输出 0/)).toBeNull()
  })

  it.each([{ input_tokens: 0, cached_input_tokens: 0 }, { input_tokens: 1000, cached_input_tokens: -1 },
    { input_tokens: 1000, cached_input_tokens: 1001 }, { input_tokens: 1000, cached_input_tokens: NaN }])('无效缓存比例不显示为有效命中率：%j', (usage) => {
    render(answer(usage))
    expect(screen.queryByText(/缓存命中.*%/)).toBeNull()
  })

  it('各工具的结果和耗时直接可见，错误原文仍按需展开', () => {
    const rows: StepRow[] = [
      { key: 'a', kind: 'command', text: '编译', state: 'succeeded', durationMs: 1432 },
      { key: 'b', kind: 'command', text: '检查格式', state: 'succeeded', outcome: 'failed', durationMs: 230, detail: 'error: unexpected token at line 3' },
      { key: 'c', kind: 'web', text: '读取网页', state: 'succeeded', outcome: 'cancelled', durationMs: 0 },
      { key: 'd', kind: 'other', text: '正在下载', state: 'running', durationMs: 4000 },
    ]
    const view = render(<StepList rows={rows} />)
    expect(screen.getByText('完成')).toBeVisible()
    expect(screen.getByText('失败')).toBeVisible()
    expect(screen.getByText('已取消')).toBeVisible()
    expect(screen.getByText('执行中')).toBeVisible()
    expect(screen.getByText('1.4 秒')).toBeVisible()
    expect(screen.getByText('230 毫秒')).toBeVisible()
    expect(screen.getByText('0 毫秒')).toBeVisible()
    expect(screen.queryByText('4 秒')).toBeNull()
    expect(screen.queryByText(/unexpected token/)).toBeNull()
    expect(view.container.querySelector('.zm-step.is-failed')).toBeNull()
    fireEvent.click(screen.getByRole('button', { name: '检查格式' }))
    expect(screen.getByText('error: unexpected token at line 3')).toBeVisible()
  })

  it('折起连续工具时仍能看见该组有未完成操作，不编串行总耗时', () => {
    render(<StepList rows={[
      { key: 'a', kind: 'read', text: '读取甲', state: 'succeeded', outcome: 'succeeded', durationMs: 200 },
      { key: 'b', kind: 'read', text: '读取乙', state: 'succeeded', outcome: 'failed', durationMs: 200 },
    ]} />)
    expect(screen.getByText('含失败')).toBeVisible()
    expect(screen.queryByText(/400 毫秒/)).toBeNull()
    fireEvent.click(screen.getByRole('button', { name: /读取/ }))
    expect(screen.getByText('失败')).toBeVisible()
    expect(screen.getAllByText('200 毫秒')).toHaveLength(2)
  })
})

describe('运行过程展开状态', () => {
  it('运行时默认展开，用户折叠后更新活动或转历史都保持选择', () => {
    const states = new Map<string, boolean>()
    const view = render(<ProcessGroup stateKey="s:r:u" states={states} initiallyOpen currentAction="正在读取">读取结果</ProcessGroup>)
    const group = () => view.container.querySelector('details')!
    expect(group().open).toBe(true)
    fireEvent.click(screen.getByLabelText('查看处理过程'))
    expect(group().open).toBe(false)
    view.rerender(<ProcessGroup stateKey="s:r:u" states={states} initiallyOpen currentAction="正在检查">检查结果</ProcessGroup>)
    expect(group().open).toBe(false)
    view.unmount()
    const history = render(<ProcessGroup stateKey="s:r:u" states={states}>检查结果</ProcessGroup>)
    expect(history.container.querySelector('details')!.open).toBe(false)
    fireEvent.click(screen.getByLabelText('查看处理过程'))
    expect(history.container.querySelector('details')!.open).toBe(true)
  })

  it('旧过程懒加载失败可重试，重新渲染不会循环请求', async () => {
    const load = vi.fn().mockRejectedValueOnce(new Error('offline')).mockResolvedValueOnce(undefined)
    const view = render(<ProcessGroup onLoad={load}>历史步骤</ProcessGroup>)
    expect(load).not.toHaveBeenCalled()
    fireEvent.click(screen.getByLabelText('查看处理过程'))
    await waitFor(() => expect(screen.getByRole('button', { name: '过程暂未读取，点击重试' })).toBeVisible())
    view.rerender(<ProcessGroup onLoad={load}>历史步骤更新</ProcessGroup>)
    expect(load).toHaveBeenCalledTimes(1)
    fireEvent.click(screen.getByRole('button', { name: '过程暂未读取，点击重试' }))
    await waitFor(() => expect(screen.queryByText('正在读取处理过程…')).toBeNull())
    expect(load).toHaveBeenCalledTimes(2)
  })
})
