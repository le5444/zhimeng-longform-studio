import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import {
  AgentContractError,
  cancelAgentRun,
  createAgentRun,
  createAgentSession,
  getAgentSession,
  getAgentWorkflowState,
  getAgentStatus,
  isTerminalAgentRun,
  listAgentActivities,
  listAgentMessages,
  listAgentRuns,
  listAgentSessions,
  resumeAgentRun,
  streamAgentRunEvents,
  type AgentActivity,
  type AgentAvailability,
  type AgentMessage,
  type AgentMessagePayload,
  type AgentRun,
  type AgentRunStatePayload,
  type AgentSession,
  type AgentWorkflowState,
  type WriterMode,
} from '../api/agent'
import {
  createContextPack,
  getContextPack,
  getNlclRoute,
  getNlclRouteExecution,
  getPrewriteOperationPackage,
  listSessionContextPacks,
  projectKbContractBindingStatus,
  type ContextPackRecord,
  type FormalKnowledgeSelector,
  type NlclRouteReceipt,
  type NlclRouteExecutionState,
  type PrewriteOperationPackage,
} from '../api/contextPacks'
import {
  currentProjectKbBinding,
  getProjectKbState,
  type ProjectKbBinding,
  type ProjectKbStateRecord,
} from '../api/knowledge'
import { RuntimeApiError } from '../api/runtime'

interface AgentConversationOptions {
  enabled: boolean
  projectId: string | null
  projectRevision: number | null
  projectFingerprint: string | null
  activeChapterId: string | null
}

export interface AgentConversationState {
  availability: AgentAvailability
  detail: string
  sessions: AgentSession[]
  session: AgentSession | null
  messages: AgentMessage[]
  streamingContent: string
  activeRun: AgentRun | null
  workflow: AgentWorkflowState | null
  activities: AgentActivity[]
  pendingContextPack: ContextPackRecord | null
  pendingNlclRoute: NlclRouteReceipt | null
  pendingNlclRouteExecutions: NlclRouteExecutionState[]
  pendingPrewriteOperationPackage: PrewriteOperationPackage | null
  activeContextPack: ContextPackRecord | null
  activeNlclRoute: NlclRouteReceipt | null
  activeNlclRouteExecutions: NlclRouteExecutionState[]
  activePrewriteOperationPackage: PrewriteOperationPackage | null
  contextState:
    | 'idle'
    | 'restoring'
    | 'compiling'
    | 'ready'
    | 'degraded'
    | 'blocked'
    | 'stale'
    | 'confirming'
  contextStaleReasons: string[]
  running: boolean
  resuming: boolean
  switchingSession: boolean
  busy: boolean
  error: string | null
  compileContext: (
    message: string,
    profile?: string,
    formalKnowledgeSelector?: FormalKnowledgeSelector | null,
    activeChapterIdOverride?: string | null,
    manualFileIds?: string[],
  ) => Promise<void>
  confirmPendingContext: (writerMode?: WriterMode) => Promise<void>
  discardPendingContext: () => void
  refreshPendingContext: () => Promise<void>
  openContextPackForReview: (contextPackId: string) => Promise<void>
  cancel: () => Promise<void>
  resume: () => Promise<void>
  createConversation: () => Promise<void>
  selectConversation: (sessionId: string) => Promise<void>
  refresh: () => void
}

function errorMessage(error: unknown): string {
  if (error instanceof RuntimeApiError) {
    const payload = error.payload as { error?: { message?: unknown } } | null
    if (typeof payload?.error?.message === 'string') return payload.error.message
  }
  return error instanceof Error ? error.message : 'Writing Agent 请求失败'
}

function executorInstanceId(executor: Record<string, unknown> | null): string | null {
  const value = executor?.instance_id
  return typeof value === 'string' && value.length > 0 ? value : null
}

function sessionMatchesCurrentProvider(
  session: AgentSession,
  currentExecutor: Record<string, unknown> | null,
): boolean {
  const currentInstanceId = executorInstanceId(currentExecutor)
  const sessionInstanceId = executorInstanceId(session.executor)
  return currentInstanceId === null
    || sessionInstanceId === null
    || currentInstanceId === sessionInstanceId
}

function shouldReconcileResumeFailure(error: unknown): boolean {
  if (error instanceof AgentContractError) return false
  if (error instanceof RuntimeApiError) return error.status === 409 || error.status >= 500
  return true
}

function projectKbUnavailableMessage(record: ProjectKbStateRecord): string {
  if (record.status === 'missing') return '项目知识快照尚未生成，请先在知识库重建快照'
  if (record.status === 'stale') return '项目知识快照已过期，请先在知识库重建快照'
  if (record.status === 'tampered') return '项目知识快照校验失败，请先诊断或重新校验'
  return '项目知识快照的 current pointer 与 Manifest 不完整或不一致'
}

function isManagedO2eContextPack(
  record: ContextPackRecord,
  route: NlclRouteReceipt | null,
): boolean {
  const packId = record.pack.context_pack_id
  const sessionId = record.pack.session_id
  return (
    packId.startsWith('context-pack-o2e-')
    && sessionId.startsWith('session-o2e-')
    && route?.source === 'external_distillation'
    && Boolean(record.pack.project_kb_state_id)
    && Boolean(record.pack.project_kb_manifest_hash)
  )
}

async function loadCurrentProjectKbBinding(
  projectId: string,
  projectRevision: number,
  projectFingerprint: string,
  signal?: AbortSignal,
): Promise<ProjectKbBinding> {
  const record = await getProjectKbState(projectId, {}, signal)
  const binding = currentProjectKbBinding(record, {
    projectId,
    projectRevision,
    projectFingerprint,
  })
  if (!binding) throw new Error(projectKbUnavailableMessage(record))
  return binding
}

async function loadNlclBundle(record: ContextPackRecord, signal?: AbortSignal) {
  const contextPackId = record.pack.context_pack_id
  const [route, operationPackage] = await Promise.all([
    getNlclRoute(contextPackId, {}, signal),
    getPrewriteOperationPackage(contextPackId, {}, signal),
  ])
  if (
    route.context_pack_id !== contextPackId
    || route.context_pack_hash !== record.pack.pack_hash
  ) {
    throw new Error('Runtime 返回的 NLCL 路由与 Context Pack 不匹配')
  }
  if (projectKbContractBindingStatus(record, route) === 'mismatch') {
    throw new Error('Runtime 返回的 NLCL 路由与项目知识快照绑定不匹配')
  }
  if (
    operationPackage.context_pack_hash !== record.pack.pack_hash
    || operationPackage.route_receipt_hash !== route.receipt_hash
  ) {
    throw new Error('Runtime 返回的写前运行包绑定不匹配')
  }
  const executions = await Promise.all(
    route.chapter_call_list.map((call) => getNlclRouteExecution(
      contextPackId,
      call.ordinal,
      {},
      signal,
    )),
  )
  for (const execution of executions) {
    const call = route.chapter_call_list.find((item) => item.ordinal === execution.call_ordinal)
    if (
      !call
      || execution.route_receipt_id !== route.route_receipt_id
      || execution.context_pack_id !== contextPackId
      || (execution.receipt && (
        execution.receipt.route_receipt_id !== route.route_receipt_id
        || execution.receipt.route_receipt_hash !== route.receipt_hash
        || execution.receipt.context_pack_id !== contextPackId
        || execution.receipt.context_pack_hash !== record.pack.pack_hash
        || execution.receipt.call_ordinal !== call.ordinal
      ))
    ) {
      throw new Error('Runtime 返回的 NLCL 执行收据绑定不匹配')
    }
  }
  if (projectKbContractBindingStatus(record, route, executions) === 'mismatch') {
    throw new Error('Runtime 返回的 NLCL 执行收据与项目知识快照绑定不匹配')
  }
  return { route, operationPackage, executions }
}

function newIdempotencyKey(prefix: 'context' | 'turn'): string {
  const random = globalThis.crypto?.randomUUID?.() ?? `${Date.now()}-${Math.random()}`
  return `${prefix}-${random.replace(/[^A-Za-z0-9._:-]/g, '')}`
}

function upsertMessage(messages: AgentMessage[], message: AgentMessage): AgentMessage[] {
  const index = messages.findIndex((item) => item.message_id === message.message_id)
  if (index < 0) return [...messages, message].sort((a, b) => a.ordinal - b.ordinal)
  const next = [...messages]
  next[index] = message
  return next
}

function restoreStreamingContent(activities: AgentActivity[]): string {
  let content = ''
  for (const activity of [...activities].sort((left, right) => left.sequence - right.sequence)) {
    if (activity.kind !== 'message' || activity.payload.role !== 'assistant') continue
    if (activity.payload.phase === 'delta' && activity.payload.delta) {
      content += activity.payload.delta
    } else if (activity.payload.phase === 'completed' && activity.payload.content !== null) {
      content = activity.payload.content
    }
  }
  return content
}

export function useAgentConversation({
  enabled,
  projectId,
  projectRevision,
  projectFingerprint,
  activeChapterId,
}: AgentConversationOptions): AgentConversationState {
  const [availability, setAvailability] = useState<AgentAvailability>('checking')
  const [detail, setDetail] = useState('正在检查 Writing Agent')
  const [sessions, setSessions] = useState<AgentSession[]>([])
  const [session, setSession] = useState<AgentSession | null>(null)
  const [messages, setMessages] = useState<AgentMessage[]>([])
  const [historyOrdinal, setHistoryOrdinal] = useState(0)
  const [streamingContent, setStreamingContent] = useState('')
  const [activeRun, setActiveRun] = useState<AgentRun | null>(null)
  const [workflow, setWorkflow] = useState<AgentWorkflowState | null>(null)
  const [activities, setActivities] = useState<AgentActivity[]>([])
  const [pendingContextPack, setPendingContextPack] = useState<ContextPackRecord | null>(null)
  const [pendingNlclRoute, setPendingNlclRoute] = useState<NlclRouteReceipt | null>(null)
  const [pendingNlclRouteExecutions, setPendingNlclRouteExecutions] = useState<NlclRouteExecutionState[]>([])
  const [pendingPrewriteOperationPackage, setPendingPrewriteOperationPackage] = useState<PrewriteOperationPackage | null>(null)
  const [activeContextPack, setActiveContextPack] = useState<ContextPackRecord | null>(null)
  const [activeNlclRoute, setActiveNlclRoute] = useState<NlclRouteReceipt | null>(null)
  const [activeNlclRouteExecutions, setActiveNlclRouteExecutions] = useState<NlclRouteExecutionState[]>([])
  const [activePrewriteOperationPackage, setActivePrewriteOperationPackage] = useState<PrewriteOperationPackage | null>(null)
  const [contextOperation, setContextOperation] = useState<
    'restoring' | 'compiling' | 'confirming' | null
  >(null)
  const [error, setError] = useState<string | null>(null)
  const [projectKbStaleReason, setProjectKbStaleReason] = useState<string | null>(null)
  const [resuming, setResuming] = useState(false)
  const [switchingSession, setSwitchingSession] = useState(false)
  const [refreshKey, setRefreshKey] = useState(0)
  const sessionRef = useRef<AgentSession | null>(null)
  const sessionPromiseRef = useRef<{
    projectId: string
    promise: Promise<AgentSession>
  } | null>(null)
  const activeRunRef = useRef<AgentRun | null>(null)
  const streamCursorRef = useRef<string | null>(null)
  const streamRunIdRef = useRef<string | null>(null)
  const seenActivitiesRef = useRef(new Set<string>())
  const scopeControllerRef = useRef<AbortController | null>(null)
  const contextOperationRef = useRef<'restoring' | 'compiling' | 'confirming' | null>(null)
  const resumePromiseRef = useRef<Promise<void> | null>(null)

  useEffect(() => {
    sessionRef.current = session
  }, [session])

  activeRunRef.current = activeRun

  const refresh = useCallback(() => setRefreshKey((value) => value + 1), [])

  const refreshMessages = useCallback(async (sessionId: string, signal?: AbortSignal) => {
    const page = await listAgentMessages(sessionId, {}, signal)
    setMessages(page.messages)
    setHistoryOrdinal(page.last_ordinal)
  }, [])

  const restoreSession = useCallback(async (
    targetSession: AgentSession,
    signal?: AbortSignal,
  ) => {
    const [messagePage, runs, contextPacks] = await Promise.all([
      listAgentMessages(targetSession.session_id, {}, signal),
      listAgentRuns(targetSession.session_id, {}, signal),
      listSessionContextPacks(targetSession.session_id, {}, signal),
    ])
    const latestRun = runs.at(-1) ?? null
    let restoredActivities: AgentActivity[] = []
    let restoredSequence = 0
    if (latestRun) {
      const activityPage = await listAgentActivities(
        latestRun.run_id,
        0,
        {},
        signal,
      )
      if (activityPage.run_id !== latestRun.run_id) {
        throw new Error('Runtime 返回了其他 Run 的 Activity')
      }
      restoredActivities = activityPage.activities
      restoredSequence = Math.max(
        activityPage.last_sequence,
        ...restoredActivities.map((activity) => activity.sequence),
      )
    }
    const pendingPack = contextPacks.find(
      (record) => record.consumed_by_run_id === null,
    ) ?? null
    const activePack = latestRun
      ? contextPacks.find(
          (record) => record.consumed_by_run_id === latestRun.run_id,
        ) ?? null
      : null
    const [pendingBundle, activeBundle] = await Promise.all([
      pendingPack ? loadNlclBundle(pendingPack, signal) : Promise.resolve(null),
      activePack ? loadNlclBundle(activePack, signal) : Promise.resolve(null),
    ])
    if (signal?.aborted) throw signal.reason

    setSession(targetSession)
    sessionRef.current = targetSession
    setMessages(messagePage.messages)
    setHistoryOrdinal(messagePage.last_ordinal)
    setPendingContextPack(pendingPack)
    setPendingNlclRoute(pendingBundle?.route ?? null)
    setPendingNlclRouteExecutions(pendingBundle?.executions ?? [])
    setPendingPrewriteOperationPackage(pendingBundle?.operationPackage ?? null)
    setActiveContextPack(activePack)
    setActiveNlclRoute(activeBundle?.route ?? null)
    setActiveNlclRouteExecutions(activeBundle?.executions ?? [])
    setActivePrewriteOperationPackage(activeBundle?.operationPackage ?? null)
    seenActivitiesRef.current.clear()
    for (const activity of restoredActivities) {
      seenActivitiesRef.current.add(activity.activity_id)
    }
    setActivities(restoredActivities)
    setActiveRun(latestRun)
    setWorkflow(null)
    setStreamingContent(
      latestRun && !isTerminalAgentRun(latestRun.status)
        ? restoreStreamingContent(restoredActivities)
        : '',
    )
    streamRunIdRef.current = latestRun?.run_id ?? null
    streamCursorRef.current = latestRun && restoredSequence > 0
      ? `${latestRun.run_id}:${restoredSequence}`
      : null
  }, [])

  useEffect(() => {
    const controller = new AbortController()
    scopeControllerRef.current?.abort()
    scopeControllerRef.current = controller
    let disposed = false
    setError(null)
    setProjectKbStaleReason(null)
    setSessions([])
    setSession(null)
    sessionRef.current = null
    setMessages([])
    setHistoryOrdinal(0)
    setActivities([])
    setStreamingContent('')
    setActiveRun(null)
    setWorkflow(null)
    setPendingContextPack(null)
    setPendingNlclRoute(null)
    setPendingNlclRouteExecutions([])
    setPendingPrewriteOperationPackage(null)
    setActiveContextPack(null)
    setActiveNlclRoute(null)
    setActiveNlclRouteExecutions([])
    setActivePrewriteOperationPackage(null)
    setResuming(false)
    setSwitchingSession(false)
    resumePromiseRef.current = null
    contextOperationRef.current = null
    setContextOperation(null)
    streamCursorRef.current = null
    streamRunIdRef.current = null
    seenActivitiesRef.current.clear()

    if (!enabled || !projectId) {
      setAvailability('checking')
      setDetail(enabled ? '请先绑定小说项目' : 'Writing Runtime 尚未就绪')
      return () => {
        disposed = true
        controller.abort()
        if (scopeControllerRef.current === controller) scopeControllerRef.current = null
      }
    }

    contextOperationRef.current = 'restoring'
    setContextOperation('restoring')

    const load = async () => {
      const [statusResult, sessionsResult] = await Promise.allSettled([
        getAgentStatus({}, controller.signal),
        listAgentSessions(projectId, {}, controller.signal),
      ])
      if (disposed) return
      if (statusResult.status === 'fulfilled') {
        setAvailability(statusResult.value.available ? 'available' : 'unavailable')
        setDetail(statusResult.value.detail)
      } else {
        setAvailability('error')
        setDetail(errorMessage(statusResult.reason))
      }
      if (sessionsResult.status === 'rejected') {
        setError(errorMessage(sessionsResult.reason))
        contextOperationRef.current = null
        setContextOperation(null)
        return
      }
      const currentExecutor = statusResult.status === 'fulfilled'
        ? statusResult.value.executor
        : null
      const compatibleSessions = sessionsResult.value.filter((item) =>
        sessionMatchesCurrentProvider(item, currentExecutor),
      )
      setSessions(compatibleSessions)
      const existing = compatibleSessions.find((item) =>
        ['active', 'idle'].includes(item.status),
      ) ?? compatibleSessions[0] ?? null
      if (!existing) {
        contextOperationRef.current = null
        setContextOperation(null)
        return
      }
      await restoreSession(existing, controller.signal)
      if (disposed) return
      contextOperationRef.current = null
      setContextOperation(null)
    }
    void load().catch((loadError) => {
      if (!disposed) {
        setAvailability('error')
        setDetail('Writing Agent 状态读取失败')
        setError(errorMessage(loadError))
        contextOperationRef.current = null
        setContextOperation(null)
      }
    })
    return () => {
      disposed = true
      controller.abort()
      if (scopeControllerRef.current === controller) scopeControllerRef.current = null
    }
  }, [enabled, projectId, refreshKey, restoreSession])

  const workflowTarget = activeRun
    && activeRun.project_revision.project_id === projectId
    && ['paused', 'interrupted', 'failed', 'cancelled'].includes(activeRun.status)
    ? { runId: activeRun.run_id, status: activeRun.status }
    : null
  const workflowTargetRunId = workflowTarget?.runId ?? null
  const workflowTargetStatus = workflowTarget?.status ?? null

  useEffect(() => {
    if (!workflowTargetRunId || !workflowTargetStatus) return
    const targetRunId = workflowTargetRunId
    const targetStatus = workflowTargetStatus
    const controller = new AbortController()
    const scopeSignal = scopeControllerRef.current?.signal
    const abortFromScope = () => controller.abort(scopeSignal?.reason)
    let disposed = false
    if (scopeSignal?.aborted) {
      controller.abort(scopeSignal.reason)
    } else {
      scopeSignal?.addEventListener('abort', abortFromScope, { once: true })
    }

    const isCurrentTarget = () => {
      const current = activeRunRef.current
      return current?.run_id === targetRunId
        && current.status === targetStatus
        && current.project_revision.project_id === projectId
    }

    void getAgentWorkflowState(targetRunId, {}, controller.signal)
      .then((state) => {
        if (disposed || controller.signal.aborted || !isCurrentTarget()) return
        if (state.run_id !== targetRunId) {
          throw new Error('Runtime 返回了其他 Run 的 Workflow')
        }
        setWorkflow(state)
        if (state.run_status !== targetStatus) {
          setActiveRun((current) => current?.run_id === targetRunId
            && current.status === targetStatus
            ? { ...current, status: state.run_status }
            : current)
        }
      })
      .catch((workflowError) => {
        if (disposed || controller.signal.aborted || !isCurrentTarget()) return
        const ordinaryTerminal = targetStatus !== 'paused'
          && workflowError instanceof RuntimeApiError
          && workflowError.status === 404
        if (ordinaryTerminal) {
          setWorkflow((current) => current?.run_id === targetRunId ? null : current)
          return
        }
        setError(errorMessage(workflowError))
      })

    return () => {
      disposed = true
      scopeSignal?.removeEventListener('abort', abortFromScope)
      controller.abort()
    }
  }, [projectId, workflowTargetRunId, workflowTargetStatus])

  const activeRouteContextPackId = activeContextPack?.pack.context_pack_id ?? null
  const activeRouteRunId = activeRun?.run_id ?? null
  const activeRouteRunStatus = activeRun?.status ?? null

  useEffect(() => {
    if (!activeRouteContextPackId || !activeRouteRunId || !activeNlclRoute) return
    const controller = new AbortController()
    void Promise.all(activeNlclRoute.chapter_call_list.map((call) => (
      getNlclRouteExecution(
        activeRouteContextPackId,
        call.ordinal,
        {},
        controller.signal,
      )
    )))
      .then((states) => setActiveNlclRouteExecutions(states))
      .catch((stateError) => {
        if (!controller.signal.aborted) setError(errorMessage(stateError))
    })
    return () => controller.abort()
  }, [activeNlclRoute, activeRouteContextPackId, activeRouteRunId, activeRouteRunStatus, activities.length])

  const streamTargetId = activeRun
    && activeRun.status !== 'paused'
    && !isTerminalAgentRun(activeRun.status)
    ? activeRun.run_id
    : null
  useEffect(() => {
    if (!streamTargetId) return
    const activeRunId = streamTargetId
    const controller = new AbortController()
    if (streamRunIdRef.current !== activeRunId) {
      streamRunIdRef.current = activeRunId
      streamCursorRef.current = null
      seenActivitiesRef.current.clear()
      setActivities([])
      setStreamingContent('')
    }

    const consume = async () => {
      let completed = false
      let completedRun: AgentRun | null = null
      while (!controller.signal.aborted && !completed) {
        try {
          await streamAgentRunEvents(activeRunId, {
            signal: controller.signal,
            lastEventId: streamCursorRef.current,
            onEvent: (event) => {
              streamCursorRef.current = event.event_id
              if (event.event_type === 'done' && event.data.run) {
                completed = true
                completedRun = event.data.run
                return
              }
              const activity = event.data.activity
              if (!activity || seenActivitiesRef.current.has(activity.activity_id)) return
              seenActivitiesRef.current.add(activity.activity_id)
              setActivities((current) => [...current, activity])
              if (activity.kind === 'message') {
                const payload = activity.payload as AgentMessagePayload
                if (payload.role === 'assistant' && payload.phase === 'delta' && payload.delta) {
                  setStreamingContent((current) => current + payload.delta)
                } else if (
                  payload.role === 'assistant' &&
                  payload.phase === 'completed' &&
                  payload.content !== null
                ) {
                  setStreamingContent(payload.content)
                }
              } else if (activity.kind === 'run_state') {
                const payload = activity.payload as AgentRunStatePayload
                if (!isTerminalAgentRun(payload.status)) {
                  setActiveRun((current) => current
                    ? { ...current, status: payload.status, updated_at: activity.created_at }
                    : current)
                }
              }
            },
          })
          if (!completed && !controller.signal.aborted) {
            await new Promise((resolve) => window.setTimeout(resolve, 250))
          }
        } catch (streamError) {
          if (controller.signal.aborted) return
          setError(`Agent 流连接中断，正在恢复：${errorMessage(streamError)}`)
          await new Promise((resolve) => window.setTimeout(resolve, 500))
        }
      }
      const currentSession = sessionRef.current
      if (completed && currentSession && !controller.signal.aborted) {
        try {
          await refreshMessages(currentSession.session_id, controller.signal)
          setStreamingContent('')
          setError(null)
        } catch (historyError) {
          if (!controller.signal.aborted) setError(errorMessage(historyError))
        } finally {
          if (!controller.signal.aborted && completedRun) setActiveRun(completedRun)
        }
      }
    }
    void consume()
    return () => controller.abort()
  }, [refreshMessages, streamTargetId])

  const requireSession = useCallback(async (): Promise<AgentSession> => {
    if (sessionRef.current) return sessionRef.current
    if (!projectId) throw new Error('请先绑定小说项目')
    const pending = sessionPromiseRef.current
    if (pending?.projectId === projectId) return pending.promise
    const signal = scopeControllerRef.current?.signal
    const promise = createAgentSession(
      projectId,
      '写作对话',
      {},
      signal,
    )
      .then((created) => {
        if (signal?.aborted) throw signal.reason
        sessionRef.current = created
        setSession(created)
        setSessions((current) => [
          created,
          ...current.filter((item) => item.session_id !== created.session_id),
        ])
        return created
      })
    const entry = { projectId, promise }
    sessionPromiseRef.current = entry
    void promise.finally(() => {
      if (sessionPromiseRef.current === entry) sessionPromiseRef.current = null
    }).catch(() => undefined)
    return promise
  }, [projectId])

  const contextStaleReasons = useMemo(() => {
    if (!pendingContextPack) return []
    const pack = pendingContextPack.pack
    const reasons: string[] = []
    if (!projectId || pack.project_id !== projectId) reasons.push('绑定项目已经变化')
    if (projectRevision === null || pack.project_revision !== projectRevision) {
      reasons.push(`项目 revision 已从 ${pack.project_revision} 变为 ${projectRevision ?? '不可用'}`)
    }
    if (!projectFingerprint || pack.project_fingerprint !== projectFingerprint) {
      reasons.push('项目内容指纹已经变化')
    }
    if (pack.active_chapter_id !== activeChapterId) reasons.push('当前章节已经变化')
    if (pack.history_through_ordinal !== historyOrdinal) {
      reasons.push(`会话历史已从 ordinal ${pack.history_through_ordinal} 变为 ${historyOrdinal}`)
    }
    if (session && pack.session_id !== session.session_id) reasons.push('Writing Agent 会话已经变化')
    if (pendingContextPack.consumed_by_run_id !== null) reasons.push('Context Pack 已被其他 Run 消费')
    const kbBindingStatus = projectKbContractBindingStatus(
      pendingContextPack,
      pendingNlclRoute,
      pendingNlclRouteExecutions,
    )
    if (kbBindingStatus === 'legacy') {
      reasons.push('旧 Context Pack 未绑定项目知识快照，只能查看，必须重新编译')
    } else if (kbBindingStatus === 'mismatch') {
      reasons.push('Context、NLCL 或执行收据的项目知识快照绑定不匹配')
    }
    if (projectKbStaleReason) reasons.push(projectKbStaleReason)
    if (!pendingNlclRoute) {
      reasons.push('NLCL 路由收据尚未就绪')
    } else {
      if (pendingNlclRoute.context_pack_id !== pack.context_pack_id) reasons.push('NLCL 路由绑定了其他 Context Pack')
      if (pendingNlclRoute.context_pack_hash !== pack.pack_hash) reasons.push('NLCL 路由的 Context hash 不匹配')
      if (pendingNlclRoute.project_id !== pack.project_id
        || pendingNlclRoute.project_revision !== pack.project_revision
        || pendingNlclRoute.project_fingerprint !== pack.project_fingerprint) {
        reasons.push('NLCL 路由的项目真值不匹配')
      }
      if (pendingNlclRoute.knowledge_fingerprint !== pack.knowledge_fingerprint) {
        reasons.push('NLCL 路由的知识指纹不匹配')
      }
    }
    if (!pendingPrewriteOperationPackage) {
      reasons.push('写前运行包尚未就绪')
    } else if (
      pendingPrewriteOperationPackage.context_pack_hash !== pack.pack_hash
      || pendingPrewriteOperationPackage.route_receipt_hash !== pendingNlclRoute?.receipt_hash
    ) {
      reasons.push('写前运行包绑定不匹配')
    } else if (pendingPrewriteOperationPackage.missing_requirements.includes(
      'migrated_prewrite_package_requires_recompile',
    )) {
      reasons.push('写前运行包来自旧版本，必须重新编译')
    }
    return reasons
  }, [
    activeChapterId,
    historyOrdinal,
    pendingContextPack,
    pendingNlclRoute,
    pendingNlclRouteExecutions,
    pendingPrewriteOperationPackage,
    projectFingerprint,
    projectId,
    projectKbStaleReason,
    projectRevision,
    session,
  ])

  const compilePack = useCallback(async (
    rawMessage: string,
    profile: string,
    replacePending: boolean,
    formalKnowledgeSelector: FormalKnowledgeSelector | null,
    targetChapterId: string | null,
    manualFileIds: string[],
  ) => {
    const message = rawMessage.trim()
    if (!message) return
    if (!projectId || projectRevision === null || !projectFingerprint) throw new Error('项目快照尚未就绪')
    if (activeRun && !isTerminalAgentRun(activeRun.status)) {
      throw new Error('当前 Run 尚未结束')
    }
    if (pendingContextPack && !replacePending) {
      throw new Error('已有待确认的 Context Pack，请先确认、刷新或放弃')
    }
    if (contextOperationRef.current !== null) throw new Error('Context 操作正在进行')
    contextOperationRef.current = 'compiling'
    setContextOperation('compiling')
    setError(null)
    setProjectKbStaleReason(null)
    const signal = scopeControllerRef.current?.signal
    try {
      const projectKbBinding = await loadCurrentProjectKbBinding(
        projectId,
        projectRevision,
        projectFingerprint,
        signal,
      )
      const currentSession = await requireSession()
      const messagePage = await listAgentMessages(currentSession.session_id, {}, signal)
      if (signal?.aborted) throw signal.reason
      setMessages(messagePage.messages)
      setHistoryOrdinal(messagePage.last_ordinal)
      const record = await createContextPack(
        currentSession.session_id,
        {
          objective: message,
          profile,
          active_chapter_id: targetChapterId,
          expected_project_revision: projectRevision,
          expected_project_fingerprint: projectFingerprint,
          project_kb_state_id: projectKbBinding.project_kb_state_id,
          project_kb_manifest_hash: projectKbBinding.project_kb_manifest_hash,
          formal_knowledge_selector: formalKnowledgeSelector,
          manual_file_ids: [...new Set(manualFileIds)].sort(),
        },
        newIdempotencyKey('context'),
        {},
        signal,
      )
      if (record.consumed_by_run_id !== null) {
        throw new Error('Runtime 返回了已被消费的 Context Pack')
      }
      if (
        record.pack.project_kb_state_id !== projectKbBinding.project_kb_state_id
        || record.pack.project_kb_manifest_hash !== projectKbBinding.project_kb_manifest_hash
      ) {
        throw new Error('Runtime 返回的 Context Pack 未绑定请求中的项目知识快照')
      }
      const bundle = await loadNlclBundle(record, signal)
      setPendingContextPack(record)
      setPendingNlclRoute(bundle.route)
      setPendingNlclRouteExecutions(bundle.executions)
      setPendingPrewriteOperationPackage(bundle.operationPackage)
    } catch (compileError) {
      if (!signal?.aborted) setError(errorMessage(compileError))
      throw compileError
    } finally {
      if (contextOperationRef.current === 'compiling') {
        contextOperationRef.current = null
        setContextOperation(null)
      }
    }
  }, [
    activeRun,
    pendingContextPack,
    projectFingerprint,
    projectId,
    projectRevision,
    requireSession,
  ])


  const openContextPackForReview = useCallback(async (contextPackId: string) => {
    const targetId = contextPackId.trim()
    if (!targetId) throw new Error('缺少 Context Pack ID')
    if (contextOperationRef.current !== null) throw new Error('Context 操作正在进行')
    if (activeRun && !isTerminalAgentRun(activeRun.status)) throw new Error('当前 Run 尚未结束，不能切换到其他 Context Pack')
    contextOperationRef.current = 'restoring'
    setContextOperation('restoring')
    setError(null)
    setProjectKbStaleReason(null)
    const signal = scopeControllerRef.current?.signal
    try {
      const record = await getContextPack(targetId, {}, signal)
      if (signal?.aborted) throw signal.reason
      if (projectId && record.pack.project_id !== projectId) throw new Error('该 Context Pack 不属于当前项目')
      const packSession = await getAgentSession(record.pack.session_id, {}, signal)
      if (signal?.aborted) throw signal.reason
      sessionRef.current = packSession
      setSession(packSession)
      const messagePage = await listAgentMessages(packSession.session_id, {}, signal)
      if (signal?.aborted) throw signal.reason
      setMessages(messagePage.messages)
      setHistoryOrdinal(messagePage.last_ordinal)
      const bundle = await loadNlclBundle(record, signal)
      if (signal?.aborted) throw signal.reason
      setPendingContextPack(record)
      setPendingNlclRoute(bundle.route)
      setPendingNlclRouteExecutions(bundle.executions)
      setPendingPrewriteOperationPackage(bundle.operationPackage)
      setActiveContextPack(null)
      setActiveNlclRoute(null)
      setActiveNlclRouteExecutions([])
      setActivePrewriteOperationPackage(null)
      setActiveRun(null)
      setWorkflow(null)
      setActivities([])
      setStreamingContent('')
      seenActivitiesRef.current.clear()
      streamCursorRef.current = null
      streamRunIdRef.current = null
    } catch (openError) {
      if (!signal?.aborted) setError(errorMessage(openError))
      throw openError
    } finally {
      if (contextOperationRef.current === 'restoring') {
        contextOperationRef.current = null
        setContextOperation(null)
      }
    }
  }, [activeRun, projectId])

  const compileContext = useCallback((
    message: string,
    profile?: string,
    formalKnowledgeSelector: FormalKnowledgeSelector | null = null,
    activeChapterIdOverride?: string | null,
    manualFileIds: string[] = [],
  ) => (
    compilePack(
      message,
      profile ?? (
        (activeChapterIdOverride === undefined ? activeChapterId : activeChapterIdOverride) === null
          ? 'draft_assist'
          : 'chapter'
      ),
      false,
      formalKnowledgeSelector,
      activeChapterIdOverride === undefined ? activeChapterId : activeChapterIdOverride,
      manualFileIds,
    )
  ), [activeChapterId, compilePack])

  const refreshPendingContext = useCallback(async () => {
    if (!pendingContextPack) return
    await compilePack(
      pendingContextPack.pack.objective,
      pendingContextPack.pack.profile,
      true,
      pendingContextPack.pack.formal_knowledge_selector ?? null,
      pendingContextPack.pack.active_chapter_id,
      pendingContextPack.pack.manual_file_ids ?? [],
    )
  }, [compilePack, pendingContextPack])

  const discardPendingContext = useCallback(() => {
    if (contextOperationRef.current !== null) return
    setPendingContextPack(null)
    setPendingNlclRoute(null)
    setPendingNlclRouteExecutions([])
    setPendingPrewriteOperationPackage(null)
    setError(null)
    setProjectKbStaleReason(null)
  }, [])

  const confirmPendingContext = useCallback(async (
    writerMode: WriterMode = 'repair_only',
  ) => {
    if (!pendingContextPack) return
    if (!pendingNlclRoute) throw new Error('NLCL 路由收据尚未就绪')
    if (!pendingPrewriteOperationPackage) throw new Error('写前运行包尚未就绪')
    if (availability !== 'available') throw new Error(detail)
    if (activeRun && !isTerminalAgentRun(activeRun.status)) {
      throw new Error('当前 Run 尚未结束')
    }
    if (pendingContextPack.pack.readiness === 'blocked') {
      throw new Error('Context Pack 存在阻断 Gate，不能启动 Run')
    }
    if (pendingPrewriteOperationPackage.missing_requirements.includes(
      'migrated_prewrite_package_requires_recompile',
    )) {
      throw new Error('写前运行包来自旧版本，必须重新编译')
    }
    if (contextStaleReasons.length > 0) {
      throw new Error('Context Pack 已过期，请刷新后再运行')
    }
    if (contextOperationRef.current !== null) throw new Error('Context 操作正在进行')
    contextOperationRef.current = 'confirming'
    setContextOperation('confirming')
    setError(null)
    const signal = scopeControllerRef.current?.signal
    try {
      const pack = pendingContextPack.pack
      const managedO2ePack = isManagedO2eContextPack(pendingContextPack, pendingNlclRoute)
      if (!managedO2ePack) {
        let projectKbBinding: ProjectKbBinding
        try {
          projectKbBinding = await loadCurrentProjectKbBinding(
            pack.project_id,
            pack.project_revision,
            pack.project_fingerprint,
            signal,
          )
        } catch (kbError) {
          setProjectKbStaleReason('当前项目知识快照不可运行，请先重建或校验后重新编译')
          throw kbError
        }
        if (
          pack.project_kb_state_id !== projectKbBinding.project_kb_state_id
          || pack.project_kb_manifest_hash !== projectKbBinding.project_kb_manifest_hash
        ) {
          setProjectKbStaleReason('项目知识快照已变化，当前 Context Pack 只能查看，请重新编译')
          throw new Error('项目知识快照已变化，请重新编译 Context Pack')
        }
      } else {
        // O2-E packs are closed over the managed distillation-index binding.
        setProjectKbStaleReason(null)
      }
      const messagePage = await listAgentMessages(pack.session_id, {}, signal)
      if (signal?.aborted) throw signal.reason
      setMessages(messagePage.messages)
      setHistoryOrdinal(messagePage.last_ordinal)
      if (messagePage.last_ordinal !== pack.history_through_ordinal) {
        throw new Error('会话历史已变化，请重新编译 Context Pack')
      }
      const result = await createAgentRun(
        pack.session_id,
        {
          message: pack.objective,
          expected_project_revision: pack.project_revision,
          expected_project_fingerprint: pack.project_fingerprint,
          active_chapter_id: pack.active_chapter_id,
          writer_mode: writerMode,
          context_pack_id: pack.context_pack_id,
          expected_context_pack_hash: pack.pack_hash,
        },
        newIdempotencyKey('turn'),
        {},
        signal,
      )
      setActiveContextPack(pendingContextPack)
      setActiveNlclRoute(pendingNlclRoute)
      setActiveNlclRouteExecutions(pendingNlclRouteExecutions)
      setActivePrewriteOperationPackage(pendingPrewriteOperationPackage)
      setPendingContextPack(null)
      setPendingNlclRoute(null)
      setPendingNlclRouteExecutions([])
      setPendingPrewriteOperationPackage(null)
      setMessages((current) => upsertMessage(current, result.user_message))
      setHistoryOrdinal((current) => Math.max(current, result.user_message.ordinal))
      setActivities([])
      setStreamingContent('')
      seenActivitiesRef.current.clear()
      streamCursorRef.current = null
      streamRunIdRef.current = result.run.run_id
      setActiveRun(result.run)
      setWorkflow(null)
    } catch (confirmError) {
      if (!signal?.aborted) setError(errorMessage(confirmError))
      throw confirmError
    } finally {
      if (contextOperationRef.current === 'confirming') {
        contextOperationRef.current = null
        setContextOperation(null)
      }
    }
  }, [
    activeRun,
    availability,
    contextStaleReasons,
    detail,
    pendingContextPack,
    pendingNlclRoute,
    pendingNlclRouteExecutions,
    pendingPrewriteOperationPackage,
  ])

  const cancel = useCallback(async () => {
    if (!activeRun || activeRun.status === 'paused' || isTerminalAgentRun(activeRun.status)) return
    const signal = scopeControllerRef.current?.signal
    try {
      const cancelled = await cancelAgentRun(
        activeRun.run_id,
        {},
        signal,
      )
      setActiveRun(cancelled)
    } catch (cancelError) {
      if (!signal?.aborted) setError(errorMessage(cancelError))
    }
  }, [activeRun])

  const resume = useCallback((): Promise<void> => {
    if (resumePromiseRef.current) return resumePromiseRef.current
    const run = activeRun
    const state = workflow
    if (!run || run.status !== 'paused' || !state?.resume_allowed) {
      return Promise.reject(new Error('当前工作流不可继续'))
    }
    if (state.run_id !== run.run_id
      || state.run_status !== 'paused'
      || state.resume_state !== 'resumable'
      || state.plan.run_id !== run.run_id
      || state.provider_boundary !== null) {
      return Promise.reject(new AgentContractError('Workflow Resume 身份或边界状态无效'))
    }
    const signal = scopeControllerRef.current?.signal
    const promise = (async () => {
      setResuming(true)
      setError(null)
      try {
        const result = await resumeAgentRun(
          run.run_id,
          {
            schema: 'zhimeng.agent-run-resume-request.v1',
            expected_workflow_plan_id: state.plan.workflow_plan_id,
            expected_plan_hash: state.plan.plan_hash,
            expected_checkpoint_id: state.last_checkpoint_id,
            expected_checkpoint_hash: state.last_checkpoint_hash,
          },
          {},
          signal,
        )
        if (signal?.aborted) throw signal.reason
        streamRunIdRef.current = result.run.run_id
        setActiveRun(result.run)
        setWorkflow((current) => current?.run_id === result.run.run_id
          ? {
              ...current,
              run_status: result.run.status,
              resume_state: 'running',
              resume_allowed: false,
              reason_code: 'workflow_execution_active',
            }
          : current)
      } catch (resumeError) {
        if (!signal?.aborted && shouldReconcileResumeFailure(resumeError)) {
          try {
            const reconciled = await getAgentWorkflowState(run.run_id, {}, signal)
            if (signal?.aborted) throw signal.reason
            const current = activeRunRef.current
            if (current?.run_id === run.run_id && current.status === 'paused') {
              setWorkflow(reconciled)
              setActiveRun((latest) => latest?.run_id === run.run_id
                && latest.status === 'paused'
                ? { ...latest, status: reconciled.run_status }
                : latest)
              if (reconciled.run_status !== 'paused'
                && !isTerminalAgentRun(reconciled.run_status)) {
                setError(null)
                return
              }
            } else if (current?.run_id === run.run_id
              && !isTerminalAgentRun(current.status)
              && current.status !== 'paused') {
              setError(null)
              return
            }
          } catch (reconcileError) {
            if (signal?.aborted) throw reconcileError
            if (reconcileError instanceof AgentContractError) {
              setError(errorMessage(reconcileError))
              throw reconcileError
            }
          }
        }
        if (!signal?.aborted) setError(errorMessage(resumeError))
        throw resumeError
      } finally {
        setResuming(false)
      }
    })()
    resumePromiseRef.current = promise
    void promise.finally(() => {
      if (resumePromiseRef.current === promise) resumePromiseRef.current = null
    }).catch(() => undefined)
    return promise
  }, [activeRun, workflow])

  const selectConversation = useCallback(async (sessionId: string) => {
    const targetId = sessionId.trim()
    if (!targetId || targetId === sessionRef.current?.session_id) return
    if (contextOperationRef.current !== null || resuming) {
      throw new Error('写作助手正在处理当前任务，请稍后切换对话')
    }
    if (activeRun && !isTerminalAgentRun(activeRun.status)) {
      throw new Error('当前任务尚未结束，不能切换对话')
    }
    if (pendingContextPack) {
      throw new Error('当前资料已经备齐，请先发送或放弃后再切换对话')
    }
    const target = sessions.find((item) => item.session_id === targetId)
    if (!target) throw new Error('找不到这条历史对话，请刷新后重试')

    contextOperationRef.current = 'restoring'
    setContextOperation('restoring')
    setSwitchingSession(true)
    setError(null)
    const signal = scopeControllerRef.current?.signal
    try {
      const latest = await getAgentSession(targetId, {}, signal)
      await restoreSession(latest, signal)
      setSessions((current) => current.map((item) => (
        item.session_id === latest.session_id ? latest : item
      )))
    } catch (switchError) {
      if (!signal?.aborted) setError(errorMessage(switchError))
      throw switchError
    } finally {
      setSwitchingSession(false)
      if (contextOperationRef.current === 'restoring') {
        contextOperationRef.current = null
        setContextOperation(null)
      }
    }
  }, [activeRun, pendingContextPack, restoreSession, resuming, sessions])

  const createConversation = useCallback(async () => {
    if (!projectId) throw new Error('请先打开一本作品')
    if (contextOperationRef.current !== null || resuming) {
      throw new Error('写作助手正在处理当前任务，请稍后新建对话')
    }
    if (activeRun && !isTerminalAgentRun(activeRun.status)) {
      throw new Error('当前任务尚未结束，不能新建对话')
    }
    if (pendingContextPack) {
      throw new Error('当前资料已经备齐，请先发送或放弃后再新建对话')
    }

    contextOperationRef.current = 'restoring'
    setContextOperation('restoring')
    setSwitchingSession(true)
    setError(null)
    const signal = scopeControllerRef.current?.signal
    const title = `新对话 · ${new Intl.DateTimeFormat('zh-CN', {
      month: 'numeric',
      day: 'numeric',
      hour: '2-digit',
      minute: '2-digit',
    }).format(new Date())}`
    try {
      const created = await createAgentSession(projectId, title, {}, signal)
      await restoreSession(created, signal)
      setSessions((current) => [
        created,
        ...current.filter((item) => item.session_id !== created.session_id),
      ])
    } catch (createError) {
      if (!signal?.aborted) setError(errorMessage(createError))
      throw createError
    } finally {
      setSwitchingSession(false)
      if (contextOperationRef.current === 'restoring') {
        contextOperationRef.current = null
        setContextOperation(null)
      }
    }
  }, [activeRun, pendingContextPack, projectId, restoreSession, resuming])

  const paused = activeRun?.status === 'paused'
  const running = activeRun !== null && !paused && !isTerminalAgentRun(activeRun.status)
  const busy = contextOperation !== null || running || paused || resuming
  const contextState = contextOperation ?? (
    pendingContextPack === null
      ? 'idle'
      : contextStaleReasons.length > 0
        ? 'stale'
        : pendingContextPack.pack.readiness
  )
  return useMemo(() => ({
    availability,
    detail,
    sessions,
    session,
    messages,
    streamingContent,
    activeRun,
    workflow,
    activities,
    pendingContextPack,
    pendingNlclRoute,
    pendingNlclRouteExecutions,
    pendingPrewriteOperationPackage,
    activeContextPack,
    activeNlclRoute,
    activeNlclRouteExecutions,
    activePrewriteOperationPackage,
    contextState,
    contextStaleReasons,
    running,
    resuming,
    switchingSession,
    busy,
    error,
    compileContext,
    confirmPendingContext,
    discardPendingContext,
    refreshPendingContext,
    openContextPackForReview,
    cancel,
    resume,
    createConversation,
    selectConversation,
    refresh,
  }), [
    activeRun,
    activeContextPack,
    activeNlclRoute,
    activeNlclRouteExecutions,
    activePrewriteOperationPackage,
    activities,
    availability,
    busy,
    cancel,
    compileContext,
    confirmPendingContext,
    createConversation,
    contextStaleReasons,
    contextState,
    detail,
    discardPendingContext,
    error,
    messages,
    pendingContextPack,
    pendingNlclRoute,
    pendingNlclRouteExecutions,
    pendingPrewriteOperationPackage,
    refresh,
    openContextPackForReview,
    refreshPendingContext,
    resume,
    resuming,
    running,
    selectConversation,
    session,
    sessions,
    switchingSession,
    streamingContent,
    workflow,
  ])
}
