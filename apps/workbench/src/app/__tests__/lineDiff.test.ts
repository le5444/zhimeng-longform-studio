import { describe, expect, it } from 'vitest'
import { diffLines } from '../lib/lineDiff'

describe('diffLines：把两段文字对齐成逐行对照', () => {
  it('长代码行的空白修改不能在显示拆行时消失', () => {
    const prefix = 'x'.repeat(200)
    const before = `${prefix}    return value;`
    const after = `${prefix} return value;`
    const rows = diffLines(before, after)
    expect(rows.filter((row) => row.kind !== 'add').map((row) => row.text).join('\n')).toBe(before)
    expect(rows.filter((row) => row.kind !== 'del').map((row) => row.text).join('\n')).toBe(after)
    expect(rows.some((row) => row.kind === 'del')).toBe(true)
  })

  it('大文件只改中间一行时，保留前后的实际相同行', () => {
    const before = Array.from({ length: 5000 }, (_, i) => `const value${i} = ${i};`)
    const after = [...before]
    after[2500] = 'const value2500 = 42;'
    const rows = diffLines(before.join('\n'), after.join('\n'))
    expect(rows.filter((row) => row.kind !== 'same')).toEqual([
      { kind: 'del', text: before[2500] }, { kind: 'add', text: after[2500] },
    ])
  })

  it('完全一样的两段，每一行都是没动的', () => {
    const rows = diffLines('甲\n乙\n丙', '甲\n乙\n丙')
    expect(rows).toEqual([
      { kind: 'same', text: '甲' },
      { kind: 'same', text: '乙' },
      { kind: 'same', text: '丙' },
    ])
  })

  it('中间改了一句：那句先红后绿，上下文不动', () => {
    const rows = diffLines('开头。\n旧的一句。\n结尾。', '开头。\n新的一句。\n结尾。')
    expect(rows).toEqual([
      { kind: 'same', text: '开头。' },
      { kind: 'del', text: '旧的一句。' },
      { kind: 'add', text: '新的一句。' },
      { kind: 'same', text: '结尾。' },
    ])
  })

  it('只加了几行：全是绿的，没有红的', () => {
    const rows = diffLines('第一段。', '第一段。\n插进来的。\n又一句。')
    expect(rows.map((r) => r.kind)).toEqual(['same', 'add', 'add'])
  })

  it('只删了几行：全是红的', () => {
    const rows = diffLines('第一段。\n不要了。\n第二段。', '第一段。\n第二段。')
    expect(rows).toEqual([
      { kind: 'same', text: '第一段。' },
      { kind: 'del', text: '不要了。' },
      { kind: 'same', text: '第二段。' },
    ])
  })

  it('空的一边：新建全绿，删除全红', () => {
    expect(diffLines('', '新的')).toEqual([{ kind: 'add', text: '新的' }])
    expect(diffLines('旧的', '')).toEqual([{ kind: 'del', text: '旧的' }])
    expect(diffLines('', '')).toEqual([])
  })

  it('两段空行分开的网文段落：只动的那一段变红绿，其余原样', () => {
    const before = '沈青云放下笔。\n\n院子里安静。\n\n朱砂没干。'
    const after = '沈青云放下笔。\n\n院子里静得可怕。\n\n朱砂没干。'
    const rows = diffLines(before, after)
    const changed = rows.filter((r) => r.kind !== 'same')
    expect(changed).toEqual([
      { kind: 'del', text: '院子里安静。' },
      { kind: 'add', text: '院子里静得可怕。' },
    ])
    expect(rows.filter((r) => r.kind === 'same').length).toBe(4)
  })
})
