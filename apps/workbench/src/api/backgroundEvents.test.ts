import { describe, expect, it, vi } from 'vitest'
import { readBackgroundEvents } from './backgroundEvents'

describe('会话后台命令事件', () => {
  it('只读最新交付状态，编码会话并传递认证和取消信号', async () => {
    const fetcher = vi.fn<typeof fetch>().mockResolvedValue(new Response(JSON.stringify({ events: [], next_cursor: 5, active_run: null, latest_run: null, has_running_commands: false, pending_count: 0 })))
    const controller = new AbortController()
    const view = await readBackgroundEvents('session /a', { fetcher, signal: controller.signal, token: 'local-fixture' })
    expect(fetcher.mock.calls[0][0]).toBe('/api/v1/agent/sessions/session%20%2Fa/background-events?after=0')
    expect(fetcher.mock.calls[0][1]?.method).toBeUndefined()
    expect(new Headers(fetcher.mock.calls[0][1]?.headers).get('Authorization')).toBe('Bearer local-fixture')
    expect(fetcher.mock.calls[0][1]?.signal).toBeInstanceOf(AbortSignal)
    expect(view.next_cursor).toBe(5)
  })
})
