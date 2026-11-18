import type { AgentImageAttachment, AgentProviderPreference, AgentRunRequest } from '../api/agent'

const PREFIX = 'zhimeng.agent.send-admission:'
const volatileReceipts = new Map<string, PendingAgentSend>()
const locallyCleared = new Set<string>()
export const IMAGE_DRAFT_PREFIX = 'zhimeng.desk.composer-images:'
const IMAGE_DRAFT_CLAIM_EVENT = 'zhimeng:composer-image-claim'
export const SKILL_DRAFT_PREFIX = 'zhimeng.desk.composer-skills:'
const SKILL_DRAFT_CLAIM_EVENT = 'zhimeng:composer-skill-claim'

export interface ImageDraftIdentity {
  projectId: string | null
  sessionId: string | null
  clientIds: string[]
}
export interface SkillDraftIdentity {
  projectId: string | null
  sessionId: string | null
  tagIds: string[]
}
interface SkillDraftClaim { keys: string[]; tagIds: string[]; names: string[] }

export function claimPendingSkillDraft(item: PendingAgentSend): void {
  const draft = item.skillDraft
  if (!draft || !Array.isArray(draft.tagIds) || !draft.tagIds.every((id) => typeof id === 'string')
    || !(draft.projectId === null || typeof draft.projectId === 'string')
    || !(draft.sessionId === null || typeof draft.sessionId === 'string')) return
  const claim: SkillDraftClaim = {
    keys: [...new Set([JSON.stringify([draft.projectId, draft.sessionId]), JSON.stringify([draft.projectId, item.sessionId])])],
    tagIds: draft.tagIds, names: item.request.manual_skill_names ?? [],
  }
  for (const key of claim.keys) {
    try {
      const stored: unknown = JSON.parse(window.localStorage.getItem(SKILL_DRAFT_PREFIX + key) ?? '[]')
      if (!Array.isArray(stored)) continue
      const remaining = stored.filter((tag) => !(claim.tagIds.includes(tag?.id) && claim.names.includes(tag?.name)))
      if (remaining.length === stored.length) continue
      if (remaining.length) window.localStorage.setItem(SKILL_DRAFT_PREFIX + key, JSON.stringify(remaining))
      else window.localStorage.removeItem(SKILL_DRAFT_PREFIX + key)
    } catch { /* The mounted composer receives the same exact identity below. */ }
  }
  window.dispatchEvent(new CustomEvent<SkillDraftClaim>(SKILL_DRAFT_CLAIM_EVENT, { detail: claim }))
}

export function subscribeSkillDraftClaims(listener: (claim: SkillDraftClaim) => void): () => void {
  const receive = (event: Event) => listener((event as CustomEvent<SkillDraftClaim>).detail)
  window.addEventListener(SKILL_DRAFT_CLAIM_EVENT, receive)
  return () => window.removeEventListener(SKILL_DRAFT_CLAIM_EVENT, receive)
}

interface ImageDraftClaim { keys: string[]; clientIds: string[]; imageIds: string[] }

/** Restore the receipt's exact attachment ownership before resolving its admission. */
export function claimPendingImageDraft(item: PendingAgentSend): void {
  const draft = item.imageDraft
  if (!draft || !Array.isArray(draft.clientIds) || !draft.clientIds.every((id) => typeof id === 'string')
    || !(draft.projectId === null || typeof draft.projectId === 'string')
    || !(draft.sessionId === null || typeof draft.sessionId === 'string')) return
  const claim: ImageDraftClaim = {
    keys: [...new Set([JSON.stringify([draft.projectId, draft.sessionId]), JSON.stringify([draft.projectId, item.sessionId])])],
    clientIds: draft.clientIds,
    imageIds: item.request.image_ids ?? [],
  }
  for (const key of claim.keys) {
    try {
      const stored: unknown = JSON.parse(window.localStorage.getItem(IMAGE_DRAFT_PREFIX + key) ?? '[]')
      if (!Array.isArray(stored)) continue
      const remaining = stored.filter((entry) => !(claim.clientIds.includes(entry?.clientId) && claim.imageIds.includes(entry?.image?.image_id)))
      if (remaining.length === stored.length) continue
      if (remaining.length) window.localStorage.setItem(IMAGE_DRAFT_PREFIX + key, JSON.stringify(remaining))
      else window.localStorage.removeItem(IMAGE_DRAFT_PREFIX + key)
    } catch { /* The mounted composer still receives the ownership update below. */ }
  }
  window.dispatchEvent(new CustomEvent<ImageDraftClaim>(IMAGE_DRAFT_CLAIM_EVENT, { detail: claim }))
}

export function subscribeImageDraftClaims(listener: (claim: ImageDraftClaim) => void): () => void {
  const receive = (event: Event) => listener((event as CustomEvent<ImageDraftClaim>).detail)
  window.addEventListener(IMAGE_DRAFT_CLAIM_EVENT, receive)
  return () => window.removeEventListener(IMAGE_DRAFT_CLAIM_EVENT, receive)
}

/** 只保存这一条尚未确认的发送，模型标识没有凭据；确认受理后立即移除。 */
export interface PendingAgentSend {
  version: 1
  sessionId: string
  key: string
  request: AgentRunRequest
  provider: AgentProviderPreference | null
  createdAt: string
  images?: AgentImageAttachment[]
  /** Composer identities distinguish the submitted images from later reattachments. */
  imageDraft?: ImageDraftIdentity
  skillDraft?: SkillDraftIdentity
  /** 浏览器存储不可用时仍可发送，只能在当前页面保存身份。 */
  persistent?: boolean
}

export function readPendingAgentSend(sessionId: string): PendingAgentSend | null {
  const volatile = volatileReceipts.get(sessionId)
  if (volatile) return volatile
  try {
    const value: unknown = JSON.parse(window.localStorage.getItem(PREFIX + sessionId) ?? 'null')
    if (!value || typeof value !== 'object') return null
    const item = value as PendingAgentSend
    if (item.version !== 1 || item.sessionId !== sessionId || typeof item.key !== 'string'
      || !item.key || typeof item.request?.message !== 'string'
      || !['repair_only', 'author_review_change_proposal'].includes(item.request?.writer_mode)) return null
    if (locallyCleared.has(`${sessionId}:${item.key}`)) return null
    return item
  } catch { return null }
}

export function savePendingAgentSend(item: PendingAgentSend): PendingAgentSend {
  locallyCleared.delete(`${item.sessionId}:${item.key}`)
  try {
    window.localStorage.setItem(PREFIX + item.sessionId, JSON.stringify(item))
    volatileReceipts.delete(item.sessionId)
    return item
  } catch {
    const fallback = { ...item, persistent: false }
    volatileReceipts.set(item.sessionId, fallback)
    return fallback
  }
}

export function clearPendingAgentSend(item: PendingAgentSend): void {
  if (readPendingAgentSend(item.sessionId)?.key !== item.key) return
  locallyCleared.add(`${item.sessionId}:${item.key}`)
  volatileReceipts.delete(item.sessionId)
  try { window.localStorage.removeItem(PREFIX + item.sessionId) }
  catch { /* 已确认的记录下次恢复仍会向后台核对，不会重新发送。 */ }
}

export class AgentSendUnconfirmedError extends Error {
  override readonly name = 'AgentSendUnconfirmedError'
}
