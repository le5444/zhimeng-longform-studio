import { describe, expect, it, vi } from 'vitest'
import { listCommandSessions, readCommandSession, stopCommandSession } from './commandSessions'

describe('会话命令 API', () => {
  it('编码会话及命令 ID、保留字节游标并携带本机认证', async () => {
    const fetcher = vi.fn<typeof fetch>().mockResolvedValue(new Response(JSON.stringify({ stdout: { text: '中文' } })))
    const controller = new AbortController()
    await readCommandSession('session /1', 'command/2', { stdout_offset: 0, stderr_before: 12, max_bytes: 65536 }, {
      fetcher, token: 'local-test-token', baseUrl: 'http://127.0.0.1:8788', signal: controller.signal,
    })
    const [url, init] = fetcher.mock.calls[0]
    expect(url).toBe('http://127.0.0.1:8788/api/v1/agent/sessions/session%20%2F1/commands/command%2F2?stdout_offset=0&stderr_before=12&max_bytes=65536')
    expect(new Headers(init?.headers).get('Authorization')).toBe('Bearer local-test-token')
    expect(init?.signal).toBeInstanceOf(AbortSignal)
  })

  it('列表只读、停止只发命令自己的端点，错误原由不会丢失', async () => {
    const fetcher = vi.fn<typeof fetch>().mockResolvedValueOnce(new Response(JSON.stringify({ commands: [] })))
      .mockResolvedValueOnce(new Response(JSON.stringify({ detail: '这个对话里找不到该命令会话。' }), { status: 404 }))
    await expect(listCommandSessions('a', { fetcher })).resolves.toEqual({ commands: [] })
    await expect(stopCommandSession('a', 'c', { fetcher })).rejects.toThrow('这个对话里找不到该命令会话。')
    expect(fetcher.mock.calls[0][0]).toBe('/api/v1/agent/sessions/a/commands')
    expect(fetcher.mock.calls[1][0]).toBe('/api/v1/agent/sessions/a/commands/c/stop')
    expect(fetcher.mock.calls[1][1]?.method).toBe('POST')
  })
})
