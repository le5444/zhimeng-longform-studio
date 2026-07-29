import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import {
  finalizeKnowledgeSourceImport,
  finalizeProjectKnowledgeSourceImport,
  getKnowledgeSourceFormalization,
  getProjectKnowledgeSourceFormalization,
  importKnowledgeCandidates,
  importProjectKnowledgeCandidates,
  listKnowledgeCandidates,
  listProjectKnowledgeCandidates,
  listKnowledgeCards,
  listProjectKnowledgeCards,
  promoteKnowledgeCandidate,
  promoteProjectKnowledgeCandidate,
  rejectKnowledgeCandidate,
  rejectProjectKnowledgeCandidate,
  rollbackKnowledgeCard,
  rollbackProjectKnowledgeCard,
  setKnowledgeCardStatus,
  setProjectKnowledgeCardStatus,
  updateKnowledgeCandidate,
  updateProjectKnowledgeCandidate,
  type KnowledgeCandidate,
  type KnowledgeCandidateList,
  type KnowledgeCard,
  type KnowledgeCardList,
  type KnowledgeScopedCardList,
  type KnowledgeFormalizationRecord,
  type KnowledgeScopeRecord,
} from '../api/knowledge'
import { RuntimeApiError } from '../api/runtime'
import {
  getSourceImportExecutionReceipt,
  type SourceImportExecutionReceiptRecord,
} from '../api/sourceImports'

export type KnowledgeLoadState = 'idle' | 'loading' | 'ready' | 'error'
export type KnowledgeAction =
  | 'import'
  | 'save'
  | 'promote'
  | 'reject'
  | 'finalize'
  | 'state'
  | 'rollback'
  | null

interface KnowledgeRequestError {
  message: string
  status: number | null
}

function knowledgeRequestError(error: unknown): KnowledgeRequestError {
  if (error instanceof RuntimeApiError) {
    const payload = error.payload as {
      error?: { message?: unknown; details?: unknown }
    } | null
    return {
      message: typeof payload?.error?.message === 'string'
        ? payload.error.message
        : error.message,
      status: error.status,
    }
  }
  return {
    message: error instanceof Error ? error.message : '知识库请求失败',
    status: null,
  }
}

export function useKnowledgeReview(
  enabled = true,
  sourceImportId: string | null = null,
  projectId: string | null = null,
) {
  const scopedProjectId = projectId?.trim() || null
  const scopedSourceImportId = sourceImportId?.trim() || null
  const scopeKey = JSON.stringify([scopedProjectId, scopedSourceImportId])
  const [candidates, setCandidates] = useState<KnowledgeCandidate[]>([])
  const [candidateSetHash, setCandidateSetHash] = useState<string | null>(null)
  const [cards, setCards] = useState<KnowledgeCard[]>([])
  const [fingerprint, setFingerprint] = useState<string | null>(null)
  const [scope, setScope] = useState<KnowledgeScopeRecord | null>(null)
  const [includesApplicationScope, setIncludesApplicationScope] = useState(false)
  const [inheritedCardIds, setInheritedCardIds] = useState<string[]>([])
  const [sourceReceipt, setSourceReceipt] = useState<SourceImportExecutionReceiptRecord | null>(null)
  const [formalization, setFormalization] = useState<KnowledgeFormalizationRecord | null>(null)
  const [state, setState] = useState<KnowledgeLoadState>('idle')
  const [action, setAction] = useState<KnowledgeAction>(null)
  const [error, setError] = useState<string | null>(null)
  const [errorStatus, setErrorStatus] = useState<number | null>(null)
  const [selectedId, setSelectedId] = useState<string | null>(null)
  const epoch = useRef(0)
  const activeScopeKey = useRef(scopeKey)
  activeScopeKey.current = scopeKey

  const requestIsCurrent = useCallback((requestEpoch: number, requestScopeKey: string) => (
    epoch.current === requestEpoch && activeScopeKey.current === requestScopeKey
  ), [])

  const selected = useMemo(
    () => candidates.find((item) => item.candidate_id === selectedId) ?? candidates[0] ?? null,
    [candidates, selectedId],
  )

  const storeCandidateList = useCallback((record: KnowledgeCandidateList) => {
    setCandidates(record.candidates)
    setCandidateSetHash(record.candidate_set_hash)
    setSelectedId((current) => (
      current && record.candidates.some((item) => item.candidate_id === current)
        ? current
        : record.candidates[0]?.candidate_id ?? null
    ))
  }, [])

  const storeCardList = useCallback((record: KnowledgeCardList | KnowledgeScopedCardList) => {
    setCards(record.cards)
    setFingerprint(record.fingerprint)
    if (record.schema === 'zhimeng.knowledge-scoped-card-list.v1') {
      setScope(record.scope)
      setIncludesApplicationScope(record.includes_application_scope)
      setInheritedCardIds(record.inherited_card_ids)
    } else {
      setScope(null)
      setIncludesApplicationScope(false)
      setInheritedCardIds([])
    }
  }, [])

  const listCandidates = useCallback((sourceId?: string | null) => (
    scopedProjectId
      ? listProjectKnowledgeCandidates(scopedProjectId, sourceId)
      : listKnowledgeCandidates(sourceId)
  ), [scopedProjectId])
  const importCandidates = useCallback((sourceId: string) => (
    scopedProjectId
      ? importProjectKnowledgeCandidates(scopedProjectId, sourceId)
      : importKnowledgeCandidates(sourceId)
  ), [scopedProjectId])
  const listCards = useCallback(() => (
    scopedProjectId
      ? listProjectKnowledgeCards(scopedProjectId)
      : listKnowledgeCards()
  ), [scopedProjectId])
  const getFormalization = useCallback((sourceId: string) => (
    scopedProjectId
      ? getProjectKnowledgeSourceFormalization(scopedProjectId, sourceId)
      : getKnowledgeSourceFormalization(sourceId)
  ), [scopedProjectId])
  const updateCandidate = useCallback((
    candidate: Pick<KnowledgeCandidate, 'candidate_id' | 'revision' | 'content_hash'>,
    card: Record<string, unknown>,
  ) => (
    scopedProjectId
      ? updateProjectKnowledgeCandidate(scopedProjectId, candidate, card)
      : updateKnowledgeCandidate(candidate, card)
  ), [scopedProjectId])
  const promoteCandidate = useCallback((
    candidate: Pick<KnowledgeCandidate, 'candidate_id' | 'revision' | 'content_hash'>,
    replaceActive: boolean,
  ) => (
    scopedProjectId
      ? promoteProjectKnowledgeCandidate(scopedProjectId, candidate, replaceActive)
      : promoteKnowledgeCandidate(candidate, replaceActive)
  ), [scopedProjectId])
  const rejectCandidate = useCallback((
    candidate: Pick<KnowledgeCandidate, 'candidate_id' | 'revision' | 'content_hash'>,
  ) => (
    scopedProjectId
      ? rejectProjectKnowledgeCandidate(scopedProjectId, candidate)
      : rejectKnowledgeCandidate(candidate)
  ), [scopedProjectId])
  const finalizeSource = useCallback((
    sourceId: string,
    request: Parameters<typeof finalizeKnowledgeSourceImport>[1],
  ) => (
    scopedProjectId
      ? finalizeProjectKnowledgeSourceImport(scopedProjectId, sourceId, request)
      : finalizeKnowledgeSourceImport(sourceId, request)
  ), [scopedProjectId])
  const setCardStatus = useCallback((
    card: Pick<KnowledgeCard, 'card_id' | 'state_revision' | 'active_version_id'>,
    status: 'active' | 'disabled',
  ) => (
    scopedProjectId
      ? setProjectKnowledgeCardStatus(scopedProjectId, card, status)
      : setKnowledgeCardStatus(card, status)
  ), [scopedProjectId])
  const rollbackCard = useCallback((
    card: Pick<KnowledgeCard, 'card_id' | 'state_revision' | 'active_version_id'>,
    versionId: string,
  ) => (
    scopedProjectId
      ? rollbackProjectKnowledgeCard(scopedProjectId, card, versionId)
      : rollbackKnowledgeCard(card, versionId)
  ), [scopedProjectId])

  const refresh = useCallback(async () => {
    if (!enabled) return
    const currentEpoch = ++epoch.current
    const requestScopeKey = scopeKey
    setState('loading')
    setError(null)
    setErrorStatus(null)
    try {
      let nextCandidateList = await listCandidates(scopedSourceImportId)
      if (!requestIsCurrent(currentEpoch, requestScopeKey)) return
      if (scopedSourceImportId && nextCandidateList.candidates.length === 0) {
        setAction('import')
        nextCandidateList = await importCandidates(scopedSourceImportId)
        if (!requestIsCurrent(currentEpoch, requestScopeKey)) return
      }
      const [nextCards, nextSourceReceipt, nextFormalization] = await Promise.all([
        listCards(),
        scopedSourceImportId
          ? getSourceImportExecutionReceipt(scopedSourceImportId)
          : Promise.resolve(null),
        scopedSourceImportId
          ? getFormalization(scopedSourceImportId)
          : Promise.resolve(null),
      ])
      if (!requestIsCurrent(currentEpoch, requestScopeKey)) return
      storeCandidateList(nextCandidateList)
      storeCardList(nextCards)
      setSourceReceipt(nextSourceReceipt)
      setFormalization(nextFormalization)
      setState('ready')
    } catch (nextError) {
      if (!requestIsCurrent(currentEpoch, requestScopeKey)) return
      const requestError = knowledgeRequestError(nextError)
      setError(requestError.message)
      setErrorStatus(requestError.status)
      setState('error')
    } finally {
      if (requestIsCurrent(currentEpoch, requestScopeKey)) setAction(null)
    }
  }, [
    enabled,
    getFormalization,
    importCandidates,
    listCandidates,
    listCards,
    requestIsCurrent,
    scopeKey,
    scopedSourceImportId,
    storeCandidateList,
    storeCardList,
  ])

  useEffect(() => {
    epoch.current += 1
    setCandidates([])
    setCandidateSetHash(null)
    setCards([])
    setFingerprint(null)
    setScope(null)
    setIncludesApplicationScope(false)
    setInheritedCardIds([])
    setSourceReceipt(null)
    setFormalization(null)
    setState('idle')
    setAction(null)
    setError(null)
    setErrorStatus(null)
    setSelectedId(null)
  }, [scopedProjectId, scopedSourceImportId])

  useEffect(() => {
    if (!enabled) {
      epoch.current += 1
      setCandidates([])
      setCandidateSetHash(null)
      setCards([])
      setFingerprint(null)
      setScope(null)
      setIncludesApplicationScope(false)
      setInheritedCardIds([])
      setSourceReceipt(null)
      setFormalization(null)
      setState('idle')
      setAction(null)
      setError(null)
      setErrorStatus(null)
      setSelectedId(null)
      return
    }
    void refresh()
    return () => {
      epoch.current += 1
    }
  }, [enabled, refresh])

  const save = useCallback(async (
    candidate: KnowledgeCandidate,
    card: Record<string, unknown>,
  ) => {
    if (action) return null
    const currentEpoch = epoch.current
    const requestScopeKey = scopeKey
    setAction('save')
    setError(null)
    setErrorStatus(null)
    try {
      const updated = await updateCandidate(candidate, card)
      if (!requestIsCurrent(currentEpoch, requestScopeKey)) return updated
      const nextCandidateList = await listCandidates(scopedSourceImportId)
      if (requestIsCurrent(currentEpoch, requestScopeKey)) {
        storeCandidateList(nextCandidateList)
      }
      return updated
    } catch (nextError) {
      if (requestIsCurrent(currentEpoch, requestScopeKey)) {
        const requestError = knowledgeRequestError(nextError)
        setError(requestError.message)
        setErrorStatus(requestError.status)
      }
      throw nextError
    } finally {
      if (requestIsCurrent(currentEpoch, requestScopeKey)) setAction(null)
    }
  }, [
    action,
    listCandidates,
    requestIsCurrent,
    scopeKey,
    scopedSourceImportId,
    storeCandidateList,
    updateCandidate,
  ])

  const promote = useCallback(async (
    candidate: KnowledgeCandidate,
    replaceActive = false,
  ) => {
    if (action) return null
    const currentEpoch = epoch.current
    const requestScopeKey = scopeKey
    setAction('promote')
    setError(null)
    setErrorStatus(null)
    try {
      const promoted = await promoteCandidate(candidate, replaceActive)
      if (!requestIsCurrent(currentEpoch, requestScopeKey)) return promoted
      const [nextCandidateList, nextCards] = await Promise.all([
        listCandidates(scopedSourceImportId),
        listCards(),
      ])
      if (requestIsCurrent(currentEpoch, requestScopeKey)) {
        storeCandidateList(nextCandidateList)
        storeCardList(nextCards)
      }
      return promoted
    } catch (nextError) {
      if (requestIsCurrent(currentEpoch, requestScopeKey)) {
        const requestError = knowledgeRequestError(nextError)
        setError(requestError.message)
        setErrorStatus(requestError.status)
      }
      throw nextError
    } finally {
      if (requestIsCurrent(currentEpoch, requestScopeKey)) setAction(null)
    }
  }, [
    action,
    listCandidates,
    listCards,
    promoteCandidate,
    requestIsCurrent,
    scopeKey,
    scopedSourceImportId,
    storeCandidateList,
    storeCardList,
  ])

  const reject = useCallback(async (candidate: KnowledgeCandidate) => {
    if (action) return null
    const currentEpoch = epoch.current
    const requestScopeKey = scopeKey
    setAction('reject')
    setError(null)
    setErrorStatus(null)
    try {
      const rejected = await rejectCandidate(candidate)
      if (!requestIsCurrent(currentEpoch, requestScopeKey)) return rejected
      const [nextCandidateList, nextCards] = await Promise.all([
        listCandidates(scopedSourceImportId),
        listCards(),
      ])
      if (requestIsCurrent(currentEpoch, requestScopeKey)) {
        storeCandidateList(nextCandidateList)
        storeCardList(nextCards)
      }
      return rejected
    } catch (nextError) {
      if (requestIsCurrent(currentEpoch, requestScopeKey)) {
        const requestError = knowledgeRequestError(nextError)
        setError(requestError.message)
        setErrorStatus(requestError.status)
      }
      throw nextError
    } finally {
      if (requestIsCurrent(currentEpoch, requestScopeKey)) setAction(null)
    }
  }, [
    action,
    listCandidates,
    listCards,
    rejectCandidate,
    requestIsCurrent,
    scopeKey,
    scopedSourceImportId,
    storeCandidateList,
    storeCardList,
  ])

  const finalize = useCallback(async () => {
    if (action) return null
    const currentEpoch = epoch.current
    const requestScopeKey = scopeKey
    const receipt = sourceReceipt?.receipt
    if (!scopedSourceImportId || !candidateSetHash || !receipt) {
      throw new Error('当前批次缺少可验证的来源收据或候选集合 hash')
    }
    setAction('finalize')
    setError(null)
    setErrorStatus(null)
    try {
      const candidateDraftHash = candidates[0]?.source_draft_hash ?? receipt.cards_draft_hash
      const finalized = await finalizeSource(scopedSourceImportId, {
        expected_source_receipt_hash: receipt.receipt_hash,
        expected_draft_hash: candidateDraftHash,
        expected_candidate_set_hash: candidateSetHash,
      })
      if (!requestIsCurrent(currentEpoch, requestScopeKey)) return finalized
      const [nextCandidateList, nextCards, nextFormalization] = await Promise.all([
        listCandidates(scopedSourceImportId),
        listCards(),
        getFormalization(scopedSourceImportId),
      ])
      if (requestIsCurrent(currentEpoch, requestScopeKey)) {
        storeCandidateList(nextCandidateList)
        storeCardList(nextCards)
        setFormalization(nextFormalization)
      }
      return finalized
    } catch (nextError) {
      if (requestIsCurrent(currentEpoch, requestScopeKey)) {
        const requestError = knowledgeRequestError(nextError)
        setError(requestError.message)
        setErrorStatus(requestError.status)
      }
      throw nextError
    } finally {
      if (requestIsCurrent(currentEpoch, requestScopeKey)) setAction(null)
    }
  }, [
    action,
    candidateSetHash,
    candidates,
    finalizeSource,
    getFormalization,
    listCandidates,
    listCards,
    requestIsCurrent,
    scopeKey,
    scopedSourceImportId,
    sourceReceipt,
    storeCandidateList,
    storeCardList,
  ])

  const setCardEnabled = useCallback(async (card: KnowledgeCard, enabledState: boolean) => {
    if (action) return
    const currentEpoch = epoch.current
    const requestScopeKey = scopeKey
    setAction('state')
    setError(null)
    setErrorStatus(null)
    try {
      await setCardStatus(card, enabledState ? 'active' : 'disabled')
      if (!requestIsCurrent(currentEpoch, requestScopeKey)) return
      const nextCards = await listCards()
      if (requestIsCurrent(currentEpoch, requestScopeKey)) storeCardList(nextCards)
    } catch (nextError) {
      if (requestIsCurrent(currentEpoch, requestScopeKey)) {
        const requestError = knowledgeRequestError(nextError)
        setError(requestError.message)
        setErrorStatus(requestError.status)
      }
      throw nextError
    } finally {
      if (requestIsCurrent(currentEpoch, requestScopeKey)) setAction(null)
    }
  }, [action, listCards, requestIsCurrent, scopeKey, setCardStatus, storeCardList])

  const rollback = useCallback(async (card: KnowledgeCard, versionId: string) => {
    if (action) return
    const currentEpoch = epoch.current
    const requestScopeKey = scopeKey
    setAction('rollback')
    setError(null)
    setErrorStatus(null)
    try {
      await rollbackCard(card, versionId)
      if (!requestIsCurrent(currentEpoch, requestScopeKey)) return
      const nextCards = await listCards()
      if (requestIsCurrent(currentEpoch, requestScopeKey)) storeCardList(nextCards)
    } catch (nextError) {
      if (requestIsCurrent(currentEpoch, requestScopeKey)) {
        const requestError = knowledgeRequestError(nextError)
        setError(requestError.message)
        setErrorStatus(requestError.status)
      }
      throw nextError
    } finally {
      if (requestIsCurrent(currentEpoch, requestScopeKey)) setAction(null)
    }
  }, [action, listCards, requestIsCurrent, rollbackCard, scopeKey, storeCardList])

  const scopeLabel = useMemo(() => {
    if (!scopedProjectId) return '应用知识范围'
    return includesApplicationScope
      ? '项目知识范围（含应用基础）'
      : '项目知识范围'
  }, [includesApplicationScope, scopedProjectId])

  return {
    candidates,
    candidateSetHash,
    cards,
    fingerprint,
    scope,
    scopeLabel,
    scopeFingerprint: fingerprint,
    includesApplicationScope,
    inheritedCardIds,
    sourceReceipt,
    formalization,
    selected,
    selectedId,
    select: setSelectedId,
    state,
    action,
    error,
    errorStatus,
    refresh,
    save,
    promote,
    reject,
    finalize,
    setCardEnabled,
    rollback,
  }
}
