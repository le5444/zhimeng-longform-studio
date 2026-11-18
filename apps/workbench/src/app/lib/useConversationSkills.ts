import { useCallback, useEffect, useLayoutEffect, useMemo, useRef, useState, type SetStateAction } from 'react'
import { SKILL_DRAFT_PREFIX as PREFIX, subscribeSkillDraftClaims } from '../../hooks/agentSendReceipt'

interface Tag { id: string; name: string }
interface Scope { key: string; restoring: boolean }
interface Submission {
  key: string
  scope: Scope
  tags: Tag[]
  canAdoptSession: boolean
  status: 'pending' | 'accepted' | 'failed'
}
function readStored(key: string): Tag[] {
  try {
    const value: unknown = JSON.parse(window.localStorage.getItem(PREFIX + key) ?? '[]')
    return Array.isArray(value) ? value.filter((item): item is Tag => typeof item?.id === 'string' && typeof item?.name === 'string') : []
  } catch { return [] }
}
function saveStored(key: string, entries: Tag[]) {
  try {
    if (entries.length) window.localStorage.setItem(PREFIX + key, JSON.stringify(entries))
    else window.localStorage.removeItem(PREFIX + key)
  } catch { /* 当前页面里的标签仍按会话保存。 */ }
}

/** 技能属于这条未发送的输入，按项目/会话存放，不跟着组件跨对话串用。 */
export function useConversationSkills(projectId: string | null, sessionId: string | null, restoring: boolean) {
  const key = JSON.stringify([projectId, sessionId])
  const scope = useMemo<Scope>(() => ({ key, restoring }), [key, restoring])
  const currentScope = useRef(scope)
  currentScope.current = scope
  const cache = useRef(new Map<string, Tag[]>())
  const available = useRef<Set<string> | null>(null)
  const submissions = useRef(new Set<Submission>())
  const previous = useRef({ scope, projectId, sessionId })
  const [revision, refresh] = useState(0)
  const get = useCallback((target: string): Tag[] => {
    if (!cache.current.has(target)) cache.current.set(target, readStored(target))
    const tags = cache.current.get(target)!
    return available.current ? tags.filter((tag) => available.current!.has(tag.name)) : tags
  }, [])
  const put = useCallback((target: string, tags: Tag[]) => {
    cache.current.set(target, tags)
    saveStored(target, tags)
  }, [])
  useEffect(() => subscribeSkillDraftClaims((claim) => {
    for (const key of claim.keys) put(key, get(key).filter((tag) => !(claim.tagIds.includes(tag.id) && claim.names.includes(tag.name))))
    refresh((value) => value + 1)
  }), [get, put])

  useLayoutEffect(() => {
    const before = previous.current
    let changed = false
    for (const submission of submissions.current) {
      if (before.scope !== scope) {
        if (submission.canAdoptSession && submission.scope === before.scope && !restoring && !before.scope.restoring
          && before.projectId === projectId && before.sessionId === null && sessionId !== null) {
          const moving = get(submission.key)
          const destination = get(key)
          const names = new Set(destination.map((tag) => tag.name))
          put(key, [...destination, ...moving.filter((tag) => !names.has(tag.name))])
          put(submission.key, [])
          submission.key = key
          submission.scope = scope
          changed = true
        }
        submission.canAdoptSession = false
      }
      if (submission.status === 'pending') continue
      if (submission.status === 'accepted') {
        const sent = new Set(submission.tags.map((tag) => tag.id))
        put(submission.key, get(submission.key).filter((tag) => !sent.has(tag.id)))
        changed = true
      }
      submissions.current.delete(submission)
    }
    previous.current = { scope, projectId, sessionId }
    if (changed) refresh((value) => value + 1)
  }, [get, key, projectId, put, restoring, revision, scope, sessionId])

  const update = useCallback((value: SetStateAction<string[]>) => {
    if (scope.restoring || currentScope.current !== scope) return
    const old = get(scope.key)
    const requested = typeof value === 'function' ? value(old.map((tag) => tag.name)) : value
    const names = [...new Set(requested)].filter((name) => !available.current || available.current.has(name))
    const oldByName = new Map(old.map((tag) => [tag.name, tag]))
    put(scope.key, names.map((name) => oldByName.get(name) ?? { id: crypto.randomUUID(), name }))
    refresh((count) => count + 1)
  }, [get, put, scope])

  const reconcileAvailable = useCallback((names: readonly string[]) => {
    available.current = new Set(names)
    const keys = new Set(cache.current.keys())
    try {
      for (let index = 0; index < window.localStorage.length; index++) {
        const stored = window.localStorage.key(index)
        if (stored?.startsWith(PREFIX)) keys.add(stored.slice(PREFIX.length))
      }
    } catch { /* 可用来源变化仍会裁剪本页缓存。 */ }
    for (const target of keys) put(target, get(target))
    refresh((count) => count + 1)
  }, [get, put])

  const captureSubmission = useCallback(() => {
    const tags = scope.restoring || currentScope.current !== scope ? [] : [...get(scope.key)]
    const submission: Submission = { key: scope.key, scope, tags, canAdoptSession: sessionId === null, status: 'pending' }
    submissions.current.add(submission)
    const finish = (status: 'accepted' | 'failed') => {
      submission.status = status
      refresh((count) => count + 1)
    }
    return { names: tags.map((tag) => tag.name), draftIdentity: { projectId, sessionId, tagIds: tags.map((tag) => tag.id) }, accepted: () => finish('accepted'), retained: () => finish('accepted'), failed: () => finish('failed') }
  }, [get, projectId, scope, sessionId])

  const captureTransfer = useCallback(() => {
    const captured = scope.restoring ? [] : [...get(scope.key)]
    return (targetProject: string | null, targetSession: string) => {
      const target = JSON.stringify([targetProject, targetSession])
      const existing = get(target)
      const names = new Set(existing.map(tag => tag.name))
      put(target, [...existing, ...captured.filter(tag => !names.has(tag.name)).map(tag => ({ ...tag, id: crypto.randomUUID() }))])
      refresh(count => count + 1)
    }
  }, [get, put, scope])
  return [restoring ? [] : get(key).map((tag) => tag.name), update, { captureSubmission, reconcileAvailable, captureTransfer }] as const
}
