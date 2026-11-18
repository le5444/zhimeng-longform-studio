/**
 * 一轮里的步骤（9-29 重做）：工具调用说人话、计划单独一条（只画最新一次）、运行时自己记的账不算；
 * 连着的读 / 列 / 搜并成「已探索」，连着改文件并成「改了 N 个文件」，点开看每一步。
 */
import { act, cleanup, fireEvent, render, screen } from '@testing-library/react'
import { afterEach, describe, expect, it } from 'vitest'
import type { AgentActivity } from '../../api/agent'
import { PlanChecklist, StepList, StepsFold, Thinking } from '../chat/Steps'
import { writeChatPreferences } from '../lib/chatPreferences'
import { groupSteps, latestPlan, latestThoughtLine, parsePlanItems, stepRows, thoughtBlocksOf, thoughtsOf, toolKind, turnTailActivities, type StepRow } from '../chat/stepRows'
import { T } from '../strings'
import { A } from '../text'

afterEach(() => { cleanup(); window.localStorage.clear(); window.sessionStorage.clear() })

const call = (id: string, sequence: number, tool: string, args: Record<string, unknown>, runId = 'r1') => ({
  activity_id: `a${sequence}`, run_id: runId, sequence, kind: 'tool_call',
  payload: { tool_call_id: id, tool_name: tool, arguments: args },
} as unknown as AgentActivity)
const result = (id: string, sequence: number, status = 'succeeded', runId = 'r1') => ({
  activity_id: `a${sequence}`, run_id: runId, sequence, kind: 'tool_result',
  payload: { tool_call_id: id, status },
} as unknown as AgentActivity)
const thought = (sequence: number, content: string, runId = 'r1', summary = '想') => ({
  activity_id: `a${sequence}`, run_id: runId, sequence, kind: 'reasoning',
  payload: { summary, content },
} as unknown as AgentActivity)
const row = (key: string, kind: StepRow['kind'], state: StepRow['state'] = 'succeeded', targetPath?: string): StepRow => ({ key, kind, text: key, state, targetPath })

const PLAN = {
  items: [
    { content: '读第 3 章', status: 'completed' },
    { content: '改第 4 章开头', status: 'in_progress' },
    { content: '通读检查' },
  ],
}

it('长任务展开后仍能查看第一个失败步骤及全部75次工具调用', () => {
  writeChatPreferences({ groupTools: false })
  const activities = Array.from({ length: 75 }, (_, index) => [
    call(`read-${index}`, index * 2 + 1, 'read_file', { path: `purchase-${index}.csv` }),
    result(`read-${index}`, index * 2 + 2, index === 0 ? 'failed' : 'succeeded'),
  ]).flat()
  const rows = stepRows(activities, null)
  render(<StepList rows={rows} />)
  fireEvent.click(screen.getByRole('button', { name: /查看更早的/ }))
  expect(rows).toHaveLength(75)
  expect(screen.getByText(/purchase-0.csv/)).toBeInTheDocument()
  expect(screen.getByText('失败')).toBeInTheDocument()
})

describe('计划', () => {
  it('认不出的条目丢掉，没写状态按还没做', () => {
    expect(parsePlanItems(PLAN)).toEqual([
      { content: '读第 3 章', status: 'completed' },
      { content: '改第 4 章开头', status: 'in_progress' },
      { content: '通读检查', status: 'pending' },
    ])
    expect(parsePlanItems(null)).toEqual([])
    expect(parsePlanItems({ items: 'not-a-list' })).toEqual([])
    expect(parsePlanItems({ items: [{ content: '  ' }, { nope: 1 }] })).toEqual([])
  })

  it('拿最近一次；给了 runId 就只看那一轮', () => {
    const activities = [
      call('p1', 1, 'plan_update', { items: [{ content: '别的轮的计划' }] }, 'r0'), result('p1', 2, 'succeeded', 'r0'),
      call('p2', 3, 'plan_update', PLAN), result('p2', 4),
    ]
    expect(latestPlan(activities, 'r1')?.map((item) => item.content)).toEqual(['读第 3 章', '改第 4 章开头', '通读检查'])
    expect(latestPlan(activities, 'r0')?.map((item) => item.content)).toEqual(['别的轮的计划'])
    expect(latestPlan(activities, 'r2')).toBeNull()
    expect(latestPlan([], null)).toBeNull()
  })

  it('清单上写做完几条，进行中那条标出来', () => {
    render(<PlanChecklist items={parsePlanItems(PLAN)} />)
    expect(screen.getByText('1/3')).toBeInTheDocument()
    expect(screen.getByText('改第 4 章开头').closest('li')?.className).toContain('is-in_progress')
    expect(screen.getByText('读第 3 章').closest('li')?.className).toContain('is-completed')
  })

  it.each(['failed', 'cancelled', undefined])('未成功的更新 %s 不覆盖有效计划', (status) => {
    const activities = [call('saved', 1, 'plan_update', PLAN), result('saved', 2),
      call('attempt', 3, 'plan_update', { items: [{ content: '未经确认的完成', status: 'completed' }] }),
      ...(status ? [result('attempt', 4, status)] : [])]
    expect(latestPlan(activities, 'r1')).toEqual(parsePlanItems(PLAN))
    const rows = stepRows(activities, null)
    expect(rows.filter(item => item.plan)).toHaveLength(1)
    expect(rows.find(item => item.plan)?.plan).toEqual(parsePlanItems(PLAN))
    render(<StepList rows={rows} live />)
    expect(screen.queryByText('未经确认的完成')).toBeNull()
    expect(screen.getByText(status === 'failed' ? '失败' : status === 'cancelled' ? '已取消' : '执行中')).toBeInTheDocument()
  })

  it('按同一轮的成功结果及顺序恢复计划，不使用请求里的旧值', () => {
    const saved = { ...result('p1', 2), payload: { ...result('p1', 2).payload,
      output: { schema: 'zhimeng.tool-result.plan-update.v1', items: [{ content: '读取配置', status: 'pending' }] } } } as AgentActivity
    const activities = [saved, result('p1', 5, 'failed', 'r2'), call('p1', 4, 'plan_update', PLAN, 'r2'),
      call('p1', 1, 'plan_update', { items: [{ content: '读取配置', status: 'completed' }] })]
    expect(latestPlan(activities, 'r1')).toEqual([{ content: '读取配置', status: 'pending' }])
    expect(latestPlan(activities, 'r2')).toBeNull()
    expect(stepRows(activities, null).filter(item => item.plan).map(item => item.plan)).toEqual([[{ content: '读取配置', status: 'pending' }]])
  })

  it('损坏的成功结果不被请求参数替代，旧版空结果仍兼容', () => {
    const invalid = { ...result('bad', 4), payload: { ...result('bad', 4).payload, output: { items: 'invalid' } } } as AgentActivity
    const activities = [call('ok', 1, 'plan_update', PLAN), result('ok', 2), call('bad', 3, 'plan_update', PLAN), invalid]
    expect(latestPlan(activities, 'r1')).toEqual(parsePlanItems(PLAN))
    render(<StepList rows={stepRows(activities, null)} />)
    expect(screen.getByText('计划结果无法读取')).toBeInTheDocument()
  })

  it('历史清单保留进度但不转圈，只有当前运行的计划展示动画', () => {
    const { container, rerender } = render(<PlanChecklist items={parsePlanItems(PLAN)} />)
    expect(container.querySelector('.zm-spinner')).toBeNull()
    expect(screen.getByText('未完成 2 项')).toBeInTheDocument()
    rerender(<PlanChecklist items={parsePlanItems(PLAN)} live />)
    expect(container.querySelector('.zm-spinner')).not.toBeNull()
  })

  it('结束后缺少更新结果时保持未确认，不显示还在执行', () => {
    const { container } = render(<StepList rows={stepRows([call('pending', 1, 'plan_update', PLAN)], null)} />)
    expect(screen.getByText('结果未确认')).toBeInTheDocument()
    expect(container.querySelector('.zm-spinner')).toBeNull()
    expect(screen.queryByRole('group', { name: '工作计划' })).toBeNull()
  })
})

describe('步骤', () => {
  it('显示设置立即控制思考展开和工具合并，不改变活动内容', () => {
    render(<><Thinking thoughts={['检查配置']} live /><StepList rows={[row('读配置', 'read'), row('读说明', 'read')]} /></>)
    expect(screen.getByText('检查配置')).toBeInTheDocument()
    expect(screen.queryByText('读配置')).toBeNull()
    act(() => writeChatPreferences({ expandThinking: false, groupTools: false }))
    expect(screen.queryByText('检查配置')).toBeNull()
    expect(screen.getByText('读配置')).toBeInTheDocument()
    expect(screen.getByText('读说明')).toBeInTheDocument()
  })
  it('计划不占工具那一行，最新一次画成一条清单', () => {
    const rows = stepRows([
      call('p1', 1, 'plan_update', { items: [{ content: '旧计划' }] }), result('p1', 2),
      call('c1', 3, 'read_file', { path: '004_玉佩上的青云.md' }), result('c1', 4),
      call('p2', 5, 'plan_update', PLAN), result('p2', 6),
    ], null)
    expect(rows.map((item) => item.kind)).toEqual(['read', 'plan'])
    const plan = rows[1]
    expect(plan.text).toBe(T.agent.events.plan)
    expect(plan.state).toBe('succeeded')
    expect(plan.plan?.map((item) => item.content)).toEqual(['读第 3 章', '改第 4 章开头', '通读检查'])
  })

  it('运行时每轮自己记的账不算步骤，别的产出物照常算', () => {
    const artifact = (id: string, sequence: number, type: string, name: string) => ({
      activity_id: `a${sequence}`, run_id: 'r1', sequence, kind: 'artifact',
      payload: { artifact_id: id, artifact_type: type, name, uri: `runtime://x/${id}`, media_type: 'application/json', content_hash: 'h', size_bytes: 1 },
    } as unknown as AgentActivity)
    const rows = stepRows([
      artifact('k1', 1, 'context_pack', 'Reviewed Context Pack'),
      artifact('k2', 2, 'writer_intent', 'Writer intent'),
      call('c1', 3, 'read_file', { path: 'notes.md' }), result('c1', 4),
      artifact('k3', 5, 'context_injection_receipt', 'Provider Context injection receipt'),
      artifact('k4', 6, 'writer_candidate_result', 'Writer candidate result'),
    ], null)
    expect(rows.map((item) => item.kind)).toEqual(['read', 'artifact'])
    expect(rows[1].text).toBe(T.agent.events.artifact(T.agent.events.artifactFallback))
  })

  it('每种工具配一类图标：读、列、搜、写、命令、网页、技能、记忆', () => {
    expect(['read_file', 'list_files', 'project_search', 'edit_file', 'computer_run_command', 'search_web', 'skill_read_file', 'memory_write', 'not_a_tool'].map(toolKind))
      .toEqual(['read', 'list', 'search', 'write', 'command', 'web', 'skill', 'memory', 'other'])
  })

  it('思考隔开的相同工具调用各留在原位置，试错未完成不显示成完成', () => {
    const missing = { ...result('c1', 2, 'failed'), payload: { tool_call_id: 'c1', status: 'failed',
      error: { error: { code: 'agent_tool_path_not_found', message: '找不到文件' } } } } as unknown as AgentActivity
    const rows = stepRows([call('c1', 1, 'read_file', { path: 'config.json' }), missing,
      thought(3, '再读一次'), call('c2', 4, 'read_file', { path: 'config.json' }), result('c2', 5)], null)
    expect(rows.map((item) => item.sequence)).toEqual([1, 4])
    expect(rows.map((item) => item.outcome)).toEqual(['failed', 'succeeded'])
    render(<StepList rows={[{ key: 'trial', kind: 'read', text: '找不到文件', state: 'succeeded', outcome: 'failed' }]} />)
    expect(screen.queryByText('未完成')).toBeNull()
    expect(screen.queryByText('完成')).toBeNull()
  })
})

describe('并组', () => {
  it('连着的读 / 列 / 搜并成一组，连着改文件并成一组；只有一步不并', () => {
    const items = groupSteps([
      row('r1', 'read'), row('l1', 'list'), row('s1', 'search'),
      row('w1', 'write', 'succeeded', 'a.md'), row('w2', 'write', 'succeeded', 'b.md'),
      row('c1', 'command'),
      row('r2', 'read'),
    ])
    expect(items.map((item) => (item.type === 'group' ? `${item.group}:${item.rows.length}` : item.row.key))).toEqual(['explore:3', 'edit:2', 'c1', 'r2'])
  })

  it('一组里有一步在跑整组算在跑，有一步没成整组算没成', () => {
    const running = groupSteps([row('r1', 'read'), row('r2', 'read', 'running')])[0]
    const failed = groupSteps([row('r1', 'read', 'failed'), row('r2', 'read')])[0]
    expect(running.type === 'group' && running.state).toBe('running')
    expect(failed.type === 'group' && failed.state).toBe('failed')
  })

  it('界面上一组是一行「已探索 · …」，点开看里面每一步；改文件按文件数算', () => {
    render(<StepList rows={[
      row('读 a.md', 'read'), row('读 b.md', 'read'), row('搜「沈砚」', 'search'),
      row('改 a.md', 'write', 'succeeded', 'a.md'), row('又改 a.md', 'write', 'succeeded', 'a.md'), row('改 b.md', 'write', 'succeeded', 'b.md'),
    ]} />)
    const explore = screen.getByRole('button', { name: A.chat.exploreGroup({ read: 2, list: 0, search: 1 }, false) })
    expect(screen.getByRole('button', { name: A.chat.editGroup(2, false) })).toBeInTheDocument()
    expect(screen.queryByText('读 a.md')).toBeNull()
    fireEvent.click(explore)
    expect(explore).toHaveAttribute('aria-expanded', 'true')
    expect(screen.getByText('读 a.md')).toBeInTheDocument()
    expect(screen.getByText('搜「沈砚」')).toBeInTheDocument()
  })

  it('跑完的一轮折成「执行了 N 步」，点开才列出来', () => {
    render(<StepsFold rows={[row('读 a.md', 'read'), row('跑命令', 'command', 'failed')]} />)
    const head = screen.getByRole('button', { name: '查看处理过程' })
    expect(screen.queryByText('跑命令')).toBeNull()
    fireEvent.click(head)
    expect(screen.getByText('跑命令')).toBeInTheDocument()
  })
})

describe('思考', () => {
  it('按稳定块归并增量，重连去重，最终快照覆盖；相同文字的不同块仍各自保留', () => {
    const chunk = (sequence: number, block: string, phase: string, text: string) => ({
      ...thought(sequence, phase === 'delta' ? '' : text),
      payload: { block_id: block, phase, summary: '模型思考', content: phase === 'delta' ? null : text, delta: phase === 'delta' ? text : null, visibility: 'full' },
    } as AgentActivity)
    const first = chunk(1, 'b1', 'delta', '先读')
    const activities = [first, first, chunk(2, 'b1', 'delta', '配置'), chunk(3, 'b1', 'completed', '先读配置。'), chunk(4, 'b2', 'delta', '再核对'), chunk(5, 'b2', 'interrupted', '再核对'), chunk(6, 'b3', 'completed', '再核对')]
    expect(thoughtsOf(activities, 'r1')).toEqual(['先读配置。', '再核对', '再核对'])
    expect(thoughtBlocksOf(activities, 'r1').map((block) => [block.sequence, block.phase])).toEqual([[1, 'completed'], [4, 'interrupted'], [6, 'completed']])
  })

  it('中断前只有增量也可恢复；运行时说明按来源分开', () => {
    const activities = [
      { ...thought(1, ''), payload: { block_id: 'b1', phase: 'delta', delta: '已看完一半', content: null, summary: '模型思考', visibility: 'summary' } },
      { ...thought(2, '按自动档发送'), payload: { source: 'runtime', content: '按自动档发送', summary: '自动档' } },
    ] as AgentActivity[]
    expect(thoughtBlocksOf(activities, 'r1')).toEqual([{ key: 'r1:b1', text: '已看完一半', sequence: 1, phase: 'delta', visibility: 'summary' }])
  })

  it('尾段按最后一条完成消息边界，不把先前思考再显示一遍', () => {
    const tail = thought(7, '后来的思考')
    const done = { ...thought(5, ''), kind: 'message', payload: { role: 'assistant', phase: 'completed' } } as AgentActivity
    expect(turnTailActivities([tail, thought(2, '先前思考'), thought(9, '别的轮', 'r2'), done], 'r1')).toEqual([tail])
  })

  it('主动展开在完成和历史重挂载后保留，折叠后增量不会强行打开', () => {
    const view = render(<Thinking storageKey="test:sticky" thoughts={['正在看配置']} live />)
    fireEvent.click(screen.getByRole('button'))
    view.rerender(<Thinking storageKey="test:sticky" thoughts={['正在看配置，继续读取']} live />)
    expect(screen.queryByText('正在看配置，继续读取')).toBeNull()
    fireEvent.click(screen.getByRole('button'))
    view.unmount()
    render(<Thinking storageKey="test:sticky" thoughts={['最终收到的内容']} interrupted />)
    expect(screen.getByText('最终收到的内容')).toBeInTheDocument()
    expect(screen.getByText('已保留中断前收到的内容')).toBeInTheDocument()
    expect(screen.getByRole('button')).toHaveAttribute('aria-expanded', 'true')
  })

  it('思考摘要如实命名，正文作为文字显示', () => {
    render(<Thinking thoughts={['<配置>']} summary live />)
    expect(screen.getByRole('button', { name: '正在接收思考摘要…' })).toBeInTheDocument()
    expect(screen.getByText('<配置>')).toBeInTheDocument()
    expect(screen.queryByText(/深度思考/)).toBeNull()
  })

  it('按先后、去重，发送参数说明不算思考；状态行只取最新一段的第一行', () => {
    const note = thought(3, '这描述的是发送参数，不是模型想的', 'r1', '本次按最大档发送')
    const activities = [thought(1, '先看第 3 章\n再想怎么改'), thought(2, '先看第 3 章\n再想怎么改'), note, thought(4, '第 4 章开头太慢了'), thought(5, '别的轮', 'r0')]
    expect(thoughtsOf(activities, 'r1')).toEqual(['先看第 3 章\n再想怎么改', '第 4 章开头太慢了'])
    expect(latestThoughtLine(activities, 'r1')).toBe('第 4 章开头太慢了')
    expect(latestThoughtLine([thought(1, `${'很长'.repeat(30)}`)], null)).toHaveLength(41)
  })
})
