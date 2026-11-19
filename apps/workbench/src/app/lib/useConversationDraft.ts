import { useCallback, useLayoutEffect, useMemo, useRef, useState, type SetStateAction } from 'react'

const PREFIX = 'zhimeng.desk.composer:'
interface RecoverableDraft { id: string; text: string }
interface Submission {
  key: string
  scope: object
  text: string
  status: 'pending' | 'accepted' | 'failed'
  canAdoptSession: boolean
}
function readRecovery(key: string): RecoverableDraft[] {
  try {
    const value: unknown = JSON.parse(window.localStorage.getItem(PREFIX + 'recovery:' + key) ?? '[]')
    return Array.isArray(value) ? value.filter((item): item is RecoverableDraft => typeof item?.id === 'string' && typeof item?.text === 'string') : []
  } catch { return [] }
}
function saveRecovery(key: string, entries: RecoverableDraft[]) {
  try {
    if (entries.length) window.localStorage.setItem(PREFIX + 'recovery:' + key, JSON.stringify(entries))
    else window.localStorage.removeItem(PREFIX + 'recovery:' + key)
  } catch { /* 草稿仍在当前页面里 */ }
}
function read(key: string): string {
  try { return window.localStorage.getItem(PREFIX + key) ?? '' } catch { return '' }
}
function save(key: string, value: string) {
  try {
    if (value) window.localStorage.setItem(PREFIX + key, value)
    else window.localStorage.removeItem(PREFIX + key)
  } catch { /* 输入区仍能用 */ }
}

/** 切换对话和打开设置时保留输入，输入过的内容不跟着跑进另一本书。 */
export function useConversationDraft(projectId: string | null, sessionId: string | null, restoring: boolean) {
  const requestedKey = `${projectId ?? 'loose'}:${sessionId ?? 'new'}`
  const scope = useMemo(() => ({ key: requestedKey, restoring }), [requestedKey, restoring])
  const currentScope = useRef(scope)
  currentScope.current = scope
  const [state, setState] = useState(() => ({ key: requestedKey, draft: read(requestedKey) }))
  const submissions = useRef(new Set<Submission>())
  const recovery = useRef(new Map<string, RecoverableDraft[]>())
  const previousScope = useRef({ scope, projectId, sessionId })
  const [submissionRevision, refreshSubmissions] = useState(0)
  const recoveriesFor = useCallback((key: string) => {
    if (!recovery.current.has(key)) recovery.current.set(key, readRecovery(key))
    return recovery.current.get(key)!
  }, [])
  const storeRecoveries = useCallback((key: string, entries: RecoverableDraft[]) => {
    recovery.current.set(key, entries)
    saveRecovery(key, entries)
  }, [])
  useLayoutEffect(() => {
    const previous = previousScope.current
    let recovered = false
    // 首次发送自己创建的会话承接输入。作者主动切换时会经过 restoring，仍按不同作用域隔离。
    for (const submission of submissions.current) {
      if (previous.scope !== scope) {
        if (submission.canAdoptSession && submission.scope === previous.scope && !restoring && !previous.scope.restoring
          && previous.projectId === projectId && previous.sessionId === null && sessionId !== null) {
          const nextDraft = read(submission.key)
          if (nextDraft && !read(requestedKey)) save(requestedKey, nextDraft)
          else if (nextDraft && nextDraft !== read(requestedKey)) {
            storeRecoveries(requestedKey, [...recoveriesFor(requestedKey), { id: crypto.randomUUID(), text: nextDraft }])
            recovered = true
          }
          save(submission.key, '')
          submission.key = requestedKey
          submission.scope = scope
        }
        submission.canAdoptSession = false
      }
      if (submission.status === 'pending') continue
      if (submission.status === 'failed') {
        const current = read(submission.key)
        if (!current) save(submission.key, submission.text)
        else if (current !== submission.text) {
          storeRecoveries(submission.key, [...recoveriesFor(submission.key), { id: crypto.randomUUID(), text: submission.text }])
          recovered = true
        }
      }
      submissions.current.delete(submission)
    }
    previousScope.current = { scope, projectId, sessionId }
    if (recovered) refreshSubmissions((value) => value + 1)
    if (restoring) return
    const saved = read(requestedKey)
    setState((current) => current.key === requestedKey && current.draft === saved ? current : { key: requestedKey, draft: saved })
  }, [projectId, recoveriesFor, requestedKey, restoring, scope, sessionId, storeRecoveries, submissionRevision])
  const update = useCallback((value: SetStateAction<string>) => {
    // 发送失败等异步回调仍握着旧 setter；不能把旧输入写进后来打开的对话。
    if (scope.restoring || currentScope.current !== scope) return
    setState((current) => {
      if (currentScope.current !== scope) return current
      const previous = current.key === scope.key ? current.draft : read(scope.key)
      const next = typeof value === 'function' ? value(previous) : value
      save(scope.key, next)
      return { key: scope.key, draft: next }
    })
  }, [scope])
  // Only an explicit project move calls this; ordinary navigation keeps drafts isolated.
  const captureTransfer = useCallback(() => {
    const captured = !scope.restoring && state.key === scope.key ? state.draft : ''
    return (targetProject: string | null, targetSession: string) => {
      if (!captured) return
      const target = `${targetProject ?? 'loose'}:${targetSession}`
      const existing = read(target)
      const next = !existing || existing === captured ? captured : `${existing}\n\n${captured}`
      save(target, next)
      if (currentScope.current.key === target && !currentScope.current.restoring) setState({ key: target, draft: next })
    }
  }, [scope, state])
  const beginSubmission = useCallback((text: string) => {
    const submission: Submission = { key: scope.key, scope, text, status: 'pending', canAdoptSession: sessionId === null }
    submissions.current.add(submission)
    update((current) => current === text ? '' : current)
    const finish = (status: 'accepted' | 'failed') => {
      submission.status = status
      refreshSubmissions((value) => value + 1)
    }
    return {
      accepted: () => finish('accepted'),
      // 请求已经保存在独立的发送记录里，原文由发送恢复区保管。
      retained: () => finish('accepted'),
      failed: () => finish('failed'),
      isCurrent: () => currentScope.current === submission.scope,
    }
  }, [scope, sessionId, update])
  const dismissRecovery = useCallback((id: string) => {
    if (scope.restoring || currentScope.current !== scope) return
    storeRecoveries(scope.key, recoveriesFor(scope.key).filter((entry) => entry.id !== id))
    refreshSubmissions((value) => value + 1)
  }, [recoveriesFor, scope, storeRecoveries])
  const appendRecovery = useCallback((id: string) => {
    const entry = recoveriesFor(scope.key).find((item) => item.id === id)
    if (!entry || scope.restoring || currentScope.current !== scope) return
    update((current) => current ? `${current}\n\n${entry.text}` : entry.text)
    dismissRecovery(id)
  }, [dismissRecovery, recoveriesFor, scope, update])
  // 恢复新对话期间不短暂展示或修改上一条对话的输入。
  const draft = !restoring && state.key === requestedKey ? state.draft : ''
  return [draft, update, { beginSubmission, captureTransfer, recoveries: restoring ? [] : recoveriesFor(requestedKey), appendRecovery, dismissRecovery }] as const
}
