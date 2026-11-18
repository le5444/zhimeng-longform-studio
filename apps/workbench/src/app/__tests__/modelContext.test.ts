import { describe, expect, it } from 'vitest'
import { contextWindowLabel } from '../../lib/modelContext'

describe('contextWindowLabel', () => {
  it('两档各自的显示名；未知值回落 256K，不再出现「预估」', () => {
    expect(contextWindowLabel('1m')).toBe('1M')
    expect(contextWindowLabel('256k')).toBe('256K')
    expect(contextWindowLabel(undefined)).toBe('256K')
    expect(contextWindowLabel('moonshot-v1-1m')).toBe('256K')
    expect(contextWindowLabel('256K')).not.toContain('预估')
  })
})
