import { useCallback, useEffect, useLayoutEffect, useMemo, useRef, useState } from 'react'
import type { AgentImageAttachment } from '../../api/agent'
import { isImageFile, uploadAgentImage } from '../../api/agentImages'
import { authorFacingError } from '../../lib/authorFacingError'
import { IMAGE_DRAFT_PREFIX as PREFIX, subscribeImageDraftClaims } from '../../hooks/agentSendReceipt'

export interface DraftImage {
  clientId: string
  name: string
  status: 'uploading' | 'ready' | 'failed'
  image?: AgentImageAttachment
  preview?: string
  error?: string
}
interface Entry extends DraftImage { file?: File }
interface Scope { key: string; restoring: boolean }
interface Submission { key: string; scope: Scope; ids: string[]; canAdopt: boolean; status: 'pending' | 'accepted' | 'failed' }
function read(key: string): Entry[] {
  try {
    const stored = JSON.parse(localStorage.getItem(PREFIX + key) ?? '[]') as Entry[]
    if (!Array.isArray(stored)) return []
    return stored.filter((item) => typeof item?.clientId === 'string' && typeof item?.name === 'string').map((item) => ({
      clientId: item.clientId, name: item.name, image: item.image,
      status: item.image?.image_id ? 'ready' : 'failed',
      error: item.image?.image_id ? undefined : '这张图片尚未上传完成，请移除后重新添加。',
    }))
  } catch { return [] }
}
function persist(key: string, entries: Entry[]) {
  try {
    if (!entries.length) localStorage.removeItem(PREFIX + key)
    else localStorage.setItem(PREFIX + key, JSON.stringify(entries.map(({ clientId, name, status, image, error }) => ({ clientId, name, status, image, error }))))
  } catch { /* 本页附件仍保留；不把二进制或 data URL 塞进浏览器存储。 */ }
}

export function useConversationImages(projectId: string | null, sessionId: string | null, restoring: boolean) {
  const key = JSON.stringify([projectId, sessionId])
  const scope = useMemo<Scope>(() => ({ key, restoring }), [key, restoring])
  const current = useRef(scope); current.current = scope
  const cache = useRef(new Map<string, Entry[]>())
  const inflight = useRef(new Set<string>())
  const submissions = useRef(new Set<Submission>())
  const previous = useRef({ scope, projectId, sessionId })
  const mounted = useRef(true)
  const [revision, refresh] = useState(0)
  const repaint = useCallback(() => { if (mounted.current) refresh((value) => value + 1) }, [])
  const get = useCallback((target: string) => {
    if (!cache.current.has(target)) cache.current.set(target, read(target))
    return cache.current.get(target)!
  }, [])
  const put = useCallback((target: string, entries: Entry[]) => { cache.current.set(target, entries); persist(target, entries) }, [])
  useEffect(() => subscribeImageDraftClaims((claim) => {
    for (const key of claim.keys) {
      const entries = get(key)
      const belongsToReceipt = (entry: Entry) => claim.clientIds.includes(entry.clientId) && !!entry.image && claim.imageIds.includes(entry.image.image_id)
      for (const entry of entries) if (belongsToReceipt(entry) && entry.preview) URL.revokeObjectURL(entry.preview)
      put(key, entries.filter((entry) => !belongsToReceipt(entry)))
    }
    repaint()
  }), [get, put, repaint])
  useEffect(() => {
    mounted.current = true
    return () => { mounted.current = false; for (const entries of cache.current.values()) for (const entry of entries) if (entry.preview) URL.revokeObjectURL(entry.preview) }
  }, [])
  useLayoutEffect(() => {
    const before = previous.current
    let changed = false
    for (const submission of submissions.current) {
      if (before.scope !== scope) {
        if (submission.canAdopt && submission.scope === before.scope && !restoring && !before.scope.restoring
          && before.projectId === projectId && before.sessionId === null && sessionId !== null) {
          put(key, [...get(key), ...get(submission.key)])
          put(submission.key, [])
          submission.key = key; submission.scope = scope; changed = true
        }
        submission.canAdopt = false
      }
      if (submission.status === 'pending') continue
      if (submission.status === 'accepted') {
        const entries = get(submission.key)
        for (const entry of entries) if (submission.ids.includes(entry.clientId) && entry.preview) URL.revokeObjectURL(entry.preview)
        put(submission.key, entries.filter((entry) => !submission.ids.includes(entry.clientId)))
        changed = true
      }
      submissions.current.delete(submission)
    }
    previous.current = { scope, projectId, sessionId }
    if (changed) repaint()
  }, [get, key, projectId, put, repaint, restoring, revision, scope, sessionId])

  const upload = useCallback(async (entry: Entry) => {
    if (inflight.current.has(entry.clientId)) return
    if (!entry.file) { entry.error = '请移除后重新添加这张图片。'; entry.status = 'failed'; repaint(); return }
    inflight.current.add(entry.clientId)
    entry.status = 'uploading'; entry.error = undefined
    const save = () => {
      for (const [target, entries] of cache.current) if (entries.some((item) => item.clientId === entry.clientId)) put(target, entries)
      repaint()
    }
    save()
    try { entry.image = await uploadAgentImage(entry.file); entry.status = 'ready' }
    catch (error) { entry.status = 'failed'; entry.error = authorFacingError(error, '图片上传失败') }
    finally { inflight.current.delete(entry.clientId); save() }
  }, [put, repaint])
  const add = useCallback((files: File[]) => {
    if (scope.restoring || current.current !== scope) return
    const entries = files.map((file): Entry => ({ clientId: crypto.randomUUID(), name: file.name || '粘贴的图片', file,
      preview: isImageFile(file) ? URL.createObjectURL(file) : undefined, status: isImageFile(file) ? 'uploading' : 'failed',
      error: isImageFile(file) ? undefined : '请选择图片文件。' }))
    put(scope.key, [...get(scope.key), ...entries]); repaint()
    for (const entry of entries) if (entry.status === 'uploading') void upload(entry)
  }, [get, put, repaint, scope, upload])
  const remove = useCallback((clientId: string) => {
    if (scope.restoring || current.current !== scope) return
    const entries = get(scope.key)
    const item = entries.find((entry) => entry.clientId === clientId)
    if (item?.preview) URL.revokeObjectURL(item.preview)
    put(scope.key, entries.filter((entry) => entry.clientId !== clientId)); repaint()
  }, [get, put, repaint, scope])
  const retry = useCallback((clientId: string) => {
    if (scope.restoring || current.current !== scope) return
    const item = get(scope.key).find((entry) => entry.clientId === clientId)
    if (item) void upload(item)
  }, [get, scope, upload])
  const restore = useCallback((images: AgentImageAttachment[]) => {
    if (scope.restoring || current.current !== scope) return
    const existing = get(scope.key)
    const ids = new Set(existing.map((entry) => entry.image?.image_id))
    put(scope.key, [...existing, ...images.filter((image) => !ids.has(image.image_id)).map((image): Entry => ({ clientId: crypto.randomUUID(), name: image.name, status: 'ready', image }))])
    repaint()
  }, [get, put, repaint, scope])
  const captureSubmission = useCallback(() => {
    const entries = scope.restoring || current.current !== scope ? [] : [...get(scope.key)]
    if (entries.some((entry) => entry.status !== 'ready' || !entry.image)) throw new Error('图片尚未上传完成，请重试失败的图片或移除后再发送。')
    const submission: Submission = { key: scope.key, scope, ids: entries.map((entry) => entry.clientId), canAdopt: sessionId === null, status: 'pending' }
    submissions.current.add(submission)
    const finish = (status: Submission['status']) => { submission.status = status; repaint() }
    return { imageIds: entries.map((entry) => entry.image!.image_id), images: entries.map((entry) => entry.image!),
      draftIdentity: { projectId, sessionId, clientIds: submission.ids },
      accepted: () => finish('accepted'), retained: () => finish('accepted'), failed: () => finish('failed') }
  }, [get, projectId, repaint, scope, sessionId])
  const captureTransfer = useCallback(() => {
    const captured = scope.restoring ? [] : [...get(scope.key)]
    return (targetProject: string | null, targetSession: string) => {
      const target = JSON.stringify([targetProject, targetSession])
      if (target === scope.key) return
      const existing = get(target)
      const ids = new Set(existing.flatMap(entry => entry.image ? [entry.image.image_id] : []))
      const copies = captured.filter(entry => !entry.image || !ids.has(entry.image.image_id)).map((entry): Entry => ({
        ...entry, clientId: crypto.randomUUID(), preview: entry.file && isImageFile(entry.file) ? URL.createObjectURL(entry.file) : undefined,
      }))
      put(target, [...existing, ...copies]); repaint()
      for (const entry of copies) if (entry.status === 'uploading') void upload(entry)
    }
  }, [get, put, repaint, scope, upload])
  const images = restoring ? [] : get(key)
  return { images: images as DraftImage[], add, remove, retry, restore, captureSubmission, captureTransfer, blocked: images.some((image) => image.status !== 'ready') }
}
