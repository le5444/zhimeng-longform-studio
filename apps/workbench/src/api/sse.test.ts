import { describe, expect, it, vi } from 'vitest'
import { fetchRuntimeSse } from './sse'

function streamResponse(chunks: string[]): Response {
  const encoder = new TextEncoder()
  return new Response(
    new ReadableStream({
      start(controller) {
        chunks.forEach((chunk) => controller.enqueue(encoder.encode(chunk)))
        controller.close()
      },
    }),
    { status: 200, headers: { 'Content-Type': 'text/event-stream; charset=utf-8' } },
  )
}

describe('fetchRuntimeSse', () => {
  it('parses fragmented CRLF and multiline SSE while forwarding the replay cursor', async () => {
    const fetcher = vi.fn<typeof fetch>().mockResolvedValue(
      streamResponse([
        ': heartbeat\r\nid: run-1:4\r\nevent: activ',
        'ity\r\ndata: {"part":1}\r\ndata: {"part":2}\r\n\r\n',
      ]),
    )
    const events: Array<{ id?: string; event?: string; data: string }> = []
    const controller = new AbortController()

    await fetchRuntimeSse('/api/v1/agent/runs/run-1/events', {
      baseUrl: 'http://127.0.0.1:8787',
      token: 'local-token',
      fetcher,
      signal: controller.signal,
      lastEventId: 'run-1:3',
      onEvent: (event) => events.push(event),
    })

    const request = fetcher.mock.calls[0]
    const headers = new Headers(request[1]?.headers)
    expect(headers.get('Last-Event-ID')).toBe('run-1:3')
    expect(headers.get('Authorization')).toBe('Bearer local-token')
    expect(request[1]?.signal).toBe(controller.signal)
    expect(events).toEqual([{
      id: 'run-1:4',
      event: 'activity',
      data: '{"part":1}\n{"part":2}',
    }])
  })
})
