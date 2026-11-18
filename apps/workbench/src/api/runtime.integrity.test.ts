import { describe, expect, it, vi } from 'vitest'
import { fetchRuntimeJson, fetchRuntimeSnapshot, RuntimeApiError, RuntimeTimeoutError } from './runtime'

describe('response body integrity', () => {
  it('does not accept truncated JSON as a successful mutation', async () => {
    const fetcher = vi.fn<typeof fetch>().mockResolvedValue(new Response('{"ok":', { status: 200 }))
    await expect(fetchRuntimeJson('/api/v1/test-save', { fetcher }, { method: 'POST' })).rejects.toThrow()
  })

  it('does not report malformed health and status bodies as online', async () => {
    const fetcher = vi.fn<typeof fetch>().mockImplementation(async () => new Response('<html>proxy error</html>'))
    const result = await fetchRuntimeSnapshot({ fetcher })
    expect(result.ready).toBe(false)
    expect(result.connection).not.toBe('online')
  })

  it.each([null, {}, [], { status: 'offline' }])('does not infer readiness from %j', async (health) => {
    const fetcher = vi.fn<typeof fetch>()
      .mockResolvedValueOnce(new Response(JSON.stringify(health)))
      .mockResolvedValueOnce(new Response('{"status":"idle"}'))
    const result = await fetchRuntimeSnapshot({ fetcher })
    expect(result.ready).toBe(false)
    expect(result.connection).not.toBe('online')
  })

  it('preserves timeout classification after the response headers arrive', async () => {
    vi.useFakeTimers()
    try {
      const fetcher = vi.fn<typeof fetch>().mockImplementation(async (_input, init) => ({
        ok: true, status: 200,
        json: () => new Promise((_resolve, reject) => {
          init?.signal?.addEventListener('abort', () => reject(new DOMException('aborted', 'AbortError')))
        }),
      }) as Response)
      const result = fetchRuntimeJson('/api/v1/test-save', { fetcher, timeoutMs: 2000 }, { method: 'POST' })
        .then(() => null, (error: unknown) => error)
      await vi.advanceTimersByTimeAsync(2100)
      expect(await result).toBeInstanceOf(RuntimeTimeoutError)
    } finally {
      vi.useRealTimers()
    }
  })

  it('preserves cancellation while reading a body', async () => {
    const controller = new AbortController()
    let bodyStarted!: () => void
    const started = new Promise<void>((resolve) => { bodyStarted = resolve })
    const fetcher = vi.fn<typeof fetch>().mockImplementation(async (_input, init) => ({
      ok: true, status: 200,
      json: () => new Promise((_resolve, reject) => {
        init?.signal?.addEventListener('abort', () => reject(new DOMException('aborted', 'AbortError')))
        bodyStarted()
      }),
    }) as Response)
    const result = fetchRuntimeJson('/api/v1/test-read', { fetcher, signal: controller.signal })
      .then(() => null, (error: unknown) => error)
    await started
    controller.abort()
    expect(await result).toMatchObject({ name: 'AbortError' })
  })

  it('preserves a known HTTP failure when its body is not JSON', async () => {
    const fetcher = vi.fn<typeof fetch>().mockResolvedValue(new Response('unavailable', { status: 503 }))
    await expect(fetchRuntimeJson('/api/v1/test-read', { fetcher })).rejects.toMatchObject({
      name: 'RuntimeApiError', status: 503,
    } satisfies Partial<RuntimeApiError>)
  })

  it('accepts an intentionally empty no-content response', async () => {
    const fetcher = vi.fn<typeof fetch>().mockResolvedValue(new Response(null, { status: 204 }))
    await expect(fetchRuntimeJson('/api/v1/test-delete', { fetcher }, { method: 'DELETE' })).resolves.toBeNull()
  })
})
