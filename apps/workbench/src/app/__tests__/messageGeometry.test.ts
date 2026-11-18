import { describe, expect, it, vi } from 'vitest'
import { firstMessageAfter } from '../chat/messageGeometry'

function messages(count: number) {
  const measure = vi.fn()
  const nodes = Array.from({ length: count }, (_, index) => ({
    getBoundingClientRect() { measure(); return { top: index * 100, bottom: index * 100 + 60 } },
  }) as HTMLElement)
  return { nodes, measure }
}

describe('ordered message geometry', () => {
  it('finds the current turn at boundaries and between unequal message boxes', () => {
    const { nodes } = messages(3)
    expect(firstMessageAfter(nodes, 'top', -1)).toBe(0)
    expect(firstMessageAfter(nodes, 'top', 0)).toBe(1)
    expect(firstMessageAfter(nodes, 'top', 100)).toBe(2)
    expect(firstMessageAfter(nodes, 'top', 999)).toBe(3)
    expect(firstMessageAfter(nodes, 'bottom', 59)).toBe(0)
    expect(firstMessageAfter(nodes, 'bottom', 60)).toBe(1)
    expect(firstMessageAfter(nodes, 'bottom', 80)).toBe(1)
    expect(firstMessageAfter([], 'bottom', 0)).toBe(0)
  })

  it('locates a reading anchor in a long conversation without measuring every message', () => {
    const { nodes, measure } = messages(10_000)
    expect(firstMessageAfter(nodes, 'bottom', 932_150)).toBe(9321)
    expect(measure.mock.calls.length).toBeLessThanOrEqual(14)
  })
})
