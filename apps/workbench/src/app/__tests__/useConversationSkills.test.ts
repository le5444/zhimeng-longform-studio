import { act, cleanup, renderHook } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it } from 'vitest'
import { useConversationSkills } from '../lib/useConversationSkills'

type Scope = { project: string | null; session: string | null; restoring: boolean }
const a: Scope = { project: 'project-a', session: 'session-a', restoring: false }
const b: Scope = { project: null, session: 'session-b', restoring: false }
const empty: Scope = { project: null, session: null, restoring: false }
function setup(scope: Scope = a) {
  return renderHook((props: Scope) => useConversationSkills(props.project, props.session, props.restoring), { initialProps: scope })
}
type Submission = ReturnType<ReturnType<typeof useConversationSkills>[2]['captureSubmission']>
beforeEach(() => localStorage.clear())
afterEach(cleanup)

describe('对话草稿技能归属', () => {
  it('A附技能后新无项目对话为空，B有自己的标签，回A保留A的未发标签', () => {
    const hook = setup()
    act(() => hook.result.current[1](['文档整理']))
    hook.rerender(b)
    expect(hook.result.current[0]).toEqual([])
    act(() => hook.result.current[1](['表格检查']))
    hook.rerender(a)
    expect(hook.result.current[0]).toEqual(['文档整理'])
    hook.rerender(b)
    expect(hook.result.current[0]).toEqual(['表格检查'])
  })

  it('技能草稿刷新重挂仍按项目+会话恢复，同session不同project也隔离', () => {
    const first = setup()
    act(() => first.result.current[1](['文档整理']))
    first.unmount()
    const second = setup()
    expect(second.result.current[0]).toEqual(['文档整理'])
    second.rerender({ ...a, project: 'project-other' })
    expect(second.result.current[0]).toEqual([])
  })

  it('恢复期间不显示或修改旧标签，旧setter不会污染后来会话', () => {
    const hook = setup()
    const oldSetter = hook.result.current[1]
    act(() => oldSetter(['A技能']))
    hook.rerender({ ...a, restoring: true })
    expect(hook.result.current[0]).toEqual([])
    act(() => oldSetter(['错误旧回调']))
    hook.rerender(b)
    expect(hook.result.current[0]).toEqual([])
    hook.rerender(a)
    expect(hook.result.current[0]).toEqual(['A技能'])
  })

  it.each(['accepted', 'retained'] as const)('%s只移除捕获的标签，等待期间新增或同名摘掉再附标签保持', (finish) => {
    const hook = setup()
    act(() => hook.result.current[1](['文档整理', '旧技能']))
    let submission!: Submission
    act(() => { submission = hook.result.current[2].captureSubmission() })
    expect(submission.names).toEqual(['文档整理', '旧技能'])
    act(() => hook.result.current[1](['旧技能', '新技能']))
    act(() => hook.result.current[1]((current) => [...current, '文档整理']))
    act(() => submission[finish]())
    expect(hook.result.current[0]).toEqual(['新技能', '文档整理'])
  })

  it('A发送晚到成功清A对应快照，B标签和A后来新增标签不受影响', () => {
    const hook = setup()
    act(() => hook.result.current[1](['A已发']))
    let submission!: Submission
    act(() => { submission = hook.result.current[2].captureSubmission() })
    act(() => hook.result.current[1]((current) => [...current, 'A新添']))
    hook.rerender(b)
    act(() => hook.result.current[1](['B未发']))
    act(() => submission.accepted())
    expect(hook.result.current[0]).toEqual(['B未发'])
    hook.rerender(a)
    expect(hook.result.current[0]).toEqual(['A新添'])
  })

  it('明确发送失败保留原标签，也保留等待期间新增标签', () => {
    const hook = setup()
    act(() => hook.result.current[1](['原技能']))
    let submission!: Submission
    act(() => { submission = hook.result.current[2].captureSubmission() })
    act(() => hook.result.current[1]((current) => [...current, '新技能']))
    act(() => submission.failed())
    expect(hook.result.current[0]).toEqual(['原技能', '新技能'])
  })

  it('首次发送创建会话承接未发新标签，成功只清已发快照', () => {
    const hook = setup(empty)
    act(() => hook.result.current[1](['首发技能']))
    let submission!: Submission
    act(() => { submission = hook.result.current[2].captureSubmission() })
    act(() => hook.result.current[1]((current) => [...current, '下一句技能']))
    hook.rerender(b)
    expect(hook.result.current[0]).toEqual(['首发技能', '下一句技能'])
    act(() => submission.accepted())
    expect(hook.result.current[0]).toEqual(['下一句技能'])
    hook.rerender(empty)
    expect(hook.result.current[0]).toEqual([])
  })

  it('首次发送失败后新建会话仍承接原技能；用户主动切换经过restoring则不迁移', () => {
    const hook = setup(empty)
    act(() => hook.result.current[1](['原技能']))
    let submission!: Submission
    act(() => { submission = hook.result.current[2].captureSubmission() })
    hook.rerender(b)
    act(() => submission.failed())
    expect(hook.result.current[0]).toEqual(['原技能'])
    hook.rerender(empty)
    act(() => hook.result.current[1](['另一次首发']))
    let switched!: Submission
    act(() => { switched = hook.result.current[2].captureSubmission() })
    hook.rerender({ ...empty, restoring: true })
    hook.rerender({ ...empty, session: 'manually-created' })
    expect(hook.result.current[0]).toEqual([])
    act(() => switched.failed())
    hook.rerender(empty)
    expect(hook.result.current[0]).toEqual(['另一次首发'])
  })

  it('没有发送身份的主动新建会话不把new草稿标签带过去', () => {
    const hook = setup(empty)
    act(() => hook.result.current[1](['未发技能']))
    hook.rerender(b)
    expect(hook.result.current[0]).toEqual([])
    hook.rerender(empty)
    expect(hook.result.current[0]).toEqual(['未发技能'])
  })

  it('停用来源裁掉所有会话和已保存草稿标签，重新启用不会偷偷恢复', () => {
    const first = setup(a)
    act(() => first.result.current[1](['停用技能', 'A有效']))
    first.rerender(b)
    act(() => first.result.current[1](['停用技能', 'B有效']))
    first.unmount()
    const second = setup(a)
    act(() => second.result.current[2].reconcileAvailable(['A有效', 'B有效']))
    expect(second.result.current[0]).toEqual(['A有效'])
    second.rerender(b)
    expect(second.result.current[0]).toEqual(['B有效'])
    act(() => second.result.current[2].reconcileAvailable(['A有效', 'B有效', '停用技能']))
    second.rerender(a)
    expect(second.result.current[0]).toEqual(['A有效'])
    second.unmount()
    expect(setup(b).result.current[0]).toEqual(['B有效'])
  })
})

it('explicit project moves merge skill tags without changing the source or duplicates', () => {
  const hook = setup()
  act(() => hook.result.current[1](['report', 'table']))
  const transfer = hook.result.current[2].captureTransfer()
  hook.rerender(b)
  act(() => hook.result.current[1](['table', 'code']))
  act(() => transfer(b.project, b.session!))
  expect(hook.result.current[0]).toEqual(['table', 'code', 'report'])
  hook.rerender(a)
  expect(hook.result.current[0]).toEqual(['report', 'table'])
})
