import { useEffect, useId, useMemo, useState } from 'react'
import {
  Brain,
  BookOpen,
  Check,
  ChevronRight,
  CircleAlert,
  CircleDashed,
  FileDiff,
  FilePen,
  FileText,
  FolderTree,
  Globe,
  Lightbulb,
  ListChecks,
  Package,
  Search,
  SquareTerminal,
  Wrench,
} from 'lucide-react'
import { T } from '../strings'
import { IconBtn, Spinner } from '../ui'
import { A } from '../text'
import { useChatPreferences } from '../lib/chatPreferences'
import { groupSteps, type PlanItem, type StepKind, type StepRow } from './stepRows'
import './visibility.css'
import { SourceLink } from './ConversationSources'

const E = T.agent.events

const KIND_ICONS: Record<StepKind, typeof FileText> = {
  read: FileText,
  list: FolderTree,
  search: Search,
  write: FilePen,
  command: SquareTerminal,
  web: Globe,
  skill: BookOpen,
  memory: Brain,
  plan: ListChecks,
  change: FileDiff,
  artifact: Package,
  error: CircleAlert,
  other: Wrench,
}

function StepIcon({ kind, state }: { kind: StepKind; state: StepRow['state'] }) {
  if (state === 'running') return <span className="zm-step-icon"><Spinner /></span>
  const Icon = KIND_ICONS[kind]
  return <span className="zm-step-icon"><Icon aria-hidden /></span>
}

function duration(row: StepRow): string | null {
  if (row.state === 'running' || row.durationMs == null || !Number.isFinite(row.durationMs) || row.durationMs < 0) return null
  return row.durationMs < 1000 ? `${Math.round(row.durationMs)} 毫秒` : `${Math.round(row.durationMs / 100) / 10} 秒`
}

function outcome(row: StepRow): 'running' | 'succeeded' | 'failed' | 'cancelled' {
  return row.outcome ?? row.state
}

function outcomeText(state: ReturnType<typeof outcome>): string {
  return state === 'running' ? '执行中' : state === 'cancelled' ? '已取消' : state === 'failed' ? '失败' : '完成'
}

/** 一步：图标 + 一句话；有细节的点整行展开，针对某个文件的给「打开」。 */
type ResultNavigation = { onOpenFile?: (path: string) => void; onShowChange?: (id: string) => void; onShowCommands?: (id: string) => void }

function Step({ row, onOpenFile, onShowChange, onShowCommands, live = false }: ResultNavigation & { row: StepRow; live?: boolean }) {
  const [open, setOpen] = useState(false)
  if (row.plan) return <li className="zm-step is-plan"><PlanChecklist items={row.plan} live={live} /></li>
  const unconfirmedPlan = row.kind === 'plan' && row.state === 'running' && !live
  const took = duration(row)
  const result = outcome(row)
  const expandable = !!(row.detail || row.sources?.length || row.fileResults?.length)
  const process = row.commandSession
  const processText = process?.status === 'running' ? '后台运行'
    : process?.status === 'cancelled' ? '已停止' : process?.status === 'interrupted' ? '已中断'
      : process?.status === 'exited' ? process.exitCode === 0 ? '已退出 · 0' : `已退出 · ${process.exitCode ?? '未知'}` : null
  return (
    <li className={`zm-step${row.state === 'running' && !unconfirmedPlan ? ' is-running' : ''}`}>
      <div className="zm-step-line">
        <StepIcon kind={row.kind} state={unconfirmedPlan ? 'failed' : row.state} />
        {expandable ? (
          <button type="button" className="zm-step-text is-toggle" aria-expanded={open} onClick={() => setOpen((value) => !value)}>
            {row.text}
          </button>
        ) : (
          <span className="zm-step-text">{row.text}</span>
        )}
        {row.targetPath && onOpenFile && !row.fileResults?.length ? (
          <button type="button" className="zm-step-open" onClick={() => onOpenFile(row.targetPath!)} title={T.agent.openFileHint(row.targetPath)}>
            {T.agent.openFile}
          </button>
        ) : null}
        {process && onShowCommands ? <IconBtn size="sm" label="查看命令输出" icon={<SquareTerminal />} onClick={() => onShowCommands(process.id)} /> : null}
        <span className="zm-step-status" data-outcome={unconfirmedPlan ? 'unknown' : process?.status === 'exited' && process.exitCode !== 0 ? 'failed' : result} title={process ? '命令在这次工具结果返回时的状态' : undefined}>{unconfirmedPlan ? '结果未确认' : processText ?? outcomeText(result)}</span>
        {took !== null ? <span className="zm-step-meta">{took}</span> : null}
      </div>
      {open && expandable ? <div className="zm-step-details">
        {row.fileResults?.length ? <ul className="zm-step-files" aria-label="文件保存结果">
          {row.fileResults.map(file => <li key={file.path}>
            {(file.external ? file.changeId && onShowChange : onOpenFile) ? <button type="button" title={file.external ? `查看修改：${file.path}` : T.agent.openFileHint(file.path)} onClick={() => file.external ? onShowChange?.(file.changeId!) : onOpenFile?.(file.path)}>
              <FileText aria-hidden="true" /><span>{file.path}</span>
            </button> : <span className="zm-step-file-path"><FileText aria-hidden="true" /><span>{file.path}</span></span>}
            <span className="zm-step-file-status" data-status={file.status}>{file.status === 'saved' ? '已保存' : file.status === 'unchanged' ? '未变更' : file.status === 'rolled_back' ? '已撤回' : '未确认'}</span>
          </li>)}
        </ul> : null}
        {row.detail ? <pre className="zm-step-detail">{row.detail}</pre> : null}
        {row.sources?.length ? <ol className="zm-source-excerpts">{row.sources.map((source, index) => <li key={`${source.messageId}:${index}`}>
        <p>{source.label}</p><pre className="zm-step-detail">{source.content}</pre>
        {source.sessionId && source.messageId ? <SourceLink target={source} /> : null}
      </li>)}</ol> : null}</div> : null}
    </li>
  )
}

/** 并起来的一组（「已探索 · 3 次读取」「改了 2 个文件」）：一行，点开是里面每一步。 */
function StepGroup({ group, rows, state, onOpenFile, onShowChange, onShowCommands }: ResultNavigation & { group: 'explore' | 'edit'; rows: StepRow[]; state: StepRow['state'] }) {
  const [open, setOpen] = useState(false)
  const running = state === 'running'
  const result = running ? 'running' : rows.some((row) => outcome(row) === 'failed') ? 'failed'
    : rows.some((row) => outcome(row) === 'cancelled') ? 'cancelled' : 'succeeded'
  const resultText = result === 'failed' && rows.some((row) => outcome(row) !== 'failed') ? '含失败'
    : result === 'cancelled' && rows.some((row) => outcome(row) !== 'cancelled') ? '含取消' : outcomeText(result)
  const label = group === 'explore'
    ? A.chat.exploreGroup({
      read: rows.filter((row) => row.kind === 'read').length,
      list: rows.filter((row) => row.kind === 'list').length,
      search: rows.filter((row) => row.kind === 'search').length,
    }, running)
    : A.chat.editGroup(new Set(rows.map((row) => row.targetPath ?? row.key)).size, running)
  return (
    <li className={`zm-step${running ? ' is-running' : ''}`}>
      <div className="zm-step-line">
        <StepIcon kind={group === 'explore' ? 'search' : 'write'} state={running ? 'running' : state === 'failed' ? 'failed' : 'succeeded'} />
        <button type="button" className="zm-step-text is-toggle" aria-expanded={open} onClick={() => setOpen((value) => !value)}>{label}</button>
        <span className="zm-step-status" data-outcome={result}>{resultText}</span>
      </div>
      {open ? (
        <ol className="zm-steps zm-step-children">
          {rows.map((row) => <Step key={row.key} row={row} onOpenFile={onOpenFile} onShowChange={onShowChange} onShowCommands={onShowCommands} />)}
        </ol>
      ) : null}
    </li>
  )
}

export function StepList({ rows, onOpenFile, onShowChange, onShowCommands, limit = 12, live = false }: ResultNavigation & { rows: StepRow[]; limit?: number; live?: boolean }) {
  const [all, setAll] = useState(false)
  const preferences = useChatPreferences()
  const items = useMemo(() => preferences.groupTools ? groupSteps(rows) : rows.map((row) => ({ type: 'row' as const, row })), [rows, preferences.groupTools])
  if (items.length === 0) return null
  const shown = all || items.length <= limit ? items : items.slice(-limit)
  const hidden = items.length - shown.length
  return (
    <ol className="zm-steps">
      {hidden > 0 ? (
        <li className="zm-step">
          <button type="button" className="zm-step-earlier" onClick={() => setAll(true)}>{E.showEarlier(hidden)}</button>
        </li>
      ) : null}
      {shown.map((item) => (item.type === 'row'
        ? <Step key={item.row.key} row={item.row} onOpenFile={onOpenFile} onShowChange={onShowChange} onShowCommands={onShowCommands} live={live} />
        : <StepGroup key={item.key} group={item.group} rows={item.rows} state={item.state} onOpenFile={onOpenFile} onShowChange={onShowChange} onShowCommands={onShowCommands} />))}
    </ol>
  )
}

/** 跑完的一轮：步骤折成一行「执行了 N 步」，点开看全。 */
export function StepsFold({ rows, onOpenFile, onShowChange, onShowCommands }: ResultNavigation & { rows: StepRow[] }) {
  const [open, setOpen] = useState(false)
  if (rows.length === 0) return null
  return (
    <div className={`zm-fold${open ? ' is-open' : ''}`}>
      <button type="button" className="zm-fold-head" aria-expanded={open} onClick={() => setOpen((value) => !value)}>
        <ChevronRight className="zm-fold-chevron" aria-hidden />
        <span>查看处理过程</span>
      </button>
      {open ? <StepList rows={rows} onOpenFile={onOpenFile} onShowChange={onShowChange} onShowCommands={onShowCommands} limit={40} /> : null}
    </div>
  )
}

function readThinkingOpen(storageKey?: string): boolean | null {
  if (!storageKey) return null
  try {
    const value = window.sessionStorage.getItem(`zhimeng.thinking:${storageKey}`)
    return value === 'open' ? true : value === 'closed' ? false : null
  } catch { return null }
}

/** 只呈现接口返回的可见思考；用户展开状态跨流式 / 历史消息挂载保留。 */
export function Thinking({ thoughts, live = false, elapsed = 0, storageKey, summary = false, interrupted = false }: {
  thoughts: string[]
  live?: boolean
  elapsed?: number
  storageKey?: string
  summary?: boolean
  interrupted?: boolean
}) {
  const [open, setOpen] = useState<boolean | null>(() => readThinkingOpen(storageKey))
  const contentId = useId()
  useEffect(() => { setOpen(readThinkingOpen(storageKey)) }, [storageKey])
  const preferences = useChatPreferences()
  if (thoughts.length === 0) return null
  const expanded = open ?? (live && preferences.expandThinking)
  const label = summary ? live ? '正在接收思考摘要…' : `思考摘要（${thoughts.length} 段）`
    : live ? T.agent.thoughtLiveSeconds(elapsed) : T.agent.thoughtsCount(thoughts.length)
  const toggle = () => {
    setOpen(!expanded)
    if (storageKey) {
      try { window.sessionStorage.setItem(`zhimeng.thinking:${storageKey}`, expanded ? 'closed' : 'open') } catch { /* 当前页面仍保留。 */ }
    }
  }
  return (
    <div className={`zm-fold is-thinking${expanded ? ' is-open' : ''}`}>
      <button type="button" className="zm-fold-head" aria-expanded={expanded} aria-controls={contentId} onClick={toggle}
        title={summary ? '模型返回的思考摘要' : '仅展示模型接口实际返回的思考内容'}>
        <Lightbulb className="zm-fold-lead" aria-hidden />
        <span className={live ? 'zm-shimmer' : undefined}>{label}</span>
        <ChevronRight className="zm-fold-chevron" aria-hidden />
      </button>
      {expanded ? (
        <div className="zm-thinking" id={contentId}>
          {thoughts.map((thought, index) => <p key={index}>{thought}</p>)}
          {interrupted ? <p className="zm-thinking-note">已保留中断前收到的内容</p> : null}
        </div>
      ) : null}
    </div>
  )
}

/** 工作计划：一条一行，做完的打勾、进行中的带转圈。 */
export function PlanChecklist({ items, live = false }: { items: PlanItem[]; live?: boolean }) {
  const done = items.filter((item) => item.status === 'completed').length
  return (
    <div className="zm-plan" role="group" aria-label="工作计划">
      <div className="zm-plan-head">
        <ListChecks aria-hidden />
        <span title="Agent 记录的计划进度；实际产物以文件与执行结果为准">{E.plan}</span>
        {!live && done < items.length ? <span className="zm-plan-remaining">未完成 {items.length - done} 项</span> : null}
        <span className="zm-step-meta" aria-label={`Agent 已标记完成 ${done} 项，共 ${items.length} 项`}>{done}/{items.length}</span>
      </div>
      <ul className="zm-plan-list">
        {items.map((item, index) => (
          <li key={index} className={`zm-plan-item is-${item.status}`}>
            <span className="zm-plan-mark" aria-hidden>
              {item.status === 'completed' ? <Check /> : item.status === 'in_progress' ? live ? <Spinner /> : <CircleDashed /> : null}
            </span>
            <span><span className="zm-sr-only">{item.status === 'completed' ? '已标记完成：' : item.status === 'in_progress' ? live ? '进行中：' : '尚未完成：' : '未开始：'}</span>{item.content}</span>
          </li>
        ))}
      </ul>
    </div>
  )
}
