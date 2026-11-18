/**
 * 对话流里的「修改卡」：Agent 整理出一份修改后，卡片挂在那一轮回答下面，
 * 保存方式由设置决定，保存后能撤回。本机只缓存展示，真实状态以后台记录为准。
 */
import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { isTerminalAgentRun, type AgentActivity, type AgentRun } from '../../api/agent'
import { applyChangeSetDirect, discardChangeSet, getChangeSet, listProjectChangeSets, rollbackChangeSet, type ChangeSetStatus } from '../../api/changeSets'
import { RuntimeApiError } from '../../api/runtime'
import { authorFacingError } from '../../lib/authorFacingError'
import { readApplyPolicy } from '../../lib/applyPolicy'
import { agentRunChangeSetId } from '../../lib/candidateChangeSet'
import { T } from '../strings'
import { agentFileEffects } from '../lib/agentFileEffects'

export type ChangeCardStatus = 'pending' | 'busy' | 'applied' | 'rolled_back' | 'discarded' | 'attention' | 'error'

export interface ChangeCard {
  changeSetId: string
  runId: string
  status: ChangeCardStatus
  message?: string | null
  resultRevision?: number | null
}

const STORAGE_PREFIX = 'zhimeng.desk.change-cards:'
const STATUSES: readonly ChangeCardStatus[] = ['pending', 'busy', 'applied', 'rolled_back', 'discarded', 'attention', 'error']

function readStored(sessionId: string): ChangeCard[] {
  try {
    const raw = window.localStorage.getItem(`${STORAGE_PREFIX}${sessionId}`)
    if (!raw) return []
    const parsed = JSON.parse(raw) as unknown
    if (!Array.isArray(parsed)) return []
    return parsed
      .filter((item): item is ChangeCard => (
        typeof item === 'object' && item !== null
        && typeof (item as ChangeCard).changeSetId === 'string'
        && typeof (item as ChangeCard).runId === 'string'
        && STATUSES.includes((item as ChangeCard).status)
      ))
      // 刷新时还在「正在处理」的卡，回到待确认，让作者再点一次。
      .map((item) => (item.status === 'busy' ? { ...item, status: 'pending' as const } : item))
  } catch {
    return []
  }
}

function writeStored(sessionId: string, cards: ChangeCard[]) {
  try {
    window.localStorage.setItem(`${STORAGE_PREFIX}${sessionId}`, JSON.stringify(cards))
  } catch {
    // 存不了只影响刷新后还能不能看到旧卡
  }
}

function errorCode(error: unknown): string | null {
  if (!(error instanceof RuntimeApiError)) return null
  const payload = error.payload as { error?: { code?: unknown } } | null
  return typeof payload?.error?.code === 'string' ? payload.error.code : null
}

export interface UseChangeCardsOptions {
  sessionId: string | null
  projectId?: string | null
  runIds?: string[]
  /** 右栏记录变化后重新核对服务端状态，不能继续展示本机旧卡。 */
  refreshKey?: string
  autoApply?: boolean
  activeRun: AgentRun | null
  activities: AgentActivity[]
  /** 书的内容变了（保存 / 撤回之后），让书目录和修改记录刷新。 */
  onBookChanged: () => void
}

function cardStatus(status: ChangeSetStatus): ChangeCardStatus {
  if (status === 'applied' || status === 'rolled_back' || status === 'discarded') return status
  if (status === 'applying' || status === 'rolling_back') return 'busy'
  if (status === 'failed' || status === 'conflicted') return 'error'
  return 'pending'
}

export function useChangeCards({ sessionId, projectId, runIds, refreshKey, autoApply, activeRun, activities, onBookChanged }: UseChangeCardsOptions) {
  // 对话 A → B → A 也必须换令牌，不能只比较字符串 ID 放行 A 的旧请求。
  const token = useMemo(() => ({ sessionId, projectId }), [sessionId, projectId])
  const scope = useRef<typeof token | null>(token)
  scope.current = token
  const [state, setState] = useState<{ token: typeof token | null; cards: ChangeCard[] }>({ token: null, cards: [] })
  const cards = useMemo(() => state.token === token ? state.cards : [], [state, token])
  const setCards = useCallback((next: ChangeCard[] | ((current: ChangeCard[]) => ChangeCard[])) => {
    if (scope.current !== token) return
    setState((current) => scope.current !== token ? current : ({ token, cards: typeof next === 'function'
      ? next(current.token === token ? current.cards : []) : next }))
  }, [token])
  const ownRun = activeRun?.session_id === sessionId ? activeRun : null
  const ownActivities = useMemo(() => activities.filter((activity) => activity.session_id === sessionId), [activities, sessionId])
  const observedRunning = useRef(new Set<string>())
  const handled = useRef(new Set<string>())
  const inFlight = useRef(new Set<string>())
  const seenWrites = useRef(new Set<string>())
  const autoEligible = useRef(new Set<string>())
  const readSequence = useRef(0)
  const latestRead = useRef(new Map<string, number>())
  const changed = useCallback(() => { if (scope.current === token) onBookChanged() }, [onBookChanged, token])
  const currentBookChanged = useRef(onBookChanged)
  currentBookChanged.current = onBookChanged
  const [refreshVersion, setRefreshVersion] = useState(0)
  const finish = useCallback((changeSetId: string) => {
    latestRead.current.set(changeSetId, ++readSequence.current)
    inFlight.current.delete(changeSetId)
    const visible = scope.current
    if (visible && visible !== token && visible.sessionId === token.sessionId && visible.projectId === token.projectId) {
      // A 的写入已发出后切走又回来：旧响应不直接入卡，完成后让当前 A 重新读真状态。
      setRefreshVersion((version) => version + 1)
      currentBookChanged.current()
    }
  }, [token])

  // 切对话：换成这个对话自己的卡。
  useEffect(() => {
    scope.current = token
    const next = sessionId ? readStored(sessionId) : []
    setCards(next)
    handled.current = new Set(next.map((card) => card.changeSetId))
    observedRunning.current.clear()
    seenWrites.current.clear()
    autoEligible.current.clear()
    return () => { if (scope.current === token) scope.current = null }
  }, [sessionId, setCards, token])

  useEffect(() => {
    if (sessionId && state.token === token) writeStored(sessionId, state.cards)
  }, [state, sessionId, token])

  // 本机缓存不拥有保存/撤回状态；即使历史消息尚未加载，也先逐张核对。
  useEffect(() => {
    if (!sessionId) return
    for (const card of readStored(sessionId)) {
      const sequence = ++readSequence.current
      latestRead.current.set(card.changeSetId, sequence)
      void getChangeSet(card.changeSetId).then((record) => {
        if (scope.current !== token || inFlight.current.has(card.changeSetId)
          || sequence < (latestRead.current.get(card.changeSetId) ?? 0)) return
        setCards((current) => current.map((item) => item.changeSetId === card.changeSetId
          ? { ...item, status: cardStatus(record.status), message: null,
            resultRevision: record.rollback?.result_revision ?? record.writeback?.result_revision ?? null } : item))
      }).catch(() => { /* 断线保留展示；任何写操作前还会再次向后台核实。 */ })
    }
  }, [sessionId, token, setCards, refreshVersion, refreshKey])

  // 回到老对话时从真实修改记录补全，清掉浏览器缓存也能看到保存和撤回的状态。
  const runKey = [...new Set([...(runIds ?? []), ...(ownRun ? [ownRun.run_id] : [])])].sort().join('|')
  useEffect(() => {
    if (!projectId || !sessionId || !runKey) return
    let cancelled = false
    const ownRuns = new Set(runKey.split('|'))
    // 旧壳/轻量测试替身可能没有列表接口；实时 Agent 仍会走真实接口，
    // 这里缺少时保留当前卡片，不阻断对话主链。
    if (typeof listProjectChangeSets !== 'function') return
    const sequence = ++readSequence.current
    void listProjectChangeSets(projectId, { timeoutMs: 30_000 }).then((records) => {
      if (cancelled || scope.current !== token) return
      const restored = records.filter((record) => record.source_run_id && ownRuns.has(record.source_run_id)).map((record): ChangeCard => ({
        changeSetId: record.change_set_id, runId: record.source_run_id!, status: cardStatus(record.status),
        resultRevision: null,
      }))
      setCards((current) => {
        const merged = new Map(current.map((card) => [card.changeSetId, card]))
        for (const card of restored) {
          // 正在本页保存或撤回时不让稍早的列表响应盖掉操作状态。
          if (!inFlight.current.has(card.changeSetId) && sequence >= (latestRead.current.get(card.changeSetId) ?? 0)) {
            latestRead.current.set(card.changeSetId, sequence)
            const previous = merged.get(card.changeSetId)
            merged.set(card.changeSetId, { ...card,
              resultRevision: previous?.status === card.status ? previous.resultRevision : null })
          }
        }
        return [...merged.values()]
      })
    }).catch(() => { /* 暂时连不上保留已有记录，动作时仍会向服务核实。 */ })
    return () => { cancelled = true }
  }, [projectId, sessionId, runKey, ownRun?.status, setCards, token, refreshVersion, refreshKey])

  // 直接写工具每次成功就显示结果，即使整轮尚未完成或随后失败也不丢。
  useEffect(() => {
    if (!sessionId) return
    const effects = agentFileEffects(ownActivities, ownRun?.run_id ?? null)
    let refreshNeeded = false
    for (const effect of effects.refreshes) {
      const key = `${sessionId}:refresh:${effect.key}`
      if (seenWrites.current.has(key)) continue
      seenWrites.current.add(key)
      refreshNeeded = true
    }
    if (ownRun && isTerminalAgentRun(ownRun.status)
      && (effects.commandRan || (ownRun.status !== 'succeeded' && effects.writeAttempted))) {
      const key = `${sessionId}:finished-files:${ownRun.run_id}`
      if (!seenWrites.current.has(key)) {
        seenWrites.current.add(key)
        // 停止或失败也可能已有磁盘改动；结束时再核对，包含没带新版本的取消结果。
        refreshNeeded = true
      }
    }
    if (refreshNeeded) changed()
    for (const write of effects.writes) {
      const key = `${sessionId}:${write.changeSetId}`
      if (seenWrites.current.has(key)) continue
      seenWrites.current.add(key)
      handled.current.add(write.changeSetId)
      setCards((current) => current.some((card) => card.changeSetId === write.changeSetId) ? current : [...current, {
        changeSetId: write.changeSetId, runId: write.runId, status: 'applied', resultRevision: write.revision,
      }])
      const sequence = ++readSequence.current
      latestRead.current.set(write.changeSetId, sequence)
      void getChangeSet(write.changeSetId).then((record) => {
        if (scope.current !== token || inFlight.current.has(write.changeSetId)
          || sequence < (latestRead.current.get(write.changeSetId) ?? 0)) return
        setCards((current) => current.map((card) => card.changeSetId === write.changeSetId
          ? { ...card, status: cardStatus(record.status), resultRevision: record.writeback?.result_revision ?? write.revision } : card))
      }).catch(() => { /* 已收到成功写入结果，详情可在卡片中重试。 */ })
    }
    // Agent 在对话里撤回了某次修改：那张卡跟着变成「已撤回」，书重新读一遍。
    for (const undo of effects.undone) {
      const key = `${sessionId}:undo:${undo.changeSetId}`
      if (seenWrites.current.has(key)) continue
      seenWrites.current.add(key)
      const sequence = ++readSequence.current
      latestRead.current.set(undo.changeSetId, sequence)
      void getChangeSet(undo.changeSetId).then((record) => {
        if (scope.current !== token || inFlight.current.has(undo.changeSetId)
          || sequence < (latestRead.current.get(undo.changeSetId) ?? 0)) return
        setCards((current) => current.map((card) => card.changeSetId === undo.changeSetId
          ? { ...card, status: cardStatus(record.status), message: null,
            resultRevision: record.rollback?.result_revision ?? null } : card))
      }).catch(() => { /* 卡片下次核对时会更新。 */ })
    }
  }, [ownActivities, ownRun, sessionId, setCards, changed, token])

  useEffect(() => {
    if (ownRun && !isTerminalAgentRun(ownRun.status)) observedRunning.current.add(ownRun.run_id)
  }, [ownRun])

  // 这一轮跑完并且整理出了一份修改：挂一张待确认的卡。
  useEffect(() => {
    if (!ownRun || ownRun.status !== 'succeeded') return
    if (!observedRunning.current.has(ownRun.run_id)) return
    const changeSetId = agentRunChangeSetId(ownRun.run_id, ownActivities)
    if (!changeSetId || handled.current.has(changeSetId)) return
    handled.current.add(changeSetId)
    autoEligible.current.add(changeSetId)
    setCards((current) => current.some((card) => card.changeSetId === changeSetId) ? current
      : [...current, { changeSetId, runId: ownRun.run_id, status: 'pending' }])
  }, [ownRun, ownActivities, setCards])

  const update = useCallback((changeSetId: string, patch: Partial<ChangeCard>) => {
    setCards((current) => current.map((card) => (card.changeSetId === changeSetId ? { ...card, ...patch } : card)))
  }, [setCards])

  const approve = useCallback(async (changeSetId: string) => {
    if (scope.current !== token || inFlight.current.has(changeSetId)) return
    inFlight.current.add(changeSetId)
    autoEligible.current.delete(changeSetId)
    latestRead.current.set(changeSetId, ++readSequence.current)
    update(changeSetId, { status: 'busy', message: null })
    try {
      const current = await getChangeSet(changeSetId)
      if (scope.current !== token) return
      if (current.status === 'applied') {
        update(changeSetId, { status: 'applied', resultRevision: current.writeback?.result_revision ?? null })
        changed()
        return
      }
      // 缓存里的待保存可能已经在别处被撤回、放弃或处理过，不能借查看旧卡再次写入。
      if (current.status !== 'reviewing' && current.status !== 'ready') {
        update(changeSetId, { status: cardStatus(current.status), message: null })
        return
      }
      const applied = await applyChangeSetDirect(changeSetId, current.review_revision)
      update(changeSetId, {
        status: 'applied',
        resultRevision: applied.writeback?.result_revision ?? current.base_revision + 1,
      })
      changed()
    } catch (error) {
      const code = errorCode(error)
      if (code === 'change_set_review_required') {
        update(changeSetId, { status: 'attention', message: T.agent.changeNeedsReview })
      } else if (code === 'change_set_conflict') {
        update(changeSetId, { status: 'error', message: T.agent.changeConflict })
      } else {
        update(changeSetId, { status: 'error', message: authorFacingError(error) })
      }
    } finally { finish(changeSetId) }
  }, [changed, update, token, finish])

  const discard = useCallback(async (changeSetId: string) => {
    if (scope.current !== token || inFlight.current.has(changeSetId)) return
    inFlight.current.add(changeSetId)
    autoEligible.current.delete(changeSetId)
    latestRead.current.set(changeSetId, ++readSequence.current)
    update(changeSetId, { status: 'busy', message: null })
    try {
      const current = await getChangeSet(changeSetId)
      if (scope.current !== token) return
      if (current.status !== 'reviewing' && current.status !== 'ready' && current.status !== 'discarded') {
        update(changeSetId, { status: cardStatus(current.status), message: null })
        return
      }
      if (current.status !== 'discarded') await discardChangeSet(changeSetId, current.review_revision)
      update(changeSetId, { status: 'discarded' })
      changed()
    } catch (error) {
      update(changeSetId, { status: 'error', message: authorFacingError(error) })
    } finally { finish(changeSetId) }
  }, [changed, update, token, finish])

  const rollback = useCallback(async (changeSetId: string) => {
    if (scope.current !== token || inFlight.current.has(changeSetId)) return
    inFlight.current.add(changeSetId)
    autoEligible.current.delete(changeSetId)
    latestRead.current.set(changeSetId, ++readSequence.current)
    update(changeSetId, { status: 'busy', message: null })
    try {
      const current = await getChangeSet(changeSetId)
      if (scope.current !== token) return
      if (current.status !== 'applied' && current.status !== 'rolled_back') {
        update(changeSetId, { status: cardStatus(current.status), message: null })
        return
      }
      if (current.status !== 'rolled_back') await rollbackChangeSet(changeSetId, current.review_revision)
      update(changeSetId, { status: 'rolled_back' })
      changed()
    } catch (error) {
      const code = errorCode(error)
      update(changeSetId, {
        status: 'error',
        message: code === 'change_set_conflict' ? T.agent.changeConflict : authorFacingError(error),
      })
    } finally { finish(changeSetId) }
  }, [changed, update, token, finish])

  const byRun = useMemo(() => {
    const map = new Map<string, ChangeCard[]>()
    for (const card of cards) {
      const list = map.get(card.runId) ?? []
      list.push(card)
      map.set(card.runId, list)
    }
    return map
  }, [cards])

  // 「直接保存」（默认）：卡一挂出来就自动存进书里，不用作者逐张点头；
  // 被写入前检查拦下或书已变化的，仍会留在卡里等作者看一眼。
  // 设置里改成「先看修改」后恢复逐张确认。
  useEffect(() => {
    if (!(autoApply ?? readApplyPolicy() === 'auto')) return
    for (const card of cards) {
      if (card.status === 'pending' && autoEligible.current.has(card.changeSetId)) void approve(card.changeSetId)
    }
  }, [autoApply, cards, approve])

  return { cards, byRun, approve, discard, rollback }
}

export type ChangeCardsApi = ReturnType<typeof useChangeCards>
