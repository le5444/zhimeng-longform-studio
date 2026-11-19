import { afterEach, expect, it, vi } from 'vitest'
import type { AgentActivity } from '../api/agent'
import { createActivityBuffer } from './agentActivityBuffer'

afterEach(() => vi.useRealTimers())

it('首条立即显示，大量回放按序完整交付，尾段无需等待下一条事件', () => {
  vi.useFakeTimers()
  const publish = vi.fn()
  const buffer = createActivityBuffer(publish)
  const events = Array.from({ length: 14787 }, (_, i) => ({ activity_id: `a${i}`, sequence: i + 1 }) as AgentActivity)
  buffer.push(events[0])
  expect(publish).toHaveBeenCalledWith([events[0]])
  for (const event of events.slice(1)) buffer.push(event)
  vi.advanceTimersByTime(32)
  expect(publish.mock.calls.flatMap(([batch]) => batch)).toEqual(events)
  expect(publish.mock.calls.length).toBeLessThan(65)
  expect(Math.max(...publish.mock.calls.map(([batch]) => batch.length))).toBeLessThanOrEqual(250)
})

it('完成或断开时立即交付已收到的尾段，清掉旧计时器；后续连接可继续', () => {
  vi.useFakeTimers()
  const publish = vi.fn()
  const buffer = createActivityBuffer(publish)
  const events = [1, 2, 3].map(sequence => ({ sequence }) as AgentActivity)
  buffer.push(events[0]); buffer.push(events[1])
  buffer.flush()
  expect(publish.mock.calls.flatMap(([batch]) => batch)).toEqual(events.slice(0, 2))
  vi.runAllTimers()
  expect(publish).toHaveBeenCalledTimes(2)
  buffer.push(events[2])
  expect(publish.mock.calls.flatMap(([batch]) => batch)).toEqual(events)
  buffer.flush()
  expect(vi.getTimerCount()).toBe(0)
})
