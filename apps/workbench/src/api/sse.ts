import { createParser, type EventSourceMessage, type ParseError } from 'eventsource-parser'
import { joinRuntimeUrl, RuntimeApiError, type RuntimeClientOptions } from './runtime'

export interface RuntimeSseOptions extends RuntimeClientOptions {
  signal: AbortSignal
  lastEventId?: string | null
  onEvent: (event: EventSourceMessage) => void
}

export async function fetchRuntimeSse(
  path: string,
  options: RuntimeSseOptions,
): Promise<void> {
  const baseUrl = options.baseUrl ?? import.meta.env.VITE_RUNTIME_URL ?? ''
  const token = options.token ?? import.meta.env.VITE_RUNTIME_TOKEN ?? ''
  const fetcher = options.fetcher ?? fetch
  const headers = new Headers({ Accept: 'text/event-stream' })
  if (token) headers.set('Authorization', `Bearer ${token}`)
  if (options.lastEventId) headers.set('Last-Event-ID', options.lastEventId)

  const response = await fetcher(joinRuntimeUrl(baseUrl, path), {
    method: 'GET',
    headers,
    signal: options.signal,
    cache: 'no-store',
  })
  if (!response.ok) {
    const payload = await response.json().catch(() => null)
    throw new RuntimeApiError(response.status, payload)
  }
  const contentType = response.headers.get('content-type')?.toLowerCase() ?? ''
  if (!contentType.includes('text/event-stream')) {
    throw new Error(`Runtime returned a non-SSE response: ${contentType || 'missing'}`)
  }
  if (!response.body) throw new Error('Runtime returned an empty SSE body')

  let fatalError: ParseError | null = null
  const parser = createParser({
    maxBufferSize: 1_048_576,
    onEvent: options.onEvent,
    onError: (error) => {
      if (error.type === 'max-buffer-size-exceeded') fatalError = error
    },
  })
  const reader = response.body.getReader()
  const decoder = new TextDecoder()
  try {
    while (true) {
      const { done, value } = await reader.read()
      if (done) break
      parser.feed(decoder.decode(value, { stream: true }))
      if (fatalError) throw fatalError
    }
    const tail = decoder.decode()
    if (tail) parser.feed(tail)
    parser.reset({ consume: true })
    if (fatalError) throw fatalError
  } finally {
    reader.releaseLock()
  }
}
