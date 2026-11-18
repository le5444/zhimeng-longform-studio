import { describe, expect, it } from 'vitest'
import { effortOptionLabel, resolveEffortFamily } from './effortOptions'

describe('resolveEffortFamily', () => {
  it('GPT 系按偏好排列请求档位，不宣称渠道支持', () => {
    const family = resolveEffortFamily('gpt-5.6-turbo')
    expect(family.label).toContain('GPT')
    const values = family.efforts.map((item) => item.value)
    expect(values).toContain('xhigh')
    expect(values).not.toContain('max')
    expect(values).not.toContain('ultra')
    expect(values).not.toContain('none')
  })

  it('GLM 系保留 max 请求偏好，不保证它是最高能力', () => {
    const family = resolveEffortFamily('glm-4.6')
    expect(family.label).toContain('GLM')
    const values = family.efforts.map((item) => item.value)
    expect(values).toContain('max')
    expect(values).not.toContain('xhigh')
    expect(values).not.toContain('ultra')
  })

  it('认不出的模型走通用全档位（含 ultra），并如实说明', () => {
    const family = resolveEffortFamily('mystery-model-9000')
    const values = family.efforts.map((item) => item.value)
    expect(values).toContain('ultra')
    expect(values).toContain('max')
    expect(values).toContain('xhigh')
    expect(family.note).toContain('自动去掉')
  })

  it('空模型名走通用全档位', () => {
    expect(resolveEffortFamily('').efforts.length).toBeGreaterThanOrEqual(7)
  })
})

describe('effortOptionLabel', () => {
  it('所有档位都有人话标签', () => {
    expect(effortOptionLabel('auto')).toBe('自动')
    expect(effortOptionLabel('xhigh')).toBe('极高')
    expect(effortOptionLabel('max')).toBe('最大')
    expect(effortOptionLabel('ultra')).toBe('超高')
  })
})


it('describes extension levels as requests instead of verified maximum capabilities', () => {
  for (const model of ['gpt-5', 'glm-4.6', 'deepseek-chat', 'qwen', 'grok', 'unknown']) {
    const family = resolveEffortFamily(model)
    expect(family.note).not.toContain('最高档叫')
    expect(family.note).not.toContain('顶格')
    expect(family.efforts.find((item) => item.value === 'auto')?.hint).toContain('不发送')
  }
})
