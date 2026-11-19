import { afterEach, expect, it, vi } from 'vitest'
import { changedSettings, publishSettingsChange, subscribeSettingsChanges } from './settingsChanges'

afterEach(() => vi.restoreAllMocks())

it('only publishes resource invalidation after a confirmed write, never its payload or credentials', async () => {
  const post = vi.fn()
  const close = vi.fn()
  const original = Object.getOwnPropertyDescriptor(window, 'BroadcastChannel')
  Object.defineProperty(window, 'BroadcastChannel', { configurable: true, value: class {
    postMessage = post
    close = close
    onmessage = null
  } })
  const refresh = vi.fn()
  const unsubscribe = subscribeSettingsChanges('providers', refresh)
  try {
    await expect(changedSettings('providers', Promise.reject(new Error('write failed')))).rejects.toThrow('write failed')
    expect(refresh).not.toHaveBeenCalled()
    await changedSettings('providers', Promise.resolve({ api_key: 'synthetic-secret', saved: true }))
    expect(refresh).toHaveBeenCalledTimes(1)
    expect(post).toHaveBeenCalledExactlyOnceWith('providers')
    publishSettingsChange('mcp')
    expect(refresh).toHaveBeenCalledTimes(1)
    window.dispatchEvent(new Event('focus'))
    expect(refresh).toHaveBeenCalledTimes(2)
    unsubscribe()
    window.dispatchEvent(new Event('focus'))
    expect(refresh).toHaveBeenCalledTimes(2)
  } finally {
    unsubscribe()
    if (original) Object.defineProperty(window, 'BroadcastChannel', original)
    else Reflect.deleteProperty(window, 'BroadcastChannel')
  }
})
