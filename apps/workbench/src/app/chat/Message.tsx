import { memo, useEffect, useMemo, useRef, useState, type ReactNode } from 'react'
import { ChevronRight, Copy, GitBranch, ListChecks, Pencil, RotateCcw, ScanSearch } from 'lucide-react'
import type { AgentActivity, AgentMessage } from '../../api/agent'
import type { ProjectInspection } from '../../api/projects'
import { presentAiText } from '../../lib/humanizeAiText'
import { MessageImages } from './ImageAttachments'
import { T } from '../strings'
import { IconBtn, Spinner } from '../ui'
import { Markdown } from './Markdown'
import { MessageDetails } from './MessageDetails'
import { PlanChecklist, StepList, StepsFold, Thinking } from './Steps'
import { isParameterNote, latestPlan, stepRows, thoughtBlocksOf, turnTailActivities, type StepRow, type ThoughtBlock } from './stepRows'
import './visibility.css'

const DeferredProcessContent = memo(function DeferredProcessContent({ content, render }: {
  content: ReactNode
  render: boolean
}) {
  // 折叠的历史过程不交子树给 React 协调；展开过以后正常跟随新记录更新。
  return render ? <>{content}</> : null
}, (before, after) => !before.render && !after.render)

export function ProcessGroup({ children, currentAction, onLoad, initiallyOpen = false, stateKey, states, summary = null }: {
  children: ReactNode; currentAction?: string | null; onLoad?: () => Promise<void>; initiallyOpen?: boolean
  stateKey?: string; states?: Map<string, boolean>
  /** 可选：折叠时用后端已有摘要显示工作量，不为此读取/构建全部历史过程。 */
  summary?: string | null
}) {
  const [loadState, setLoadState] = useState<'idle' | 'loading' | 'failed' | 'done'>('idle')
  const loadStatus = useRef(loadState)
  const [chosenOpen, setChosenOpen] = useState<boolean | undefined>(() => stateKey ? states?.get(stateKey) : undefined)
  const open = chosenOpen ?? initiallyOpen
  const [hasOpened, setHasOpened] = useState(open)
  const contentRef = useRef(children)
  contentRef.current = children
  const shouldRender = open || hasOpened
  const frozenContent = useRef(children)
  if (shouldRender) frozenContent.current = children
  const load = (retry = false) => {
    if (!onLoad || (loadStatus.current !== 'idle' && !(retry && loadStatus.current === 'failed'))) return
    loadStatus.current = 'loading'
    setLoadState('loading')
    void onLoad().then(() => { loadStatus.current = 'done'; setLoadState('done') }, () => { loadStatus.current = 'failed'; setLoadState('failed') })
  }
  useEffect(() => { if (open) { setHasOpened(true); load() } }, [open, onLoad])
  return <details className="zm-process-group" open={open}>
    <summary aria-label="查看处理过程" onClick={(event) => {
      event.preventDefault()
      const next = !open
      setChosenOpen(next)
      if (stateKey) states?.set(stateKey, next)
    }}><ChevronRight aria-hidden />{currentAction ? <Spinner /> : null}<span>{currentAction || (summary ? `处理过程 · ${summary}` : '查看处理过程')}</span></summary>
    <div className="zm-process-content">
      {loadState === 'loading' ? <p className="zm-muted-line">正在读取处理过程…</p> : null}
      {loadState === 'failed' ? <button type="button" className="zm-link" onClick={() => load(true)}>过程暂未读取，点击重试</button> : null}
      <DeferredProcessContent content={shouldRender ? contentRef.current : frozenContent.current} render={shouldRender} />
    </div>
  </details>
}

export function HistoryProcessLookup({ onLoad }: { onLoad: () => Promise<void> }) {
  const [state, setState] = useState<'idle' | 'loading' | 'failed'>('idle')
  return <IconBtn size="sm" label={state === 'loading' ? '正在读取处理记录' : state === 'failed' ? '重新读取处理记录' : '查看处理记录'}
    icon={state === 'loading' ? <Spinner /> : <ListChecks />} className="zm-history-process" disabled={state === 'loading'} onClick={() => {
    setState('loading')
    void onLoad().then(() => setState('idle'), () => setState('failed'))
  }} />
}

function ParameterNotes({ activities }: { activities: AgentActivity[] }) {
  const notes = [...new Set(activities.filter(isParameterNote).map((item) => (item.kind === 'reasoning' ? item.payload.summary : '')))]
  if (notes.length === 0) return null
  return <div className="zm-param-notes" role="status">{notes.map((note) => <p key={note}>{note}</p>)}</div>
}

/** 保留真实先后：思考、工具、下一段思考。相邻工具仍可分组。 */
function TurnProcess({ blocks, rows, live = false, onOpenFile, onShowChange, onShowCommands, inline = false }: {
  blocks: ThoughtBlock[]
  rows: StepRow[]
  live?: boolean
  onOpenFile?: (path: string) => void
  onShowChange?: (id: string) => void
  onShowCommands?: (id: string) => void
  inline?: boolean
}) {
  const parts = useMemo(() => {
    const events = [
      ...blocks.map((block) => ({ type: 'thought' as const, key: block.key, sequence: block.sequence, block })),
      ...rows.map((row) => ({ type: 'step' as const, key: row.key, sequence: row.sequence ?? 0, row })),
    ].sort((a, b) => a.sequence - b.sequence)
    const result: Array<{ type: 'thought'; key: string; block: ThoughtBlock } | { type: 'steps'; key: string; rows: StepRow[] }> = []
    for (const event of events) {
      if (event.type === 'thought') result.push(event)
      else {
        const previous = result.at(-1)
        if (previous?.type === 'steps') previous.rows.push(event.row)
        else result.push({ type: 'steps', key: event.key, rows: [event.row] })
      }
    }
    return result
  }, [blocks, rows])
  if (parts.length === 0) return null
  return <div className="zm-turn-process">{parts.map((part) => part.type === 'thought'
    ? <Thinking key={part.key} storageKey={part.key} thoughts={[part.block.text]}
      summary={part.block.visibility === 'summary'} interrupted={part.block.phase === 'interrupted'}
      live={live && part.block.phase === 'delta'} />
    : live || inline ? <StepList key={part.key} rows={part.rows} onOpenFile={onOpenFile} onShowChange={onShowChange} onShowCommands={onShowCommands} live={live} />
      : <StepsFold key={part.key} rows={part.rows} onOpenFile={onOpenFile} onShowChange={onShowChange} onShowCommands={onShowCommands} />)}</div>
}

/** 插话本身是明确的时间边界：它到来之前已执行的步骤展示在用户消息之前。 */
export function ProcessBeforeUser({ message, activities, snapshot, onOpenFile, onShowChange, onShowCommands }: {
  message: AgentMessage
  activities: AgentActivity[]
  snapshot: ProjectInspection | null
  onOpenFile?: (path: string) => void
  onShowChange?: (id: string) => void
  onShowCommands?: (id: string) => void
}) {
  const segment = useMemo(() => {
    const run = activities.filter((activity) => activity.run_id === message.run_id)
    const boundary = run.find((activity) => activity.kind === 'message' && activity.payload.role === 'user'
      && activity.payload.phase === 'completed' && activity.payload.message_id === message.message_id)?.sequence
    if (boundary === undefined) return []
    const previous = run.reduce((latest, activity) => activity.sequence < boundary && activity.kind === 'message'
      && activity.payload.phase === 'completed' && (activity.payload.role === 'user' || activity.payload.role === 'assistant')
      ? Math.max(latest, activity.sequence) : latest, 0)
    return run.filter((activity) => activity.sequence > previous && activity.sequence < boundary)
  }, [activities, message.message_id, message.run_id])
  const rows = useMemo(() => stepRows(segment, snapshot), [segment, snapshot])
  const thoughts = useMemo(() => thoughtBlocksOf(segment, message.run_id), [segment, message.run_id])
  if (rows.length === 0 && thoughts.length === 0 && !segment.some(isParameterNote)) return null
  return <div className="zm-before-steering" role="group" aria-label="补充指令前的处理过程">
    <ParameterNotes activities={segment} />
    <TurnProcess blocks={thoughts} rows={rows} onOpenFile={onOpenFile} onShowChange={onShowChange} onShowCommands={onShowCommands} inline />
  </div>
}

export interface MessageActions {
  onCopy: (text: string) => void
  onEdit?: (text: string) => void
  onRegenerate?: () => void
  regenerateDisabled?: boolean
  onFork?: () => void
  onReview?: () => void
  reviewDisabled?: boolean
}

/** 用户说的话：靠右一个气泡；鼠标移上来露出复制 / 改了重发 / 从这里分叉。 */
export function UserMessage({ message, actions, isLatest, onOpenFile }: {
  message: AgentMessage
  actions: MessageActions
  isLatest: boolean
  onOpenFile?: (path: string) => void
}) {
  return (
    <div className={`zm-msg is-user${isLatest ? ' is-latest' : ''}`}>
      <span className="zm-sr-only">{T.you}</span>
      <div className="zm-bubble">
        <MessageImages images={message.images} />
        {!(message.images?.length && message.content === '[图片]') ? <Markdown text={message.content} onOpenFile={onOpenFile} /> : null}
      </div>
      <div className="zm-msg-actions">
        <IconBtn size="sm" label={T.agent.copyPrompt} icon={<Copy />} onClick={() => actions.onCopy(message.content)} />
        {actions.onEdit ? <IconBtn size="sm" label={T.agent.editPrompt} icon={<Pencil />} onClick={() => actions.onEdit?.(message.content)} /> : null}
        {actions.onFork ? <IconBtn size="sm" label={T.agent.forkConversationTitle} icon={<GitBranch />} onClick={actions.onFork} /> : null}
      </div>
    </div>
  )
}

/** Agent 的回答：处理过程、正文、修改卡，底下保留操作与真实用量。 */
export function AgentMessageView({ message, activities, stepsUnloaded, onLoadSteps, snapshot, cards, actions, isLatest, onOpenFile, onShowChange, onShowCommands,
  progress = false, showUnpartitionedProcess = true, processOnly = false, hideProcess = false, inlineProcess = false, historyLookup, resultDetails }: {
  message: AgentMessage
  activities: AgentActivity[]
  /** 重新打开的对话里旧的一轮：步骤还没从后台读回来。 */
  stepsUnloaded: boolean
  onLoadSteps: () => Promise<void>
  snapshot: ProjectInspection | null
  cards: ReactNode
  actions: MessageActions
  isLatest: boolean
  onOpenFile?: (path: string) => void
  onShowChange?: (id: string) => void
  onShowCommands?: (id: string) => void
  /** 同一轮的阶段说明保留全文与操作，以较轻的层级接在下一段前面。 */
  progress?: boolean
  responseHeading?: boolean
  processOnly?: boolean
  hideProcess?: boolean
  inlineProcess?: boolean
  historyLookup?: ReactNode
  resultDetails?: ReactNode
  /** 旧记录没有消息边界时整轮过程只放一次。 */
  showUnpartitionedProcess?: boolean
}) {
  const presented = useMemo(() => presentAiText(message.content, { preserveMarkdown: true }), [message.content])
  const [loadState, setLoadState] = useState<'idle' | 'loading' | 'failed' | 'done'>('idle')
  const runActivities = useMemo(() => {
    if (hideProcess) return []
    const run = activities.filter((item) => item.run_id === message.run_id)
    const end = run.find((item) => item.kind === 'message' && item.payload.phase === 'completed' && item.payload.message_id === message.message_id)?.sequence
    if (end === undefined) return showUnpartitionedProcess ? run : []
    const start = run.reduce((latest, item) => item.sequence < end && item.kind === 'message' && item.payload.phase === 'completed'
      && (item.payload.role === 'assistant' || item.payload.role === 'user') ? Math.max(latest, item.sequence) : latest, 0)
    return run.filter((item) => item.sequence > start && item.sequence <= end)
  }, [activities, hideProcess, message.message_id, message.run_id, showUnpartitionedProcess])
  const rows = useMemo(() => hideProcess ? [] : stepRows(runActivities, snapshot), [hideProcess, runActivities, snapshot])
  const runHasSteps = useMemo(() => !hideProcess && loadState === 'done' && stepRows(activities.filter((activity) => activity.run_id === message.run_id), snapshot).length > 0,
    [activities, hideProcess, loadState, message.run_id, snapshot])
  const parameterNotes = useMemo(() => [...new Set(activities.filter((activity) => activity.run_id === message.run_id && isParameterNote(activity))
    .map((activity) => activity.kind === 'reasoning' ? activity.payload.content ?? activity.payload.summary : '').filter(Boolean))], [activities, message.run_id])
  const thoughts = useMemo(() => {
    if (hideProcess) return []
    const list = thoughtBlocksOf(runActivities, message.run_id)
    const embedded = presented.thinking
    if (embedded && !list.some((block) => block.text.trim() === embedded.trim())) {
      list.push({ key: `${message.run_id}:embedded`, text: embedded, sequence: 0, phase: 'completed', visibility: 'full' })
    }
    return list
  }, [hideProcess, message.run_id, presented.thinking, runActivities])
  const actionButtons = <div className="zm-msg-actions">
    <IconBtn size="sm" label={T.agent.copyReply} icon={<Copy />} onClick={() => actions.onCopy(presented.answer)} />
    {actions.onRegenerate ? <IconBtn size="sm" label={T.agent.regenerate} icon={<RotateCcw />} disabled={actions.regenerateDisabled} onClick={actions.onRegenerate} /> : null}
    {actions.onReview ? <IconBtn size="sm" label="独立复核结果（只读）" icon={<ScanSearch />} disabled={actions.reviewDisabled} onClick={actions.onReview} /> : null}
    {actions.onFork ? <IconBtn size="sm" label={T.agent.forkConversationTitle} icon={<GitBranch />} onClick={actions.onFork} /> : null}
  </div>
  if (processOnly && rows.length === 0 && thoughts.length === 0 && !runActivities.some(isParameterNote)) return null
  return (
    <div className={`zm-msg is-agent${progress ? ' is-progress' : ''}${isLatest ? ' is-latest' : ''}`}>
      <span className="zm-sr-only">{T.agent.title}</span>
      {!hideProcess ? <><ParameterNotes activities={runActivities} />
      <TurnProcess blocks={thoughts} rows={rows} onOpenFile={onOpenFile} onShowChange={onShowChange} onShowCommands={onShowCommands} inline={inlineProcess} /></> : null}
      {!hideProcess && stepsUnloaded ? (
        <button
          type="button"
          className="zm-fold-head is-quiet"
          disabled={loadState === 'loading'}
          onClick={() => {
            setLoadState('loading')
            onLoadSteps().then(() => setLoadState('done'), () => setLoadState('failed'))
          }}
        >
          {loadState === 'loading' ? <Spinner /> : null}
          <span>{loadState === 'failed' ? T.agent.runStepsFailed : T.agent.showRunSteps}</span>
        </button>
      ) : null}
      {loadState === 'done' && !stepsUnloaded && !runHasSteps ? <p className="zm-muted-line">{T.agent.runStepsNone}</p> : null}
      {!processOnly && presented.answer ? <Markdown text={presented.answer} onOpenFile={onOpenFile} /> : null}
      {!processOnly ? cards : null}
      {!processOnly && !progress ? <MessageDetails usage={message.usage} runUsage={message.run_usage} parameterNotes={parameterNotes} /> : null}
      {!processOnly ? <div className={progress ? 'zm-process-actions' : 'zm-msg-foot'}>{actionButtons}{historyLookup}</div> : null}
      {!processOnly && !progress ? resultDetails : null}
    </div>
  )
}

/** 正在进行的这一轮：计划、步骤（边做边出）、思考、边写边出的回答；上面一行过了几秒。 */
export function LiveTurn({ activities, runId, startedAt, streaming, snapshot, onOpenFile, onShowChange, onShowCommands, live = true, afterUserSequence = 0, processOnly = false, hideProcess = false }: {
  activities: AgentActivity[]
  runId: string | null
  startedAt: string | null
  streaming: string
  snapshot: ProjectInspection | null
  onOpenFile?: (path: string) => void
  onShowChange?: (id: string) => void
  onShowCommands?: (id: string) => void
  /** 终态尾段保留已收到内容；不继续显示等待动画。 */
  live?: boolean
  /** 已展示在用户插话之前的过程不再出现在流式尾段。 */
  afterUserSequence?: number
  processOnly?: boolean
  hideProcess?: boolean
}) {
  const runActivities = useMemo(() => hideProcess ? [] : turnTailActivities(activities, runId).filter((activity) => activity.sequence > afterUserSequence), [activities, afterUserSequence, hideProcess, runId])
  const plan = useMemo(() => hideProcess ? null : latestPlan(runActivities, runId), [hideProcess, runActivities, runId])
  const rows = useMemo(() => hideProcess ? [] : stepRows(runActivities, snapshot).filter((row) => !row.plan), [hideProcess, runActivities, snapshot])
  const presented = useMemo(() => streaming ? presentAiText(streaming, { preserveMarkdown: true }) : null, [streaming])
  const thoughts = useMemo(() => {
    if (hideProcess) return []
    const list = thoughtBlocksOf(runActivities, runId)
    const embedded = presented?.thinking
    if (embedded && !list.some((block) => block.text.trim() === embedded.trim())) {
      list.push({ key: `${runId}:embedded`, text: embedded, sequence: 0, phase: live ? 'delta' : 'interrupted', visibility: 'full' })
    }
    return list
  }, [hideProcess, runActivities, runId, presented?.thinking, live])
  const [elapsed, setElapsed] = useState(0)
  useEffect(() => {
    if (!startedAt || !live || hideProcess || processOnly) return undefined
    const tick = () => setElapsed(Math.max(0, Math.round((Date.now() - Date.parse(startedAt)) / 1000)))
    tick()
    const timer = window.setInterval(tick, 1000)
    return () => window.clearInterval(timer)
  }, [startedAt, live, hideProcess, processOnly])
  return (
    <div className={`zm-msg is-agent${live ? ' is-live' : ''}`} aria-live={live ? 'polite' : undefined}>
      <span className="zm-sr-only">{T.agent.title}</span>
      {!hideProcess ? <>{plan ? <PlanChecklist items={plan} live={live} /> : null}
      <TurnProcess blocks={thoughts} rows={rows} live={live} onOpenFile={onOpenFile} onShowChange={onShowChange} onShowCommands={onShowCommands} inline />
      <ParameterNotes activities={runActivities} /></> : null}
      {!processOnly && presented?.answer ? (
        <Markdown text={presented.answer} streaming={live} onOpenFile={onOpenFile} />
      ) : !processOnly && !hideProcess && live && !thoughts.some((block) => block.phase === 'delta') ? (
        <p className="zm-working"><span className="zm-shimmer">{rows.some((row) => row.state === 'running') ? T.agent.status.working : '等待模型回复…'}</span>{elapsed >= 3 ? <span className="zm-working-time">{T.agent.events.elapsed(elapsed)}</span> : null}</p>
      ) : null}
    </div>
  )
}
