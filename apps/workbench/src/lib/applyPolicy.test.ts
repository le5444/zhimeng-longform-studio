import { afterEach, describe, expect, it } from 'vitest'
import { readApplyPolicy, writeApplyPolicy } from './applyPolicy'

describe('applyPolicy', () => {
  afterEach(() => {
    window.localStorage.clear()
  })

  it('默认是「直接保存」，可以切换为「先看修改」并存回来', () => {
    expect(readApplyPolicy()).toBe('auto')
    writeApplyPolicy('review')
    expect(readApplyPolicy()).toBe('review')
    writeApplyPolicy('auto')
    expect(readApplyPolicy()).toBe('auto')
  })
})
