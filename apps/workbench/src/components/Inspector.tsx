import { useCallback, useEffect, useRef, useState } from 'react'
import {
  Activity,
  AlertTriangle,
  Bot,
  CheckCircle2,
  CircleDot,
  Check,
  Copy,
  Download,
  Eye,
  Files,
  FileSearch,
  FileText,
  FolderSearch,
  History,
  Library,
  LoaderCircle,
  MessageSquareText,
  Play,
  RefreshCw,
  Search,
  SendHorizontal,
  Settings2,
  ShieldCheck,
  Sparkles,
  Square,
  SquarePen,
  UserRound,
} from 'lucide-react'
import type { RuntimeSnapshot } from '../api/runtime'
import type { AgentActivity, AgentMessage, WriterMode } from '../api/agent'
import type { AgentConversationState } from '../hooks/useAgentConversation'
import type { AgentCandidateOutputSnapshot } from '../hooks/useAgentCandidateOutput'
import type { ChangeSetReviewState } from '../hooks/useChangeSets'
import type { ProviderRecoverySnapshot } from '../hooks/useProviderRecovery'
import { gateItems } from '../data/workbenchFixture'
import { classifyAgentInstruction } from '../lib/agentIntent'
import { ChangeSetReview } from './ChangeSetReview'
import { DistillationCoveragePanel } from './DistillationCoveragePanel'
import { TomatoImportPanel } from './TomatoImportPanel'
import { SafeMarkdown } from './SafeMarkdown'
import { ContextPackReview } from './ContextPackReview'
import { KnowledgeReviewPanel } from './KnowledgeReviewPanel'
import { ProviderRecoveryPanel } from './ProviderRecoveryPanel'
import { AdjacentChainPanel } from './AdjacentChainPanel'
import { SkillHandoffPanel } from './SkillHandoffPanel'
import { ContextHandoffPanel } from './ContextHandoffPanel'
import { ManagedSkillLifecyclePanel } from './ManagedSkillLifecyclePanel'
import { ManagedSkillChainPanel } from './ManagedSkillChainPanel'
import { SkillAdaptationPanel } from './SkillAdaptationPanel'
import { FourSkillsInstructionChainPanel } from './FourSkillsInstructionChainPanel'
import { GateWorkbenchSurface } from './GateWorkbenchSurface'
import { ReaderWorkbenchPanel } from './ReaderWorkbenchPanel'
import { ProjectSearchPanel } from './ProjectSearchPanel'
import { ProjectAssetsPanel } from './ProjectAssetsPanel'
import { RuntimeOpsPanel } from './RuntimeOpsPanel'

export type InspectorTab =
  | 'agent'
  | 'sources'
  | 'knowledge'
  | 'changes'
  | 'context'
  | 'gate'
  | 'skills'
  | 'reader'
  | 'search'
  | 'assets'
  | 'more'
  | 'runtime'

const primaryTabs: Array<{ id: InspectorTab; label: string }> = [
  { id: 'agent', label: '对话' },
  { id: 'sources', label: '资料' },
  { id: 'changes', label: '改稿' },
  { id: 'more', label: '更多' },
]

const quickInstructions = [
  { label: '改当前章', instruction: '检查并修改当前章节。先读取正文和相关设定，保留既定剧情事实，给我可审阅的修改结果。' },
  { label: '续写下一章', instruction: '根据当前章节、章纲和项目设定续写下一章，先给我可审阅的正文结果，不要跳过必要的项目资料。' },
  { label: '去 AI 味', instruction: '在不改变剧情事实、人物动机和叙事视角的前提下，重写当前章节中过于模板化的表达。' },
  { label: '审这一章', instruction: '审读当前章节，检查开篇、冲突、人设、套话和章末钩子，并给出可执行的修改建议。' },
  { label: '查找伏笔', instruction: '搜索全书与当前章节有关的伏笔、人物关系和未收束线索，列出来源后再给建议。' },
  { label: '蒸馏资料', instruction: '把本次相关资料蒸馏成可复用的写作知识，并说明准备调用的技能与产出位置。' },
] as const

function primaryTabFor(tab: InspectorTab): InspectorTab {
  if (tab === 'agent' || tab === 'context') return 'agent'
  if (tab === 'sources') return 'sources'
  if (tab === 'changes') return 'changes'
  return 'more'
}

interface InspectorProps {
  runtime: RuntimeSnapshot
  onRefreshRuntime: () => Promise<void>
  demo: boolean
  chapterLabel: string
  projectId: string | null
  projectRevision?: number | null
  projectFingerprint?: string | null
  activeChapterId?: string | null
  projectError: string | null
  onRefreshProject: () => void
  conversation: AgentConversationState
  activeTab?: InspectorTab
  onTabChange?: (tab: InspectorTab) => void
  preferredChangeSetId?: string | null
  preferredChangeSetRequestKey?: number
  changeSetReview?: ChangeSetReviewState
  candidateOutput?: AgentCandidateOutputSnapshot
  providerRecovery?: ProviderRecoverySnapshot
  onReviewCandidate?: () => void
  onOpenSearchHit?: (sourceId: string, relativePath: string | null) => void
  onPrepareInstruction?: (instruction: string) => Promise<void>
  onDraftNextChapter?: () => Promise<void>
  onOpenSettings?: () => void
}

const emptyCandidateOutput: AgentCandidateOutputSnapshot = {
  output: null,
  state: 'idle',
  error: null,
  refresh: () => undefined,
}

function messageTime(value: string): string {
  const date = new Date(value)
  return Number.isNaN(date.getTime())
    ? ''
    : date.toLocaleTimeString('zh-CN', { hour: '2-digit', minute: '2-digit' })
}

function currentRouteExecution(
  executions: AgentConversationState['activeNlclRouteExecutions'],
) {
  const validExecutions = executions.filter((execution) => execution != null)
  return validExecutions.find((execution) => (
    execution.status === 'failed' || execution.status === 'cancelled'
  ))
    ?? validExecutions.find((execution) => execution.status === 'running')
    ?? (validExecutions.every((execution) => execution.status === 'succeeded')
      ? validExecutions.at(-1)
      : validExecutions.find((execution) => execution.status === 'awaiting_execution'))
    ?? validExecutions.at(-1)
    ?? null
}

function workflowStepLabel(skillId: string): string {
  if (skillId.endsWith('.project-audit')) return '核对作品资料'
  if (skillId.endsWith('.prewrite-gate')) return '检查写作条件'
  if (skillId.endsWith('.creation-handoff')) return '准备写作指令'
  return skillId
}

function conversationTime(value: string): string {
  const date = new Date(value)
  return Number.isNaN(date.getTime())
    ? ''
    : date.toLocaleString('zh-CN', {
        month: 'numeric',
        day: 'numeric',
        hour: '2-digit',
        minute: '2-digit',
      })
}

function conversationDocument(title: string, messages: AgentMessage[]): string {
  const turns = messages
    .filter((message) => message.role === 'user' || message.role === 'assistant')
    .map((message) => {
      const speaker = message.role === 'user' ? '你' : '写作助手'
      return `## ${speaker}\n\n${message.content.trim()}`
    })
  return [`# ${title || '写作对话'}`, ...turns].join('\n\n').trim() + '\n'
}

function conversationFileName(title: string): string {
  const safeTitle = (title || '写作对话')
    .replace(/[<>:"/\\|?*\u0000-\u001f]/g, '-')
    .replace(/\s+/g, ' ')
    .trim()
    .slice(0, 60)
  return `${safeTitle || '写作对话'}.md`
}


function routeExecutionAuthorSummary(
  execution: NonNullable<ReturnType<typeof currentRouteExecution>>,
  skillId?: string | null,
): { title: string; detail: string } {
  const step = skillId ? workflowStepLabel(skillId) : '写前检查'
  const domainStatus = execution.receipt?.domain_status ?? null
  if (execution.status === 'awaiting_execution') {
    return {
      title: `${step}等待执行`,
      detail: `正在等待系统执行${step}，不会在你确认前改动正文。`,
    }
  }
  if (execution.status === 'running') {
    return {
      title: `${step}进行中`,
      detail: `系统正在${step}，完成后会把结果交给你确认。`,
    }
  }
  if (execution.status === 'failed') {
    return {
      title: `${step}失败`,
      detail: execution.error_message ?? `${step}没有完成，请查看详情后重新准备本次任务。`,
    }
  }
  if (execution.status === 'cancelled') {
    return {
      title: `${step}已取消`,
      detail: execution.error_message ?? '本次任务已经停止，正文没有被改动。',
    }
  }
  if (domainStatus === 'blocked') {
    return {
      title: `${step}已完成`,
      detail: '已经生成待审结果，正文仍要由你确认后才会改动。',
    }
  }
  if (domainStatus === 'healthy') {
    return {
      title: `${step}已完成`,
      detail: '资料与写作条件已经核对，结果仍要由你确认后才会写入正文。',
    }
  }
  if (domainStatus === 'degraded') {
    return {
      title: `${step}已完成 · 有提醒`,
      detail: '检查已经完成，但还有需要你留意的事项。',
    }
  }
  return {
    title: `${step}已完成`,
    detail: '这一步已经完成，结果仍要由你确认。',
  }
}

function candidateKindLabel(kind: string): string {
  if (kind === 'repair_plan') return '修复方案'
  if (kind === 'risk_draft') return '风险草稿'
  if (kind === 'change_proposal') return '修改建议'
  return '下一步建议'
}

interface AuthorActivityItem {
  key: string
  label: string
  state: 'running' | 'succeeded' | 'failed'
}

function toolAuthorLabel(toolName: string): string {
  if (toolName === 'project_list_files') return '查看作品目录'
  if (toolName === 'project_read_file') return '阅读正文和设定'
  if (toolName === 'project_search') return '在全书中搜索相关内容'
  if (toolName === 'skill_execute') return '调用写作技能'
  if (toolName === 'change_set_propose') return '整理可审阅的修改'
  if (toolName === 'candidate_output_submit') return '整理本次结果'
  return '使用写作工具'
}

function authorActivityItems(activities: AgentActivity[]): AuthorActivityItem[] {
  const results = new Map<string, Extract<AgentActivity, { kind: 'tool_result' }>>()
  for (const activity of activities) {
    if (activity.kind === 'tool_result') results.set(activity.payload.tool_call_id, activity)
  }

  const items: AuthorActivityItem[] = []
  for (const activity of activities) {
    if (activity.kind === 'tool_call') {
      const result = results.get(activity.payload.tool_call_id)
      items.push({
        key: activity.payload.tool_call_id,
        label: toolAuthorLabel(activity.payload.tool_name),
        state: result?.payload.status === 'failed' || result?.payload.status === 'cancelled'
          ? 'failed'
          : result?.payload.status === 'succeeded'
            ? 'succeeded'
            : 'running',
      })
    }
  }
  return items.slice(-4)
}

export function Inspector({ runtime, onRefreshRuntime, demo, chapterLabel, projectId, projectRevision = null, projectFingerprint = null, activeChapterId = null, projectError, onRefreshProject, conversation, activeTab, onTabChange, preferredChangeSetId, preferredChangeSetRequestKey = 0, changeSetReview, candidateOutput = emptyCandidateOutput, providerRecovery, onReviewCandidate, onOpenSearchHit, onPrepareInstruction, onDraftNextChapter, onOpenSettings }: InspectorProps) {
  const [internalTab, setInternalTab] = useState<InspectorTab>('agent')
  const [contextView, setContextView] = useState<'coverage' | 'execution'>('coverage')
  const [instruction, setInstruction] = useState('')
  const [instructionError, setInstructionError] = useState<string | null>(null)
  const [writerMode, setWriterMode] = useState<WriterMode>('repair_only')
  const [previewBeforeRun, setPreviewBeforeRun] = useState(false)
  const [showConversationHistory, setShowConversationHistory] = useState(false)
  const [conversationActionNotice, setConversationActionNotice] = useState<string | null>(null)
  const [autoStartObjective, setAutoStartObjective] = useState<string | null>(null)
  const autoStartWriterMode = useRef<WriterMode>('repair_only')
  const autoStartConfirming = useRef(false)
  const autoOpenedContextPackId = useRef<string | null>(null)
  const [knowledgeSource, setKnowledgeSource] = useState<{
    projectId: string | null
    sourceImportId: string
  } | null>(null)
  const [skillCatalogRefreshKey, setSkillCatalogRefreshKey] = useState(0)
  const scopedProjectId = projectId?.trim() || null
  const activeKnowledgeSourceImportId = knowledgeSource?.projectId === scopedProjectId
    ? knowledgeSource.sourceImportId
    : null
  const tab = activeTab ?? internalTab
  const selectTab = useCallback((nextTab: InspectorTab) => {
    if (activeTab === undefined) setInternalTab(nextTab)
    onTabChange?.(nextTab)
  }, [activeTab, onTabChange])
  const refreshAssistant = useCallback(async () => {
    conversation.refresh()
    await onRefreshRuntime()
  }, [conversation.refresh, onRefreshRuntime])
  const pausedWorkflow = conversation.activeRun?.status === 'paused'
    && conversation.workflow?.run_id === conversation.activeRun.run_id
    ? conversation.workflow
    : null
  const providerBoundaryWorkflow = conversation.activeRun
    && ['failed', 'cancelled', 'interrupted'].includes(conversation.activeRun.status)
    && conversation.workflow?.run_id === conversation.activeRun.run_id
    && (
      conversation.workflow.provider_boundary !== null
      || conversation.workflow.resume_state === 'provider_dispatch_uncertain'
    )
    ? conversation.workflow
    : null
  const providerBoundaryRun = providerBoundaryWorkflow
    ? conversation.activeRun
    : null
  const managedProviderRecovery = providerBoundaryRun
    && ['failed', 'interrupted'].includes(providerBoundaryRun.status)
    && providerRecovery
    ? providerRecovery
    : null
  const recoveryRun = conversation.activeRun
    && ['failed', 'interrupted'].includes(conversation.activeRun.status)
    && providerBoundaryRun === null
    ? conversation.activeRun
    : null
  const runCanRetry = recoveryRun?.status === 'interrupted'
    || recoveryRun?.error?.error.retryable === true
  const latestRouteExecution = currentRouteExecution(
    conversation.activeNlclRouteExecutions,
  )
  const latestRouteSkillId = latestRouteExecution
    ? conversation.activeNlclRoute?.chapter_call_list.find(
        (call) => call.ordinal === latestRouteExecution.call_ordinal,
      )?.skill_id
      ?? latestRouteExecution.receipt?.skill_id
      ?? null
    : null
  const completedRouteExecutions = conversation.activeNlclRouteExecutions.filter(
    (execution) => execution.status === 'succeeded',
  ).length
  const totalRouteExecutions = conversation.activeNlclRoute?.chapter_call_list.length
    ?? conversation.activeNlclRouteExecutions.length
  const latestRouteExecutionSummary = latestRouteExecution
    ? routeExecutionAuthorSummary(latestRouteExecution, latestRouteSkillId)
    : null
  const writePreRunning = conversation.running
    && conversation.activeNlclRoute !== null
    && conversation.activeContextPack !== null
  const completedWorkflowSteps = pausedWorkflow?.checkpoints.filter(
    (checkpoint) => checkpoint.execution_status === 'succeeded',
  ).length ?? 0
  const providerBoundaryCompletedSteps = providerBoundaryWorkflow?.checkpoints.filter(
    (checkpoint) => checkpoint.execution_status === 'succeeded',
  ).length ?? 0
  const nextWorkflowStep = pausedWorkflow?.next_step_ordinal === null
    ? null
    : pausedWorkflow?.plan.steps.find(
        (step) => step.ordinal === pausedWorkflow.next_step_ordinal,
      ) ?? null
  const nextWorkflowAction = nextWorkflowStep
    ? `第 ${nextWorkflowStep.ordinal} 步 · ${workflowStepLabel(nextWorkflowStep.skill_id)}`
    : pausedWorkflow?.next_step_ordinal === null
        && completedWorkflowSteps === pausedWorkflow.plan.steps.length
      ? '调用模型写作'
      : '等待系统判断'
  const candidateForRun = conversation.activeRun
    && candidateOutput.output?.run_id === conversation.activeRun.run_id
    ? candidateOutput.output
    : null
  const candidateNoticeVisible = candidateForRun !== null
    || (conversation.activeRun?.status === 'succeeded'
      && ['loading', 'missing', 'error'].includes(candidateOutput.state))
  const assistantStatusText = runtime.connection !== 'online'
    ? runtime.connection === 'checking'
      ? '正在连接写作服务'
      : '写作服务暂不可用'
    : conversation.availability === 'available'
      ? '可以读取作品、调用工具与技能'
      : conversation.availability === 'checking'
        ? '正在检查写作模型'
        : conversation.availability === 'unavailable'
          ? '还没配置写作模型'
          : '写作助手状态读取失败'
  const assistantUnavailableDetail = conversation.availability === 'unavailable'
    ? '先配置接口地址、密钥和模型，写作助手才能开始工作。'
    : conversation.detail
  const visibleActivities = authorActivityItems(conversation.activities)

  useEffect(() => {
    const contextPackId = conversation.pendingContextPack?.pack.context_pack_id ?? null
    if (contextPackId === null) {
      autoOpenedContextPackId.current = null
      return
    }
    // Keep knowledge/source author work in place; only auto-open from Agent.
    if (
      !demo
      && tab === 'agent'
      && autoStartObjective === null
      && autoOpenedContextPackId.current !== contextPackId
    ) {
      autoOpenedContextPackId.current = contextPackId
      selectTab('context')
    }
  }, [autoStartObjective, conversation.pendingContextPack?.pack.context_pack_id, demo, selectTab, tab])

  // 日常对话默认是“发出指令就执行”。只有作者主动勾选预览时，
  // 才把内部资料包页面挡在 Agent 前面。
  useEffect(() => {
    const pending = conversation.pendingContextPack
    if (
      pending === null
      || autoStartObjective === null
      || pending.pack.objective !== autoStartObjective
      || autoStartConfirming.current
    ) {
      return
    }

    autoStartConfirming.current = true
    void conversation.confirmPendingContext(autoStartWriterMode.current)
      .then(() => {
        setAutoStartObjective(null)
        selectTab('agent')
      })
      .catch((caught) => {
        setInstructionError(caught instanceof Error ? caught.message : '写作助手启动失败')
        setAutoStartObjective(null)
        selectTab('agent')
      })
      .finally(() => {
        autoStartConfirming.current = false
      })
  }, [
    autoStartObjective,
    conversation.confirmPendingContext,
    conversation.pendingContextPack,
    selectTab,
  ])

  useEffect(() => {
    setKnowledgeSource(null)
    setWriterMode('repair_only')
    setInstructionError(null)
    setAutoStartObjective(null)
    setShowConversationHistory(false)
    setConversationActionNotice(null)
    autoStartConfirming.current = false
  }, [scopedProjectId])

  const exportableMessages = conversation.messages.filter(
    (message) => message.role === 'user' || message.role === 'assistant',
  )
  const copyCurrentConversation = useCallback(async () => {
    if (!conversation.session || exportableMessages.length === 0) return
    try {
      await navigator.clipboard.writeText(
        conversationDocument(conversation.session.title, exportableMessages),
      )
      setConversationActionNotice('已复制')
    } catch {
      setConversationActionNotice('复制失败')
    }
  }, [conversation.session, exportableMessages])
  const downloadCurrentConversation = useCallback(() => {
    if (!conversation.session || exportableMessages.length === 0) return
    const content = conversationDocument(conversation.session.title, exportableMessages)
    const url = URL.createObjectURL(new Blob([content], { type: 'text/markdown;charset=utf-8' }))
    const link = document.createElement('a')
    link.href = url
    link.download = conversationFileName(conversation.session.title)
    link.click()
    URL.revokeObjectURL(url)
    setConversationActionNotice('已导出')
  }, [conversation.session, exportableMessages])

  const submitInstruction = useCallback(async () => {
    const nextInstruction = instruction.trim()
    if (!nextInstruction) return
    if (demo) {
      setInstruction('')
      setInstructionError(null)
      return
    }

    const intent = classifyAgentInstruction(nextInstruction)
    if (intent.batchChapterCount !== null) {
      setInstructionError(
        `一次写 ${intent.batchChapterCount} 章需要可暂停、可恢复的批量任务队列；当前版本不会拿这条命令误改正在看的章节。`,
      )
      return
    }

    setInstructionError(null)
    setWriterMode(intent.writerMode)
    autoStartWriterMode.current = intent.writerMode
    try {
      if (intent.draftsNextChapter) {
        if (!onDraftNextChapter) throw new Error('续写下一章暂不可用')
        await onDraftNextChapter()
      } else if (onPrepareInstruction) {
        if (!previewBeforeRun) setAutoStartObjective(nextInstruction)
        await onPrepareInstruction(nextInstruction)
      } else {
        if (!previewBeforeRun) setAutoStartObjective(nextInstruction)
        await conversation.compileContext(nextInstruction)
      }
      setInstruction('')
    } catch (caught) {
      setAutoStartObjective(null)
      setInstructionError(caught instanceof Error ? caught.message : '任务准备失败')
    }
  }, [
    conversation,
    demo,
    instruction,
    onDraftNextChapter,
    onPrepareInstruction,
    previewBeforeRun,
  ])

  return (
    <aside className="inspector panel-mobile" data-mobile-pane="inspector" aria-label="写作助手">
      <div className="inspector-tabs" role="tablist" aria-label="写作助手视图">
        {primaryTabs.map((item) => (
          <button
            key={item.id}
            id={`inspector-tab-${item.id}`}
            type="button"
            role="tab"
            aria-label={item.label}
            aria-selected={primaryTabFor(tab) === item.id}
            className={primaryTabFor(tab) === item.id ? 'is-active' : ''}
            disabled={!runtime.ready && item.id !== 'agent' && item.id !== 'changes'}
            onClick={() => selectTab(item.id)}
          >
            {item.label}
          </button>
        ))}
      </div>

      {tab === 'agent' && (
        <div className="inspector-body agent-panel">
          <header className="assistant-header">
            <span className="assistant-mark"><Bot size={17} /></span>
            <div>
              <strong>写作助手</strong>
              <span>{assistantStatusText}</span>
            </div>
            <div className="assistant-header-actions">
              <button type="button" className={`icon-button${showConversationHistory ? ' is-active' : ''}`} aria-label="查看历史对话" title="历史对话" aria-expanded={showConversationHistory} onClick={() => setShowConversationHistory((value) => !value)}><History size={14} /></button>
              <button type="button" className="icon-button" aria-label="新建对话" title={conversation.availability === 'available' ? '新建对话' : '配置写作模型后可新建对话'} disabled={!projectId || conversation.availability !== 'available' || conversation.busy || conversation.pendingContextPack !== null} onClick={() => { void conversation.createConversation().then(() => setShowConversationHistory(false)).catch((caught) => setInstructionError(caught instanceof Error ? caught.message : '新建对话失败')) }}><SquarePen size={14} /></button>
              <button type="button" className="icon-button" aria-label="刷新写作助手" title="刷新写作助手" disabled={conversation.switchingSession} onClick={() => void refreshAssistant()}><RefreshCw size={14} /></button>
            </div>
          </header>
          {showConversationHistory && (
            <section className="assistant-session-switcher" aria-label="历史对话">
              <div className="assistant-session-heading"><div><strong>历史对话</strong><span>{conversationActionNotice ?? `${conversation.sessions.length} 条`}</span></div><div className="assistant-session-actions"><button type="button" className="icon-button" aria-label="复制当前对话" title="复制当前对话" disabled={exportableMessages.length === 0} onClick={() => void copyCurrentConversation()}><Copy size={13} /></button><button type="button" className="icon-button" aria-label="导出当前对话" title="导出 Markdown" disabled={exportableMessages.length === 0} onClick={downloadCurrentConversation}><Download size={13} /></button></div></div>
              <div className="assistant-session-list">
                {conversation.sessions.length === 0
                  ? <p>还没有历史对话</p>
                  : conversation.sessions.map((item) => {
                      const active = item.session_id === conversation.session?.session_id
                      return <button type="button" className={active ? 'is-active' : ''} key={item.session_id} disabled={conversation.busy || conversation.pendingContextPack !== null} onClick={() => { void conversation.selectConversation(item.session_id).then(() => setShowConversationHistory(false)).catch((caught) => setInstructionError(caught instanceof Error ? caught.message : '切换对话失败')) }}><MessageSquareText size={14} /><span><strong>{item.title || '写作对话'}</strong><small>{conversationTime(item.updated_at)}</small></span>{active && <Check size={13} aria-label="当前对话" />}</button>
                    })}
              </div>
            </section>
          )}
          <div className="assistant-target">
            <div><span>当前目标章节</span><strong>{demo ? '第 12 章' : chapterLabel}</strong></div>
            <span className="assistant-follow"><CircleDot size={11} />跟随正文</span>
          </div>
          {runtime.connection !== 'online' && (
            <div className={`runtime-notice runtime-${runtime.connection}`}>
              <span className="runtime-symbol">{runtime.connection === 'checking' ? <LoaderCircle size={15} className="spin" /> : <AlertTriangle size={15} />}</span>
              <div><strong>{runtime.connection === 'checking' ? '正在连接写作服务' : '写作服务暂不可用'}</strong><span>{runtime.detail}</span></div>
              <button type="button" className="icon-button" aria-label="重试连接写作服务" title="重试连接" onClick={() => void onRefreshRuntime()}><RefreshCw size={14} /></button>
            </div>
          )}

          <div className="conversation" aria-label="写作助手对话" aria-live="polite">
            {demo ? <>
              <div className="message-row message-agent"><span className="message-avatar"><Bot size={15} /></span><div><div className="message-meta"><strong>写作助手 · 示例</strong><time>11:41</time></div><p>我已读取第 12 章。周鹤亭的压力还没有真正进入场景，我会保留黄铜钥匙和葬礼上的女人。</p><div className="message-evidence"><FileSearch size={14} /><span>已参考 4 份资料</span><span>·</span><span>2 个写作提醒</span></div></div></div>
              <div className="message-row message-user"><span className="message-avatar"><UserRound size={15} /></span><div><div className="message-meta"><strong>你 · 示例</strong><time>11:42</time></div><p>把开头收紧一点，但别删黄铜钥匙和葬礼上的女人。先给我 Diff，不要写回。</p></div></div>
              <div className="pending-run"><Sparkles size={15} /><div><strong>示例修改方案</strong><span>当前只展示交互，不会改动作品</span></div></div>
            </> : projectError ? <div className="pending-run"><AlertTriangle size={15} /><div><strong>项目读取失败</strong><span>{projectError}</span><button type="button" className="quiet-button" onClick={onRefreshProject}><RefreshCw size={14} />重试</button></div></div> : <>
              {conversation.messages.map((message) => (
                <div className={`message-row message-${message.role === 'user' ? 'user' : 'agent'}`} key={message.message_id}>
                  <span className="message-avatar">{message.role === 'user' ? <UserRound size={15} /> : <Bot size={15} />}</span>
                  <div className="message-content"><div className="message-meta"><strong>{message.role === 'user' ? '你' : '写作助手'}</strong><time>{messageTime(message.created_at)}</time></div>{message.format === 'markdown' ? <SafeMarkdown content={message.content} /> : <p className="message-plain">{message.content}</p>}</div>
                </div>
              ))}
              {conversation.streamingContent && <div className="message-row message-agent message-streaming"><span className="message-avatar"><Bot size={15} /></span><div className="message-content"><div className="message-meta"><strong>写作助手</strong><span className="stream-indicator"><LoaderCircle size={11} className="spin" />生成中</span></div><SafeMarkdown content={conversation.streamingContent} streaming /></div></div>}
              {conversation.activeRun && visibleActivities.length > 0 && <div className="agent-live-work" aria-label="写作助手执行过程"><div className="agent-live-work-title"><Sparkles size={13} /><strong>{conversation.running ? '正在执行' : '本次执行过程'}</strong></div><ol>{visibleActivities.map((item) => <li className={`is-${item.state}`} key={item.key}>{item.state === 'running' ? <LoaderCircle size={12} className="spin" /> : item.state === 'succeeded' ? <Check size={12} /> : <AlertTriangle size={12} />}<span>{item.label}</span><small>{item.state === 'running' ? '进行中' : item.state === 'succeeded' ? '已完成' : '未完成'}</small></li>)}</ol></div>}
              {conversation.error && <div className="agent-inline-error" role="alert"><AlertTriangle size={14} /><span>{conversation.error}</span><button type="button" className="icon-button" aria-label="刷新写作助手对话" title="刷新对话" onClick={conversation.refresh}><RefreshCw size={13} /></button></div>}
              {pausedWorkflow && <div className="pending-run agent-workflow-paused" role="status"><CircleDot size={15} /><div><strong>本次任务已暂停</strong><span>已完成 {completedWorkflowSteps}/{pausedWorkflow.plan.steps.length} 步 · 下一步：{nextWorkflowAction}</span>{pausedWorkflow.resume_allowed ? <button type="button" className="quiet-button" disabled={conversation.resuming || conversation.availability !== 'available'} onClick={() => void conversation.resume().catch(() => undefined)}>{conversation.resuming ? <LoaderCircle size={13} className="spin" /> : <Play size={13} fill="currentColor" />}{conversation.resuming ? '继续中' : '继续任务'}</button> : <span>当前任务不能直接继续，请重新准备</span>}</div></div>}
              {managedProviderRecovery?.state === 'idle' && <div className="pending-run provider-recovery-state" role="status"><LoaderCircle size={15} className="spin" /><div><strong>正在核对模型返回结果</strong><span>已经完成 {providerBoundaryCompletedSteps}/{providerBoundaryWorkflow?.plan.steps.length ?? 0} 步，系统不会重复发送旧请求。</span></div></div>}
              {managedProviderRecovery && managedProviderRecovery.state !== 'idle' && <ProviderRecoveryPanel snapshot={managedProviderRecovery} compact onDecisionCommitted={conversation.refresh} onRecompile={providerBoundaryRun ? () => void conversation.compileContext(providerBoundaryRun.objective).catch(() => undefined) : undefined} recompileDisabled={conversation.busy || conversation.pendingContextPack !== null} />}
              {providerBoundaryRun && !managedProviderRecovery && <div className="pending-run agent-run-recovery run-interrupted" role="status"><AlertTriangle size={15} /><div><strong>模型返回状态不确定</strong><span>已经完成 {providerBoundaryCompletedSteps}/{providerBoundaryWorkflow?.plan.steps.length ?? 0} 步。为避免重复写作，系统不会自动重发；你可以检查已有结果或重新开始这次任务。</span><button type="button" className="quiet-button" disabled={conversation.resuming || conversation.pendingContextPack !== null} onClick={() => void conversation.compileContext(providerBoundaryRun.objective).catch(() => undefined)}><RefreshCw size={13} />重新开始这次任务</button></div></div>}
              {recoveryRun && <div className={`pending-run agent-run-recovery run-${recoveryRun.status}`} role="status"><AlertTriangle size={15} /><div><strong>{recoveryRun.status === 'failed' ? '上次任务失败' : '上次任务已中断'}</strong><span>{recoveryRun.error?.error.message ?? '写作服务在任务结束前停止'}</span>{runCanRetry && <button type="button" className="quiet-button" disabled={conversation.busy || conversation.pendingContextPack !== null} onClick={() => void conversation.compileContext(recoveryRun.objective).catch(() => undefined)}><RefreshCw size={13} />重新准备</button>}</div></div>}
              {candidateForRun && <div className="pending-run agent-candidate-ready" role="status"><FileText size={15} /><div><strong>修改结果待你确认</strong><span>{candidateKindLabel(candidateForRun.kind)} · {candidateForRun.content.title}</span>{onReviewCandidate && <button type="button" className="quiet-button" onClick={onReviewCandidate}><FileText size={13} />打开结果</button>}</div></div>}
              {!candidateForRun && conversation.activeRun?.status === 'succeeded' && candidateOutput.state === 'loading' && <div className="pending-run agent-candidate-loading" role="status"><FileText size={15} /><div><strong>正在读取写作结果</strong><span>正在核对结果与当前作品是否一致</span></div></div>}
              {!candidateForRun && conversation.activeRun?.status === 'succeeded' && candidateOutput.state === 'missing' && <div className="pending-run agent-candidate-missing" role="status"><FileText size={15} /><div><strong>本次任务没有可用结果</strong><span>写作服务没有保存可供审阅的内容。</span><button type="button" className="quiet-button" onClick={candidateOutput.refresh}><RefreshCw size={13} />重新检查</button></div></div>}
              {!candidateForRun && conversation.activeRun?.status === 'succeeded' && candidateOutput.state === 'error' && <div className="pending-run agent-candidate-error" role="alert"><AlertTriangle size={15} /><div><strong>候选输出读取失败</strong><span>{candidateOutput.error ?? '未知读取错误'}</span><button type="button" className="quiet-button" onClick={candidateOutput.refresh}><RefreshCw size={13} />重试</button></div></div>}
              {conversation.pendingContextPack && autoStartObjective === null && <div className="pending-run"><FileSearch size={15} /><div><strong>本次资料待你确认</strong><span>{conversation.pendingContextPack.pack.objective}</span><button type="button" className="quiet-button" onClick={() => selectTab('context')}>查看资料</button></div></div>}
              {conversation.pendingContextPack && autoStartObjective !== null && <div className="pending-run agent-auto-starting" role="status"><LoaderCircle size={15} className="spin" /><div><strong>资料已经备齐，正在开始</strong><span>写作助手会读取这些资料并按你的指令调用工具。</span></div></div>}
              {writePreRunning && conversation.activeNlclRoute && <div className="pending-run route-execution-summary run-running" role="status"><LoaderCircle size={15} className="spin" /><div><strong>正在准备写作 · {completedRouteExecutions}/{totalRouteExecutions} 步</strong><span>当前：{latestRouteExecutionSummary?.title ?? '正在核对作品资料与写作条件'}</span></div></div>}
              {!conversation.pendingContextPack && latestRouteExecution && latestRouteExecutionSummary && <div className={`pending-run route-execution-summary run-${latestRouteExecution.status}`}><ShieldCheck size={15} /><div><strong>写作准备 · {latestRouteExecutionSummary.title}</strong><span>{latestRouteExecutionSummary.detail}</span><details className="route-execution-technical"><summary>技术详情</summary><dl><div><dt>execution_status</dt><dd><code>{latestRouteExecution.status}</code></dd></div><div><dt>domain_status</dt><dd><code>{latestRouteExecution.receipt?.domain_status ?? 'not_reported'}</code></dd></div>{latestRouteExecution.receipt?.execution_receipt_id && <div><dt>execution_receipt_id</dt><dd><code>{latestRouteExecution.receipt.execution_receipt_id}</code></dd></div>}{latestRouteExecution.receipt?.receipt_hash && <div><dt>receipt_hash</dt><dd><code>{latestRouteExecution.receipt.receipt_hash}</code></dd></div>}{latestRouteExecution.error_code && <div><dt>error_code</dt><dd><code>{latestRouteExecution.error_code}</code></dd></div>}</dl></details><button type="button" className="quiet-button" onClick={() => { setContextView('execution'); selectTab('context') }}>查看详细记录</button></div></div>}
              {conversation.messages.length === 0 && !conversation.streamingContent && !conversation.error && !conversation.pendingContextPack && !pausedWorkflow && !providerBoundaryRun && !recoveryRun && !candidateNoticeVisible && <div className="pending-run assistant-empty-state"><CircleDot size={15} /><div><strong>{conversation.availability === 'available' ? '写作助手可以开始工作' : conversation.availability === 'checking' ? '正在准备写作助手' : '写作助手暂不可用'}</strong><span>{conversation.availability === 'available' ? `${chapterLabel}会作为当前目标，系统会自动整理相关资料；勾选“发送前看资料”可以先预览。` : assistantUnavailableDetail}</span>{conversation.availability === 'unavailable' && onOpenSettings && <button type="button" className="quiet-button" onClick={onOpenSettings}><Settings2 size={13} />配置写作模型</button>}</div></div>}
            </>}
          </div>

          <form className="agent-composer" onSubmit={(event) => { event.preventDefault(); void submitInstruction() }}>
            <div className="assistant-quick-actions" aria-label="常用写作指令">
              {quickInstructions.map((item) => <button type="button" key={item.label} disabled={!demo && (!runtime.ready || !projectId || conversation.busy || conversation.pendingContextPack !== null)} onClick={() => { setInstruction(item.instruction); setInstructionError(null) }}>{item.label}</button>)}
            </div>
            <textarea
              aria-label="给写作助手的指令"
              placeholder={demo ? '示例指令输入…' : runtime.ready && projectId ? '例如：修改第二章的开头，保留原有伏笔…' : runtime.detail}
              value={instruction}
              disabled={!demo && (!runtime.ready || !projectId || conversation.busy || conversation.pendingContextPack !== null)}
              onChange={(event) => { setInstruction(event.target.value); setInstructionError(null) }}
              onKeyDown={(event) => {
                if (event.key === 'Enter' && !event.shiftKey && !event.nativeEvent.isComposing) {
                  event.preventDefault()
                  event.currentTarget.form?.requestSubmit()
                }
              }}
            />
            {instructionError && <div className="agent-composer-error" role="alert"><AlertTriangle size={13} /><span>{instructionError}</span></div>}
            <div className="composer-context-status" aria-label="本次任务会使用的内容"><span className="is-on"><Check size={11} />自动资料</span><span className={activeChapterId ? 'is-on' : ''}><Check size={11} />当前正文</span><span className="is-on"><Check size={11} />修改先审阅</span></div>
            <div className="composer-actions"><div><span className="context-chip"><span>{demo ? '第 12 章' : chapterLabel}</span></span><label className="preview-before-run"><input type="checkbox" checked={previewBeforeRun} disabled={!demo && conversation.busy} onChange={(event) => setPreviewBeforeRun(event.target.checked)} /><Eye size={12} /><span>发送前看资料</span></label></div>{!demo && conversation.running ? <button className="send-button stop-button" type="button" aria-label="停止生成" title="停止生成" onClick={() => void conversation.cancel()}><Square size={14} fill="currentColor" /></button> : <button className="compile-context-button" type="submit" aria-label="发送给写作助手" disabled={demo ? !instruction.trim() : !runtime.ready || !projectId || !instruction.trim() || conversation.busy || conversation.pendingContextPack !== null}>{conversation.contextState === 'compiling' ? <LoaderCircle size={14} className="spin" /> : previewBeforeRun ? <Eye size={14} /> : <SendHorizontal size={14} />}<span>{conversation.contextState === 'compiling' ? '正在整理资料' : previewBeforeRun ? '预览并发送' : '发送'}</span></button>}</div>
          </form>
        </div>
      )}

      {tab === 'more' && (
        <div className="inspector-body assistant-more">
          <header><strong>更多能力</strong><span>按需要打开，不挤占日常写作界面</span></header>
          <div className="assistant-more-list">
            <button type="button" disabled={!runtime.ready} onClick={() => selectTab('search')}><Search size={17} /><span><strong>全书搜索</strong><small>章节、人物、伏笔</small></span></button>
            <button type="button" disabled={!runtime.ready} onClick={() => selectTab('assets')}><Files size={17} /><span><strong>项目文件</strong><small>作品目录与文件</small></span></button>
            <button type="button" disabled={!runtime.ready} onClick={() => selectTab('knowledge')}><Library size={17} /><span><strong>写作知识</strong><small>蒸馏结果与知识库</small></span></button>
            <button type="button" disabled={!runtime.ready} onClick={() => selectTab('gate')}><ShieldCheck size={17} /><span><strong>写作检查</strong><small>改稿前后的硬性问题</small></span></button>
            <button type="button" disabled={!runtime.ready} onClick={() => selectTab('reader')}><MessageSquareText size={17} /><span><strong>审稿反馈</strong><small>读者视角与修改意见</small></span></button>
            <button type="button" disabled={!runtime.ready} onClick={() => selectTab('skills')}><Sparkles size={17} /><span><strong>技能管理</strong><small>可执行技能与接入状态</small></span></button>
            <button type="button" disabled={!runtime.ready} onClick={() => selectTab('context')}><FolderSearch size={17} /><span><strong>本次资料</strong><small>当前任务实际读取的内容</small></span></button>
            <button type="button" onClick={() => selectTab('runtime')}><Activity size={17} /><span><strong>运行与备份</strong><small>服务状态、备份与恢复</small></span></button>
          </div>
        </div>
      )}

      {tab === 'runtime' && (
        <div className="inspector-body assistant-runtime">
          <header className="assistant-section-heading"><Settings2 size={16} /><div><strong>运行与备份</strong><span>写作服务、数据备份与恢复</span></div></header>
          <RuntimeOpsPanel enabled={runtime.ready} />
        </div>
      )}

      {tab === 'changes' && (
        <ChangeSetReview projectId={projectId} onProjectChanged={onRefreshProject} preferredChangeSetId={preferredChangeSetId} preferredChangeSetRequestKey={preferredChangeSetRequestKey} review={changeSetReview} />
      )}

      {tab === 'search' && (
        <ProjectSearchPanel
          enabled={runtime.ready}
          projectId={projectId}
          onOpenProjectFile={onOpenSearchHit}
        />
      )}

      {tab === 'assets' && (
        <ProjectAssetsPanel
          enabled={runtime.ready}
          projectId={projectId}
        />
      )}

      {tab === 'sources' && (
        <TomatoImportPanel
          enabled={runtime.ready}
          projectId={scopedProjectId}
          projectRevision={projectRevision}
          projectFingerprint={projectFingerprint}
          onReviewDraft={(sourceImportId) => {
            setKnowledgeSource({ projectId: scopedProjectId, sourceImportId })
            selectTab('knowledge')
          }}
        />
      )}

      {tab === 'knowledge' && <div className="knowledge-workflow-stack">
        <KnowledgeReviewPanel enabled={runtime.ready} projectId={projectId} projectRevision={projectRevision} projectFingerprint={projectFingerprint} sourceImportId={activeKnowledgeSourceImportId} onClearSourceImport={() => setKnowledgeSource(null)} startWritingBusy={conversation.busy || conversation.pendingContextPack !== null} onStartWriting={async (selector) => {
          setWriterMode('author_review_change_proposal')
          await conversation.compileContext('使用本批已正式化知识，为当前章节生成可供作者逐项审阅的正文修改建议。', 'chapter', selector)
          selectTab('context')
        }} />
        <AdjacentChainPanel
          enabled={runtime.ready}
          projectId={projectId}
          projectRevision={projectRevision}
          projectFingerprint={projectFingerprint}
          sourceImportId={activeKnowledgeSourceImportId}
          activeChapterId={activeChapterId}
          openContextReviewBusy={conversation.busy}
          onOpenContextReview={async (contextPackId) => {
            await conversation.openContextPackForReview(contextPackId)
            selectTab('context')
          }}
        />
        <SkillHandoffPanel enabled={runtime.ready} projectId={projectId} projectRevision={projectRevision} projectFingerprint={projectFingerprint} sourceImportId={activeKnowledgeSourceImportId} />
        <ContextHandoffPanel enabled={runtime.ready} projectId={projectId} projectRevision={projectRevision} projectFingerprint={projectFingerprint} />
      </div>}

      {tab === 'context' && (conversation.pendingContextPack
        ? <ContextPackReview
            record={conversation.pendingContextPack}
            nlclRoute={conversation.pendingNlclRoute}
            routeExecutions={conversation.pendingNlclRouteExecutions}
            operationPackage={conversation.pendingPrewriteOperationPackage}
            state={conversation.contextState}
            staleReasons={conversation.contextStaleReasons}
            agentAvailable={conversation.availability === 'available'}
            agentDetail={conversation.detail}
            runActive={conversation.running}
            error={conversation.error}
            writerMode={writerMode}
            onWriterModeChange={setWriterMode}
            onConfirm={async () => {
              await conversation.confirmPendingContext(writerMode)
              selectTab('agent')
            }}
            onDiscard={() => {
              conversation.discardPendingContext()
              setWriterMode('repair_only')
            }}
            onRefresh={conversation.refreshPendingContext}
          />
        : contextView === 'execution' && conversation.activeContextPack && conversation.activeNlclRoute
          ? <>
              <div className="context-view-switch"><button type="button" className="quiet-button" onClick={() => setContextView('coverage')}><RefreshCw size={14} />返回蒸馏覆盖</button></div>
              <ContextPackReview
                record={conversation.activeContextPack}
                nlclRoute={conversation.activeNlclRoute}
                routeExecutions={conversation.activeNlclRouteExecutions}
                operationPackage={conversation.activePrewriteOperationPackage}
                state={conversation.activeContextPack.pack.readiness}
                staleReasons={[]}
                agentAvailable={conversation.availability === 'available'}
                agentDetail={conversation.detail}
                runActive={conversation.running}
                error={conversation.error}
                readOnly
              />
            </>
        : <>
            {conversation.activeContextPack && conversation.activeNlclRoute && <div className="context-view-switch"><button type="button" className="quiet-button" onClick={() => setContextView('execution')}><ShieldCheck size={14} />查看最近执行收据</button></div>}
            <DistillationCoveragePanel enabled={runtime.ready} />
          </>)}

      {tab === 'skills' && (
        <div className="knowledge-workflow-stack">
          <ManagedSkillLifecyclePanel
            key={`managed-skills-${skillCatalogRefreshKey}`}
            enabled={runtime.ready}
            projectId={projectId}
          />
          <SkillAdaptationPanel
            enabled={runtime.ready}
            onImported={() => setSkillCatalogRefreshKey((value) => value + 1)}
          />
          <FourSkillsInstructionChainPanel
            enabled={runtime.ready}
            projectId={projectId}
            projectRevision={projectRevision}
            projectFingerprint={projectFingerprint}
            onImported={() => setSkillCatalogRefreshKey((value) => value + 1)}
          />
          <ManagedSkillChainPanel
            enabled={runtime.ready}
            projectId={projectId}
            projectRevision={projectRevision}
            projectFingerprint={projectFingerprint}
          />
        </div>
      )}

      {tab === 'gate' && demo && (
        <div className="inspector-body gate-panel">
          <div className="gate-summary"><ShieldCheck size={19} /><div><strong>可继续生成候选</strong><span>0 阻断 · 2 警告 · 2 通过</span></div></div>
          <div className="gate-list">{gateItems.map((item) => <div className={`gate-row gate-${item.state}`} key={item.title}><span className="gate-level">{item.level}</span><span className="gate-icon">{item.state === 'pass' ? <CheckCircle2 size={15} /> : <AlertTriangle size={15} />}</span><div><strong>{item.title}</strong><span>{item.detail}</span></div></div>)}</div>
          <button type="button" className="full-action"><RefreshCw size={15} />重新运行门禁</button>
        </div>
      )}

      {tab === 'gate' && !demo && (
        <GateWorkbenchSurface
          enabled={runtime.ready}
          projectId={projectId}
          projectRevision={projectRevision}
          projectFingerprint={projectFingerprint}
          changeSetId={changeSetReview?.selectedId ?? preferredChangeSetId ?? null}
          reviewRevision={changeSetReview?.detail?.review_revision ?? null}
          changeSetStatus={changeSetReview?.detail?.status ?? null}
        />
      )}

      {tab === 'reader' && demo && (
        <div className="inspector-body reader-panel">
          <div className="reader-mode"><span className="avatar">冷</span><div><strong>严格冷读</strong><span>仅使用已发布前文与当前候选</span></div><button type="button" className="quiet-button">切换</button></div>
          <section><span className="section-label">继续阅读意愿</span><div className="score-line"><strong>7.4</strong><span>/ 10</span><div className="score-bar"><i style={{ width: '74%' }} /></div></div></section>
          <section><h3>读者在追问</h3><ul className="reader-questions"><li>黑色轿车里的人是否在监视林见川？</li><li>葬礼上的女人为什么知道钥匙？</li><li>周鹤亭是否已经拿到另一把钥匙？</li></ul></section>
          <section className="reader-warning"><AlertTriangle size={15} /><div><strong>章末危险不够即时</strong><span>认出女人后，场景压力暂时下降。</span></div></section>
          <button type="button" className="full-action"><Sparkles size={15} />对当前候选冷读</button>
        </div>
      )}

      {tab === 'reader' && !demo && (
        <ReaderWorkbenchPanel
          enabled={runtime.ready}
          detail={changeSetReview?.detail ?? null}
          onOpenChanges={() => selectTab('changes')}
        />
      )}

    </aside>
  )
}
