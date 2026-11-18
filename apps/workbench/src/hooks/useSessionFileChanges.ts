import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { getSessionFileChange, listSessionFileChanges, undoSessionFileChange, type SessionFileChange, type SessionFileChangeDetail } from '../api/fileChanges'
import { authorFacingError } from '../lib/authorFacingError'

export function useSessionFileChanges(sessionId: string | null, refreshKey: string | number = '', onChanged?: () => void, enabled = true) {
  const scope = useMemo(() => ({ sessionId }), [sessionId])
  const current = useRef<typeof scope | null>(scope)
  current.current = scope
  const changed = useRef(onChanged)
  changed.current = onChanged
  const [list, setList] = useState<{ scope: typeof scope; records: SessionFileChange[]; state: 'loading' | 'ready' | 'error'; error: string | null } | null>(null)
  const [selection, setSelection] = useState<{ scope: typeof scope; id: string | null }>({ scope, id: null })
  const selectedId = selection.scope === scope ? selection.id : null
  const selected = useRef(selectedId)
  selected.current = selectedId
  const [loaded, setLoaded] = useState<{ scope: typeof scope; id: string; detail: SessionFileChangeDetail | null; error: string | null; loading: boolean } | null>(null)
  const [actions, setActions] = useState(new Map<string, { scope: typeof scope; id: string; error: string | null; busy: boolean }>())
  const [revision, setRevision] = useState(0)
  const listSequence = useRef(0)
  const detailSequence = useRef(0)
  const inFlight = useRef(new Set<string>())
  const refresh = useCallback(() => setRevision((value) => value + 1), [])
  const select = useCallback((id: string | null) => {
    detailSequence.current++
    setSelection({ scope, id })
    setLoaded(null)
    setActions((value) => {
      if (!sessionId || value.get(sessionId)?.busy || !value.has(sessionId)) return value
      const next = new Map(value)
      next.delete(sessionId)
      return next
    })
  }, [sessionId, scope])

  useEffect(() => {
    current.current = scope
    return () => { if (current.current === scope) current.current = null }
  }, [scope])

  useEffect(() => {
    if (!sessionId || !enabled) return
    const request = ++listSequence.current
    const controller = new AbortController()
    setList((value) => ({ scope, records: value?.scope === scope ? value.records : [], state: 'loading', error: null }))
    void listSessionFileChanges(sessionId, { signal: controller.signal }).then((records) => {
      if (!controller.signal.aborted && current.current === scope && listSequence.current === request) setList({ scope, records, state: 'ready', error: null })
    }).catch((error: unknown) => {
      if (current.current !== scope || request !== listSequence.current || controller.signal.aborted) return
      setList((value) => ({ scope, records: value?.scope === scope ? value.records : [], state: 'error', error: authorFacingError(error) }))
    })
    return () => controller.abort()
  }, [sessionId, scope, revision, refreshKey, enabled])

  useEffect(() => {
    if (!sessionId || !selectedId || !enabled) return
    const request = ++detailSequence.current
    const controller = new AbortController()
    setLoaded((value) => ({ scope, id: selectedId,
      detail: value?.scope === scope && value.id === selectedId ? value.detail : null,
      error: null, loading: true }))
    void getSessionFileChange(sessionId, selectedId, { signal: controller.signal }).then((detail) => {
      if (detail.session_id !== sessionId || detail.change_id !== selectedId) throw new Error('修改详情与当前对话不一致，请重新加载。')
      if (!controller.signal.aborted && current.current === scope && request === detailSequence.current) setLoaded({ scope, id: selectedId, detail, error: null, loading: false })
    }).catch((error: unknown) => {
      if (current.current !== scope || request !== detailSequence.current || controller.signal.aborted) return
      setLoaded({ scope, id: selectedId, detail: null, error: authorFacingError(error), loading: false })
    })
    return () => controller.abort()
  }, [sessionId, selectedId, scope, revision, refreshKey, enabled])

  const undo = useCallback(async (id: string) => {
    if (!sessionId || current.current !== scope || inFlight.current.has(sessionId)) return
    inFlight.current.add(sessionId)
    listSequence.current++
    detailSequence.current++
    setActions((value) => new Map(value).set(sessionId, { scope, id, error: null, busy: true }))
    let actionError: string | null = null
    try {
      const detail = await undoSessionFileChange(sessionId, id)
      if (detail.session_id !== sessionId || detail.change_id !== id) throw new Error('撤回结果与这次操作不一致，请刷新核对。')
      if (current.current === scope && selected.current === id) setLoaded({ scope, id, detail, error: null, loading: false })
    } catch (error) {
      actionError = authorFacingError(error)
    } finally {
      inFlight.current.delete(sessionId)
      const visible = current.current
      setActions((value) => {
        const next = new Map(value)
        next.delete(sessionId)
        if (actionError && visible?.sessionId === sessionId) next.set(sessionId, { scope: visible, id, error: actionError, busy: false })
        return next
      })
      // 超时或部分撤回也重新读真实记录，不能猜成成功或完全未动。
      // 切走又回来时不直接接旧详情，让当前页面和文件视图重新核对。
      if (visible?.sessionId === sessionId) {
        listSequence.current++
        detailSequence.current++
        refresh()
      }
      // 文件可以位于当前打开的另一个项目，刷新当前工作区以核对磁盘。
      if (visible) changed.current?.()
    }
  }, [sessionId, scope, refresh])

  const ownList = list?.scope === scope ? list : null
  const ownDetail = loaded?.scope === scope && loaded.id === selectedId ? loaded : null
  const ownAction = sessionId ? actions.get(sessionId) : null
  return {
    records: ownList?.records ?? [], listState: ownList?.state ?? (sessionId ? 'loading' : 'ready'),
    listError: ownList?.error ?? null, selectedId, detail: ownDetail?.detail ?? null,
    detailError: ownDetail?.error ?? null, detailLoading: Boolean(selectedId && (!ownDetail || ownDetail.loading)),
    busy: ownAction?.busy ?? false, actionError: ownAction?.scope === scope && ownAction.id === selectedId ? ownAction.error : null,
    select, refresh, undo,
  }
}
