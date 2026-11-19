import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import {
  agentSessionProviderPreference,
  cancelAgentRun,
  compactAgentSession,
  createAgentRun,
  createAgentSession,
  createUnboundAgentSession,
  deleteAgentSession,
  forkAgentSession,
  getAgentRunContinuation,
  getAgentRunAdmission,
  getAgentSession,
  getAgentStatus,
  getLatestSessionCheckpoint,
  isTerminalAgentRun,
  listAgentActivities,
  listAgentMessages,
  listAgentRuns,
  listAgentSessions,
  listUnboundAgentSessions,
  streamAgentRunEvents,
  updateAgentSession,
  withdrawAgentRunAdmission,
  type AgentActivity,
  type AgentImageAttachment,
  type AgentAvailability,
  type AgentMessage,
  type AgentMessagePage,
  type AgentMessagePayload,
  type AgentRun,
  type AgentRunProcessSummary,
  type AgentReviewSource,
  type AgentRunContinuation,
  type AgentRunStatePayload,
  type RunCreateResponse,
  type AgentSession,
  type SessionCheckpointRecord,
  type WriterMode,
} from '../api/agent'
import type { ProviderSettingsView } from '../api/providerSettings'
import { inspectBoundProject } from '../api/projects'
import { RuntimeApiError, RuntimeTimeoutError, isCancelledRequest } from '../api/runtime'
import { mergeAgentMessages, readAgentMessageUpdates } from './agentMessageHistory'
import { createActivityBuffer } from './agentActivityBuffer'
import { AgentSendUnconfirmedError, claimPendingImageDraft, claimPendingSkillDraft, clearPendingAgentSend, readPendingAgentSend, savePendingAgentSend, type ImageDraftIdentity, type SkillDraftIdentity, type PendingAgentSend } from './agentSendReceipt'

export { AgentSendUnconfirmedError } from './agentSendReceipt'

export interface PendingSendView {
  item: PendingAgentSend
  status: 'checking' | 'unknown' | 'not_found' | 'withdrawn'
}

function isWithdrawnAdmission(error: unknown): boolean {
  return error instanceof RuntimeApiError && error.status === 410
    && (error.payload as { error?: { code?: string } })?.error?.code === 'agent_admission_withdrawn'
}

interface AgentConversationOptions {
  enabled: boolean
  projectId: string | null
  projectRevision: number | null
  projectFingerprint: string | null
  /** 用户这时在右栏开着的文件（打开的文件夹里的相对路径）；发消息时照实告诉后端，没开就是 null。 */
  openFilePath: string | null
  /**
   * 换书时优先恢复这一条对话。左栏点的是「某本书下面的某条对话」，换书之后不能随便挑
   * 最近那条，得落到点的那条上。只在换书那一下读一次，同一本书内换对话走 selectConversation。
   */
  preferredSessionId?: string | null
}

/** 发一句话。打开了文件夹就发到那个文件夹的对话里；没打开就是随便聊。 */
export interface AgentSendRequest {
  goal?: import('../api/sessionGoal').GoalAdmission
  text: string
  manualFileIds?: string[]
  /** 作者在输入框用 / 点名的技能（引用标签）。 */
  skillNames?: string[]
  imageIds?: string[]
  images?: AgentImageAttachment[]
  imageDraftIdentity?: ImageDraftIdentity
  skillDraftIdentity?: SkillDraftIdentity
  writerMode?: WriterMode
  /** 「继续任务」：接着上一轮没做完的事。 */
  continuationSourceRunId?: string
  reviewSourceRunId?: string
}

/** 对话链此刻在忙什么：恢复对话、发送、压缩对话。 */
export type AgentConversationOperation = 'restoring' | 'sending' | 'compacting'

export interface AgentConversationState {
  messageSyncError?: string | null
  syncingMessages?: boolean
  availability: AgentAvailability
  detail: string
  sessions: AgentSession[]
  session: AgentSession | null
  /** 界面上的消息：打开时是最新的 500 条，点「显示更早的消息」再往前补。 */
  messages: AgentMessage[]
  hasEarlierMessages: boolean
  loadEarlierMessages: () => Promise<void>
  streamingContent: string
  activeRun: AgentRun | null
  continuation: AgentRunContinuation | null
  /** 当前这一轮的步骤（右栏「现在」、修改卡都只看这一轮）。 */
  activities: AgentActivity[]
  /** 这条对话里前几轮的步骤，按轮存：旧回答上的「执行了 N 步」靠它，不跟着新一轮消失。没加载的轮不在里面。 */
  pastActivities: Readonly<Record<string, AgentActivity[]>>
  runProcessSummaries?: Readonly<Record<string, AgentRunProcessSummary>>
  runOrigins?: Readonly<Record<string, 'user' | 'command_completion'>>
  reviewSources?: Readonly<Record<string, AgentReviewSource>>
  observeBackgroundRun?: (run: AgentRun) => Promise<void>
  /** 重新打开的对话里，旧的一轮要看步骤时再去后台读。 */
  loadRunActivities: (runId: string) => Promise<void>
  contextState: AgentConversationOperation | 'idle'
  running: boolean
  switchingSession: boolean
  busy: boolean
  error: string | null
  latestCheckpoint: SessionCheckpointRecord | null
  send: (request: AgentSendRequest) => Promise<void>
  pendingSend?: PendingSendView | null
  checkPendingSend?: () => Promise<void>
  retryPendingSend?: () => Promise<void>
  withdrawPendingSend?: () => Promise<string | null>
  cancel: () => Promise<void>
  prepareContinuation: () => Promise<AgentRunContinuation | null>
  rebindProvider: (settings?: Pick<ProviderSettingsView, 'provider_id' | 'model' | 'reasoning_effort'>) => Promise<void>
  createConversation: (customTitle?: string) => Promise<AgentSession | void>
  forkConversation: (upToMessageId: string, title?: string) => Promise<AgentSession>
  compactConversation: () => Promise<SessionCheckpointRecord>
  selectConversation: (sessionId: string) => Promise<void>
  renameConversation: (sessionId: string, title: string) => Promise<void>
  setConversationPinned: (sessionId: string, pinned: boolean) => Promise<void>
  deleteConversation: (sessionId: string) => Promise<void>
  refresh: () => void
  /** 更新已落盘消息，保留当前流、草稿与会话选择。 */
  refreshMessages?: () => Promise<boolean>
}

/**
 * 对话链这会儿正忙（换了项目正在读它的对话、上一步还没收尾）：等它闲下来再做就行，不是出错。
 * 外面排着队的「换完项目开一条新对话」认这个类型，接着等，不弹红字。
 */
export class AgentBusyError extends Error {
  override readonly name = 'AgentBusyError'
}

function errorMessage(error: unknown): string {
  if (error instanceof RuntimeApiError) {
    const payload = error.payload as { error?: { message?: unknown } } | null
    if (typeof payload?.error?.message === 'string') return payload.error.message
  }
  if (error instanceof RuntimeTimeoutError) {
    return `${error.message}。可以先重试一次；要是每次都这样，看看本机服务（8787）是不是卡在别的事上。`
  }
  if (isCancelledRequest(error)) {
    return '已经停下了，这不是出错。'
  }
  return error instanceof Error ? error.message : '这次请求没有成功'
}

function sortSessions(sessions: AgentSession[]): AgentSession[] {
  return [...sessions].sort((left, right) => {
    const pinned = Number(Boolean(right.pinned)) - Number(Boolean(left.pinned))
    if (pinned !== 0) return pinned
    const updated = Date.parse(right.updated_at) - Date.parse(left.updated_at)
    return Number.isNaN(updated) ? left.session_id.localeCompare(right.session_id) : updated
  })
}

const AGENT_CONTROL_TIMEOUT_MS = 30_000
const SELECTED_SESSION_PREFIX = 'zhimeng.agent.selected-session:'
function selectedSession(projectId: string | null): string | null {
  try { return localStorage.getItem(SELECTED_SESSION_PREFIX + JSON.stringify(projectId)) } catch { return null }
}
function rememberSession(projectId: string | null, sessionId: string | null) {
  try {
    const key = SELECTED_SESSION_PREFIX + JSON.stringify(projectId)
    if (sessionId) localStorage.setItem(key, sessionId)
    else localStorage.removeItem(key)
  } catch { /* 本页导航保持可用。 */ }
}
const FILE_EFFECT_TOOLS = new Set([
  'write_file', 'edit_file', 'move_file', 'delete_file', 'make_folder', 'undo_change', 'computer_run_command',
  'project_write_file', 'project_edit_file', 'project_move_file', 'project_delete_file', 'project_mkdir', 'project_undo_change',
])

/** 新对话的占位名字「新对话 · 9/27 10:05」：后台认得它是占位，第一轮收尾时按第一句话改成正经名字。 */
function placeholderSessionTitle(): string {
  return `新对话 · ${new Intl.DateTimeFormat('zh-CN', {
    month: 'numeric',
    day: 'numeric',
    hour: '2-digit',
    minute: '2-digit',
  }).format(new Date())}`
}

function newIdempotencyKey(prefix: 'turn'): string {
  const random = globalThis.crypto?.randomUUID?.() ?? `${Date.now()}-${Math.random()}`
  return `${prefix}-${random.replace(/[^A-Za-z0-9._:-]/g, '')}`
}

function upsertMessage(messages: AgentMessage[], message: AgentMessage): AgentMessage[] {
  return mergeAgentMessages(messages, [message])
}

function restoreStreamingContent(activities: AgentActivity[]): string {
  let content = ''
  for (const activity of [...activities].sort((left, right) => left.sequence - right.sequence)) {
    if (activity.kind === 'message' && activity.payload.role === 'user' && activity.payload.phase === 'completed') {
      content = ''
      continue
    }
    if (activity.kind !== 'message' || activity.payload.role !== 'assistant') continue
    if (activity.payload.phase === 'delta' && activity.payload.delta) {
      content += activity.payload.delta
    } else if (activity.payload.phase === 'completed' && activity.payload.content !== null) {
      // 完整阶段已在消息列表中；恢复流只接着展示尚未完成的下一段。
      content = ''
    }
  }
  return content
}

function executorInstanceId(session: AgentSession): string | null {
  return typeof session.executor?.instance_id === 'string' ? session.executor.instance_id : null
}

/**
 * 中栏那一条对话：恢复历史、发一句话、流式收回答、停、换对话。
 *
 * 发一句话就是一个请求（2026-09-27 起，纠偏指令 3.9）：打开了文件夹也不再先编资料包，只带开着哪个文件、
 * 亲手附上了哪些文件；文件夹里的说明文件和用户记忆由后端放在对话开头，别的模型要用自己读。
 * 更早（9-25 以前）中间还要查知识库快照、重建快照、拉路由回执和写前运行包。
 */
export function useAgentConversation({
  enabled,
  projectId,
  projectRevision,
  projectFingerprint,
  openFilePath,
  preferredSessionId = null,
}: AgentConversationOptions): AgentConversationState {
  const [availability, setAvailability] = useState<AgentAvailability>('checking')
  const [detail, setDetail] = useState('正在检查 Agent')
  // 只在换书那一下读，所以放 ref：它变了不该把整条链重来一遍。
  const preferredSessionRef = useRef<string | null>(preferredSessionId)
  preferredSessionRef.current = preferredSessionId
  // 外部点击的是一次导航意图；恢复后刷新应保持后来实际选中的会话。
  const preferredIntent = useRef({ projectId, sessionId: preferredSessionId, used: false })
  if (preferredIntent.current.projectId !== projectId || preferredIntent.current.sessionId !== preferredSessionId) {
    preferredIntent.current = { projectId, sessionId: preferredSessionId, used: false }
  }
  const [sessions, setSessions] = useState<AgentSession[]>([])
  const [session, setSession] = useState<AgentSession | null>(null)
  const [messages, setMessages] = useState<AgentMessage[]>([])
  const messagesRef = useRef<AgentMessage[]>(messages)
  messagesRef.current = messages
  const [hasEarlierMessages, setHasEarlierMessages] = useState(false)
  const syncedMessagesRef = useRef({ sessionId: '', ordinal: 0 })
  const [messageSyncError, setMessageSyncError] = useState<string | null>(null)
  const [syncingMessages, setSyncingMessages] = useState(false)
  const [streamingContent, setStreamingContent] = useState('')
  const streamingMessageIdRef = useRef<string | null>(null)
  const streamingGenerationRef = useRef(0)
  const [activeRun, setActiveRun] = useState<AgentRun | null>(null)
  const [continuation, setContinuation] = useState<AgentRunContinuation | null>(null)
  const [activities, setActivities] = useState<AgentActivity[]>([])
  const [pastActivities, setPastActivities] = useState<Record<string, AgentActivity[]>>({})
  const [runProcessSummaries, setRunProcessSummaries] = useState<Record<string, AgentRunProcessSummary>>({})
  const [runOrigins, setRunOrigins] = useState<Record<string, 'user' | 'command_completion'>>({})
  const [reviewSources, setReviewSources] = useState<Record<string, AgentReviewSource>>({})
  const [latestCheckpoint, setLatestCheckpoint] = useState<SessionCheckpointRecord | null>(null)
  const [operation, setOperationState] = useState<AgentConversationOperation | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [pendingSendState, setPendingSend] = useState<PendingSendView | null>(null)
  const [switchingSession, setSwitchingSession] = useState(false)
  const [observationEpoch, setObservationEpoch] = useState(0)
  const [refreshKey, setRefreshKey] = useState(0)
  const sessionRef = useRef<AgentSession | null>(null)
  const sessionPromiseRef = useRef<{ projectId: string; promise: Promise<AgentSession> } | null>(null)
  const activeRunRef = useRef<AgentRun | null>(null)
  const streamCursorRef = useRef<string | null>(null)
  const streamRunIdRef = useRef<string | null>(null)
  const seenActivitiesRef = useRef(new Set<string>())
  const scopeControllerRef = useRef<AbortController | null>(null)
  const streamControllerRef = useRef<AbortController | null>(null)
  const operationRef = useRef<AgentConversationOperation | null>(null)
  const preparingContinuationRef = useRef(false)
  const activitiesRef = useRef<AgentActivity[]>([])

  useEffect(() => {
    sessionRef.current = session
  }, [session])

  const replaceActivities = useCallback((next: AgentActivity[]) => {
    activitiesRef.current = next
    setActivities(next)
  }, [])

  /**
   * 换到新的一轮之前，把手上这几轮的步骤按轮挪进「前几轮」，再清空当前的。
   * 以前直接清空：发一句新的，上一条回答的「执行了 N 步」就没了（改前改后都这样，2026-09-26 真机看到）。
   */
  const stashActivities = useCallback((nextRunId: string | null) => {
    const previous = activitiesRef.current
    if (previous.length === 0) return
    const byRun = new Map<string, AgentActivity[]>()
    for (const activity of previous) {
      if (activity.run_id === nextRunId) continue
      const list = byRun.get(activity.run_id)
      if (list) list.push(activity)
      else byRun.set(activity.run_id, [activity])
    }
    if (byRun.size === 0) return
    setPastActivities((past) => ({ ...past, ...Object.fromEntries(byRun) }))
  }, [])

  activeRunRef.current = activeRun

  const setOperation = useCallback((next: AgentConversationOperation | null) => {
    operationRef.current = next
    setOperationState(next)
  }, [])
  /** 只在自己这一步还占着的时候才清：换书之后旧的 finally 不能把新作用域的状态抹掉。 */
  const releaseOperation = useCallback((mine: AgentConversationOperation, signal: AbortSignal | undefined) => {
    if (scopeControllerRef.current?.signal === signal && operationRef.current === mine) setOperation(null)
  }, [setOperation])

  // 导航只更换浏览器的观察范围；后台任务及发送回执仍属于原对话。
  const beginNavigation = useCallback(() => {
    scopeControllerRef.current?.abort()
    streamControllerRef.current?.abort()
    const controller = new AbortController()
    scopeControllerRef.current = controller
    preparingContinuationRef.current = false
    setOperation('restoring')
    setSwitchingSession(true)
    setObservationEpoch((value) => value + 1)
    setError(null)
    return controller.signal
  }, [setOperation])

  const refresh = useCallback(() => setRefreshKey((value) => value + 1), [])
  const messageRefreshSequence = useRef(0)
  const messageRefreshController = useRef<AbortController | null>(null)
  const applyMessagePage = useCallback((target: string, page: AgentMessagePage) => {
    const first = messagesRef.current[0]?.ordinal
    if (first === undefined || (page.messages[0] && page.messages[0].ordinal <= first)) {
      setHasEarlierMessages(page.has_earlier === true)
    }
    syncedMessagesRef.current = { sessionId: target, ordinal: Math.max(page.messages.at(-1)?.ordinal ?? 0,
      syncedMessagesRef.current.sessionId === target ? syncedMessagesRef.current.ordinal : 0) }
    setMessages((current) => mergeAgentMessages(current, page.messages))
    setRunProcessSummaries((current) => ({ ...current, ...page.run_process_summaries }))
    setRunOrigins((current) => ({ ...current, ...page.run_origins }))
    setReviewSources((current) => ({ ...current, ...page.review_sources }))
    setMessageSyncError(null)
  }, [])
  const refreshMessages = useCallback(async (): Promise<boolean> => {
    const target = sessionRef.current?.session_id
    if (!target) return false
    messageRefreshController.current?.abort()
    const controller = new AbortController()
    messageRefreshController.current = controller
    const scopeSignal = scopeControllerRef.current?.signal
    const signal = scopeSignal ? AbortSignal.any([scopeSignal, controller.signal]) : controller.signal
    const sequence = ++messageRefreshSequence.current
    const generation = streamingGenerationRef.current
    setSyncingMessages(true)
    try {
      const through = syncedMessagesRef.current.sessionId === target ? syncedMessagesRef.current.ordinal : 0
      const page = await readAgentMessageUpdates(target, through, { timeoutMs: AGENT_CONTROL_TIMEOUT_MS }, signal)
      if (signal?.aborted || sessionRef.current?.session_id !== target || sequence !== messageRefreshSequence.current) return false
      applyMessagePage(target, page)
      const completedMessage = activeRunRef.current?.status === 'succeeded' || activitiesRef.current.some((activity) => activity.kind === 'message'
        && activity.payload.message_id === streamingMessageIdRef.current && activity.payload.phase === 'completed')
      if (generation === streamingGenerationRef.current && completedMessage
        && page.messages.some((message) => message.message_id === streamingMessageIdRef.current)) setStreamingContent('')
      return true
    } catch (loadError) {
      if (!signal?.aborted && sessionRef.current?.session_id === target && sequence === messageRefreshSequence.current) setMessageSyncError(errorMessage(loadError))
      return false
    } finally {
      if (!signal?.aborted && sessionRef.current?.session_id === target && sequence === messageRefreshSequence.current) setSyncingMessages(false)
    }
  }, [applyMessagePage])

  const adoptSession = useCallback((updated: AgentSession) => {
    sessionRef.current = updated
    setSession(updated)
    setSessions((items) => sortSessions(items.map((item) => (item.session_id === updated.session_id ? updated : item))))
  }, [])

  /** 按这条对话自己的模型选择恢复 Provider，保留其他对话及全局默认。 */
  const ensureProviderBound = useCallback(async (candidate: AgentSession, signal?: AbortSignal): Promise<AgentSession> => {
    if (activeRunRef.current && !isTerminalAgentRun(activeRunRef.current.status)) {
      throw new Error('这一轮还没结束，不能切换模型')
    }
    const preference = agentSessionProviderPreference(candidate)
    const sessionInstanceId = executorInstanceId(candidate)
    // 服务的地址、密钥和请求头可在两轮之间更新；旧会话身份不能证明当前配置仍相同。
    // 下一轮开始前按该会话自己的精确选择刷新身份，保留模型、强度与全部历史。
    if (!preference) {
      const status = await getAgentStatus({ timeoutMs: AGENT_CONTROL_TIMEOUT_MS }, signal)
      const runtimeInstanceId = typeof status.executor?.instance_id === 'string' ? status.executor.instance_id : null
      // 没有身份指纹时保留原有兼容行为；真正发生漂移时才原地 PATCH。
      if (!runtimeInstanceId || !sessionInstanceId || runtimeInstanceId === sessionInstanceId) return candidate
    }
    const updated = await updateAgentSession(
      candidate.session_id,
      preference
        ? { rebind_provider: true, provider_id: preference.providerId, model: preference.model, reasoning_effort: preference.reasoningEffort }
        : { rebind_provider: true },
      { timeoutMs: AGENT_CONTROL_TIMEOUT_MS },
      signal,
    )
    if (signal?.aborted) throw signal.reason
    adoptSession(updated)
    return updated
  }, [adoptSession])

  const restoreSession = useCallback(async (targetSession: AgentSession, signal?: AbortSignal, preserveHistory = false) => {
    const retained = preserveHistory && sessionRef.current?.session_id === targetSession.session_id
    const pending = readPendingAgentSend(targetSession.session_id)
    if (pending) claimPendingImageDraft(pending)
    if (pending) claimPendingSkillDraft(pending)
    let admission: RunCreateResponse | null = null
    let pendingView: PendingSendView | null = pending ? { item: pending, status: 'checking' } : null
    if (pending) {
      try {
        admission = await getAgentRunAdmission(pending.sessionId, pending.key, { timeoutMs: AGENT_CONTROL_TIMEOUT_MS }, signal)
        if (admission) { clearPendingAgentSend(pending); pendingView = null }
        else pendingView = { item: pending, status: 'not_found' }
      } catch (admissionError) {
        pendingView = { item: pending, status: isWithdrawnAdmission(admissionError) ? 'withdrawn' : 'unknown' }
      }
    }
    if (signal?.aborted) throw signal.reason
    const [messagePage, runs, checkpoint] = await Promise.all([
      retained
        ? readAgentMessageUpdates(targetSession.session_id, syncedMessagesRef.current.ordinal, { timeoutMs: AGENT_CONTROL_TIMEOUT_MS }, signal)
        : listAgentMessages(targetSession.session_id, { timeoutMs: AGENT_CONTROL_TIMEOUT_MS }, signal),
      listAgentRuns(targetSession.session_id, { timeoutMs: AGENT_CONTROL_TIMEOUT_MS }, signal),
      getLatestSessionCheckpoint(targetSession.session_id, { timeoutMs: AGENT_CONTROL_TIMEOUT_MS }, signal).catch(() => null),
    ])
    const latestRun = runs.at(-1) ?? admission?.run ?? null
    let restoredActivities: AgentActivity[] = []
    let restoredSequence = 0
    if (latestRun) {
      const activityPage = await listAgentActivities(latestRun.run_id, 0, { timeoutMs: AGENT_CONTROL_TIMEOUT_MS }, signal)
      if (activityPage.run_id !== latestRun.run_id) {
        throw new Error('本机服务返回的不是这一次处理的记录')
      }
      restoredActivities = activityPage.activities
      restoredSequence = activityPage.last_event_sequence ?? activityPage.last_sequence
    }
    if (signal?.aborted) throw signal.reason

    setSession(targetSession)
    rememberSession(projectId, targetSession.session_id)
    setPendingSend(pendingView)
    sessionRef.current = targetSession
    setMessageSyncError(null)
    setSyncingMessages(false)
    const restoredPage = admission ? { ...messagePage, messages: upsertMessage(messagePage.messages, admission.user_message) } : messagePage
    if (retained) {
      applyMessagePage(targetSession.session_id, restoredPage)
      stashActivities(latestRun?.run_id ?? null)
    } else {
      syncedMessagesRef.current = { sessionId: targetSession.session_id, ordinal: messagePage.messages.at(-1)?.ordinal ?? 0 }
      setMessages(restoredPage.messages)
      setRunProcessSummaries(messagePage.run_process_summaries ?? {})
      setRunOrigins(messagePage.run_origins ?? {})
      setReviewSources(messagePage.review_sources ?? {})
      setHasEarlierMessages(messagePage.has_earlier === true)
      setPastActivities({})
    }
    setLatestCheckpoint(checkpoint)
    seenActivitiesRef.current.clear()
    for (const activity of restoredActivities) seenActivitiesRef.current.add(activity.activity_id)
    replaceActivities(restoredActivities)
    setActiveRun(latestRun)
    activeRunRef.current = latestRun
    setContinuation(null)
    // 阶段说明已在正式消息中；尚未收尾的下一段按活动恢复。
    setStreamingContent(latestRun && latestRun.status !== 'succeeded' ? restoreStreamingContent(restoredActivities) : '')
    const restoredMessage = [...restoredActivities].reverse().find((activity) => activity.kind === 'message' && activity.payload.role === 'assistant')
    streamingMessageIdRef.current = restoredMessage?.kind === 'message' ? restoredMessage.payload.message_id : null
    streamingGenerationRef.current += 1
    streamRunIdRef.current = latestRun?.run_id ?? null
    streamCursorRef.current = latestRun && restoredSequence > 0 ? `${latestRun.run_id}:${restoredSequence}` : null
  }, [applyMessagePage, projectId, replaceActivities, stashActivities])

  const observingBackground = useRef<string | null>(null)
  const observeBackgroundRun = useCallback(async (candidate: AgentRun) => {
    const target = sessionRef.current
    const before = activeRunRef.current
    if (!target || candidate.session_id !== target.session_id || operationRef.current || observingBackground.current
      || (before && (!isTerminalAgentRun(before.status) || candidate.ordinal <= before.ordinal))) return
    const signal = scopeControllerRef.current?.signal
    observingBackground.current = candidate.run_id
    const inScope = () => !signal?.aborted && scopeControllerRef.current?.signal === signal
      && sessionRef.current?.session_id === target.session_id && activeRunRef.current?.run_id === before?.run_id
      && operationRef.current === null
    try {
      const [page, activityPage] = await Promise.all([
        readAgentMessageUpdates(target.session_id, syncedMessagesRef.current.sessionId === target.session_id ? syncedMessagesRef.current.ordinal : 0,
          { timeoutMs: AGENT_CONTROL_TIMEOUT_MS }, signal),
        listAgentActivities(candidate.run_id, 0, { timeoutMs: AGENT_CONTROL_TIMEOUT_MS }, signal),
      ])
      if (!inScope()) return
      if (activityPage.run_id !== candidate.run_id) throw new Error('后台接续返回的不是这次任务。')
      stashActivities(candidate.run_id)
      applyMessagePage(target.session_id, page)
      setRunOrigins((current) => ({ ...current, [candidate.run_id]: candidate.origin_kind ?? 'user' }))
      const nextActivities = activityPage.activities
      replaceActivities(nextActivities)
      seenActivitiesRef.current = new Set(nextActivities.map((activity) => activity.activity_id))
      streamRunIdRef.current = candidate.run_id
      const eventSequence = activityPage.last_event_sequence ?? activityPage.last_sequence
      streamCursorRef.current = eventSequence > 0 ? `${candidate.run_id}:${eventSequence}` : null
      setStreamingContent(candidate.status === 'succeeded' ? '' : restoreStreamingContent(nextActivities))
      streamingMessageIdRef.current = null
      streamingGenerationRef.current += 1
      activeRunRef.current = candidate
      setActiveRun(candidate)
    } catch (loadError) {
      if (inScope()) throw loadError
    } finally {
      if (observingBackground.current === candidate.run_id) observingBackground.current = null
    }
  }, [applyMessagePage, replaceActivities, stashActivities])

  const loadedProject = useRef<string | null | undefined>(undefined)
  // Connectivity changes pause observation; only a different project clears its history.
  useEffect(() => {
    const retained = loadedProject.current === projectId && sessionRef.current !== null
    loadedProject.current = projectId
    const controller = new AbortController()
    const statusController = new AbortController()
    scopeControllerRef.current?.abort()
    streamControllerRef.current?.abort()
    scopeControllerRef.current = controller
    let disposed = false
    setError(null)
    const clearHistory = () => {
      setPendingSend(null)
      setSessions([])
      setSession(null)
      setLatestCheckpoint(null)
      sessionRef.current = null
      sessionPromiseRef.current = null
      setMessages([])
      setRunProcessSummaries({})
      setRunOrigins({})
      setReviewSources({})
      setHasEarlierMessages(false)
      syncedMessagesRef.current = { sessionId: '', ordinal: 0 }
      setMessageSyncError(null)
      setSyncingMessages(false)
      replaceActivities([])
      setPastActivities({})
      setStreamingContent('')
      setActiveRun(null)
      setContinuation(null)
      streamCursorRef.current = null
      streamRunIdRef.current = null
      seenActivitiesRef.current.clear()
    }
    if (!retained) clearHistory()
    setSwitchingSession(false)
    setSyncingMessages(false)
    preparingContinuationRef.current = false

    if (!enabled) {
      setOperation(null)
      setAvailability('checking')
      setDetail('本机服务还没准备好')
      return () => {
        disposed = true
        controller.abort()
        if (scopeControllerRef.current === controller) scopeControllerRef.current = null
      }
    }

    setOperation('restoring')
    const load = async () => {
      // projectId 为空 = 「随便聊」：列出无项目会话，其余与项目会话同一套。
      const sessionsRequest: Promise<AgentSession[]> = projectId
        ? listAgentSessions(projectId, { timeoutMs: AGENT_CONTROL_TIMEOUT_MS }, controller.signal)
        : listUnboundAgentSessions({ timeoutMs: AGENT_CONTROL_TIMEOUT_MS }, controller.signal)
      const [statusResult, sessionsResult] = await Promise.allSettled([
        getAgentStatus({ timeoutMs: AGENT_CONTROL_TIMEOUT_MS }, statusController.signal),
        sessionsRequest,
      ])
      if (disposed) return
      if (statusResult.status === 'fulfilled') {
        setAvailability(statusResult.value.available ? 'available' : 'unavailable')
        setDetail(statusResult.value.detail)
      } else {
        setAvailability('error')
        setDetail(errorMessage(statusResult.reason))
      }
      if (controller.signal.aborted) return
      if (sessionsResult.status === 'rejected') throw sessionsResult.reason
      const allSessions = sessionsResult.value.filter((item) => item.status !== 'closed')
      setSessions(sortSessions(allSessions))
      const preferred = preferredIntent.current.used ? null : preferredSessionRef.current
      preferredIntent.current.used = true
      const remembered = selectedSession(projectId)
      const existing = (preferred ? allSessions.find((item) => item.session_id === preferred) : null)
        ?? (remembered ? allSessions.find((item) => item.session_id === remembered) : null)
        ?? [...allSessions]
          .filter((item) => ['active', 'idle'].includes(item.status))
          .sort((left, right) => Date.parse(right.updated_at) - Date.parse(left.updated_at))[0]
        ?? allSessions[0]
        ?? null
      if (existing) await restoreSession(existing, controller.signal, retained)
      else { clearHistory(); rememberSession(projectId, null) }
    }
    void load()
      .catch((loadError) => {
        if (disposed || controller.signal.aborted) return
        setAvailability('error')
        setDetail('Agent 的状态没读到')
        setError(errorMessage(loadError))
      })
      .finally(() => {
        if (!disposed && !controller.signal.aborted && operationRef.current === 'restoring') setOperation(null)
      })
    return () => {
      disposed = true
      controller.abort()
      statusController.abort()
      scopeControllerRef.current?.abort()
      streamControllerRef.current?.abort()
      scopeControllerRef.current = null
    }
  }, [enabled, projectId, refreshKey, replaceActivities, restoreSession, setOperation])

  // 这一轮结束了：问一下后台能不能「继续任务」（失败、中断、停下的那几种）。
  const continuationTarget = activeRun && activeRun.session_id === session?.session_id && isTerminalAgentRun(activeRun.status)
    ? activeRun.run_id
    : null
  useEffect(() => {
    setContinuation(null)
    if (!continuationTarget) return
    const controller = new AbortController()
    void getAgentRunContinuation(continuationTarget, {}, controller.signal).then((state) => {
      if (!controller.signal.aborted && activeRunRef.current?.run_id === continuationTarget) setContinuation(state)
    }).catch(() => { /* 未支持旧服务或暂时断线时不虚构可继续状态。 */ })
    return () => controller.abort()
  }, [continuationTarget, projectId])

  const restoreContinuedRun = useCallback(async (sourceRunId: string, signal?: AbortSignal): Promise<boolean> => {
    const origin = sessionRef.current
    if (!origin) return false
    const state = await getAgentRunContinuation(sourceRunId, {}, signal)
    if (signal?.aborted || sessionRef.current?.session_id !== origin.session_id) throw new DOMException('Aborted', 'AbortError')
    if (!state.continued_by_run_id || state.reason_code !== 'already_continued') return false
    await restoreSession(origin, signal)
    return true
  }, [restoreSession])

  const prepareContinuation = useCallback(async () => {
    const current = activeRunRef.current
    const originSession = sessionRef.current?.session_id
    if (!current || !isTerminalAgentRun(current.status) || operationRef.current || preparingContinuationRef.current) {
      throw new Error('Agent 正在处理，请稍后再继续')
    }
    preparingContinuationRef.current = true
    setOperation('restoring')
    const signal = scopeControllerRef.current?.signal
    try {
      const state = await getAgentRunContinuation(current.run_id, {}, signal)
      if (signal?.aborted || sessionRef.current?.session_id !== originSession || activeRunRef.current?.run_id !== current.run_id) {
        throw new DOMException('Aborted', 'AbortError')
      }
      if (state.continued_by_run_id && state.reason_code === 'already_continued') {
        await restoreSession(sessionRef.current!, signal)
        return null
      }
      if (!state.can_continue) throw new Error(state.reason || '这次任务不能继续')
      setContinuation(state)
      return state
    } finally {
      if (scopeControllerRef.current?.signal === signal) preparingContinuationRef.current = false
      releaseOperation('restoring', signal)
    }
  }, [releaseOperation, restoreSession, setOperation])

  // 正在跑的这一轮：接上事件流，断了自己重连，收尾后从后台读正式的对话记录。
  const streamTargetId = enabled && operation !== 'restoring' && !switchingSession && activeRun && activeRun.status !== 'paused' && !isTerminalAgentRun(activeRun.status)
    ? activeRun.run_id
    : null
  useEffect(() => {
    if (!streamTargetId) return
    const activeRunId = streamTargetId
    const targetSessionId = sessionRef.current?.session_id
    const controller = new AbortController()
    streamControllerRef.current = controller
    if (streamRunIdRef.current !== activeRunId) {
      streamRunIdRef.current = activeRunId
      streamCursorRef.current = null
      seenActivitiesRef.current.clear()
      stashActivities(activeRunId)
      replaceActivities([])
      setStreamingContent('')
      streamingMessageIdRef.current = null
      streamingGenerationRef.current += 1
    }

    const buffer = createActivityBuffer((batch) => {
      if (sessionRef.current?.session_id !== targetSessionId || streamRunIdRef.current !== activeRunId) return
      replaceActivities([...activitiesRef.current, ...batch])
      let resetContent: string | undefined
      let appendContent = ''
      let refreshHistory = false
      let completedGeneration: number | undefined
      let latestState: { status: AgentRunStatePayload['status']; updated_at: string } | undefined
      for (const activity of batch) {
        if (activity.kind === 'message') {
          const payload = activity.payload as AgentMessagePayload
          if (payload.role === 'user' && payload.phase === 'completed') {
            // 同轮插话是消息边界，旧段已经独立落盘。
            resetContent = ''; appendContent = ''
            streamingMessageIdRef.current = null
            streamingGenerationRef.current += 1
            completedGeneration = undefined
            refreshHistory = true
          } else if (payload.role === 'assistant' && payload.phase === 'delta' && payload.delta) {
            if (payload.message_id && payload.message_id !== streamingMessageIdRef.current) {
              resetContent = ''; appendContent = ''
            }
            streamingMessageIdRef.current = payload.message_id
            streamingGenerationRef.current += 1
            completedGeneration = undefined
            appendContent += payload.delta
          } else if (payload.role === 'assistant' && payload.phase === 'completed' && payload.content !== null) {
            streamingMessageIdRef.current = payload.message_id
            completedGeneration = ++streamingGenerationRef.current
            resetContent = payload.content; appendContent = ''
            refreshHistory = true
          }
        } else if (activity.kind === 'run_state') {
          const payload = activity.payload as AgentRunStatePayload
          if (!isTerminalAgentRun(payload.status)) latestState = { status: payload.status, updated_at: activity.created_at }
        }
      }
      if (resetContent !== undefined || appendContent) setStreamingContent((current) => (resetContent ?? current) + appendContent)
      if (latestState) setActiveRun((current) => current ? { ...current, ...latestState } : current)
      if (refreshHistory) void refreshMessages().then((loaded) => {
        if (loaded && completedGeneration !== undefined && completedGeneration === streamingGenerationRef.current
          && !controller.signal.aborted) setStreamingContent('')
      })
    })
    // 导航、停止观察或卸载前同步交付已接收尾段，旧计时器不留到新会话。
    controller.signal.addEventListener('abort', buffer.flush, { once: true })
    const consume = async () => {
      let completed = false
      let completedRun: AgentRun | null = null
      while (!controller.signal.aborted && !completed) {
        try {
          await streamAgentRunEvents(activeRunId, {
            signal: controller.signal,
            lastEventId: streamCursorRef.current,
            onEvent: (event) => {
              if (controller.signal.aborted || sessionRef.current?.session_id !== targetSessionId
                || activeRunRef.current?.run_id !== activeRunId) return
              streamCursorRef.current = event.event_id
              if (event.event_type === 'done' && event.data.run) {
                buffer.flush()
                completed = true
                completedRun = event.data.run
                return
              }
              const activity = event.data.activity
              if (!activity || seenActivitiesRef.current.has(activity.activity_id)) return
              seenActivitiesRef.current.add(activity.activity_id)
              buffer.push(activity)
              // 消息边界立即可见；连续增量按同一批更新正文和过程。
              if (activity.kind === 'message' && activity.payload.phase === 'completed') buffer.flush()
            },
          })
          buffer.flush()
          if (!completed && !controller.signal.aborted) {
            // 流是正常回来（服务器收尾/瞬断后重连成功）才走到这里，
            // 把上一轮挂着的「连接中断」横幅摘掉，不要一直挂到这一轮结束。
            setError(null)
            await new Promise((resolve) => window.setTimeout(resolve, 250))
          }
        } catch (streamError) {
          buffer.flush()
          if (controller.signal.aborted) return
          setError(`Agent 流连接中断，正在恢复：${errorMessage(streamError)}`)
          await new Promise((resolve) => window.setTimeout(resolve, 500))
        }
      }
      const currentSession = sessionRef.current
      if (completed && currentSession?.session_id === targetSessionId && currentSession && !controller.signal.aborted) {
        try {
          const loaded = await refreshMessages()
          if (controller.signal.aborted || sessionRef.current?.session_id !== currentSession.session_id) return
          // 失败 / 停下的一轮可能已经流出了一段能用的回答，留着；成功的一轮以存下的回答为准。
          if (loaded && (completedRun as AgentRun | null)?.status === 'succeeded') setStreamingContent('')
          setError(null)
        } catch (historyError) {
          if (!controller.signal.aborted) setError(errorMessage(historyError))
        } finally {
          if (!controller.signal.aborted && completedRun) setActiveRun(completedRun)
        }
      }
    }
    void consume()
    return () => {
      controller.abort()
      if (streamControllerRef.current === controller) streamControllerRef.current = null
    }
  }, [observationEpoch, refreshMessages, replaceActivities, stashActivities, streamTargetId])

  /** 这本书还没有对话就现开一条（同一时刻只开一次）。 */
  const requireBookSession = useCallback(async (projectId: string, signal?: AbortSignal): Promise<AgentSession> => {
    if (sessionRef.current) return sessionRef.current
    const pending = sessionPromiseRef.current
    if (pending?.projectId === projectId) return pending.promise
    // 占位名字和「新对话」按钮开的一样：第一轮收尾时后台会按第一句话起个正经名字（以前固定叫「写作对话」，一直不改名）。
    const promise = createAgentSession(projectId, placeholderSessionTitle(), { timeoutMs: AGENT_CONTROL_TIMEOUT_MS }, signal)
      .then((created) => {
        if (signal?.aborted) throw signal.reason
        sessionRef.current = created
        setSession(created)
        rememberSession(projectId, created.session_id)
        setSessions((current) => sortSessions([created, ...current.filter((item) => item.session_id !== created.session_id)]))
        return created
      })
    const entry = { projectId: projectId, promise }
    sessionPromiseRef.current = entry
    void promise.finally(() => {
      if (sessionPromiseRef.current === entry) sessionPromiseRef.current = null
    }).catch(() => undefined)
    return promise
  }, [])

  const adoptAdmission = useCallback((result: RunCreateResponse) => {
    setMessages((existing) => upsertMessage(existing, result.user_message))
    stashActivities(result.run.run_id)
    replaceActivities([])
    setStreamingContent('')
    seenActivitiesRef.current.clear()
    streamCursorRef.current = null
    streamRunIdRef.current = result.run.run_id
    activeRunRef.current = result.run
    setActiveRun(result.run)
    setPendingSend(null)
    setError(null)
  }, [replaceActivities, stashActivities])

  const transmitPending = useCallback(async (pending: PendingAgentSend, signal?: AbortSignal, isRetry = false) => {
    const inScope = () => !signal?.aborted && sessionRef.current?.session_id === pending.sessionId
    let result: RunCreateResponse
    try {
      result = await createAgentRun(pending.sessionId, pending.request, pending.key, { timeoutMs: AGENT_CONTROL_TIMEOUT_MS }, signal)
    } catch (sendError) {
      if (!inScope()) throw new AgentSendUnconfirmedError('发送结果尚未确认，原消息已保留，回到这条对话后会继续核对。')
      setPendingSend({ item: pending, status: 'checking' })
      let admission: RunCreateResponse | null
      try {
        admission = await getAgentRunAdmission(pending.sessionId, pending.key, { timeoutMs: AGENT_CONTROL_TIMEOUT_MS }, signal)
      } catch (admissionError) {
        if (isWithdrawnAdmission(admissionError)) {
          if (inScope()) setPendingSend({ item: pending, status: 'withdrawn' })
          throw new AgentSendUnconfirmedError('这条发送已取回，原文仍保留，可以重新编辑。')
        }
        if (inScope()) setPendingSend({ item: pending, status: 'unknown' })
        throw new AgentSendUnconfirmedError('发送结果尚未确认，原消息已保留。可以核对发送结果，或重试这条消息。')
      }
      if (!admission) {
        // 完整错误响应可确认这个 POST 被拒绝；网络故障/超时仍可能晚到。
        if (sendError instanceof RuntimeApiError && sendError.status >= 400 && sendError.status < 500
          && sendError.status !== 408) {
          if (isRetry) {
            // 这次重试被拒绝不能证明更早的网络请求永不晚到，保留原身份。
            if (inScope()) setPendingSend({ item: pending, status: 'not_found' })
            throw sendError
          }
          clearPendingAgentSend(pending)
          if (inScope()) setPendingSend(null)
          throw sendError
        }
        if (inScope()) setPendingSend({ item: pending, status: 'not_found' })
        throw new AgentSendUnconfirmedError('暂未查到这条消息，原内容已保留。重试会沿用同一条发送记录。')
      }
      result = admission
    }
    clearPendingAgentSend(pending)
    if (inScope()) {
      adoptAdmission(result)
      // ACK 可能很晚才恢复；终态没有事件流，补读已经保存的完整回答。
      if (isTerminalAgentRun(result.run.status)) await refreshMessages()
    }
  }, [adoptAdmission, refreshMessages])

  const resolvePendingSend = useCallback(async (retry: boolean) => {
    const origin = sessionRef.current
    if (!origin || operationRef.current) return
    const pending = readPendingAgentSend(origin.session_id)
    if (!pending) { setPendingSend(null); return }
    setOperation('sending')
    setPendingSend({ item: pending, status: 'checking' })
    setError(null)
    const signal = scopeControllerRef.current?.signal
    const inScope = () => !signal?.aborted && sessionRef.current?.session_id === origin.session_id
    try {
      const admission = await getAgentRunAdmission(pending.sessionId, pending.key, { timeoutMs: AGENT_CONTROL_TIMEOUT_MS }, signal)
      if (admission) {
        clearPendingAgentSend(pending)
        if (inScope()) {
          adoptAdmission(admission)
          await restoreSession(origin, signal)
        }
        return
      }
      if (!inScope()) return
      setPendingSend({ item: pending, status: 'not_found' })
      if (!retry) return
      if (activeRunRef.current && !isTerminalAgentRun(activeRunRef.current.status)) throw new Error('当前任务仍在运行，请稍后再重试这条消息')
      // 重试使用当时的模型和权限，不读取后来编辑过的草稿或附加项。
      if (pending.provider && JSON.stringify(agentSessionProviderPreference(origin)) !== JSON.stringify(pending.provider)) {
        const updated = await updateAgentSession(origin.session_id, {
          rebind_provider: true, provider_id: pending.provider.providerId, model: pending.provider.model,
          reasoning_effort: pending.provider.reasoningEffort,
        }, { timeoutMs: AGENT_CONTROL_TIMEOUT_MS }, signal)
        if (!inScope()) return
        adoptSession(updated)
      } else await ensureProviderBound(origin, signal)
      if (!inScope()) return
      await transmitPending(pending, signal, true)
    } catch (pendingError) {
      if (inScope()) {
        const remaining = readPendingAgentSend(origin.session_id)
        if (remaining) setPendingSend((current) => isWithdrawnAdmission(pendingError)
          ? { item: remaining, status: 'withdrawn' }
          : current?.status === 'checking' ? { item: remaining, status: 'unknown' } : current)
        if (!isWithdrawnAdmission(pendingError)) setError(errorMessage(pendingError))
      }
    } finally { releaseOperation('sending', signal) }
  }, [adoptAdmission, adoptSession, ensureProviderBound, releaseOperation, restoreSession, setOperation, transmitPending])

  const checkPendingSend = useCallback(() => resolvePendingSend(false), [resolvePendingSend])
  const retryPendingSend = useCallback(() => resolvePendingSend(true), [resolvePendingSend])
  const withdrawPendingSend = useCallback(async (): Promise<string | null> => {
    const origin = sessionRef.current
    if (!origin || operationRef.current) return null
    const pending = readPendingAgentSend(origin.session_id)
    if (!pending) return null
    setOperation('sending')
    setPendingSend({ item: pending, status: 'checking' })
    setError(null)
    const signal = scopeControllerRef.current?.signal
    const inScope = () => !signal?.aborted && sessionRef.current?.session_id === origin.session_id
    try {
      const result = await withdrawAgentRunAdmission(pending.sessionId, pending.key, { timeoutMs: AGENT_CONTROL_TIMEOUT_MS }, signal)
      if (result.state === 'accepted') {
        clearPendingAgentSend(pending)
        if (inScope()) {
          adoptAdmission(result.accepted)
          await restoreSession(origin, signal)
        }
        return null
      }
      // 切走时保留原文；回来 GET 会读到已撤销，可再取回，迟到请求不能新开任务。
      if (!inScope()) return null
      clearPendingAgentSend(pending)
      setPendingSend(null)
      return pending.request.message
    } catch (withdrawError) {
      if (inScope()) {
        if (readPendingAgentSend(origin.session_id)) {
          setPendingSend({ item: pending, status: 'unknown' })
          setError(`取回结果尚未确认，原文仍保留：${errorMessage(withdrawError)}`)
        } else setError(`消息已接收，恢复对话记录时遇到问题：${errorMessage(withdrawError)}`)
      }
      return null
    } finally { releaseOperation('sending', signal) }
  }, [adoptAdmission, releaseOperation, restoreSession, setOperation])
  useEffect(() => {
    if (!pendingSendState || pendingSendState.item.sessionId !== session?.session_id) return
    const reconcile = () => { void resolvePendingSend(false) }
    window.addEventListener('online', reconcile)
    window.addEventListener('focus', reconcile)
    return () => { window.removeEventListener('online', reconcile); window.removeEventListener('focus', reconcile) }
  }, [pendingSendState, resolvePendingSend, session?.session_id])

  const send = useCallback(async ({
    text,
    manualFileIds = [],
    skillNames = [],
    imageIds = [],
    images: imageAttachments,
    imageDraftIdentity,
    skillDraftIdentity,
    writerMode = 'repair_only',
    continuationSourceRunId,
    reviewSourceRunId,
    goal,
  }: AgentSendRequest) => {
    const message = text.trim()
    if (!message && imageIds.length === 0) throw new Error('先写点什么或添加图片再发送')
    if (operationRef.current !== null) throw new Error('Agent 正在处理，请稍后再发送')
    if (availability !== 'available') throw new Error(detail)
    const current = activeRunRef.current
    if (current && !isTerminalAgentRun(current.status)) throw new Error('上一条回复还在生成，稍等一下')
    if (continuationSourceRunId && current?.run_id !== continuationSourceRunId) throw new Error('当前任务已经切换，请重新继续')
    if (projectId && (projectRevision === null || !projectFingerprint)) throw new Error('这个文件夹还没读完')
    if (sessionRef.current && readPendingAgentSend(sessionRef.current.session_id)) {
      throw new Error('上一条消息的发送结果还没确认，请先核对发送结果或重试保留的那条消息。')
    }

    setOperation('sending')
    setError(null)
    const signal = scopeControllerRef.current?.signal
    const control = { timeoutMs: AGENT_CONTROL_TIMEOUT_MS }
    const skills = skillNames.length > 0 ? { manual_skill_names: skillNames } : {}
    const images = imageIds.length > 0 ? { image_ids: [...new Set(imageIds)] } : {}
    const continued = continuationSourceRunId ? { continuation_source_run_id: continuationSourceRunId } : {}
    const reviewed = reviewSourceRunId ? { review_source_run_id: reviewSourceRunId } : {}
    try {
      let target: AgentSession
      let request: PendingAgentSend['request']
      if (projectId && projectRevision !== null && projectFingerprint) {
        target = await ensureProviderBound(await requireBookSession(projectId, signal), signal)
        let revision = projectRevision
        let fingerprint = projectFingerprint
        // 文件改动后的界面刷新可能尚未结束；停止命令也可能已经写过文件。
        // 这类轮次之后先读当前快照，再发下一轮。纯聊天与纯读取仍直接发送。
        const previousMayHaveChangedFiles = activitiesRef.current.some((activity) => (
          activity.session_id === target.session_id && activity.run_id === current?.run_id
          && (activity.kind === 'command' || (activity.kind === 'tool_call' && FILE_EFFECT_TOOLS.has(activity.payload.tool_name)))
        ))
        if (previousMayHaveChangedFiles) {
          const latest = await inspectBoundProject(projectId, { signal })
          if (signal?.aborted) throw signal.reason
          if (latest.project.project_id !== projectId) throw new Error('读取到的不是当前文件夹，请重新打开后再试')
          revision = latest.project.revision
          fingerprint = latest.project.fingerprint
        }
        // 不再每轮先编资料包（纠偏指令 3.9）：直接发这一轮，只带开着哪个文件、亲手附上了哪些文件；
        // 后端在对话开头带上文件夹里的说明文件（AGENTS.md 等）和用户记忆，别的模型要用自己读。
        request = {
            message,
            expected_project_revision: revision,
            expected_project_fingerprint: fingerprint,
            ...(openFilePath ? { open_file_path: openFilePath } : {}),
            manual_file_ids: [...new Set(manualFileIds)].sort(),
            writer_mode: writerMode,
            ...continued,
            ...reviewed,
            ...skills,
            ...images,
        }
      } else {
        let casual = sessionRef.current
        if (!casual) {
          casual = await createUnboundAgentSession('新对话', control, signal)
          if (signal?.aborted) throw signal.reason
          sessionRef.current = casual
          setSession(casual)
          rememberSession(null, casual.session_id)
        }
        target = await ensureProviderBound(casual, signal)
          // 不绑书也按作者选的挡位发：「只读」时后台不给执行命令、不写笔记。
        request = { message, writer_mode: writerMode, ...continued, ...reviewed, ...skills, ...images }
      }
      if (goal) request.goal = goal
      if (signal?.aborted) throw signal.reason
      const pending: PendingAgentSend = {
        version: 1, sessionId: target.session_id,
        key: continuationSourceRunId ? `continue:${continuationSourceRunId}` : newIdempotencyKey('turn'),
        request, provider: agentSessionProviderPreference(target), createdAt: new Date().toISOString(),
        ...(imageAttachments?.length ? { images: imageAttachments } : {}),
        ...(imageDraftIdentity ? { imageDraft: imageDraftIdentity } : {}),
        ...(skillDraftIdentity ? { skillDraft: skillDraftIdentity } : {}),
      }
      const saved = savePendingAgentSend(pending)
      if (saved.persistent === false) setPendingSend({ item: saved, status: 'checking' })
      await transmitPending(saved, signal)
    } catch (sendError) {
      if (continuationSourceRunId && !signal?.aborted) {
        try {
          if (await restoreContinuedRun(continuationSourceRunId, signal)) {
            const pending = sessionRef.current && readPendingAgentSend(sessionRef.current.session_id)
            if (pending) clearPendingAgentSend(pending)
            setPendingSend(null)
            return
          }
        } catch { /* 下次继续先核实，不自动重复发出任务。 */ }
      }
      if (!signal?.aborted) setError(errorMessage(sendError))
      throw sendError
    } finally {
      releaseOperation('sending', signal)
    }
  }, [
    availability,
    detail,
    ensureProviderBound,
    openFilePath,
    projectFingerprint,
    projectId,
    projectRevision,
    releaseOperation,
    requireBookSession,
    restoreContinuedRun,
    setOperation,
    transmitPending,
  ])

  const cancel = useCallback(async () => {
    const current = activeRunRef.current
    if (!current || current.status === 'paused' || isTerminalAgentRun(current.status)) return
    const signal = scopeControllerRef.current?.signal
    const stillCurrent = () => !signal?.aborted && scopeControllerRef.current?.signal === signal
      && sessionRef.current?.session_id === current.session_id
      && activeRunRef.current?.run_id === current.run_id
      && !isTerminalAgentRun(activeRunRef.current.status)
    try {
      const cancelled = await cancelAgentRun(current.run_id, { timeoutMs: AGENT_CONTROL_TIMEOUT_MS }, signal)
      if (!stillCurrent() || cancelled.run_id !== current.run_id || cancelled.session_id !== current.session_id) return
      // 取消接口可能先于事件流返回终态。继续收取最后的工具结果和文件版本，
      // 和自然结束一样，由 SSE done 完成这一轮，避免提前关闭流丢掉已发生的改动。
      if (!isTerminalAgentRun(cancelled.status)) setActiveRun((latest) => (
        latest?.run_id === current.run_id && !isTerminalAgentRun(latest.status) ? cancelled : latest
      ))
    } catch (cancelError) {
      if (stillCurrent()) setError(errorMessage(cancelError))
    }
  }, [])

  const rebindProvider = useCallback(async (
    settings?: Pick<ProviderSettingsView, 'provider_id' | 'model' | 'reasoning_effort'>,
  ) => {
    const current = sessionRef.current
    if (!current) return
    if (operationRef.current !== null) throw new Error('Agent 正在处理，请稍后再换模型')
    if (activeRunRef.current && !isTerminalAgentRun(activeRunRef.current.status)) {
      throw new Error('当前任务尚未结束，请先停止或等待任务完成')
    }
    const signal = scopeControllerRef.current?.signal
    setOperation('restoring')
    setSwitchingSession(true)
    setError(null)
    try {
      const updated = await updateAgentSession(
        current.session_id,
        {
          rebind_provider: true,
          provider_id: settings?.provider_id || undefined,
          model: settings?.model || undefined,
          reasoning_effort: settings?.reasoning_effort,
        },
        { timeoutMs: AGENT_CONTROL_TIMEOUT_MS },
        signal,
      )
      if (signal?.aborted || sessionRef.current?.session_id !== current.session_id) throw new DOMException('Aborted', 'AbortError')
      adoptSession(updated)
    } catch (rebindError) {
      if (!signal?.aborted) setError(errorMessage(rebindError))
      throw rebindError
    } finally {
      if (scopeControllerRef.current?.signal === signal) setSwitchingSession(false)
      releaseOperation('restoring', signal)
    }
  }, [adoptSession, releaseOperation, setOperation])

  /** 修改当前任务历史前核对状态；查看或新建另一条对话不受它约束。 */
  const requireIdle = useCallback((action: string) => {
    if (operationRef.current !== null) throw new AgentBusyError(`Agent 正在处理，请稍后再${action}`)
    if (activeRunRef.current && !isTerminalAgentRun(activeRunRef.current.status)) {
      throw new Error(`当前任务尚未结束，不能${action}`)
    }
  }, [])

  const selectConversation = useCallback(async (sessionId: string) => {
    const targetId = sessionId.trim()
    if (!targetId || (targetId === sessionRef.current?.session_id && operationRef.current !== 'restoring')) return
    const signal = beginNavigation()
    try {
      const latest = await getAgentSession(targetId, { timeoutMs: AGENT_CONTROL_TIMEOUT_MS }, signal)
      if (signal.aborted) return
      // 阅读和接回事件流不修改模型绑定；下一次发送时再刷新原会话的连接。
      await restoreSession(latest, signal)
      setSessions((current) => sortSessions(current.map((item) => (item.session_id === latest.session_id ? latest : item))))
    } catch (switchError) {
      if (signal.aborted) return
      setError(errorMessage(switchError))
      throw switchError
    } finally {
      if (scopeControllerRef.current?.signal === signal) setSwitchingSession(false)
      releaseOperation('restoring', signal)
    }
  }, [beginNavigation, releaseOperation, restoreSession])

  const createConversation = useCallback(async (customTitle?: string) => {
    const signal = beginNavigation()
    const title = customTitle?.trim() || placeholderSessionTitle()
    try {
      // 「随便聊」与项目会话走同一套会话/运行/事件流，只是不绑定项目。
      const created = projectId
        ? await createAgentSession(projectId, title, { timeoutMs: AGENT_CONTROL_TIMEOUT_MS }, signal)
        : await createUnboundAgentSession(title, { timeoutMs: AGENT_CONTROL_TIMEOUT_MS }, signal)
      await restoreSession(created, signal)
      setSessions((current) => sortSessions([created, ...current.filter((item) => item.session_id !== created.session_id)]))
      return created
    } catch (createError) {
      if (signal.aborted) return
      setError(errorMessage(createError))
      throw createError
    } finally {
      if (scopeControllerRef.current?.signal === signal) setSwitchingSession(false)
      releaseOperation('restoring', signal)
    }
  }, [beginNavigation, projectId, releaseOperation, restoreSession])

  const renameConversation = useCallback(async (sessionId: string, title: string) => {
    const normalized = title.trim()
    if (!normalized) throw new Error('对话名称不能为空')
    try {
      const updated = await updateAgentSession(sessionId, { title: normalized }, { timeoutMs: AGENT_CONTROL_TIMEOUT_MS }, scopeControllerRef.current?.signal)
      setSessions((current) => sortSessions(current.map((item) => (item.session_id === updated.session_id ? updated : item))))
      if (sessionRef.current?.session_id === updated.session_id) {
        sessionRef.current = updated
        setSession(updated)
      }
      setError(null)
    } catch (renameError) {
      setError(errorMessage(renameError))
      throw renameError
    }
  }, [])

  const setConversationPinned = useCallback(async (sessionId: string, pinned: boolean) => {
    try {
      const updated = await updateAgentSession(sessionId, { pinned }, { timeoutMs: AGENT_CONTROL_TIMEOUT_MS }, scopeControllerRef.current?.signal)
      setSessions((current) => sortSessions(current.map((item) => (item.session_id === updated.session_id ? updated : item))))
      if (sessionRef.current?.session_id === updated.session_id) {
        sessionRef.current = updated
        setSession(updated)
      }
      setError(null)
    } catch (pinError) {
      setError(errorMessage(pinError))
      throw pinError
    }
  }, [])

  const deleteConversation = useCallback(async (sessionId: string) => {
    const targetId = sessionId.trim()
    if (!targetId) throw new Error('找不到要删除的对话')
    const deletingCurrent = sessionRef.current?.session_id === targetId
    if (deletingCurrent) requireIdle('删除对话')
    try {
      await deleteAgentSession(targetId, { timeoutMs: AGENT_CONTROL_TIMEOUT_MS }, scopeControllerRef.current?.signal)
      setSessions((current) => current.filter((item) => item.session_id !== targetId))
      if (deletingCurrent) setRefreshKey((current) => current + 1)
      setError(null)
    } catch (deleteError) {
      setError(errorMessage(deleteError))
      throw deleteError
    }
  }, [requireIdle])

  const forkConversation = useCallback(async (upToMessageId: string, title?: string) => {
    const current = sessionRef.current
    if (!current) throw new Error('当前没有打开的对话')
    requireIdle('分叉对话')
    setOperation('restoring')
    setSwitchingSession(true)
    setError(null)
    const signal = scopeControllerRef.current?.signal
    try {
      const forked = await forkAgentSession(current.session_id, upToMessageId, title, { timeoutMs: AGENT_CONTROL_TIMEOUT_MS }, signal)
      if (signal?.aborted || sessionRef.current?.session_id !== current.session_id) throw new DOMException('Aborted', 'AbortError')
      await restoreSession(forked, signal)
      setSessions((existing) => sortSessions([forked, ...existing.filter((item) => item.session_id !== forked.session_id)]))
      return forked
    } catch (forkError) {
      if (!signal?.aborted) setError(errorMessage(forkError))
      throw forkError
    } finally {
      if (scopeControllerRef.current?.signal === signal) setSwitchingSession(false)
      releaseOperation('restoring', signal)
    }
  }, [releaseOperation, requireIdle, restoreSession, setOperation])

  const compactConversation = useCallback(async () => {
    const current = sessionRef.current
    if (!current) throw new Error('当前没有打开的对话')
    requireIdle('压缩对话')
    setOperation('compacting')
    setError(null)
    const signal = scopeControllerRef.current?.signal
    try {
      const checkpoint = await compactAgentSession(current.session_id, { timeoutMs: 120_000 }, signal)
      if (signal?.aborted || sessionRef.current?.session_id !== current.session_id) throw new DOMException('Aborted', 'AbortError')
      setLatestCheckpoint(checkpoint)
      return checkpoint
    } catch (compactError) {
      if (!signal?.aborted) setError(errorMessage(compactError))
      throw compactError
    } finally {
      releaseOperation('compacting', signal)
    }
  }, [releaseOperation, requireIdle, setOperation])

  const loadRunActivities = useCallback(async (runId: string) => {
    const signal = scopeControllerRef.current?.signal
    const page = await listAgentActivities(runId, 0, { timeoutMs: AGENT_CONTROL_TIMEOUT_MS }, signal)
    if (signal?.aborted) return
    if (page.run_id !== runId) throw new Error('本机服务返回的不是这一轮的记录')
    setPastActivities((past) => ({ ...past, [runId]: page.activities }))
  }, [])

  const loadEarlierMessages = useCallback(async () => {
    const current = sessionRef.current
    const first = messagesRef.current[0]
    if (!current || !first) return
    const signal = scopeControllerRef.current?.signal
    const page = await listAgentMessages(
      current.session_id,
      { timeoutMs: AGENT_CONTROL_TIMEOUT_MS },
      signal,
      first.ordinal,
    )
    if (signal?.aborted || sessionRef.current?.session_id !== current.session_id) return
    setRunProcessSummaries((current) => ({ ...current, ...page.run_process_summaries }))
    setRunOrigins((current) => ({ ...current, ...page.run_origins }))
    setReviewSources((current) => ({ ...current, ...page.review_sources }))
    setMessages((existing) => {
      const oldest = existing[0]?.ordinal ?? Number.POSITIVE_INFINITY
      return [...page.messages.filter((message) => message.ordinal < oldest), ...existing]
    })
    setHasEarlierMessages(page.has_earlier === true)
  }, [])

  const running = activeRun !== null && activeRun.status !== 'paused' && !isTerminalAgentRun(activeRun.status)
  const busy = operation !== null || running
  const contextState = operation ?? 'idle'
  const pendingSend = pendingSendState?.item.sessionId === session?.session_id ? pendingSendState : null
  return useMemo(() => ({
    availability,
    messageSyncError,
    syncingMessages,
    detail,
    sessions,
    session,
    messages,
    hasEarlierMessages,
    loadEarlierMessages,
    streamingContent,
    activeRun,
    continuation,
    activities,
    pastActivities,
    runProcessSummaries,
    runOrigins,
    reviewSources,
    observeBackgroundRun,
    loadRunActivities,
    contextState,
    running,
    switchingSession,
    busy,
    error,
    latestCheckpoint,
    send,
    pendingSend,
    checkPendingSend,
    retryPendingSend,
    withdrawPendingSend,
    cancel,
    prepareContinuation,
    rebindProvider,
    createConversation,
    forkConversation,
    compactConversation,
    selectConversation,
    renameConversation,
    setConversationPinned,
    deleteConversation,
    refresh,
    refreshMessages,
  }), [
    activeRun,
    activities,
    availability,
    busy,
    checkPendingSend,
    cancel,
    compactConversation,
    contextState,
    continuation,
    createConversation,
    deleteConversation,
    detail,
    error,
    forkConversation,
    hasEarlierMessages,
    latestCheckpoint,
    loadEarlierMessages,
    loadRunActivities,
    messages,
    messageSyncError,
    syncingMessages,
    pastActivities,
    runProcessSummaries,
    runOrigins,
    reviewSources,
    observeBackgroundRun,
    pendingSend,
    prepareContinuation,
    rebindProvider,
    refresh,
    refreshMessages,
    renameConversation,
    running,
    retryPendingSend,
    selectConversation,
    send,
    session,
    sessions,
    setConversationPinned,
    streamingContent,
    switchingSession,
    withdrawPendingSend,
  ])
}
