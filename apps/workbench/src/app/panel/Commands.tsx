import { lazy, Suspense, useEffect, useLayoutEffect, useRef, useState } from 'react'
import { ArrowDown, Check, ChevronUp, Copy, RefreshCw, Square } from 'lucide-react'
import { listCommandSessions, type CommandSessionSummary } from '../../api/commandSessions'
import { isCancelledRequest } from '../../api/runtime'
import { Btn, IconBtn, Spinner } from '../ui'
import { useCommandOutput, type OutputStream } from './useCommandOutput'
import './commands.css'
import CommandFiles from './CommandFiles'

const Terminal = lazy(() => import('./Terminal'))

export interface CommandsPanelProps {
  sessionId: string | null
  active?: boolean
  focusId?: string | null
  focusRequest?: number
  onSummary?: (summary: { total: number; running: number }) => void
  onChanged?: () => void | Promise<void>
}

function statusLabel(command: CommandSessionSummary) {
  if (command.status === 'running') return '运行中'
  if (command.status === 'interrupted') return '已中断'
  if (command.timed_out) return '已超时'
  if (command.status === 'cancelled') return '已停止'
  if (command.exit_code === null) return '已退出 · 退出码未记录'
  return command.exit_code === 0 ? '已完成 · 退出码 0' : `退出码 ${command.exit_code}`
}

function duration(ms: number) {
  const seconds = Math.max(0, Math.floor(ms / 1000))
  return seconds < 60 ? `${seconds} 秒` : `${Math.floor(seconds / 60)} 分 ${seconds % 60} 秒`
}

export function CommandsPanel(props: CommandsPanelProps) {
  if (!props.sessionId) return <p className="zm-muted">开始对话后，可以在这里查看命令及输出。</p>
  return <SessionCommands key={props.sessionId} {...props} sessionId={props.sessionId} />
}

function SessionCommands({ sessionId, active = true, focusId, focusRequest = 0, onSummary, onChanged }: CommandsPanelProps & { sessionId: string }) {
  const [commands, setCommands] = useState<CommandSessionSummary[]>([])
  const [selected, setSelected] = useState<string | null>(focusId ?? null)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)
  const [refresh, setRefresh] = useState(0)
  const [terminalView, setTerminalView] = useState(true)
  const [filesError, setFilesError] = useState<string | null>(null)
  const [filesRefreshing, setFilesRefreshing] = useState(false)
  const refreshSequence = useRef(0)
  const mounted = useRef(true)
  useEffect(() => { mounted.current = true; return () => { mounted.current = false } }, [])
  const callbacks = useRef({ onSummary, onChanged })
  callbacks.current = { onSummary, onChanged }
  const states = useRef(new Map<string, string>())
  const refreshFiles = async () => {
    if (!callbacks.current.onChanged) return
    const sequence = ++refreshSequence.current
    setFilesRefreshing(true)
    try {
      await callbacks.current.onChanged()
      if (mounted.current && sequence === refreshSequence.current) setFilesError(null)
    } catch (reason) {
      if (mounted.current && sequence === refreshSequence.current) setFilesError(reason instanceof Error ? reason.message : '暂时无法刷新文件列表')
    } finally {
      if (mounted.current && sequence === refreshSequence.current) setFilesRefreshing(false)
    }
  }
  const observe = (items: CommandSessionSummary[]) => {
    let changed = false
    for (const item of items) {
      if (states.current.has(item.command_session_id) && states.current.get(item.command_session_id) !== 'running' && item.status === 'running') continue
      if (states.current.get(item.command_session_id) === 'running' && item.status !== 'running') changed = true
      states.current.set(item.command_session_id, item.status)
    }
    if (changed) void refreshFiles()
  }
  const observeRef = useRef(observe)
  observeRef.current = observe
  useEffect(() => {
    if (focusId) setSelected(focusId)
  }, [focusId, focusRequest])
  useEffect(() => {
    if (!active) return
    const controller = new AbortController()
    let alive = true
    let timer: ReturnType<typeof setTimeout> | undefined
    const poll = async () => {
      try {
        const result = await listCommandSessions(sessionId, { signal: controller.signal })
        if (!alive) return
        setCommands((previous) => result.commands.map((item) => {
          const known = previous.find((value) => value.command_session_id === item.command_session_id)
          return known && known.status !== 'running' && item.status === 'running' ? known : item
        }))
        setError(null)
        setSelected((current) => current ?? (result.commands.find((item) => item.status === 'running') ?? result.commands[0])?.command_session_id ?? null)
        observeRef.current(result.commands)
        callbacks.current.onSummary?.({ total: result.commands.length, running: result.commands.filter((item) => states.current.get(item.command_session_id) === 'running').length })
      } catch (reason) {
        if (alive && !isCancelledRequest(reason)) setError(reason instanceof Error ? reason.message : '暂时无法读取命令列表')
      } finally {
        if (alive) { setLoading(false); timer = setTimeout(() => { void poll() }, 2500) }
      }
    }
    void poll()
    return () => { alive = false; controller.abort(); clearTimeout(timer) }
  }, [sessionId, active, refresh])

  const current = commands.find((item) => item.command_session_id === selected)
  const running = commands.filter((item) => item.status === 'running').length
  return <section className="zm-commands" aria-label="当前对话的命令">
    <header className="zm-commands-head">
      <span>{loading && !commands.length ? '正在读取命令' : error ? '状态待刷新' : running ? `${running} 个运行中` : `${commands.length} 条命令`}</span>
      <IconBtn size="sm" label="刷新命令列表" icon={<RefreshCw />} onClick={() => setRefresh((value) => value + 1)} />
    </header>
    {error ? <div className="zm-command-error" role="alert">{error}<span>已有记录保留，状态以重新连接后的结果为准。</span></div> : null}
    {filesError ? <div className="zm-command-error" role="alert">命令已结束，文件列表刷新失败：{filesError}<Btn size="sm" busy={filesRefreshing} onClick={() => { void refreshFiles() }}>刷新文件列表</Btn></div> : null}
    {loading && !commands.length ? <p className="zm-muted"><Spinner /> 正在读取…</p> : null}
    {!loading && !error && selected && !current ? <p className="zm-command-error" role="status">当前对话中找不到所选命令，记录可能已被移除。</p> : null}
    {!loading && !error && !commands.length && !selected ? <p className="zm-muted">这个对话还没有可接续的命令。普通命令的结果仍在聊天记录里。</p> : null}
    {commands.length ? <div className="zm-command-list" role="listbox" aria-label="选择命令" onKeyDown={(event) => {
      if (!['ArrowDown', 'ArrowUp', 'Home', 'End'].includes(event.key)) return
      event.preventDefault()
      const buttons = Array.from(event.currentTarget.querySelectorAll<HTMLButtonElement>('[role="option"]'))
      const currentIndex = buttons.indexOf(document.activeElement as HTMLButtonElement)
      const nextIndex = event.key === 'Home' ? 0 : event.key === 'End' ? buttons.length - 1 : Math.min(buttons.length - 1, Math.max(0, currentIndex + (event.key === 'ArrowDown' ? 1 : -1)))
      buttons[nextIndex]?.focus()
      buttons[nextIndex]?.click()
    }}>
      {commands.map((command) => <button type="button" key={command.command_session_id} role="option" aria-selected={selected === command.command_session_id}
        className={`zm-command-row${selected === command.command_session_id ? ' is-selected' : ''}`} onClick={() => setSelected(command.command_session_id)}>
        <span className="zm-command-row-text" title={command.command}>{command.command}</span>
        <span className={`zm-command-status is-${command.status}`}>{statusLabel(command)}{command.output_error ? ' · 输出不可读' : ''}{error ? ' · 待刷新' : ''}</span>
      </button>)}
    </div> : null}
    {current ? <CommandFiles key={`files:${current.command_session_id}`} tracking={current.file_tracking} sessionId={sessionId}
      commandId={current.command_session_id} active={active} onChanged={refreshFiles} /> : null}
    {current?.transport === 'pty' ? <div className="zm-terminal-actions" role="group" aria-label="终端显示方式">
      <Btn size="sm" variant="ghost" onClick={() => setTerminalView(true)} disabled={terminalView}>交互终端</Btn>
      <Btn size="sm" variant="ghost" onClick={() => setTerminalView(false)} disabled={!terminalView}>完整输出日志</Btn>
    </div> : null}
    {current?.transport === 'pty' && terminalView ? <Suspense fallback={<p className="zm-muted">正在加载终端…</p>}><Terminal key={current.command_session_id} sessionId={sessionId} summary={current} active={active}
      onObserved={(command) => {
        observeRef.current([command])
        setCommands(items => items.map(item => item.command_session_id === command.command_session_id && (item.status === 'running' || command.status !== 'running') ? command : item))
      }} /></Suspense> : current ? <CommandDetails key={current.command_session_id} sessionId={sessionId} summary={current} active={active}
      onObserved={(command) => {
        observeRef.current([command])
        setCommands((items) => items.map((item) => item.command_session_id === command.command_session_id && (item.status === 'running' || command.status !== 'running') ? command : item))
      }} /> : null}
  </section>
}

function CommandDetails({ sessionId, summary, active, onObserved }: { sessionId: string; summary: CommandSessionSummary; active: boolean; onObserved: (command: CommandSessionSummary) => void }) {
  const data = useCommandOutput(sessionId, summary.command_session_id, active)
  const command = data.command?.status === 'running' && summary.status !== 'running' ? summary : data.command ?? summary
  const observed = useRef(onObserved)
  observed.current = onObserved
  useEffect(() => { if (data.command) observed.current(data.command) }, [data.command])
  const [stream, setStream] = useState<OutputStream>('stdout')
  const [copied, setCopied] = useState(false)
  const [copyError, setCopyError] = useState<string | null>(null)
  const [following, setFollowing] = useState(true)
  const followRef = useRef(true)
  const scroller = useRef<HTMLDivElement>(null)
  const previous = useRef<{ stream: OutputStream; start: number; height: number } | null>(null)
  const output = data[stream]
  const copiedTimer = useRef<ReturnType<typeof setTimeout> | undefined>(undefined)
  useEffect(() => () => clearTimeout(copiedTimer.current), [])
  useLayoutEffect(() => {
    const element = scroller.current
    if (!element || !output) return
    const before = previous.current
    if (followRef.current) element.scrollTop = element.scrollHeight
    else if (before?.stream === stream && output.start < before.start) element.scrollTop += element.scrollHeight - before.height
    previous.current = { stream, start: output.start, height: element.scrollHeight }
  }, [stream, output?.text, output?.start])
  const switchStream = (next: OutputStream) => {
    setStream(next); setCopied(false); setCopyError(null)
    followRef.current = true; setFollowing(true)
  }
  const copy = async () => {
    try {
      await navigator.clipboard.writeText(output?.text ?? '')
      setCopied(true); setCopyError(null)
      clearTimeout(copiedTimer.current)
      copiedTimer.current = setTimeout(() => setCopied(false), 1800)
    } catch { setCopyError('复制没有成功，请选中输出后复制。') }
  }
  const loadEarlier = () => {
    followRef.current = false
    setFollowing(false)
    data.loadEarlier(stream)
  }
  return <div className="zm-command-detail">
    <div className="zm-command-detail-head">
      <div className="zm-command-state-line"><strong className={`zm-command-status is-${command.status}`}>{statusLabel(command)}</strong><span>{duration(command.duration_ms)}</span></div>
      {command.status === 'running' ? <Btn size="sm" variant="ghost" icon={<Square />} busy={data.stopping} onClick={data.stop}>停止命令</Btn> : null}
    </div>
    <details className="zm-command-metadata"><summary>命令详情</summary><pre>{command.command}</pre><dl><dt>目录</dt><dd>{command.cwd}</dd><dt>开始于</dt><dd>{new Date(command.created_at * 1000).toLocaleString('zh-CN')}</dd></dl></details>
    {command.interruption_reason ? <p className="zm-command-note">{command.interruption_reason}</p> : null}
    {command.output_error ? <p className="zm-command-error" role="alert">{command.output_error}</p> : null}
    {data.stopError ? <div className="zm-command-error" role="alert">停止命令未确认：{data.stopError}<Btn size="sm" onClick={data.stop}>重试停止</Btn></div> : null}
    {data.error ? <div className="zm-command-error" role="alert">输出暂未更新：{data.error}<Btn size="sm" onClick={data.retry}>重新读取输出</Btn></div> : null}
    <div className="zm-command-output-head">
      <div role="tablist" aria-label="命令输出通道">
        <button type="button" role="tab" aria-selected={stream === 'stdout'} onClick={() => switchStream('stdout')}>标准输出</button>
        <button type="button" role="tab" aria-selected={stream === 'stderr'} onClick={() => switchStream('stderr')}>错误输出{(data.stderr?.total ?? command.stderr_bytes ?? 0) > 0 ? <span className="zm-command-output-dot" aria-label="有内容" /> : null}</button>
      </div>
      <IconBtn size="sm" label={copied ? '已复制输出' : output && (output.start > 0 || output.end < output.total) ? '复制已加载输出' : '复制输出'}
        icon={copied ? <Check /> : <Copy />} disabled={!output?.text} onClick={() => { void copy() }} />
    </div>
    {copyError ? <p className="zm-command-error" role="alert">{copyError}</p> : null}
    {output && output.start > 0 ? <Btn size="sm" variant="ghost" className="zm-command-earlier" icon={<ChevronUp />} busy={data.loadingEarlier === stream} onClick={loadEarlier}>加载更早输出</Btn> : null}
    <div className="zm-command-output" ref={scroller} role="region" tabIndex={0} aria-label={stream === 'stdout' ? '标准输出内容' : '错误输出内容'} onScroll={() => {
      const element = scroller.current
      if (!element) return
      const atBottom = element.scrollHeight - element.scrollTop - element.clientHeight <= 20
      followRef.current = atBottom
      setFollowing(atBottom)
    }}>
      {output?.text ? <pre>{output.text}</pre> : <p className="zm-command-empty">{data.loading ? '正在读取输出…' : data.error || command.output_error ? '输出暂不可用' : command.status === 'running' ? '尚无输出' : '没有输出'}</p>}
    </div>
    <div className="zm-command-output-foot">
      <span>{data.error || command.output_error ? output ? '保留上次读取的输出' : '输出暂不可用' : output && output.end < output.total ? '正在读取后续输出…' : output && output.start > 0 ? '显示最近输出' : command.status === 'running' ? '输出自动更新' : '输出已保存'}</span>
      {!following ? <Btn size="sm" variant="ghost" icon={<ArrowDown />} onClick={() => {
        followRef.current = true; setFollowing(true)
        if (scroller.current) scroller.current.scrollTop = scroller.current.scrollHeight
      }}>跟随最新</Btn> : null}
    </div>
  </div>
}
