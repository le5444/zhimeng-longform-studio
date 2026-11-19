import { act, cleanup, renderHook } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it } from 'vitest'
import { useConversationDraft } from '../lib/useConversationDraft'

afterEach(cleanup)
beforeEach(() => localStorage.clear())
const prefix = 'zhimeng.desk.composer:'

describe('对话草稿隔离', () => {
  it('恢复另一条对话时隐藏旧草稿，输入不会污染任何一条对话', () => {
    localStorage.setItem(prefix + 'book:a', 'A 的草稿')
    localStorage.setItem(prefix + 'book:b', 'B 的草稿')
    const { result, rerender } = renderHook(({ session, restoring }) => useConversationDraft('book', session, restoring), {
      initialProps: { session: 'a', restoring: false },
    })
    expect(result.current[0]).toBe('A 的草稿')
    rerender({ session: 'b', restoring: true })
    expect(result.current[0]).toBe('')
    act(() => result.current[1]('正在切换时的输入'))
    expect(localStorage.getItem(prefix + 'book:a')).toBe('A 的草稿')
    expect(localStorage.getItem(prefix + 'book:b')).toBe('B 的草稿')
    rerender({ session: 'b', restoring: false })
    expect(result.current[0]).toBe('B 的草稿')
  })

  it('旧发送失败回调不能在切换后或返回原对话后覆盖新草稿', () => {
    const { result, rerender } = renderHook(({ session }) => useConversationDraft('book', session, false), {
      initialProps: { session: 'a' },
    })
    const oldUpdate = result.current[1]
    act(() => oldUpdate('A 原来的输入'))
    rerender({ session: 'b' })
    act(() => result.current[1]('B 的新输入'))
    act(() => oldUpdate('A 发送失败后恢复'))
    expect(result.current[0]).toBe('B 的新输入')
    rerender({ session: 'a' })
    act(() => result.current[1]('A 后来改过的输入'))
    act(() => oldUpdate('过期恢复'))
    expect(result.current[0]).toBe('A 后来改过的输入')
    expect(localStorage.getItem(prefix + 'book:a')).toBe('A 后来改过的输入')
  })

  it('函数式更新和重新挂载仍保存当前对话的实际输入', () => {
    const first = renderHook(() => useConversationDraft(null, 'loose-a', false))
    act(() => first.result.current[1]('第一行'))
    act(() => first.result.current[1]((current) => current + '\n第二行'))
    first.unmount()
    const second = renderHook(() => useConversationDraft(null, 'loose-a', false))
    expect(second.result.current[0]).toBe('第一行\n第二行')
  })

  it('首次发送创建会话后失败，原文恢复到新会话并留到刷新后', () => {
    const hook = renderHook(({ session }: { session: string | null }) => useConversationDraft(null, session, false), {
      initialProps: { session: null as string | null },
    })
    act(() => hook.result.current[1]('第一句原文'))
    let submission!: ReturnType<typeof hook.result.current[2]['beginSubmission']>
    act(() => { submission = hook.result.current[2].beginSubmission('第一句原文') })
    expect(hook.result.current[0]).toBe('')
    hook.rerender({ session: 'created' })
    act(() => submission.failed())
    expect(hook.result.current[0]).toBe('第一句原文')
    expect(localStorage.getItem(prefix + 'loose:new')).toBeNull()
    hook.unmount()
    const reopened = renderHook(() => useConversationDraft(null, 'created', false))
    expect(reopened.result.current[0]).toBe('第一句原文')
  })

  it('发送期间新草稿保留，旧原文能在刷新后明确追加回来', () => {
    const hook = renderHook(() => useConversationDraft(null, 'a', false))
    act(() => hook.result.current[1]('旧原文'))
    let submission!: ReturnType<typeof hook.result.current[2]['beginSubmission']>
    act(() => { submission = hook.result.current[2].beginSubmission('旧原文') })
    act(() => hook.result.current[1]('新草稿'))
    act(() => submission.failed())
    expect(hook.result.current[0]).toBe('新草稿')
    expect(hook.result.current[2].recoveries.map((item) => item.text)).toEqual(['旧原文'])
    hook.unmount()
    const reopened = renderHook(() => useConversationDraft(null, 'a', false))
    act(() => reopened.result.current[2].appendRecovery(reopened.result.current[2].recoveries[0].id))
    expect(reopened.result.current[0]).toBe('新草稿\n\n旧原文')
    expect(reopened.result.current[2].recoveries).toEqual([])
  })

  it('首次建会话承接等待期间输入的新草稿，失败原文另存恢复区', () => {
    const hook = renderHook(({ session }: { session: string | null }) => useConversationDraft(null, session, false), {
      initialProps: { session: null as string | null },
    })
    act(() => hook.result.current[1]('初始原文'))
    let submission!: ReturnType<typeof hook.result.current[2]['beginSubmission']>
    act(() => { submission = hook.result.current[2].beginSubmission('初始原文') })
    act(() => hook.result.current[1]('新补充'))
    hook.rerender({ session: 'created' })
    act(() => submission.failed())
    expect(hook.result.current[0]).toBe('新补充')
    expect(hook.result.current[2].recoveries.map((item) => item.text)).toEqual(['初始原文'])
  })

  it('离开A之后迟到的发送失败只恢复A，不改B也不串到后来新建的会话', () => {
    const hook = renderHook(({ session, restoring }) => useConversationDraft(null, session, restoring), {
      initialProps: { session: 'a', restoring: false },
    })
    act(() => hook.result.current[1]('A 原文'))
    let submission!: ReturnType<typeof hook.result.current[2]['beginSubmission']>
    act(() => { submission = hook.result.current[2].beginSubmission('A 原文') })
    hook.rerender({ session: 'a', restoring: true })
    hook.rerender({ session: 'b', restoring: false })
    act(() => hook.result.current[1]('B 草稿'))
    act(() => submission.failed())
    expect(hook.result.current[0]).toBe('B 草稿')
    expect(hook.result.current[2].recoveries).toEqual([])
    expect(submission.isCurrent()).toBe(false)
    hook.rerender({ session: 'a', restoring: false })
    expect(hook.result.current[0]).toBe('A 原文')
  })
})

it('打开文件夹后的新会话承接原文，迟到衔接不覆盖另一会话或目标新稿', () => {
  const hook = renderHook(({ project, session, restoring }: { project: string | null; session: string | null; restoring: boolean }) => useConversationDraft(project, session, restoring), {
    initialProps: { project: null as string | null, session: null as string | null, restoring: false },
  })
  act(() => hook.result.current[1]('检查采购资料'))
  const transfer = hook.result.current[2].captureTransfer()
  hook.rerender({ project: 'project', session: 'created', restoring: true })
  act(() => transfer('project', 'created'))
  hook.rerender({ project: 'project', session: 'created', restoring: false })
  expect(hook.result.current[0]).toBe('检查采购资料')
  expect(localStorage.getItem(prefix + 'loose:new')).toBe('检查采购资料')
  act(() => hook.result.current[1]('目标已有新稿'))
  hook.rerender({ project: 'other', session: 'other', restoring: false })
  act(() => hook.result.current[1]('别的对话'))
  act(() => transfer('project', 'created'))
  expect(hook.result.current[0]).toBe('别的对话')
  hook.rerender({ project: 'project', session: 'created', restoring: false })
  expect(hook.result.current[0]).toBe('目标已有新稿\n\n检查采购资料')
})
