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
  it('分页传输块包含多帧并跨UTF8分段时逐条保留事件编号和正文', async () => {
    const frames = Array.from({ length: 753 }, (_, i) => `id: replay:${i + 1}\nevent: activity\ndata: ${JSON.stringify({ delta: `采购${i}元` })}\n\n`)
    const bytes = new TextEncoder().encode(frames.join(''))
    const fetcher = vi.fn<typeof fetch>().mockResolvedValue(new Response(new ReadableStream({ start(controller) {
      for (let i = 0; i < bytes.length; i += 397) controller.enqueue(bytes.slice(i, i + 397))
      controller.close()
    } }), { headers: { 'Content-Type': 'text/event-stream' } }))
    const events: Array<{ id?: string; data: string }> = []
    await fetchRuntimeSse('/events', { fetcher, signal: new AbortController().signal, onEvent: e => events.push(e) })
    expect(events.map(e => e.id)).toEqual(frames.map((_, i) => `replay:${i + 1}`))
    expect(events.map(e => JSON.parse(e.data).delta)).toEqual(frames.map((_, i) => `采购${i}元`))
  })

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
