import type { QueueInput } from '../../api/agentQueue'

type Input = Omit<QueueInput, 'item_id'>
const PREFIX = 'zhimeng.agent.queue-admission:'

function key(sessionId: string, input: Input): string {
  // Include every submitted field, including attachments and access mode. A
  // stable property order lets a reconstructed composer find the same receipt.
  // The payload lives in the key only; no second copy of the message is stored.
  return PREFIX + JSON.stringify([sessionId, JSON.stringify(input, Object.keys(input).sort())])
}

export function readQueueSendReceipt(sessionId: string, input: Input): string | null {
  try {
    const raw = window.localStorage.getItem(key(sessionId, input))
    if (raw === null) return null
    const receipt: unknown = JSON.parse(raw)
    if (typeof receipt === 'object' && receipt !== null && 'version' in receipt && receipt.version === 1
      && 'itemId' in receipt && typeof receipt.itemId === 'string' && receipt.itemId.length > 0) return receipt.itemId
  } catch { /* An unreadable receipt cannot prove that an earlier request was never accepted. */ }
  throw new Error('暂时无法读取这条排队消息的发送记录，原文已保留，请稍后重试。')
}

export function prepareQueueSendReceipt(sessionId: string, input: Input): string {
  const saved = readQueueSendReceipt(sessionId, input)
  if (saved) return saved
  const itemId = crypto.randomUUID()
  try {
    // Persist before sending. A ref or an in-memory fallback cannot survive reload.
    window.localStorage.setItem(key(sessionId, input), JSON.stringify({ version: 1, itemId }))
  } catch {
    throw new Error('未能保存排队消息的发送记录，本次尚未发送，原文已保留。')
  }
  return itemId
}

export function clearQueueSendReceipt(sessionId: string, input: Input, itemId: string): void {
  if (readQueueSendReceipt(sessionId, input) !== itemId) return
  try {
    window.localStorage.removeItem(key(sessionId, input))
  } catch {
    // Keep the draft and original identity until both the server acknowledgement
    // and local cleanup are known, instead of silently losing a later repeat.
    throw new Error('消息已接收，但发送记录暂时无法清理。原文已保留，重试会核对同一条消息。')
  }
}
