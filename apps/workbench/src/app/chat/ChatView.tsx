import { createContext, useContext, useCallback, useEffect, useImperativeHandle, useLayoutEffect, useMemo, useRef, useState, type ComponentProps, type ReactNode, type Ref } from 'react'
import { Archive, ArrowDown, ChevronDown, FileDiff, Folder, FolderOpen, FolderPlus, Globe, PanelLeftOpen, PanelRightClose, PanelRightOpen, SquarePen, SquareTerminal } from 'lucide-react'
import { agentSessionProviderPreference, type AgentActivity, type AgentImageAttachment, type AgentMessage } from '../../api/agent'
import { type ProjectInspection } from '../../api/projects'
import type { RuntimeSnapshot } from '../../api/runtime'
import { readUserSkills, type UserSkillsView } from '../../api/userSkills'
import { subscribeSkillsChanged } from '../lib/skillsChanged'
import { authorFacingError, authorFacingErrorText } from '../../lib/authorFacingError'
import { presentAiText } from '../../lib/humanizeAiText'
import { AgentSendUnconfirmedError, type AgentConversationState } from '../../hooks/useAgentConversation'
import { useChangeCards } from './useChangeCards'
import { readAgentAccessMode, writeAgentAccessMode, type AgentAccessMode } from '../lib/accessMode'
import { agentFileEffects } from '../lib/agentFileEffects'
import { resolveFileNavigation } from '../lib/fileNavigation'
import type { FileLocation } from '../lib/fileReferences'
import { useConversationDraft } from '../lib/useConversationDraft'
import { useConversationSkills } from '../lib/useConversationSkills'
import { useConversationImages } from '../lib/useConversationImages'
import type { ProjectConversationGroup } from '../lib/useConversations'
import type { ProviderSettingsApi } from '../lib/useProviderSettings'
import { T } from '../strings'
import { A } from '../text'
import { Btn, IconBtn, Menu, type AskConfirm, type Notify } from '../ui'
import './chat.css'
import { ChangeCard } from './ChangeCard'
import { Composer } from './Composer'
import { mentionedFileIds, type CommandAction } from './composerLogic'
import { FailureNotice } from './FailureNotice'
import { AgentMessageView, HistoryProcessLookup, LiveTurn, ProcessBeforeUser, ProcessGroup, UserMessage } from './Message'
import { useBackgroundEvents } from './useBackgroundEvents'
import { CommandCompletions, CompletionOrigin } from './CommandCompletions'
import { Markdown } from './Markdown'
import { ModelControls } from './models'
import { thoughtBlocksOf, stepRows, turnTailActivities } from './stepRows'
import { useContextState } from './useContextState'
import { ContextStatus } from './ContextStatus'
import { useMessageQueue } from './useMessageQueue'
import type { QueueInput } from '../../api/agentQueue'
import { QueuePanel } from './QueuePanel'
import { MessageImages } from './ImageAttachments'
import { StartView } from './StartView'
import { ConversationRail } from './ConversationRail'
import { useMemoryChanges } from './useMemoryChanges'
import { MemoryChanges } from './MemoryChanges'
import { ConversationSources, SourceLink } from './ConversationSources'
import { useConversationScroll } from './useConversationScroll'
import { useMessageLayout } from './useMessageLayout'
import { HistoryMount } from './HistoryMount'
import { processActivities, useProcessActivities } from './useProcessActivities'
import { appendFileQuotation, type FileQuotation } from '../lib/fileQuotation'
import { SessionPlan } from './SessionPlan'
import { SessionGoal } from './SessionGoal'
import { RunArtifacts } from './RunArtifacts'
import { MessageDetails } from './MessageDetails'

const NO_ACTIVITIES: AgentActivity[] = []
const StreamingText = createContext('')

/** Only the live tail consumes token updates; historical message elements stay stable. */
function HistoryLiveTurn(props: Omit<ComponentProps<typeof LiveTurn>, 'streaming'>) {
  const streaming = useContext(StreamingText)
  return <LiveTurn {...props} streaming={streaming} />
}

export interface ChatComposerHandle {
  quoteFile: (quote: FileQuotation) => void
  captureDraftTransfer: () => (projectId: string | null, sessionId: string) => void
}

export interface ChatViewProps {
  composerRef?: Ref<ChatComposerHandle>
  conversation: AgentConversationState
  projectId: string | null
  snapshot: ProjectInspection | null
  projectState: 'idle' | 'loading' | 'ready' | 'unbound' | 'error'
  projectError: string | null
  /** 右栏开着的文件（相对路径）：顶上写「开着：…」。 */
  openFileLabel: string | null
  onProjectChanged: () => void
  onChangesChanged: () => void
  changesRefreshKey?: string
  provider: ProviderSettingsApi
  onProviderChanged: () => Promise<void>
  runtime: RuntimeSnapshot
  projects: readonly ProjectConversationGroup[]
  onRebindProject: (projectId: string | null) => Promise<void>
  onOpenFolder: () => void
  onNewFolder: () => void
  onOpenFile: (relativePath: string, location?: FileLocation) => void
  onShowChange: (changeSetId: string) => void
  onShowChanges: () => void
  onShowCommands?: (commandId?: string) => void
  onShowBrowser?: () => void
  onOpenSettings: () => void
  onOpenMemorySettings: () => void
  onOpenSkillsSettings?: () => void
  panelOpen: boolean
  onTogglePanel: () => void
  sidebarCollapsed: boolean
  onExpandSidebar: () => void
  onNewConversation: () => void
  notify: Notify
  askConfirm: AskConfirm
}

/** 一句话发出去时可以带的东西。 */
interface SendOptions {
  goal?: import('../../api/sessionGoal').GoalAdmission
  manualFileIds?: string[]
  continuationSourceRunId?: string
  reviewSourceRunId?: string
  reviewSourceSessionId?: string
  composerDraft?: string
  images?: AgentImageAttachment[]
}

/**
 * 中间的对话（9-29 重做）：顶上一行在哪个文件夹、开着什么；中间一列居中的消息；底下输入框。
 * 做的事：发送 / 排队 / 停止 / 接着做 / 重发 / 分叉 / 压缩；@ 文件、/ 命令和技能；修改卡（看改动、撤回）；
 * 这一轮记住了什么（记忆开着时，答完以后 Agent 自己挑的，也可能是它这一轮调 memory_write 记的）。
 */
export function ChatView(props: ChatViewProps) {
  const { conversation, projectId, snapshot, notify } = props
  const { forkConversation, prepareContinuation } = conversation
  const processActivitiesView = useProcessActivities(conversation.activities)
  const pastProcessActivities = useMemo(() => Object.fromEntries(Object.entries(conversation.pastActivities)
    .map(([runId, activities]) => [runId, processActivities(activities)])), [conversation.pastActivities])
  const activeRun = conversation.activeRun
  const context = useContextState(conversation.session?.session_id ?? null,
    `${activeRun?.run_id ?? ''}:${activeRun?.status ?? ''}`, conversation.latestCheckpoint?.checkpoint_id)
  const available = conversation.availability === 'available' && props.runtime.ready
  const [accessMode, setAccessMode] = useState<AgentAccessMode>(readAgentAccessMode)
  const setAccess = useCallback((next: AgentAccessMode) => {
    setAccessMode(next)
    writeAgentAccessMode(next)
  }, [])

  const { onProjectChanged, onChangesChanged } = props
  const cards = useChangeCards({
    sessionId: conversation.session?.session_id ?? null,
    projectId,
    refreshKey: props.changesRefreshKey,
    runIds: conversation.messages.filter((message) => message.session_id === conversation.session?.session_id).map((message) => message.run_id),
    // 完全访问时按设置里的「保存方式」（默认直接保存）；只读时一律不自动保存。
    autoApply: accessMode === 'full' ? undefined : false,
    activeRun,
    activities: processActivitiesView,
    onBookChanged: useCallback(() => { onProjectChanged(); onChangesChanged() }, [onChangesChanged, onProjectChanged]),
  })

  // 对话里点了文件名：认得出是文件夹里的哪一个就在右栏打开。
  const { onOpenFile, onShowChange, onShowCommands, onOpenMemorySettings } = props
  const openFile = useCallback((rawPath: string) => {
    const target = resolveFileNavigation(snapshot, rawPath)
    if (target?.location) onOpenFile(target.relativePath, target.location)
    else if (target) onOpenFile(target.relativePath)
  }, [onOpenFile, snapshot])

  // ---- 输入框 ----
  const [draft, setDraft, draftRecovery] = useConversationDraft(projectId, conversation.session?.session_id ?? null,
    conversation.switchingSession || conversation.contextState === 'restoring')
  const background = useBackgroundEvents(conversation.session?.session_id ?? null, conversation.observeBackgroundRun)
  const queue = useMessageQueue({
    sessionId: conversation.session?.session_id ?? null,
    enabled: available,
    blocked: conversation.running || conversation.busy || Boolean(conversation.pendingSend) || conversation.switchingSession || props.projectState === 'loading',
    runKey: `${activeRun?.run_id ?? ''}:${activeRun?.status ?? ''}`,
    onAccepted: conversation.observeBackgroundRun ? background.reload : conversation.refresh,
    onPoll: conversation.observeBackgroundRun ? background.reload : undefined,
    observeBackground: background.polling,
    onSteered: conversation.refreshMessages ?? conversation.refresh,
  })
  const [attachedSkills, setAttachedSkills, skillDraft] = useConversationSkills(projectId, conversation.session?.session_id ?? null,
    conversation.switchingSession || conversation.contextState === 'restoring')
  const { captureSubmission: captureSkills, reconcileAvailable } = skillDraft
  const imageDraft = useConversationImages(projectId, conversation.session?.session_id ?? null,
    conversation.switchingSession || conversation.contextState === 'restoring')
  const { restore: restoreImages } = imageDraft
  const [skills, setSkills] = useState<string[]>([])
  const textareaRef = useRef<HTMLTextAreaElement | null>(null)
  const focusInput = useCallback(() => window.setTimeout(() => textareaRef.current?.focus(), 0), [])
  useImperativeHandle(props.composerRef, () => ({
    captureDraftTransfer: () => {
      const text = draftRecovery.captureTransfer(), images = imageDraft.captureTransfer(), skills = skillDraft.captureTransfer()
      return (targetProject, targetSession) => { text(targetProject, targetSession); images(targetProject, targetSession); skills(targetProject, targetSession) }
    },
    quoteFile: (quote) => {
      if (quote.projectId !== projectId || conversation.switchingSession || conversation.contextState === 'restoring') return
      setDraft((current) => appendFileQuotation(current, quote))
      focusInput()
    },
  }), [projectId, conversation.switchingSession, conversation.contextState, setDraft, focusInput, draftRecovery, imageDraft, skillDraft])
  useEffect(() => {
    let alive = true
    let generation = 0
    const refresh = (snapshot?: UserSkillsView) => {
      const request = ++generation
      const apply = (view: UserSkillsView) => {
        if (!alive || request !== generation) return
        const names = view.skills.filter((skill) => skill.enabled).map((skill) => skill.name)
        setSkills(names)
        reconcileAvailable(names)
      }
      if (snapshot) apply(snapshot)
      else void readUserSkills().then(apply).catch(() => undefined)
    }
    const unsubscribe = subscribeSkillsChanged(refresh)
    refresh()
    return () => { alive = false; generation++; unsubscribe() }
  }, [reconcileAvailable])

  const memory = useMemoryChanges(available ? conversation.session?.session_id ?? null : null,
    conversation.messages.filter((message) => message.session_id === conversation.session?.session_id).map((message) => message.run_id),
    `${activeRun?.run_id ?? ''}:${activeRun?.status ?? ''}`)
  const memoryNotes = memory.changes

  const send = useCallback(async (text: string, options: SendOptions = {}) => {
    const targetSession = conversation.session?.session_id
    if (options.reviewSourceRunId && options.reviewSourceSessionId !== targetSession) return
    const trimmed = text.trim()
    const fromComposer = options.composerDraft !== undefined
    if (!trimmed && !(fromComposer ? imageDraft.images.length : options.images?.length)) return
    if (fromComposer && imageDraft.blocked) return
    if (!available) {
      notify('warn', T.agent.notReady)
      return
    }
    const imageSubmission = fromComposer ? imageDraft.captureSubmission() : null
    const images = imageSubmission?.images ?? options.images ?? []
    const submission = fromComposer ? draftRecovery.beginSubmission(options.composerDraft!) : null
    const skillSubmission = submission ? captureSkills() : null
    const skillsAtSend = skillSubmission?.names ?? []
    const merged = snapshot ? [...new Set([...(options.manualFileIds ?? []), ...mentionedFileIds(trimmed, snapshot.files)])] : options.manualFileIds ?? []
    try {
      await conversation.send({
        text: trimmed,
        manualFileIds: merged.length > 0 ? merged : undefined,
        skillNames: skillsAtSend.length > 0 ? skillsAtSend : undefined,
        ...(skillSubmission && skillsAtSend.length > 0 ? { skillDraftIdentity: skillSubmission.draftIdentity } : {}),
        ...(images.length ? { imageIds: images.map((image) => image.image_id), images, ...(imageSubmission ? { imageDraftIdentity: imageSubmission.draftIdentity } : {}) } : {}),
        // 完全访问：能读能写能跑命令；只读：同一条对话链，只收回写的权限。
        writerMode: options.reviewSourceRunId || (options.continuationSourceRunId && conversation.reviewSources?.[options.continuationSourceRunId])
          ? 'repair_only' : accessMode === 'full' ? 'author_review_change_proposal' : 'repair_only',
        continuationSourceRunId: options.continuationSourceRunId,
        reviewSourceRunId: options.reviewSourceRunId,
        ...(options.goal ? { goal: options.goal } : {}),
      })
      skillSubmission?.accepted()
      imageSubmission?.accepted()
      submission?.accepted()
    } catch (error) {
      if (error instanceof AgentSendUnconfirmedError) {
        // 原消息连同技能快照已由发送记录接管，随后新加的标签仍属于当前草稿。
        skillSubmission?.retained()
        imageSubmission?.retained()
        submission?.retained()
      } else { skillSubmission?.failed(); imageSubmission?.failed(); submission?.failed() }
      if (!targetSession || currentQueueSession.current === targetSession) notify('danger', authorFacingError(error, T.agent.sendFailed))
    }
  }, [accessMode, available, captureSkills, conversation, draftRecovery, imageDraft, notify, snapshot])

  // History actions use the current send context without making every draft edit rebuild history.
  const sendRef = useRef(send)
  useLayoutEffect(() => { sendRef.current = send }, [send])
  const sendCurrent = useCallback((text: string, options?: SendOptions) => sendRef.current(text, options), [])

  const submit = useCallback(() => {
    const text = draft.trim()
    // 队列正准备下一条时也属于发送过程；回车保留当前草稿，不抢先另开一轮。
    if ((!text && imageDraft.images.length === 0) || queue.busy || queue.awaitingDispatch || imageDraft.blocked) return
    const imageIds = imageDraft.images.flatMap((entry) => entry.image ? [entry.image.image_id] : [])
    const queuedInput: Omit<QueueInput, 'item_id'> = {
      message: text, project_id: projectId,
      writer_mode: accessMode === 'full' ? 'author_review_change_proposal' : 'repair_only',
      manual_file_ids: snapshot ? mentionedFileIds(text, snapshot.files) : [],
      manual_skill_names: attachedSkills, open_file_path: props.openFileLabel,
      ...(imageIds.length ? { image_ids: imageIds } : {}),
    }
    let retryQueuedInput = false
    try { retryQueuedInput = queue.hasPendingInput(queuedInput) }
    catch (error) { notify('danger', authorFacingError(error, T.agent.sendFailed)); return }
    // The previous run may have finished while its queue acknowledgement was
    // lost. Retrying must still use that queue identity, not create a fresh Run.
    if (conversation.running || conversation.busy || retryQueuedInput) {
      if (!conversation.session || queue.busy) return
      const targetSession = conversation.session.session_id
      const skillSubmission = captureSkills()
      const imageSubmission = imageDraft.captureSubmission()
      void queue.enqueue(queuedInput).then(() => {
        skillSubmission.accepted()
        imageSubmission.accepted()
        if (currentQueueSession.current !== targetSession) return
        setDraft((current) => current.trim() === text ? '' : current)
      }).catch((error: unknown) => { skillSubmission.failed(); imageSubmission.failed(); notify('danger', authorFacingError(error, T.agent.sendFailed)) })
      return
    }
    void send(text, { composerDraft: draft })
  }, [accessMode, attachedSkills, captureSkills, conversation.busy, conversation.running, conversation.session, draft, imageDraft, notify, projectId, props.openFileLabel, queue, send, setDraft, snapshot])
  const currentQueueSession = useRef(conversation.session?.session_id)
  currentQueueSession.current = conversation.session?.session_id

  const steerDraft = useCallback(() => {
    const text = draft.trim()
    const targetSession = conversation.session?.session_id
    const targetRun = activeRun?.run_id
    if ((!text && imageDraft.images.length === 0) || !targetSession || !targetRun || !conversation.running || queue.busy || imageDraft.blocked) return
    const skillSubmission = captureSkills()
    const imageSubmission = imageDraft.captureSubmission()
    const skillsAtEnqueue = skillSubmission.names
    void queue.enqueueAndSteer({ message: text, project_id: projectId,
      writer_mode: accessMode === 'full' ? 'author_review_change_proposal' : 'repair_only',
      manual_file_ids: snapshot ? mentionedFileIds(text, snapshot.files) : [],
      manual_skill_names: skillsAtEnqueue, open_file_path: props.openFileLabel,
      ...(imageSubmission.imageIds.length ? { image_ids: imageSubmission.imageIds } : {}),
    }, targetRun).then(() => {
      skillSubmission.accepted()
      imageSubmission.accepted()
      // 入队已经落盘；即使本轮刚结束未能接收，也留在队列里并显示原因。
      if (currentQueueSession.current !== targetSession) return
      setDraft((current) => current.trim() === text ? '' : current)
    }).catch((error: unknown) => { skillSubmission.failed(); imageSubmission.failed(); notify('danger', authorFacingError(error, T.agent.sendFailed)) })
  }, [accessMode, activeRun?.run_id, captureSkills, conversation.running, conversation.session?.session_id, draft, imageDraft, notify, projectId, props.openFileLabel, queue, setDraft, snapshot])

  const stop = useCallback(() => {
    if (queue.state?.items.length) void queue.update({ action: 'pause' })
    void conversation.cancel().catch(() => undefined)
  }, [conversation, queue])
  const compactBusy = useRef(false)
  const compact = useCallback(async () => {
    if (compactBusy.current || context.state?.compacting || conversation.busy || conversation.running || !conversation.session) return
    compactBusy.current = true
    try {
      await conversation.compactConversation()
      notify('ok', T.agent.compactDone)
    } catch (error) {
      notify('danger', authorFacingError(error, T.agent.compactFailed))
    } finally {
      compactBusy.current = false
    }
  }, [context.state?.compacting, conversation, notify])
  const fork = useCallback(async (message: AgentMessage) => {
    if (conversation.busy || conversation.running) return
    try {
      const forked = await forkConversation(message.message_id)
      notify('ok', T.agent.forkedNotice(forked.title))
      focusInput()
    } catch (error) {
      notify('danger', authorFacingError(error, T.agent.newChatFailed))
    }
  }, [conversation.busy, conversation.running, forkConversation, focusInput, notify])
  const rollbackLast = useCallback(() => {
    const last = [...cards.cards].reverse().find((card) => card.status === 'applied')
    if (!last) {
      notify('warn', T.agent.slash.noChangeToRollback)
      return
    }
    props.askConfirm({
      title: T.changes.rollbackConfirmTitle,
      body: T.changes.rollbackConfirmBody,
      danger: true,
      confirmText: T.changes.rollback,
      onConfirm: () => cards.rollback(last.changeSetId),
    })
  }, [cards, notify, props])
  const modelButtonRef = useRef<HTMLDivElement | null>(null)
  const runCommand = useCallback((action: CommandAction) => {
    switch (action) {
      case 'compact': void compact(); break
      case 'new-chat': props.onNewConversation(); break
      case 'stop': if (conversation.running) stop(); else notify('warn', T.agent.slash.stopIdle); break
      case 'rollback-last': rollbackLast(); break
      case 'open-changes': props.onShowChanges(); break
      case 'pick-model': {
        const select = modelButtonRef.current?.querySelector<HTMLSelectElement>('[data-model-pick] select')
        select?.focus()
        try { select?.showPicker?.() } catch { /* 不靠用户手势时只是聚个焦 */ }
        break
      }
      case 'toggle-readonly': {
        const next: AgentAccessMode = accessMode === 'full' ? 'read_only' : 'full'
        setAccess(next)
        notify('ok', next === 'full' ? T.agent.access.switchedToFull : T.agent.access.switchedToReadOnly)
        break
      }
    }
  }, [accessMode, compact, conversation.running, notify, projectId, props, rollbackLast, setAccess, stop])

  // ---- 接着做（上一轮没做完） ----
  // 「正在接着做」按对话记：A 的请求还没回来就换到 B，B 的按钮照样能点；A 回来了既不发 A 的任务，也不清 B 的等待。
  const scopeKey = `${projectId ?? ''}:${conversation.session?.session_id ?? ''}`
  const currentScope = useRef(scopeKey)
  currentScope.current = scopeKey
  const [continuingKey, setContinuingKey] = useState<string | null>(null)
  const continuing = continuingKey === scopeKey
  const continueTask = useCallback(async () => {
    if (continuing || conversation.busy || conversation.running) return
    const scope = scopeKey
    setContinuingKey(scope)
    try {
      const state = await prepareContinuation()
      if (currentScope.current !== scope) return
      if (state) await sendCurrent(conversation.activeRun?.origin_kind === 'command_completion' ? '继续核对这次命令的结果。' : state.objective, { continuationSourceRunId: state.run_id })
    } catch (error) {
      if (currentScope.current === scope) notify('danger', authorFacingError(error, T.agent.continueFailed))
    } finally {
      setContinuingKey((current) => (current === scope ? null : current))
    }
  }, [continuing, conversation.busy, conversation.running, prepareContinuation, conversation.activeRun?.origin_kind, notify, scopeKey, sendCurrent])

  // ---- 状态 ----
  const statusText = useMemo(() => {
    if (props.runtime.connection === 'offline') return T.agent.status.offline
    if (context.state?.run_compaction?.state === 'running' && !context.error) return '正在整理工具结果…'
    if (conversation.contextState === 'compacting' || (context.state?.compacting && !context.error)) return T.agent.compactBusy
    if (conversation.availability === 'checking' || props.runtime.connection === 'checking') return T.runtime.checking
    if (conversation.availability !== 'available') {
      return /模型|model|provider|configured|密钥|key/i.test(conversation.detail) ? T.agent.status.noModel : authorFacingErrorText(conversation.detail, T.agent.status.offline)
    }
    if (conversation.contextState === 'sending' || conversation.contextState === 'restoring') return T.agent.status.preparing
    if (conversation.running) {
      if (conversation.streamingContent) return T.agent.status.writing
      const tail = turnTailActivities(processActivitiesView, activeRun?.run_id ?? null)
      if (stepRows(tail, snapshot).some((row) => row.state === 'running')) return '正在执行工具…'
      const thought = thoughtBlocksOf(tail, activeRun?.run_id ?? null).findLast((block) => block.phase === 'delta')
      if (thought) return T.agent.status.thinkingAbout((thought.text.split('\n').find((line) => line.trim()) ?? '').trim().slice(0, 40))
      return '等待模型回复…'
    }
    return null
  }, [activeRun?.run_id, context.error, context.state?.compacting, context.state?.run_compaction?.state, processActivitiesView, conversation.availability, conversation.contextState, conversation.detail, conversation.running, conversation.streamingContent, props.runtime.connection, snapshot])
  const statusOff = props.runtime.connection === 'offline' || (conversation.availability !== 'available' && conversation.availability !== 'checking')

  const where = !projectId
    ? T.agent.target.none
    : props.projectState === 'error'
      ? T.agent.target.loadFailed(props.projectError || T.agent.target.loadFailedUnknown)
      : !snapshot
        ? T.agent.target.loading
        : props.openFileLabel
          ? T.agent.target.openFile(snapshot.project.display_name, props.openFileLabel)
          : T.agent.target.folder(snapshot.project.display_name)

  // ---- 消息 ----
  const messages = useMemo(() => conversation.messages.filter((message) => message.role === 'user' || message.role === 'assistant'), [conversation.messages])
  const messageIndex = useMemo(() => {
    const previousUsers = new Map<string, string>()
    const previousUserIds = new Map<string, string>()
    const assistantRuns = new Set<string>()
    const userRuns = new Set<string>()
    const firstAssistantByRun = new Map<string, string>()
    const lastAssistantByRun = new Map<string, string>()
    let lastUser: string | null = null
    let lastUserId = 'start'
    for (const message of messages) {
      if (message.role === 'user') { userRuns.add(message.run_id); lastUser = message.content; lastUserId = message.message_id }
      else {
        assistantRuns.add(message.run_id)
        if (!firstAssistantByRun.has(message.run_id)) firstAssistantByRun.set(message.run_id, message.message_id)
        lastAssistantByRun.set(message.run_id, message.message_id)
        if (lastUser !== null) previousUsers.set(message.message_id, lastUser)
        previousUserIds.set(message.message_id, lastUserId)
      }
    }
    return { previousUsers, previousUserIds, assistantRuns, userRuns, firstAssistantByRun, lastAssistantByRun, lastUser, lastUserId }
  }, [messages])
  const scrollScope = `${projectId ?? 'loose'}:${conversation.session?.session_id ?? 'new'}`
  const historyVisit = useMemo(() => ({ scope: scrollScope }), [scrollScope])
  const [mountedHistory, setMountedHistory] = useState<typeof historyVisit | null>(null)
  const historyReady = mountedHistory === historyVisit
  const onHistoryReady = useCallback(() => setMountedHistory(historyVisit), [historyVisit])
  const layoutContent = useMemo(() => [messages, historyReady], [messages, historyReady])
  const scrollContent = useMemo(() => [messages.length, historyReady, conversation.streamingContent, processActivitiesView.length, cards.cards, memoryNotes],
    [messages.length, historyReady, conversation.streamingContent, processActivitiesView.length, cards.cards, memoryNotes])
  const { scrollRef, atBottom, onScroll, toBottom, earlier, loadEarlier } = useConversationScroll(scrollScope, scrollContent, conversation.loadEarlierMessages)
  useMessageLayout(scrollRef, layoutContent)

  const activeRunId = activeRun?.run_id ?? null
  const lastVisibleUserSequence = useMemo(() => {
    const visible = new Set(messages.filter((message) => message.role === 'user').map((message) => message.message_id))
    return processActivitiesView.reduce((latest, activity) => activity.run_id === activeRunId && activity.kind === 'message'
      && activity.payload.role === 'user' && activity.payload.phase === 'completed' && activity.payload.message_id && visible.has(activity.payload.message_id)
      ? Math.max(latest, activity.sequence) : latest, 0)
  }, [activeRunId, processActivitiesView, messages])
  const lastAgentOfActiveRun = useMemo(() => [...messages].reverse().find((message) => message.role === 'assistant' && message.run_id === activeRunId)?.message_id ?? null, [activeRunId, messages])
  const showLive = conversation.running || conversation.contextState === 'sending'
  const showStoppedTail = !showLive && Boolean(activeRun && activeRun.status !== 'succeeded')
    && (Boolean(conversation.streamingContent) || turnTailActivities(processActivitiesView, activeRunId).some((activity) => activity.kind === 'reasoning' || activity.kind === 'tool_call'))
  const failed = activeRun && !conversation.running && (activeRun.status === 'failed' || activeRun.status === 'interrupted' || activeRun.status === 'cancelled') ? activeRun : null
  const continuation = conversation.continuation?.run_id === activeRunId ? conversation.continuation : null
  const canContinue = Boolean(continuation?.can_continue || (continuation?.continued_by_run_id && continuation.reason_code === 'already_continued'))
  const fileEffects = useMemo(() => agentFileEffects(processActivitiesView, activeRunId), [activeRunId, processActivitiesView])
  const failedSteps = useMemo(() => (failed ? stepRows(processActivitiesView.filter((item) => item.run_id === failed.run_id), snapshot, 20) : []), [processActivitiesView, failed, snapshot])
  const orphanCards = cards.cards.filter((card) => !messageIndex.assistantRuns.has(card.runId)
    && (!activeRun || card.runId === activeRun.run_id || !messageIndex.userRuns.has(card.runId)))
  const lastUserText = messageIndex.lastUser
  const [requestedProcesses, setRequestedProcesses] = useState<Set<string>>(() => new Set())
  const processOpenStates = useRef(new Map<string, boolean>())
  const tailJoined = Boolean((showLive || showStoppedTail) && messages.at(-1)?.role === 'assistant' && messages.at(-1)?.run_id === activeRunId)
  const currentAction = useMemo(() => showLive ? stepRows(processActivitiesView.filter((activity) => activity.run_id === activeRunId), snapshot)
    .findLast((row) => row.state === 'running')?.text ?? '正在处理…' : null, [activeRunId, processActivitiesView, showLive, snapshot])

  const copy = useCallback(async (text: string) => {
    try {
      await navigator.clipboard.writeText(text)
      notify('ok', T.common.copied)
    } catch {
      notify('warn', T.common.copyFailed)
    }
  }, [notify])

  const { byRun, approve, discard, rollback } = cards
  const renderArtifacts = useCallback((sessionId: string, runId: string) => sessionId !== conversation.session?.session_id ? null : <RunArtifacts sessionId={sessionId} runId={runId}
    refreshKey={`${props.changesRefreshKey ?? ''}:${(byRun.get(runId) ?? []).map((card) => `${card.changeSetId}:${card.status}`).join('|')}`}
    canOpenFile={(path) => Boolean(resolveFileNavigation(snapshot, path))}
    onOpenFile={openFile} onShowChange={onShowChange} />, [byRun, conversation.session?.session_id, onShowChange, openFile, props.changesRefreshKey, snapshot])
  const renderCards = useCallback((runId: string): ReactNode => (byRun.get(runId) ?? []).map((card) => (
    <ChangeCard
      key={card.changeSetId}
      card={card}
      snapshot={snapshot}
      onApprove={() => void approve(card.changeSetId)}
      onDiscard={() => void discard(card.changeSetId)}
      onRollback={() => void rollback(card.changeSetId)}
      onShowInPanel={() => onShowChange(card.changeSetId)}
      onOpenFile={openFile}
    />
  )), [approve, byRun, discard, onShowChange, openFile, rollback, snapshot])

  const observedCheckpoint = context.state?.latest_checkpoint
  const checkpoint = observedCheckpoint && observedCheckpoint.through_ordinal >= (conversation.latestCheckpoint?.through_ordinal ?? 0)
    ? observedCheckpoint : conversation.latestCheckpoint
  const checkpointBanner = useMemo(() => checkpoint ? (
    <div className="zm-checkpoint" role="status">
      <span>{T.agent.compactStored(checkpoint.source_message_count)}</span>
      <details>
        <summary>{T.agent.compactView}</summary>
        <p className="zm-muted-line">{T.agent.compactNote}</p>
        <div className="zm-checkpoint-text">{checkpoint.content}</div>
      </details>
    </div>
  ) : null, [checkpoint])

  const { runProcessSummaries, runOrigins, reviewSources, loadRunActivities, running, busy, pendingSend } = conversation
  const historySessionId = conversation.session?.session_id
  const historyCanContinue = conversation.continuation?.can_continue
  const messageHistory = useMemo(() => messages.map((message, index) => {
    const previous = messages[index - 1]
    if (message.role === 'assistant' && previous?.role === 'assistant' && previous.run_id === message.run_id
      && !(checkpoint && previous.ordinal <= checkpoint.through_ordinal && message.ordinal > checkpoint.through_ordinal)) return null
    const isLatest = index === messages.length - 1 && !showLive
    const inActiveRun = !activeRun || message.run_id === activeRun.run_id
    const past = inActiveRun ? undefined : pastProcessActivities[message.run_id]
    const runActivities = inActiveRun ? processActivitiesView : past ?? NO_ACTIVITIES
    let endIndex = index
    if (message.role === 'assistant') while (messages[endIndex + 1]?.role === 'assistant' && messages[endIndex + 1]?.run_id === message.run_id
      && !(checkpoint && messages[endIndex].ordinal <= checkpoint.through_ordinal && messages[endIndex + 1].ordinal > checkpoint.through_ordinal)) endIndex++
    const group = messages.slice(index, endIndex + 1)
    const last = group.at(-1)!
    const next = messages[endIndex + 1]
    const historyWithoutReply = message.role === 'user' && (!inActiveRun || (!activeRun && !running))
      && !messageIndex.assistantRuns.has(message.run_id) && next?.run_id !== message.run_id
    const userSequence = historyWithoutReply ? runActivities.find((activity) => activity.kind === 'message'
      && activity.payload.message_id === message.message_id && activity.payload.phase === 'completed')?.sequence ?? 0 : 0
    const includeTail = message.role === 'assistant' && endIndex === messages.length - 1 && tailJoined
    const completed = runActivities.find((activity) => activity.kind === 'message' && activity.payload.message_id === last.message_id && activity.payload.phase === 'completed')
    const finishReason = completed?.kind === 'message' ? completed.payload.finish_reason : null
    const lastIsProgress = finishReason === 'tool_calls' || finishReason === 'tool_use' || (includeTail && showLive)
    const stepsUnloaded = !inActiveRun && past === undefined
    const processSummary = runProcessSummaries?.[message.run_id]
    const hasRecordedProcess = processSummary?.has_process === true || (!stepsUnloaded && stepRows(runActivities.filter((activity) => activity.run_id === message.run_id), snapshot).length > 0)
    const commandOrigin = runOrigins?.[message.run_id] === 'command_completion' || (activeRun?.run_id === message.run_id && activeRun.origin_kind === 'command_completion')
    const reviewSource = reviewSources?.[message.run_id]
    const hasProcess = processSummary?.has_process === true || group.length > 1 || lastIsProgress || includeTail
      || group.some((item) => Boolean(presentAiText(item.content, { preserveMarkdown: true }).thinking))
      || hasRecordedProcess
      || thoughtBlocksOf(runActivities, message.run_id).length > 0
    const loadProcess = async () => {
      await loadRunActivities(message.run_id)
      setRequestedProcesses((previous) => new Set([...previous, message.run_id]))
    }
    const foldHere = checkpoint && last.ordinal <= checkpoint.through_ordinal
      && (endIndex === messages.length - 1 || messages[endIndex + 1].ordinal > checkpoint.through_ordinal)
    const notes = message.role === 'assistant' ? memoryNotes[message.run_id] : undefined
    const renderAgent = (item: AgentMessage, mode: 'process' | 'process-only' | 'answer') => <AgentMessageView key={`${item.message_id}:${mode}`}
      message={messageIndex.lastAssistantByRun.get(item.run_id) === item.message_id ? item : { ...item, run_usage: null }} activities={runActivities} stepsUnloaded={false} onLoadSteps={() => loadRunActivities(item.run_id)} snapshot={snapshot}
      showUnpartitionedProcess={messageIndex.firstAssistantByRun.get(item.run_id) === item.message_id}
      progress={mode === 'process'} processOnly={mode === 'process-only'} hideProcess={mode === 'answer'} inlineProcess
      cards={mode === 'answer' && messageIndex.lastAssistantByRun.get(item.run_id) === item.message_id ? renderCards(item.run_id) : null}
      resultDetails={mode === 'answer' && !lastIsProgress && messageIndex.lastAssistantByRun.get(item.run_id) === item.message_id
        ? renderArtifacts(item.session_id, item.run_id) : null}
      historyLookup={mode === 'answer' && !hasProcess ? stepsUnloaded && processSummary === undefined ? <HistoryProcessLookup onLoad={loadProcess} />
        : requestedProcesses.has(message.run_id) ? <span className="zm-muted-line">没有额外处理记录</span> : null : null}
      isLatest={item.message_id === messages.at(-1)?.message_id && !showLive} onOpenFile={openFile} onShowChange={onShowChange} onShowCommands={onShowCommands}
      actions={{ onCopy: (text) => void copy(text), onRegenerate: !commandOrigin && messageIndex.previousUsers.get(item.message_id) ? () => void sendCurrent(messageIndex.previousUsers.get(item.message_id)!, {
        images: messages.find((message) => message.message_id === messageIndex.previousUserIds.get(item.message_id))?.images,
        ...(reviewSource ? { reviewSourceRunId: reviewSource.source_run_id, reviewSourceSessionId: item.session_id } : {}),
      }) : undefined,
        regenerateDisabled: running || busy, onFork: () => void fork(item),
        onReview: mode === 'answer' && !lastIsProgress && !commandOrigin && !reviewSource && messageIndex.lastAssistantByRun.get(item.run_id) === item.message_id
          ? () => void sendCurrent('独立复核这次任务的实际结果。核对原始要求和当前产物，指出遗漏、错误及尚未验证之处。', {
            reviewSourceRunId: item.run_id, reviewSourceSessionId: item.session_id,
          }) : undefined,
        reviewDisabled: running || busy || Boolean(pendingSend),
      }} />
    const tail = (processOnly: boolean) => <HistoryLiveTurn activities={processActivitiesView} runId={activeRunId} startedAt={activeRun?.created_at ?? null}
      snapshot={snapshot} onOpenFile={openFile} onShowChange={onShowChange} onShowCommands={onShowCommands} afterUserSequence={lastVisibleUserSequence}
      live={showLive} processOnly={processOnly} hideProcess={!processOnly} />
    return (
      <div key={message.message_id} className="zm-turn" data-run-id={message.run_id} data-message-id={message.message_id}>
        {reviewSource && (index === 0 || messages[index - 1].run_id !== message.run_id) ? <div className="zm-muted-line">
          独立复核 · 第 {reviewSource.source_run_ordinal} 轮
          {reviewSource.source_message_id ? <SourceLink target={{ sessionId: message.session_id, messageId: reviewSource.source_message_id }}>原始要求</SourceLink> : null}
        </div> : null}
        {message.role === 'user' && !(previous?.role === 'assistant' && previous.run_id === message.run_id) ? <ProcessBeforeUser message={message} activities={runActivities} snapshot={snapshot} onOpenFile={openFile} onShowChange={onShowChange} onShowCommands={onShowCommands} /> : null}
        {message.role === 'user' ? (
          <>
          <UserMessage
            message={message}
            isLatest={isLatest}
            onOpenFile={openFile}
            actions={{ onCopy: (text) => void copy(text), onEdit: (text) => { setDraft(message.images?.length && text === '[图片]' ? '' : text); if (message.images) restoreImages(message.images); focusInput() }, onFork: () => void fork(message) }}
          />
          {historyWithoutReply ? <>
            {hasProcess ? <ProcessGroup stateKey={`${message.session_id}:${message.run_id}:${message.message_id}`} states={processOpenStates.current}
              onLoad={stepsUnloaded ? loadProcess : undefined} initiallyOpen={requestedProcesses.has(message.run_id)}>
              <LiveTurn activities={runActivities} runId={message.run_id} startedAt={null} streaming="" snapshot={snapshot}
                onOpenFile={openFile} onShowChange={onShowChange} onShowCommands={onShowCommands} afterUserSequence={userSequence} live={false} processOnly />
            </ProcessGroup> : stepsUnloaded && processSummary === undefined ? <HistoryProcessLookup onLoad={loadProcess} />
              : requestedProcesses.has(message.run_id) ? <span className="zm-muted-line">没有额外处理记录</span> : null}
            {renderCards(message.run_id)}
            {renderArtifacts(message.session_id, message.run_id)}
            <MessageDetails usage={undefined} runUsage={message.run_usage} parameterNotes={[]} />
          </> : null}
          </>
        ) : (
          <>
            {messageIndex.firstAssistantByRun.get(message.run_id) === message.message_id && commandOrigin ? <CompletionOrigin commandIds={background.events.filter((event) => event.delivered_run_id === message.run_id).map((event) => event.command_session_id)} onOpen={onShowCommands}
              onContinue={message.run_id === activeRunId && historyCanContinue ? () => void continueTask() : undefined} busy={continuing || busy} /> : null}
            {hasProcess ? <ProcessGroup key={`${message.run_id}:${messageIndex.previousUserIds.get(message.message_id)}`}
              stateKey={`${historySessionId}:${message.run_id}:${messageIndex.previousUserIds.get(message.message_id) ?? 'start'}`} states={processOpenStates.current}
              summary={processSummary?.tool_count ? `${processSummary.tool_count} 项工具记录` : null}
              currentAction={includeTail ? currentAction : null} onLoad={stepsUnloaded ? loadProcess : undefined} initiallyOpen={requestedProcesses.has(message.run_id) || (running && message.run_id === activeRunId)}>
              {group.map((item) => renderAgent(item, item !== last || lastIsProgress ? 'process' : 'process-only'))}
              {next?.role === 'user' && next.run_id === message.run_id ? <ProcessBeforeUser message={next} activities={runActivities} snapshot={snapshot} onOpenFile={openFile} onShowChange={onShowChange} onShowCommands={onShowCommands} /> : null}
              {includeTail ? tail(true) : null}
            </ProcessGroup> : null}
            {!lastIsProgress ? renderAgent(last, 'answer') : messageIndex.lastAssistantByRun.get(last.run_id) === last.message_id ? renderCards(last.run_id) : null}
            {lastIsProgress && !(inActiveRun && running) && messageIndex.lastAssistantByRun.get(last.run_id) === last.message_id
              ? <MessageDetails usage={undefined} runUsage={last.run_usage} parameterNotes={[]} /> : null}
            {includeTail ? tail(false) : null}
          </>
        )}
        {notes && last.message_id === messageIndex.lastAssistantByRun.get(last.run_id) ? <MemoryChanges sessionId={last.session_id} changes={notes} onManage={onOpenMemorySettings} /> : null}
        {foldHere ? checkpointBanner : null}
      </div>
    )
  }).filter((item) => item !== null), [activeRun, activeRunId, background.events, checkpoint, checkpointBanner, continueTask, continuing, copy, busy, running, pendingSend, historySessionId, historyCanContinue,
    runProcessSummaries, runOrigins, reviewSources, loadRunActivities,
    currentAction, focusInput, fork, lastVisibleUserSequence, memoryNotes, messageIndex, messages, pastProcessActivities, processActivitiesView,
    onOpenMemorySettings, onShowChange, onShowCommands, openFile, renderArtifacts, renderCards, requestedProcesses, restoreImages, sendCurrent, setDraft, showLive, snapshot, tailJoined])

  const folderControl = (
    <Menu
      label="切换工作区"
      items={[
        { key: 'none', label: '不关联文件夹', hint: '自由对话', icon: <SquarePen />, onSelect: () => { void props.onRebindProject(null).catch((error: unknown) => notify('danger', authorFacingError(error, T.agent.rebindFailed))) } },
        ...props.projects.map((project) => ({
          key: project.projectId,
          label: project.name,
          hint: project.rootPath,
          icon: <Folder />,
          onSelect: () => { void props.onRebindProject(project.projectId).catch((error: unknown) => notify('danger', authorFacingError(error, T.agent.rebindFailed))) },
        })),
        { key: 'open', label: T.agent.openFolderPick, icon: <FolderOpen />, onSelect: props.onOpenFolder },
        { key: 'new', label: T.agent.newFolderPick, icon: <FolderPlus />, onSelect: props.onNewFolder },
      ]}
      trigger={({ open, toggle, ref, menuId }) => (
        <button ref={ref} type="button" className="zm-workspace-picker" aria-label="切换工作区" title={snapshot?.project.root_path ?? '选择本次对话的文件夹'} aria-haspopup="menu" aria-expanded={open} aria-controls={open ? menuId : undefined} disabled={conversation.running || conversation.busy} onClick={toggle}>
          <Folder aria-hidden />
          <span>{projectId ? snapshot?.project.display_name ?? T.agent.target.loading : '自由对话'}</span>
          <ChevronDown aria-hidden />
        </button>
      )}
    />
  )

  return (
    <ConversationSources scope={conversation.session?.session_id ?? null}><section className={`zm-chat${messages.length === 0 && !showLive ? ' is-empty' : ''}`} aria-label={T.agent.title}>
      <header className="zm-chat-head">
        {props.sidebarCollapsed ? (
          <>
            <IconBtn label={A.sidebar.expand} icon={<PanelLeftOpen />} onClick={props.onExpandSidebar} />
            <IconBtn label={T.agent.newConversation} icon={<SquarePen />} onClick={props.onNewConversation} />
          </>
        ) : null}
        <div className="zm-chat-title">
          <h1>{conversation.session?.title?.trim() || T.agent.newConversation}</h1>
          <div className="zm-chat-location" title={where}>{folderControl}{props.openFileLabel ? <span className="zm-open-file-context" title={props.openFileLabel}>{props.openFileLabel}</span> : null}</div>
        </div>
        {statusText ? <span className={`zm-status${statusOff ? ' is-off' : ''}`} role="status">{statusText}</span> : null}
        <div className="zm-chat-tools" role="group" aria-label="对话工作区操作">
        {conversation.session && conversation.messages.length >= 2 ? (
          <IconBtn label={conversation.contextState === 'compacting' ? T.agent.compactBusy : T.agent.compactAction} title={T.agent.memoryHint} icon={<Archive />} disabled={conversation.busy || conversation.running} onClick={() => void compact()} />
        ) : null}
        {conversation.session && props.onShowCommands ? <Btn size="sm" variant="ghost" icon={<SquareTerminal />} aria-label="查看命令会话" onClick={() => props.onShowCommands?.()}>命令</Btn> : null}
        {conversation.session && props.onShowBrowser ? <Btn size="sm" variant="ghost" icon={<Globe />} aria-label="查看共享浏览器" onClick={props.onShowBrowser}>浏览器</Btn> : null}
        <Btn size="sm" variant="ghost" icon={<FileDiff />} className="zm-chat-changes" aria-label="查看修改记录" disabled={!conversation.session} onClick={props.onShowChanges}>修改</Btn>
        {projectId || props.panelOpen ? (
          <Btn size="sm" variant="ghost" aria-label={props.panelOpen ? A.panel.close : T.agent.showInspector} icon={props.panelOpen ? <PanelRightClose /> : <PanelRightOpen />} onClick={props.onTogglePanel}>{props.panelOpen ? '收起' : '工作区'}</Btn>
        ) : null}
        </div>
      </header>

      <div className="zm-thread" ref={scrollRef} onScroll={onScroll}>
        <div className="zm-thread-inner">
          {conversation.hasEarlierMessages ? (
            <div className="zm-earlier">
              <button type="button" className="zm-link" disabled={!historyReady || earlier === 'loading'} onClick={() => void loadEarlier()}>{T.agent.showEarlierMessages}</button>
              {earlier === 'failed' ? <span className="zm-muted-line">{T.agent.earlierMessagesFailed}</span> : null}
            </div>
          ) : null}
          {checkpoint && messages.length > 0 && messages[0].ordinal > checkpoint.through_ordinal ? checkpointBanner : null}
          {messages.length === 0 && !showLive ? (
            <StartView projectName={projectId ? snapshot?.project.display_name ?? T.agent.target.loading : null}
              projects={props.projects} model={agentSessionProviderPreference(conversation.session)?.model ?? props.provider.settings?.model ?? null}
              onOpenFolder={props.onOpenFolder} onNewFolder={props.onNewFolder}
              onProject={(id) => { void props.onRebindProject(id).catch((error) => notify('danger', authorFacingError(error, A.common.unknownError))) }}
              onModels={props.onOpenSettings} onMemory={props.onOpenMemorySettings} onSkills={props.onOpenSkillsSettings} />
          ) : null}
          <StreamingText value={conversation.streamingContent}>{messageHistory.length > 0 ? <HistoryMount key={scrollScope} scrollRef={scrollRef} onReady={onHistoryReady}>{messageHistory}</HistoryMount> : null}</StreamingText>
          {memory.error ? <p className="zm-muted-line" role="status">记忆变更暂未读取，正在重试。</p> : null}
          {(showLive || showStoppedTail) && !tailJoined ? <div className="zm-turn">
            {activeRun?.origin_kind === 'command_completion' && !messageIndex.firstAssistantByRun.has(activeRunId ?? '') ? <CompletionOrigin commandIds={background.events.filter((event) => event.delivered_run_id === activeRunId).map((event) => event.command_session_id)} onOpen={props.onShowCommands} /> : null}
            <ProcessGroup key={`${activeRunId}:${messageIndex.lastUserId}`} stateKey={`${conversation.session?.session_id}:${activeRunId}:${messageIndex.lastUserId}`} states={processOpenStates.current} currentAction={currentAction} initiallyOpen={showLive}><LiveTurn activities={processActivitiesView} runId={activeRunId} startedAt={null}
              streaming={conversation.streamingContent} snapshot={snapshot} onOpenFile={openFile} onShowChange={onShowChange} onShowCommands={onShowCommands} afterUserSequence={lastVisibleUserSequence} live={showLive} processOnly /></ProcessGroup>
            <LiveTurn activities={processActivitiesView} runId={activeRunId} startedAt={null} streaming={conversation.streamingContent}
              snapshot={snapshot} onOpenFile={openFile} afterUserSequence={lastVisibleUserSequence} live={showLive} hideProcess />
          </div> : null}
          {!showLive && !showStoppedTail && lastAgentOfActiveRun === null && conversation.streamingContent ? (
            <div className="zm-msg is-agent"><Markdown text={presentAiText(conversation.streamingContent, { preserveMarkdown: true }).answer} onOpenFile={openFile} /></div>
          ) : null}
          <CommandCompletions events={background.events} error={background.error} onRetry={() => void background.reload()} onOpen={props.onShowCommands} busy={conversation.busy || conversation.running}
            onReview={(id) => void send(`核对已完成命令 ${id} 的结果。`)} />
          {orphanCards.map((card) => (
            <ChangeCard
              key={card.changeSetId}
              card={card}
              snapshot={snapshot}
              onApprove={() => void cards.approve(card.changeSetId)}
              onDiscard={() => void cards.discard(card.changeSetId)}
              onRollback={() => void cards.rollback(card.changeSetId)}
              onShowInPanel={() => props.onShowChange(card.changeSetId)}
              onOpenFile={openFile}
            />
          ))}
          {failed ? <section className="zm-turn zm-failed-turn" aria-label="本轮未完成">
            <FailureNotice
              key={failed.run_id}
              run={failed}
              steps={failedSteps}
              onShowCommands={onShowCommands}
              replyKept={lastAgentOfActiveRun !== null}
              effects={fileEffects}
              canResume={canContinue}
              resumeBusy={continuing || conversation.busy}
              continuationReason={!canContinue ? continuation?.reason : undefined}
              provider={props.provider}
              onSelectModel={async (model, providerId) => {
                await conversation.rebindProvider({
                  provider_id: providerId ?? props.provider.settings?.provider_id,
                  model,
                  reasoning_effort: agentSessionProviderPreference(conversation.session)?.reasoningEffort ?? 'auto',
                })
              }}
              onOpenSettings={props.onOpenSettings}
              onResend={() => void send(failed.objective, {
                images: messages.find((message) => message.role === 'user' && message.run_id === failed.run_id)?.images,
                ...(conversation.reviewSources?.[failed.run_id] ? {
                  reviewSourceRunId: conversation.reviewSources[failed.run_id].source_run_id,
                  reviewSourceSessionId: failed.session_id,
                } : {}),
              })}
              onResume={() => void continueTask()}
              notify={notify}
            />
          {!messageIndex.lastAssistantByRun.has(failed.run_id) ? renderArtifacts(failed.session_id, failed.run_id) : null}
          {!messageIndex.lastAssistantByRun.has(failed.run_id) ? <MessageDetails usage={undefined}
            runUsage={messages.find((message) => message.run_id === failed.run_id)?.run_usage} parameterNotes={[]} /> : null}
          </section> : null}
          {(conversation.error && !failed) || conversation.availability === 'error' ? <div className="zm-error-line" role="alert">
            <p>{authorFacingErrorText(conversation.error || conversation.detail, T.common.unknownError)}</p>
            {conversation.availability === 'error' ? <button type="button" className="zm-link"
              disabled={!props.runtime.ready || conversation.contextState !== 'idle' || conversation.switchingSession}
              onClick={conversation.refresh}>重新读取对话</button> : null}
          </div> : null}
          {conversation.messageSyncError ? <div className="zm-error-line" role="alert">
            <p>对话记录尚未同步完整，已读内容已保留。{authorFacingErrorText(conversation.messageSyncError, T.common.unknownError)}</p>
            <button type="button" className="zm-link" disabled={conversation.syncingMessages || conversation.switchingSession}
              onClick={() => void conversation.refreshMessages?.()}>{conversation.syncingMessages ? '正在同步记录…' : '重试同步记录'}</button>
          </div> : null}
        </div>
      </div>

      {historyReady ? <ConversationRail key={scrollScope} messages={messages} scrollRef={scrollRef} hasEarlier={conversation.hasEarlierMessages} earlier={earlier} onLoadEarlier={loadEarlier} onLatest={toBottom} /> : null}
      <div className="zm-chat-foot">
        <div className="zm-chat-foot-inner" ref={modelButtonRef}>
          {!atBottom ? <div className="zm-jump-row">
            <button type="button" className="zm-jump" onClick={toBottom}>
              <ArrowDown aria-hidden />
              <span>{conversation.running ? T.agent.jumpToLatestGenerating : T.agent.jumpToLatest}</span>
            </button>
          </div> : null}
          <ContextStatus state={context.state} error={context.error} onRetry={context.refresh} />
          {conversation.pendingSend ? <section className="zm-draft-recovery" aria-label="发送结果待确认">
            {conversation.pendingSend.item.persistent === false ? <p>浏览器暂时无法保存发送记录，本页仍能核对和重试。刷新前请先核对发送结果。</p> : null}
            <p role="status">{conversation.pendingSend.status === 'checking'
              ? '正在核对发送结果…'
              : conversation.pendingSend.status === 'withdrawn'
                ? '这条发送已取回，原文仍保留，可以重新编辑。'
              : conversation.pendingSend.status === 'not_found'
                ? '暂未查到这条消息，原内容已保留。重试会沿用同一条发送记录。'
                : '发送结果尚未确认，原内容已保留。核对后会接回已受理的任务。'}</p>
            <pre>{conversation.pendingSend.item.request.message}</pre>
            {conversation.pendingSend.item.request.manual_skill_names?.length ? <p className="zm-faint">技能：{conversation.pendingSend.item.request.manual_skill_names.join('、')}</p> : null}
            <MessageImages images={conversation.pendingSend.item.images} />
            <div className="zm-draft-recovery-actions">
              <button type="button" className="zm-link" disabled={conversation.contextState !== 'idle' || conversation.switchingSession} onClick={() => void conversation.checkPendingSend?.()}>核对发送结果</button>
              {conversation.pendingSend.status !== 'withdrawn' ? <button type="button" className="zm-link" disabled={conversation.busy} onClick={() => void conversation.retryPendingSend?.()}>重试这条</button> : null}
              <button type="button" className="zm-link" disabled={conversation.contextState !== 'idle' || conversation.switchingSession} onClick={() => {
                const target = conversation.session?.session_id
                const images = conversation.pendingSend?.item.images
                const skills = conversation.pendingSend?.item.request.manual_skill_names
                void conversation.withdrawPendingSend?.().then((original) => {
                  if (original !== null && original !== undefined && currentQueueSession.current === target) {
                    if (original) setDraft((current) => current ? `${current}\n\n${original}` : original)
                    if (images) imageDraft.restore(images)
                    if (skills) setAttachedSkills((current) => [...new Set([...current, ...skills])])
                    focusInput()
                  }
                })
              }}>取回原文重新编辑</button>
              <button type="button" className="zm-link" onClick={() => void copy(conversation.pendingSend!.item.request.message)}>复制原文</button>
            </div>
          </section> : null}
          {draftRecovery.recoveries.length ? <section className="zm-draft-recovery" aria-label="待恢复消息">
            <p>发送未确认的原文已保留，当前草稿不受影响。</p>
            {draftRecovery.recoveries.map((entry) => <div key={entry.id}>
              <pre>{entry.text}</pre>
              <div className="zm-draft-recovery-actions">
                <button type="button" className="zm-link" onClick={() => { draftRecovery.appendRecovery(entry.id); focusInput() }}>追加到输入框</button>
                <button type="button" className="zm-link" onClick={() => void copy(entry.text)}>复制原文</button>
                <button type="button" className="zm-link" onClick={() => draftRecovery.dismissRecovery(entry.id)}>移除</button>
              </div>
            </div>)}
          </section> : null}
          {conversation.session ? <SessionPlan key={conversation.session.session_id} sessionId={conversation.session.session_id}
            run={activeRun} activities={processActivitiesView} /> : null}
          <Composer
            images={imageDraft.images} imagesBlocked={imageDraft.blocked} onImages={imageDraft.add} onRemoveImage={imageDraft.remove} onRetryImage={imageDraft.retry}
            draft={draft}
            onDraft={setDraft}
            onSubmit={submit}
            onSteer={conversation.running && activeRun?.run_id ? steerDraft : undefined}
            onStop={stop}
            running={conversation.running}
            busy={conversation.busy}
            available={available}
            inputDisabled={conversation.switchingSession || conversation.contextState === 'restoring'}
            placeholder={projectId ? T.agent.placeholder : T.agent.placeholderCasual}
            files={snapshot?.files ?? null}
            skills={skills}
            attachedSkills={attachedSkills}
            onAttachSkill={(name) => setAttachedSkills((current) => (current.includes(name) ? current : [...current, name]))}
            onDetachSkill={(name) => setAttachedSkills((current) => current.filter((item) => item !== name))}
            onOpenSkills={props.onOpenSkillsSettings}
            onCommand={runCommand}
            queuePanel={<QueuePanel key={conversation.session?.session_id} state={queue.state} busy={queue.busy} error={queue.error} errorSource={queue.errorSource}
              activeRunId={conversation.running ? activeRun?.run_id : null} onSteer={queue.steer} onUpdate={queue.update} onReload={queue.reload} />}
            queueBusy={queue.busy || queue.awaitingDispatch}
            lastUserText={lastUserText}
            accessFull={accessMode === 'full'}
            onToggleAccess={() => setAccess(accessMode === 'full' ? 'read_only' : 'full')}
            folderControl={null}
            onOpenFolder={props.onOpenFolder}
            goalControls={(<SessionGoal key={`goal:${conversation.session?.session_id ?? 'new'}`} sessionId={conversation.session?.session_id ?? null}
            run={activeRun} available={available} busy={conversation.busy || conversation.switchingSession || queue.busy || queue.awaitingDispatch || Boolean(conversation.pendingSend)}
            canStart={Boolean(draft.trim()) && !imageDraft.blocked}
            onStart={() => void send(draft, { composerDraft: draft, goal: { action: 'start' } })}
            onResume={goal => void send('继续完成持续目标，先核对已完成的工作，再推进剩余内容。', {
              goal: { action: 'resume', goal_id: goal.goal_id, expected_revision: goal.revision },
            })}
            onPause={stop} />)}
            modelControls={(
              <ModelControls key={conversation.session?.session_id ?? 'new'} provider={props.provider} session={conversation.session} disabled={conversation.running || conversation.busy || conversation.switchingSession} onSessionProviderChanged={conversation.rebindProvider} onProviderChanged={props.onProviderChanged} onOpenSettings={props.onOpenSettings} notify={notify} />
            )}
            textareaRef={textareaRef}
          />
        </div>
      </div>
    </section></ConversationSources>
  )
}
