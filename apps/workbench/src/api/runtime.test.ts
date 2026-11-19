import { describe, expect, it, vi } from 'vitest'
import { fetchRuntimeJson, fetchRuntimeSnapshot, RuntimeTimeoutError } from './runtime'

function jsonResponse(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { 'Content-Type': 'application/json' },
  })
}

describe('fetchRuntimeSnapshot', () => {
  it('merges readiness and runtime status payloads', async () => {
    const fetcher = vi
      .fn<typeof fetch>()
      .mockResolvedValueOnce(jsonResponse({ status: 'ready', version: '0.1.0' }))
      .mockResolvedValueOnce(
        jsonResponse({ status: 'idle', current_run: { status: 'paused' } }),
      )

    const snapshot = await fetchRuntimeSnapshot({ fetcher, baseUrl: 'http://127.0.0.1:7341' })

    expect(snapshot.connection).toBe('online')
    expect(snapshot.ready).toBe(true)
    expect(snapshot.runStatus).toBe('paused')
    expect(fetcher).toHaveBeenNthCalledWith(
      1,
      'http://127.0.0.1:7341/health/ready',
      expect.any(Object),
    )
  })

  it('returns an explicit offline state when both endpoints fail', async () => {
    const fetcher = vi.fn<typeof fetch>().mockRejectedValue(new TypeError('offline'))
    const snapshot = await fetchRuntimeSnapshot({ fetcher })

    expect(snapshot.connection).toBe('offline')
    expect(snapshot.ready).toBe(false)
    expect(snapshot.detail).toContain('未连接')
  })
})

describe('等到超时与作者自己掐断要分得开', () => {
  /** 模仿真实 fetch：signal 一 abort 就以 AbortError 失败。 */
  function fetcherThatNeverAnswers(): typeof fetch {
    return vi.fn<typeof fetch>().mockImplementation(
      (_input, init) => new Promise((_resolve, reject) => {
        init?.signal?.addEventListener('abort', () => {
          reject(new DOMException('aborted', 'AbortError'))
        })
      }),
    )
  }

  /** 先把 rejection 接住，免得定时器推进时冒「未处理的拒绝」。 */
  function settled(promise: Promise<unknown>): Promise<unknown> {
    return promise.then(() => null).catch((caught: unknown) => caught)
  }

  it('等太久：报的是超时，并说清等了多久', async () => {
    vi.useFakeTimers()
    try {
      const outcome = settled(fetchRuntimeJson('/api/v1/slow', {
        fetcher: fetcherThatNeverAnswers(),
        timeoutMs: 2000,
      }))
      await vi.advanceTimersByTimeAsync(2100)
      const error = await outcome
      expect(error).toBeInstanceOf(RuntimeTimeoutError)
      expect((error as Error).message).toContain('2 秒')
    } finally {
      vi.useRealTimers()
    }
  })

  it('作者点停止：原样抛 AbortError，不谎称等太久', async () => {
    const controller = new AbortController()
    const outcome = settled(fetchRuntimeJson('/api/v1/slow', {
      fetcher: fetcherThatNeverAnswers(),
      signal: controller.signal,
      timeoutMs: 2000,
    }))
    controller.abort()
    const error = await outcome
    expect(error).toBeInstanceOf(DOMException)
    expect((error as Error).name).toBe('AbortError')
    expect(error).not.toBeInstanceOf(RuntimeTimeoutError)
  })

  it('后端已经回了状态码就照实报，不被超时盖掉', async () => {
    const fetcher = vi.fn<typeof fetch>().mockImplementation(async () => jsonResponse({
      error: { message: '删除文件前需要你确认' },
    }, 409))
    await expect(fetchRuntimeJson('/api/v1/change-sets/x/apply-direct', { fetcher }))
      .rejects.toThrow('删除文件前需要你确认')
  })
})

describe('fetchRuntimeJson 并发 GET 在途去重', () => {
  it('不同认证的同路径请求各自发送，返回各自的数据', async () => {
    const fetcher = vi.fn<typeof fetch>().mockImplementation(async (_url, init) =>
      jsonResponse({ identity: new Headers(init?.headers).get('Authorization') }))
    vi.stubGlobal('fetch', fetcher)
    try {
      const [a, b] = await Promise.all([
        fetchRuntimeJson('/api/v1/settings/provider', { token: 'audit-A' }),
        fetchRuntimeJson('/api/v1/settings/provider', { token: 'audit-B' }),
      ])
      expect(fetcher).toHaveBeenCalledTimes(2)
      expect(a).toEqual({ identity: 'Bearer audit-A' })
      expect(b).toEqual({ identity: 'Bearer audit-B' })
    } finally {
      vi.unstubAllGlobals()
    }
  })

  it.each([
    { headers: { 'X-Request-Context': 'another-context' } },
    { credentials: 'omit' as const },
    { cache: 'no-store' as const },
    { redirect: 'error' as const },
  ])('自定义请求设置不与同路径的默认请求混用：%o', async (init) => {
    const fetcher = vi.fn<typeof fetch>()
      .mockResolvedValueOnce(jsonResponse({ request: 'default' }))
      .mockResolvedValueOnce(jsonResponse({ request: 'custom' }))
    vi.stubGlobal('fetch', fetcher)
    try {
      const [a, b] = await Promise.all([
        fetchRuntimeJson('/api/v1/agent/status'),
        fetchRuntimeJson('/api/v1/agent/status', {}, init),
      ])
      expect(fetcher).toHaveBeenCalledTimes(2)
      expect(a).toEqual({ request: 'default' })
      expect(b).toEqual({ request: 'custom' })
    } finally {
      vi.unstubAllGlobals()
    }
  })

  it('不同超时分别生效，短请求超时不取消长请求', async () => {
    vi.useFakeTimers()
    const responses: Array<(response: Response) => void> = []
    const signals: Array<AbortSignal | null | undefined> = []
    const fetcher = vi.fn<typeof fetch>().mockImplementation((_url, init) => new Promise((resolve, reject) => {
      responses.push(resolve)
      signals.push(init?.signal)
      init?.signal?.addEventListener('abort', () => reject(new DOMException('aborted', 'AbortError')))
    }))
    vi.stubGlobal('fetch', fetcher)
    try {
      const short = fetchRuntimeJson('/api/v1/runtime/status', { timeoutMs: 100 }).catch((error: unknown) => error)
      const long = fetchRuntimeJson('/api/v1/runtime/status', { timeoutMs: 2000 })
      expect(fetcher).toHaveBeenCalledTimes(2)
      await vi.advanceTimersByTimeAsync(150)
      expect(await short).toBeInstanceOf(RuntimeTimeoutError)
      expect(signals[1]?.aborted).toBe(false)
      responses[1](jsonResponse({ request: 'long' }))
      expect(await long).toEqual({ request: 'long' })
    } finally {
      vi.unstubAllGlobals()
      vi.useRealTimers()
    }
  })

  it('同时发起的同路径 GET 只发一次网络请求，两边拿到同一份数据', async () => {
    // 去重只对「无自定义 fetcher」的默认路径生效（测试隔离优先），
    // 所以这里 stub 全局 fetch。
    const fetcher = vi.fn<typeof fetch>().mockResolvedValue(jsonResponse({ ok: 1 }))
    vi.stubGlobal('fetch', fetcher)

    const [a, b] = await Promise.all([
      fetchRuntimeJson<{ ok: number }>('/api/v1/settings/provider'),
      fetchRuntimeJson<{ ok: number }>('/api/v1/settings/provider'),
    ])

    expect(a).toEqual({ ok: 1 })
    expect(b).toEqual({ ok: 1 })
    expect(fetcher).toHaveBeenCalledTimes(1)
    vi.unstubAllGlobals()
  })

  it('前一请求完成后，新调用照常发新请求（不缓存结果）', async () => {
    const fetcher = vi.fn<typeof fetch>()
      .mockResolvedValueOnce(jsonResponse({ round: 1 }))
      .mockResolvedValueOnce(jsonResponse({ round: 2 }))

    const first = await fetchRuntimeJson<{ round: number }>('/api/v1/agent/status', { fetcher })
    const second = await fetchRuntimeJson<{ round: number }>('/api/v1/agent/status', { fetcher })

    expect(first).toEqual({ round: 1 })
    expect(second).toEqual({ round: 2 })
    expect(fetcher).toHaveBeenCalledTimes(2)
  })

  it('带 abort signal 的调用不参与合并', async () => {
    const fetcher = vi.fn<typeof fetch>().mockImplementation(async () => jsonResponse({ ok: 1 }))
    const controller = new AbortController()

    await Promise.all([
      fetchRuntimeJson('/api/v1/agent/sessions', { fetcher }),
      fetchRuntimeJson('/api/v1/agent/sessions', { fetcher, signal: controller.signal }),
    ])

    expect(fetcher).toHaveBeenCalledTimes(2)
  })
})
