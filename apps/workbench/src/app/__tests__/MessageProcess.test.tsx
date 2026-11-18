import { cleanup, fireEvent, render, screen } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'
import type { AgentActivity, AgentMessage } from '../../api/agent'
import { AgentMessageView, LiveTurn } from '../chat/Message'
import { useProcessActivities } from '../chat/useProcessActivities'
import * as processRows from '../chat/stepRows'
import { T } from '../strings'

afterEach(() => { cleanup(); vi.restoreAllMocks(); window.localStorage.clear(); window.sessionStorage.clear() })

function reasoning(sequence: number, block: string, phase: 'delta' | 'completed' | 'interrupted', text: string): AgentActivity {
  return { activity_id: `a-${sequence}`, sequence, run_id: 'reasoning-test', kind: 'reasoning', payload: {
    summary: '模型思考', block_id: block, phase, content: phase === 'delta' ? null : text,
    delta: phase === 'delta' ? text : null, visibility: 'full', source: 'model',
  } } as AgentActivity
}
function tool(sequence: number, result?: 'succeeded' | 'failed'): AgentActivity {
  return { activity_id: `a-${sequence}`, sequence, run_id: 'reasoning-test',
    kind: result ? 'tool_result' : 'tool_call', payload: result
      ? { tool_call_id: 'read-1', status: result, output: { content: 'config' }, error: result === 'failed' ? { error: { message: '读取失败' } } : null }
      : { tool_call_id: 'read-1', tool_name: 'read_file', arguments: { path: 'config.json' } },
  } as unknown as AgentActivity
}
const common = { runId: 'reasoning-test', startedAt: null, streaming: '', snapshot: null }

describe('模型处理过程', () => {
  it('正文继续增长不重算未变的工具过程，真实结果到达仍及时更新', () => {
    function Turn({ activities, text }: { activities: AgentActivity[]; text: string }) {
      const process = useProcessActivities(activities)
      return <><LiveTurn {...common} activities={process} streaming={text} processOnly />
        <LiveTurn {...common} activities={process} streaming={text} hideProcess /></>
    }
    const rows = vi.spyOn(processRows, 'stepRows')
    const delta = (sequence: number): AgentActivity => ({ activity_id: `a-${sequence}`, sequence,
      run_id: common.runId, kind: 'message', payload: { role: 'assistant', phase: 'delta', delta: '正文' },
    }) as AgentActivity
    const initial = [tool(1), delta(2)]
    const view = render(<Turn activities={initial} text="正在核对采购表。" />)
    expect(rows).toHaveBeenCalledOnce()
    expect(screen.getByText('执行中')).toBeInTheDocument()
    view.rerender(<Turn activities={[...initial, delta(3), delta(4)]} text="正在核对采购表。保留所有原始记录。" />)
    expect(rows).toHaveBeenCalledOnce()
    expect(screen.getByText('正在核对采购表。保留所有原始记录。')).toBeInTheDocument()
    view.rerender(<Turn activities={[...initial, delta(3), delta(4), tool(5, 'failed')]} text="读取未完成。" />)
    expect(rows).toHaveBeenCalledTimes(2)
    expect(screen.getByText('失败')).toBeInTheDocument()
    expect(screen.getByText('读取未完成。')).toBeInTheDocument()
  })

  it('流式计划只采纳成功结果，失败和结束后保留未完成项', () => {
    const plan = (id: string, sequence: number, completed: boolean): AgentActivity => ({
      schema: 'zhimeng.activity-item.v1', session_id: 'session-plan', created_at: '2026-10-17T00:00:00Z',
      project_revision: null, correlation_id: 'correlation-plan', causation_id: null,
      activity_id: `a-${sequence}`, sequence, run_id: common.runId, kind: 'tool_call', payload: {
        tool_call_id: id, tool_name: 'plan_update', arguments: { items: [{ content: '核对采购表', status: completed ? 'completed' : 'in_progress' }] },
      },
    })
    const accepted = { ...tool(2, 'succeeded'), payload: { tool_call_id: 'plan-1', status: 'succeeded', output: {}, error: null } } as AgentActivity
    const rejected = { ...tool(4, 'failed'), payload: { tool_call_id: 'plan-2', status: 'failed', output: null,
      error: { error: { message: '计划未更新' } } } } as AgentActivity
    const activities = [plan('plan-1', 1, false), accepted, plan('plan-2', 3, true)]
    const view = render(<LiveTurn {...common} activities={activities} />)
    expect(screen.getByText('0/1')).toBeInTheDocument()
    expect(view.container.querySelector('.zm-plan .zm-spinner')).not.toBeNull()
    view.rerender(<LiveTurn {...common} live={false} activities={[...activities, rejected]} />)
    expect(screen.getByText('0/1')).toBeInTheDocument()
    expect(screen.getByText('未完成 1 项')).toBeInTheDocument()
    expect(screen.getByText('失败')).toBeInTheDocument()
    expect(view.container.querySelector('.zm-spinner')).toBeNull()
  })

  it('紧凑过程保留完整正文、复制和分叉操作，以及可展开的工具失败详情', () => {
    const copy = vi.fn(), fork = vi.fn()
    const message = { message_id: 'm1', run_id: 'reasoning-test', role: 'assistant', content: '配置读取失败，保留原因继续排查。' } as AgentMessage
    const view = render(<AgentMessageView message={message} activities={[tool(1), tool(2, 'failed')]}
      stepsUnloaded={false} onLoadSteps={async () => {}} snapshot={null} cards={null} actions={{ onCopy: copy, onFork: fork }} isLatest={false} progress />)
    fireEvent.click(screen.getByRole('button', { name: T.agent.copyReply }))
    expect(copy).toHaveBeenCalledWith(message.content)
    fireEvent.click(screen.getByRole('button', { name: T.agent.forkConversationTitle }))
    expect(fork).toHaveBeenCalledOnce()
    expect(screen.getByText(message.content)).toBeInTheDocument()
    expect(view.container.querySelector('.zm-msg-foot')).toBeNull()
    const fold = view.container.querySelector('.zm-fold-head') as HTMLButtonElement
    fireEvent.click(fold)
    expect(screen.getByText('失败')).toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: /读取 config.json/ }))
    expect(view.container.querySelector('.zm-step-detail')).toHaveTextContent('读取失败')
  })

  it('工具和思考按实际先后展示，增量合并成同一段', () => {
    const activities = [reasoning(1, 'b1', 'delta', '先检查'), reasoning(2, 'b1', 'delta', '配置。'),
      reasoning(3, 'b1', 'completed', '先检查配置。'), tool(4), tool(5, 'succeeded'), reasoning(6, 'b2', 'delta', '配置已读取，核对字段。')]
    const { container } = render(<LiveTurn {...common} activities={activities} />)
    const process = container.querySelector('.zm-turn-process')!
    expect([...process.children].map((element) => element.className)).toEqual(['zm-fold is-thinking', 'zm-steps', 'zm-fold is-thinking is-open'])
    fireEvent.click(screen.getByRole('button', { name: '模型思考（1 段）' }))
    expect(screen.getAllByText('先检查配置。')).toHaveLength(1)
    expect(screen.getAllByText('配置已读取，核对字段。')).toHaveLength(1)
    expect(screen.getByText('完成')).toBeInTheDocument()
  })

  it('未收到思考时中性等待，工具执行中与失败来自真实结果', () => {
    const view = render(<LiveTurn {...common} activities={[]} />)
    expect(screen.getByText('等待模型回复…')).toBeInTheDocument()
    expect(screen.queryByText(/深度思考|正在接收思考/)).toBeNull()
    view.rerender(<LiveTurn {...common} activities={[tool(1)]} />)
    expect(screen.getByText('执行中')).toBeInTheDocument()
    view.rerender(<LiveTurn {...common} activities={[tool(1), tool(2, 'failed')]} />)
    expect(screen.getByText('失败')).toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: /读取 config.json/ }))
    expect(screen.getByText(/读取失败/)).toBeInTheDocument()
    expect(screen.queryByText('执行中')).toBeNull()
  })

  it('只有思考就中断时仍可看已收到内容，停止等待状态', () => {
    const activities = [reasoning(1, 'b1', 'delta', '已完成初步检查')]
    const view = render(<LiveTurn {...common} activities={activities} />)
    fireEvent.click(screen.getByRole('button', { name: '正在接收思考…' }))
    fireEvent.click(screen.getByRole('button', { name: '正在接收思考…' }))
    view.rerender(<LiveTurn {...common} live={false} activities={[...activities, reasoning(2, 'b1', 'interrupted', '已完成初步检查')]} />)
    expect(screen.getByText('已完成初步检查')).toBeInTheDocument()
    expect(screen.getByText('已保留中断前收到的内容')).toBeInTheDocument()
    expect(screen.queryByText('等待模型回复…')).toBeNull()
    expect(screen.queryByText('正在接收思考…')).toBeNull()
  })

  it('流式转为保存后的回答，主动展开保留且思考不重复', () => {
    const activities = [reasoning(1, 'b1', 'delta', '核对完成')]
    const view = render(<LiveTurn {...common} activities={activities} />)
    fireEvent.click(screen.getByRole('button', { name: '正在接收思考…' }))
    fireEvent.click(screen.getByRole('button', { name: '正在接收思考…' }))
    view.unmount()
    const message = { message_id: 'm1', run_id: 'reasoning-test', role: 'assistant', content: '检查结果正确' } as AgentMessage
    render(<AgentMessageView message={message} activities={[...activities, reasoning(2, 'b1', 'completed', '核对完成')]}
      stepsUnloaded={false} onLoadSteps={async () => {}} snapshot={null} cards={null} actions={{ onCopy: () => {} }} isLatest />)
    expect(screen.getAllByText('核对完成')).toHaveLength(1)
    expect(screen.getByRole('button', { name: '模型思考（1 段）' })).toHaveAttribute('aria-expanded', 'true')
    expect(screen.getByText('检查结果正确')).toBeInTheDocument()
  })
})
