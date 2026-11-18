import { describe, expect, it } from 'vitest'
import type { ChangeSet } from '../../api/changeSets'
import { changeSize } from '../chat/ChangeCard'
import { T } from '../strings'

// 9-27 真机：grok-4.6 只读了一部分就整章重写，一章 5,282 字变成 1,085 字，修改卡上要点开每一处才看得出来。
function record(hunks: Array<{ before_text: string; after_text: string }>): ChangeSet {
  return { changes: [{ relative_path: '05_正文内容/第四章.md', operation: 'update', hunks }] } as unknown as ChangeSet
}

describe('修改卡上的「删了多少、加了多少」', () => {
  it('按逐行对照数字，不算空白', () => {
    const size = changeSize(record([{ before_text: '他缓缓走进了院子。\n\n抬头看天。', after_text: '他走进院子。\n\n抬头看天。' }]))
    expect(size).toEqual({ removed: 9, added: 6 })
    expect(T.agent.changeSize(size.removed, size.added)).toBe('这次删了 9 字、加了 6 字。')
  })

  it('整章被重写丢了一大半时数得出来', () => {
    const before = Array.from({ length: 120 }, (_, n) => `第四章第 ${n + 1} 段，写了一些字。`).join('\n\n')
    const size = changeSize(record([{ before_text: before, after_text: '第四章第 1 段，写了一些字。' }]))
    expect(size.removed).toBeGreaterThan(1500)
    expect(size.added).toBe(0)
    expect(T.agent.changeSize(size.removed, 0)).toMatch(/^这次删了 [\d,]+ 字。$/)
  })

  it('没有变化就不说', () => {
    expect(T.agent.changeSize(0, 0)).toBe('')
  })
})
