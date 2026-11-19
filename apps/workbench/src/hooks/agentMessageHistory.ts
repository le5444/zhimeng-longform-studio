import { listAgentMessages, type AgentMessage, type AgentMessagePage } from '../api/agent'
import type { RuntimeClientOptions } from '../api/runtime'

/** Merge a persisted page in one pass, preserving previously loaded history. */
export function mergeAgentMessages(existing: AgentMessage[], incoming: AgentMessage[]): AgentMessage[] {
  const messages = new Map(existing.map((message) => [message.message_id, message]))
  for (const message of incoming) messages.set(message.message_id, message)
  return [...messages.values()].sort((a, b) => a.ordinal - b.ordinal)
}

/** Backfill to the last fully read page, never to an optimistic send receipt.
 * Read backwards from a fixed upper boundary so ongoing output cannot move it.
 * Return only after all missing pages arrive; a failed page leaves the UI intact.
 */
export async function readAgentMessageUpdates(
  sessionId: string,
  throughOrdinal: number,
  options: RuntimeClientOptions,
  signal?: AbortSignal,
): Promise<AgentMessagePage> {
  let page = await listAgentMessages(sessionId, options, signal)
  signal?.throwIfAborted()
  const pages = [page]
  let before = page.messages[0]?.ordinal
  while (before !== undefined && before > throughOrdinal + 1 && page.has_earlier === true) {
    const boundary = before
    page = await listAgentMessages(sessionId, options, signal, boundary)
    signal?.throwIfAborted()
    const next = page.messages[0]?.ordinal
    if (next === undefined || next >= boundary || page.messages.some((message) => message.ordinal >= boundary)) {
      throw new Error('历史记录分页没有向前推进，请重试同步记录。')
    }
    pages.push(page)
    before = next
  }
  // Newer pages win if metadata for the same run appears in more than one page.
  const ordered = pages.reverse()
  return {
    ...ordered.at(-1)!,
    messages: ordered.flatMap((item) => item.messages),
    has_earlier: ordered[0].has_earlier,
    run_process_summaries: Object.assign({}, ...ordered.map((item) => item.run_process_summaries)),
    run_origins: Object.assign({}, ...ordered.map((item) => item.run_origins)),
    review_sources: Object.assign({}, ...ordered.map((item) => item.review_sources)),
  }
}
